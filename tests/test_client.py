"""Actual aiohttp framing/ownership tests with synthetic loopback server."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json

from aiohttp import ClientSession, web
import pytest

from custom_components.openclaw_voice.client import VoiceClient, VoiceTransportError
from custom_components.openclaw_voice.tts import OpenClawTextToSpeech
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError

TOKEN = "synthetic-test-client-token-" + "a" * 32


async def text():
    yield "Hello."


@asynccontextmanager
async def backend(events=None, *, status=200, content_type="application/x-ndjson", truncate=False):
    seen = []

    async def handler(request):
        first = json.loads(await request.content.readline())
        seen.append(request.headers.get("Authorization"))
        async for line in request.content:
            seen.append(json.loads(line))
        if status != 200:
            return web.Response(status=status, headers={"Location": "http://invalid.example/credential-trap"})
        common = {"protocol_major": 1, "job_id": "job", "request_id": first["request_id"], "profile_revision": "r1"}
        frames = events or [
            {"type": "audio_start", "segment": 0, "extension": "mp3", "mode": "sentence_buffered"},
            {"type": "audio_chunk", "segment": 0, "audio": base64.b64encode(b"sound").decode()},
            {"type": "audio_end", "segment": 0},
            {"type": "done", "segments": 1, "audio_bytes": 5},
        ]
        body = b"".join((json.dumps({**common, "sequence": index, **frame}) + "\n").encode()
                        for index, frame in enumerate(frames, 1))
        if truncate:
            body = body[:-1]
        return web.Response(body=body, content_type=content_type)

    app = web.Application()
    app.router.add_post("/tts", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # Test fixture only.
    try:
        async with ClientSession() as session:
            yield VoiceClient(session, f"http://127.0.0.1:{port}", TOKEN, "r1"), seen
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_native_tts_entity_receives_stream_and_buffered_announcement_path():
    async with backend() as (client, seen):
        entity = OpenClawTextToSpeech(client, "fixture", ["en"], "en")
        assert entity.async_supports_streaming_input()
        result = await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        assert result.extension == "mp3"
        assert b"".join([chunk async for chunk in result.data_gen]) == b"sound"
        assert await entity.async_get_tts_audio("Announcement.", "en", {}) == ("mp3", b"sound")
        assert seen[0] == "Bearer " + TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 307, 401, 403, 500])
async def test_auth_errors_and_redirects_never_follow_or_leak_response_body(status):
    async with backend(status=status) as (client, _):
        with pytest.raises(VoiceTransportError):
            _ = [event async for event in client.speech(text(), "en")]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "done", "segments": 0, "audio_bytes": 0, "request_id": "wrong"},
    {"type": "done", "segments": 0, "audio_bytes": 0, "profile_revision": "other"},
    {"type": "done", "segments": 0, "audio_bytes": 0, "sequence": 2},
    {"type": "audio_chunk", "segment": 0, "audio": "AAAA"},
    {"type": "audio_start", "segment": 0, "mode": "unknown", "extension": "mp3"},
])
async def test_ownership_order_and_segment_errors_fail_closed(frame):
    async with backend([frame]) as (client, _):
        with pytest.raises(VoiceTransportError):
            _ = [event async for event in client.speech(text(), "en")]


@pytest.mark.asyncio
async def test_missing_newline_and_early_eof_are_not_success():
    async with backend(truncate=True) as (client, _):
        with pytest.raises(VoiceTransportError):
            _ = [event async for event in client.speech(text(), "en")]


def test_url_and_credentials_are_validated_without_exposing_token():
    for url in ["ftp://server", "http://user:password@server", "http://server?token=x"]:
        with pytest.raises(ValueError):
            VoiceClient(None, url, TOKEN, "r1")
    assert TOKEN not in repr(VoiceClient(None, "http://server", TOKEN, "r1"))


@pytest.mark.asyncio
async def test_buffered_long_unicode_text_is_split_without_loss():
    original = "🐈" * 3000

    async def long_text():
        yield original

    async with backend() as (client, seen):
        _ = [event async for event in client.speech(long_text(), "en")]
        frames = [event for event in seen if isinstance(event, dict) and event["type"] == "text"]
        assert "".join(event["text"] for event in frames) == original
        assert all(len(event["text"].encode()) <= 8192 for event in frames)
