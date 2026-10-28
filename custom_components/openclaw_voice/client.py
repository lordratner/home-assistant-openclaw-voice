"""Bounded, credential-minimal streaming HTTP transport for the paired backend.

The Gateway adapter owns agent/provider/session policy. No provider credentials
or model/session target fields are accepted by this client. Turn reconnect/job
recovery is intentionally not claimed by this initial speech transport.
"""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator
import json
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp


class VoiceTransportError(RuntimeError):
    """A safe, content-free transport/protocol failure."""


class VoiceClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str,
                 profile_revision: str) -> None:
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("invalid backend URL")
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid client credential")
        if not profile_revision:
            raise ValueError("profile revision is required")
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._revision = profile_revision

    async def speech(self, text: AsyncIterator[str], language: str) -> AsyncIterator[dict]:
        request_id = str(uuid4())

        async def upload():
            def encode(value):
                return (json.dumps(value, separators=(",", ":")) + "\n").encode()
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
                # A buffered provider may supply the whole answer at once.
                # Split transport frames without losing Unicode characters.
                for offset in range(0, len(value), 2048):
                    sequence += 1
                    yield encode({"type": "text", "sequence": sequence, "text": value[offset:offset + 2048]})
            yield encode({"type": "end", "sequence": sequence + 1})

        terminal = False
        sequence = 0
        job_id = None
        active_segment = None
        next_segment = 0
        total_audio = 0
        segment_audio = 0
        try:
            async with self._session.post(
                self._base_url + "/tts", data=upload(),
                headers={"Authorization": "Bearer " + self._token,
                         "Content-Type": "application/x-ndjson", "Accept": "application/x-ndjson"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=60),
            ) as response:
                if response.status != 200:
                    raise VoiceTransportError("backend_auth_failed" if response.status in (401, 403) else "backend_unavailable")
                if response.content_type != "application/x-ndjson":
                    raise VoiceTransportError("invalid_content_type")
                async for line in response.content:
                    if len(line) > 65536 or not line.endswith(b"\n") or terminal:
                        raise VoiceTransportError("invalid_event_framing")
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("protocol_major") != 1:
                        raise VoiceTransportError("unsupported_protocol")
                    if (event.get("request_id") != request_id or event.get("profile_revision") != self._revision
                            or not isinstance(event.get("job_id"), str) or not event["job_id"]):
                        raise VoiceTransportError("event_ownership_mismatch")
                    job_id = job_id or event["job_id"]
                    if event["job_id"] != job_id or type(event.get("sequence")) is not int or event["sequence"] != sequence + 1:
                        raise VoiceTransportError("event_sequence_mismatch")
                    sequence += 1
                    if sequence > 65536:
                        raise VoiceTransportError("event_limit")
                    kind = event.get("type")
                    if kind in ("audio_start", "audio_chunk", "audio_end") and type(event.get("segment")) is not int:
                        raise VoiceTransportError("invalid_audio_segment")
                    if kind == "audio_start":
                        if active_segment is not None or type(event.get("segment")) is not int or event["segment"] != next_segment:
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
                        terminal = True
                    elif kind in ("error", "cancelled"):
                        raise VoiceTransportError("backend_job_failed")
                    else:
                        raise VoiceTransportError("unknown_event")
                    yield event
                if not terminal:
                    raise VoiceTransportError("stream_interrupted")
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as error:
            raise VoiceTransportError("voice_transport_failed") from error
