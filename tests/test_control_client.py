"""Finite controls, streamed recognition, and same-job conversation recovery."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json

from aiohttp import ClientSession, web
import pytest

from custom_components.openclaw_voice.client import VoiceClient, VoiceTransportError

JOB = "00000000-0000-4000-8000-000000000001"
HANDLE = "00000000-0000-4000-8000-000000000002"
TOKEN = "a" * 43


@asynccontextmanager
async def backend(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            yield VoiceClient(session, f"http://127.0.0.1:{port}", TOKEN, "r1")
    finally:
        await runner.cleanup()


def event(request_id, sequence, **value):
    return {"protocol_major": 1, "profile_revision": "r1", "request_id": request_id,
            "job_id": JOB, "sequence": sequence, **value}


@pytest.mark.asyncio
async def test_fragmented_json_control_response_is_fully_read_before_parsing():
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"protocol_major":1,')
        await asyncio.sleep(0)
        await response.write(b'"profile_revision":"r1"}')
        await response.write_eof()
        return response

    async with backend(handler) as client:
        assert (await client.capabilities())["profile_revision"] == "r1"


@pytest.mark.asyncio
async def test_finalized_stt_upload_splits_pcm_frames_and_never_submits_a_turn():
    paths = []
    received = []

    async def handler(request):
        paths.append(request.path)
        frames = [json.loads(line) async for line in request.content]
        assert frames[0]["encoding"] == "pcm_s16le"
        assert frames[0]["sample_rate"] == 16000
        assert frames[-1]["type"] == "end"
        received.extend(base64.b64decode(frame["audio"]) for frame in frames if frame["type"] == "audio")
        output = [event(frames[0]["request_id"], 1, type="phase", phase="running"),
                  event(frames[0]["request_id"], 2, type="transcript", text="Recognized", final=True),
                  event(frames[0]["request_id"], 3, type="done", no_speech=False)]
        return web.Response(text="".join(json.dumps(frame) + "\n" for frame in output), content_type="application/x-ndjson")

    source = b"\x01\x00" * 24000

    async def audio():
        yield source

    async with backend(handler) as client:
        assert await client.transcribe(audio(), "en") == {"text": "Recognized", "no_speech": False}
    assert paths == ["/stt"]
    assert all(len(chunk) <= 32768 for chunk in received)
    assert b"".join(received) == source


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_text", ["Opening.", ""])
async def test_dropped_turn_response_recovers_same_completed_result_without_posting_again(initial_text):
    posted = 0
    calls = []
    request_id = None

    async def handler(request):
        nonlocal posted, request_id
        calls.append((request.method, request.path))
        if request.path == "/turns":
            posted += 1
            payload = await request.json()
            request_id = payload["request_id"]
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "X-OpenClaw-Job-ID": JOB})
            await response.prepare(request)
            if initial_text:
                await response.write((json.dumps(event(request_id, 1, type="text_delta", message_id=0, text=initial_text)) + "\n").encode())
            await asyncio.sleep(0.01)
            request.transport.abort()
            return response
        if request.path.endswith("/events"):
            return web.json_response({"code": "replay_unavailable", "job_id": JOB}, status=409)
        if request.path == "/jobs/" + JOB:
            return web.json_response({"job_id": JOB, "status": "completed", "result_recoverable": True,
                                      "result": {"text": "Opening. Ending.", "continue_conversation": False}})
        return web.json_response({"code": "not_found"}, status=404)

    async with backend(handler) as client:
        frames = [frame async for frame in client.turn(HANDLE, "Synthetic request")]
    assert posted == 1
    assert "".join(frame["text"] for frame in frames if frame["type"] == "text_delta") == "Opening. Ending."
    assert frames[-1]["type"] == "done"
    assert frames[-1]["recovered"] is True
    assert all(path != "/turns" for method, path in calls if method == "GET")


@pytest.mark.asyncio
async def test_invalid_turn_event_fails_closed_and_cancels_only_the_owned_job():
    calls = []

    async def handler(request):
        calls.append(request.path)
        if request.path == "/turns":
            payload = await request.json()
            frame = event(payload["request_id"], 1, type="text_delta", message_id=-1, text="Invalid")
            return web.Response(text=json.dumps(frame) + "\n", content_type="application/x-ndjson")
        if request.path.endswith("/cancel"):
            return web.json_response({"job_id": JOB, "status": "cancel_requested"})
        return web.json_response({"code": "not_found"}, status=404)

    async with backend(handler) as client:
        with pytest.raises(VoiceTransportError, match="invalid_text_event"):
            _ = [frame async for frame in client.turn(HANDLE, "Synthetic request")]
    assert calls == ["/turns", f"/jobs/{JOB}/cancel"]


@pytest.mark.asyncio
async def test_stt_does_not_ignore_data_after_the_terminal_frame():
    async def handler(request):
        start = json.loads(await request.content.readline())
        async for _ in request.content:
            pass
        frames = [event(start["request_id"], 1, type="transcript", final=True, text="Text"),
                  event(start["request_id"], 2, type="done"), event(start["request_id"], 3, type="done")]
        return web.Response(text="".join(json.dumps(frame) + "\n" for frame in frames), content_type="application/x-ndjson")

    async def audio():
        yield b"\x00\x00"

    async with backend(handler) as client:
        with pytest.raises(VoiceTransportError, match="invalid_event_framing"):
            await client.transcribe(audio(), "en")
