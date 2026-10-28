"""Bounded paired-product HTTP and owned-job event transport."""
from __future__ import annotations

from collections.abc import AsyncIterator
import json
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp


class VoiceTransportError(RuntimeError):
    def __init__(self, code: str, job_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.job_id = job_id


def canonical_id(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise ValueError("invalid backend URL")
    _ = url.port
    return value.rstrip("/")


class VoiceTransport:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str, profile_revision: str) -> None:
        self._base_url = validate_url(base_url)
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid client credential")
        if not isinstance(profile_revision, str) or not profile_revision:
            raise ValueError("profile revision is required")
        self._session = session
        self._token = token
        self._revision = profile_revision

    @property
    def profile_revision(self) -> str:
        return self._revision

    @staticmethod
    async def _json(response):
        chunks = []
        total = 0
        async for chunk in response.content.iter_chunked(32768):
            total += len(chunk)
            if total > 131072:
                raise VoiceTransportError("response_limit")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise VoiceTransportError("invalid_json_response") from error
        if not isinstance(value, dict):
            raise VoiceTransportError("invalid_json_response")
        return value

    @classmethod
    async def pair(cls, session, base_url: str, pairing_code: str) -> dict:
        try:
            async with session.post(validate_url(base_url) + "/pair", json={"pairing_code": pairing_code},
                                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    raise VoiceTransportError("pairing_failed")
                result = await cls._json(response)
                if (not canonical_id(result.get("client_id")) or not isinstance(result.get("client_token"), str)
                        or not isinstance(result.get("capabilities"), dict) or result["capabilities"].get("protocol_major") != 1):
                    raise VoiceTransportError("invalid_pairing_response")
                return result
        except (aiohttp.ClientError, TimeoutError) as error:
            raise VoiceTransportError("backend_unavailable") from error

    def _headers(self):
        return {"Authorization": "Bearer " + self._token, "Accept": "application/json"}

    async def control(self, method: str, path: str, data: dict | None = None) -> dict:
        try:
            async with self._session.request(method, self._base_url + path, json=data,
                    headers=self._headers(), allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    raise await self._response_error(response)
                return await self._json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise VoiceTransportError("backend_unavailable") from error

    async def _response_error(self, response):
        code = "backend_auth_failed" if response.status in (401, 403) else "backend_unavailable"
        job_id = None
        try:
            value = await self._json(response)
            candidate = value.get("code")
            if candidate in {"profile_changed", "request_conflict", "request_expired", "replay_gap", "replay_unavailable",
                    "job_output_cancelled", "upload_already_admitted", "job_not_found", "conversation_not_found",
                    "conversation_expired", "session_missing_or_replaced", "job_capacity", "rate_limit",
                    "persistent_rebind_required", "persistent_binding_changed", "request_clock_skew"}:
                code = candidate
            if canonical_id(value.get("job_id")):
                job_id = value["job_id"]
        except VoiceTransportError:
            pass
        return VoiceTransportError(code, job_id)

    async def capabilities(self) -> dict:
        result = await self.control("GET", "/capabilities")
        if result.get("protocol_major") != 1 or not isinstance(result.get("profile_revision"), str):
            raise VoiceTransportError("unsupported_protocol")
        return result

    async def job_status(self, job_id: str) -> dict:
        if not canonical_id(job_id):
            raise VoiceTransportError("invalid_job_id")
        result = await self.control("GET", "/jobs/" + job_id)
        if result.get("job_id") != job_id:
            raise VoiceTransportError("event_ownership_mismatch")
        return result

    async def cancel_job(self, job_id: str) -> dict:
        if not canonical_id(job_id):
            raise VoiceTransportError("invalid_job_id")
        return await self.control("POST", "/jobs/" + job_id + "/cancel", {})

    async def wire_events(self, path: str, request_id: str, *, data=None,
                          after: int = 0, job_id: str | None = None) -> AsyncIterator[dict]:
        terminal = False
        sequence = after
        try:
            async with self._session.request("POST" if data is not None else "GET", self._base_url + path,
                    data=data, headers={**self._headers(), "Content-Type": "application/x-ndjson", "Accept": "application/x-ndjson"},
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)) as response:
                if response.status != 200:
                    raise await self._response_error(response)
                if response.content_type != "application/x-ndjson":
                    raise VoiceTransportError("invalid_content_type")
                header_id = response.headers.get("X-OpenClaw-Job-ID")
                if header_id:
                    if not canonical_id(header_id) or job_id and header_id != job_id:
                        raise VoiceTransportError("invalid_job_id")
                    job_id = header_id
                async for line in response.content:
                    if len(line) > 65536 or not line.endswith(b"\n") or terminal:
                        raise VoiceTransportError("invalid_event_framing", job_id)
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("protocol_major") != 1:
                        raise VoiceTransportError("unsupported_protocol", job_id)
                    if event.get("request_id") != request_id or event.get("profile_revision") != self._revision or not canonical_id(event.get("job_id")):
                        raise VoiceTransportError("event_ownership_mismatch", job_id)
                    job_id = job_id or event["job_id"]
                    if event["job_id"] != job_id or type(event.get("sequence")) is not int or event["sequence"] != sequence + 1:
                        raise VoiceTransportError("event_sequence_mismatch", job_id)
                    sequence += 1
                    if sequence > 65536:
                        raise VoiceTransportError("event_limit", job_id)
                    kind = event.get("type")
                    if kind in ("error", "cancelled"):
                        raise VoiceTransportError("backend_job_failed", job_id)
                    if kind == "done":
                        terminal = True
                    event = {**event, "job_id": job_id}
                    yield event
                if not terminal:
                    raise VoiceTransportError("stream_interrupted", job_id)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise VoiceTransportError("voice_transport_failed", job_id) from error
        except (ValueError, KeyError, TypeError) as error:
            raise VoiceTransportError("invalid_event", job_id) from error
