"""Native voice operations over the shared, caller-scoped OC transport."""
from __future__ import annotations

import asyncio
import base64
from contextlib import aclosing
from collections.abc import AsyncIterator
import json
from uuid import uuid7

from .transport import VoiceTransport, VoiceTransportError, canonical_id


def encode(value: dict) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


class VoiceClient(VoiceTransport):
    async def open_conversation(self, conversation_id: str, mode: str) -> dict:
        if mode not in ("persistent", "per_conversation"):
            raise ValueError("invalid session mode")
        result = await self.control("POST", "/conversations", {
            "protocol_major": 1, "profile_revision": self._revision,
            "ha_conversation_id": conversation_id, "mode": mode,
        })
        if not canonical_id(result.get("conversation_handle")) or result.get("mode") != mode or result.get("profile_revision") != self._revision:
            raise VoiceTransportError("invalid_conversation_binding")
        return result

    async def speech(self, text: AsyncIterator[str], language: str, *, on_job=None) -> AsyncIterator[dict]:
        request_id = str(uuid7())

        async def upload():
            yield encode({"type": "start", "protocol_major": 1, "request_id": request_id,
                          "profile_revision": self._revision, "language": language})
            sequence = 0
            total = 0
            async for value in text:
                if not isinstance(value, str):
                    raise VoiceTransportError("invalid_text_chunk")
                total += len(value.encode("utf-8"))
                if total > 65536:
                    raise VoiceTransportError("text_limit")
                for offset in range(0, len(value), 2048):
                    sequence += 1
                    yield encode({"type": "text", "sequence": sequence, "text": value[offset:offset + 2048]})
            yield encode({"type": "end", "sequence": sequence + 1})

        active_segment = None
        next_segment = 0
        total_audio = 0
        segment_audio = 0
        source = self.wire_events("/tts", request_id, data=upload())
        reported_job = False
        try:
            async for event in source:
                if not reported_job:
                    if on_job:
                        on_job(event["job_id"])
                    reported_job = True
                kind = event["type"]
                if kind == "phase":
                    if event.get("phase") != "running":
                        raise VoiceTransportError("unknown_job_phase")
                    continue
                if kind in ("audio_start", "audio_chunk", "audio_end") and type(event.get("segment")) is not int:
                    raise VoiceTransportError("invalid_audio_segment")
                if kind == "audio_start":
                    if active_segment is not None or event["segment"] != next_segment:
                        raise VoiceTransportError("invalid_audio_segment")
                    if event.get("mode") not in ("sentence_progressive", "sentence_buffered", "buffered", "incremental_text"):
                        raise VoiceTransportError("unknown_audio_mode")
                    if event.get("extension") not in ("mp3", "wav", "ogg", "flac", "pcm"):
                        raise VoiceTransportError("unsupported_audio_format")
                    active_segment = next_segment
                    segment_audio = 0
                elif kind == "audio_chunk":
                    if active_segment is None or event.get("segment") != active_segment:
                        raise VoiceTransportError("audio_without_segment")
                    encoded = event.get("audio")
                    if not isinstance(encoded, str) or len(encoded) > 43692:
                        raise VoiceTransportError("audio_chunk_limit")
                    audio = base64.b64decode(encoded, validate=True)
                    total_audio += len(audio)
                    segment_audio += len(audio)
                    if not audio or len(audio) > 32768 or total_audio > 32 * 1024 * 1024:
                        raise VoiceTransportError("audio_limit")
                    event = {**event, "audio": audio}
                elif kind == "audio_end":
                    if active_segment is None or event.get("segment") != active_segment or not segment_audio:
                        raise VoiceTransportError("invalid_audio_segment")
                    active_segment = None
                    next_segment += 1
                elif kind == "done":
                    if active_segment is not None or event.get("segments") != next_segment or event.get("audio_bytes") != total_audio:
                        raise VoiceTransportError("incomplete_audio")
                else:
                    raise VoiceTransportError("unknown_event")
                yield event
        except (ValueError, KeyError, TypeError) as error:
            raise VoiceTransportError("invalid_audio_event") from error
        finally:
            await source.aclose()

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> dict:
        request_id = str(uuid7())

        async def upload():
            yield encode({"type": "start", "protocol_major": 1, "request_id": request_id,
                          "profile_revision": self._revision, "language": language,
                          "encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1})
            sequence = 0
            total = 0
            async for value in audio:
                if not isinstance(value, bytes) or len(value) % 2:
                    raise VoiceTransportError("invalid_pcm_audio")
                total += len(value)
                if total > 2097152:
                    raise VoiceTransportError("audio_limit")
                for offset in range(0, len(value), 32768):
                    sequence += 1
                    yield encode({"type": "audio", "sequence": sequence,
                                  "audio": base64.b64encode(value[offset:offset + 32768]).decode()})
            yield encode({"type": "end", "sequence": sequence + 1})

        transcript = None
        completed = False
        no_speech = False
        async with aclosing(self.wire_events("/stt", request_id, data=upload())) as source:
            async for event in source:
                kind = event["type"]
                if kind == "phase" and event.get("phase") == "running":
                    continue
                if kind == "transcript":
                    if transcript is not None or event.get("final") is not True or not isinstance(event.get("text"), str):
                        raise VoiceTransportError("invalid_transcript")
                    if len(event["text"].encode()) > 65536:
                        raise VoiceTransportError("text_limit")
                    transcript = event["text"]
                elif kind == "done":
                    if transcript is None:
                        raise VoiceTransportError("missing_transcript")
                    completed = True
                    no_speech = event.get("no_speech") is True
                else:
                    raise VoiceTransportError("unknown_event")
        if not completed:
            raise VoiceTransportError("stream_interrupted")
        return {"text": transcript, "no_speech": no_speech}

    async def turn(self, handle: str, text: str, request_id: str | None = None, *, context: str | None = None) -> AsyncIterator[dict]:
        if not canonical_id(handle) or not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536:
            raise VoiceTransportError("invalid_turn")
        request_id = request_id or str(uuid7())
        payload = {"protocol_major": 1, "profile_revision": self._revision, "request_id": request_id,
                   "conversation_handle": handle, "text": text}
        if context:
            if len(context.encode()) > 8192:
                raise VoiceTransportError("context_limit")
            payload["context"] = context
        job_id = None
        sequence = 0
        messages: dict[int, str] = {}
        current_message = -1
        terminal = False

        async def checked(source):
            nonlocal job_id, sequence, current_message, terminal
            async with aclosing(source):
                async for event in source:
                    job_id = event["job_id"]
                    sequence = event["sequence"]
                    kind = event["type"]
                    if kind == "phase" and event.get("phase") == "running":
                        continue
                    if kind == "model":
                        if not isinstance(event.get("provider"), str) or not isinstance(event.get("model"), str):
                            raise VoiceTransportError("invalid_model_event", job_id)
                        continue
                    if kind == "text_delta":
                        identity = event.get("message_id")
                        if type(identity) is not int or identity not in (current_message, current_message + 1) or identity < 0 or not isinstance(event.get("text"), str):
                            raise VoiceTransportError("invalid_text_event", job_id)
                        current_message = identity
                        messages[identity] = messages.get(identity, "") + event["text"]
                        if sum(len(value.encode()) for value in messages.values()) > 65536:
                            raise VoiceTransportError("text_limit", job_id)
                    elif kind == "done":
                        if type(event.get("continue_conversation", False)) is not bool:
                            raise VoiceTransportError("invalid_completion", job_id)
                        terminal = True
                    else:
                        raise VoiceTransportError("unknown_event", job_id)
                    yield event

        try:
            try:
                async with aclosing(checked(self.wire_events("/turns", request_id, data=encode(payload)))) as source:
                    async for event in source:
                        yield event
            except VoiceTransportError as error:
                job_id = job_id or error.job_id
                recoverable = {"voice_transport_failed", "stream_interrupted", "replay_unavailable", "replay_gap"}
                if not job_id or error.code not in recoverable:
                    raise
                # Recover output of the same job. Never POST the prompt a second time.
                try:
                    async with aclosing(checked(self.wire_events(f"/jobs/{job_id}/events?after={sequence}",
                                                                 request_id, after=sequence, job_id=job_id))) as source:
                        async for event in source:
                            yield event
                except VoiceTransportError:
                    status = await self.job_status(job_id)
                    state = status.get("status")
                    if state != "completed":
                        code = "execution_outcome_unknown" if state == "interrupted" else "backend_job_cancelled" if state == "cancelled" else "backend_job_failed" if state in ("failed", "partial_failure") else "backend_job_pending"
                        raise VoiceTransportError(code, job_id)
                    if not status.get("result_recoverable"):
                        raise VoiceTransportError("reply_recovery_unavailable", job_id)
                    result = status.get("result")
                    final = result.get("text") if isinstance(result, dict) else None
                    seen = "\n\n".join(messages[key] for key in sorted(messages))
                    if not isinstance(final, str) or not final.startswith(seen):
                        raise VoiceTransportError("reply_recovery_conflict", job_id)
                    if final[len(seen):]:
                        yield {"type": "text_delta", "message_id": max(current_message, 0),
                               "text": final[len(seen):], "job_id": job_id, "recovered": True}
                    terminal = True
                    yield {"type": "done", "job_id": job_id,
                           "continue_conversation": result.get("continue_conversation", False), "recovered": True}
            if not terminal:
                raise VoiceTransportError("stream_interrupted", job_id)
        finally:
            if job_id and not terminal and not self._session.closed:
                try:
                    async with asyncio.timeout(5):
                        await self.cancel_job(job_id)
                except (VoiceTransportError, TimeoutError):
                    pass
