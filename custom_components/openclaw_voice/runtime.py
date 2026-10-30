"""Config-entry connection, capability refresh and owned action lifecycle."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import hashlib
from contextvars import ContextVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .action_context import ActionConversationBindings
from .client import VoiceClient, VoiceTransportError
from .const import DOMAIN, API_PATH, CONF_CLIENT_ID, CONF_CLIENT_TOKEN, CONF_URL, CONF_SESSION_MODE, DEFAULT_SESSION_MODE
from .conversation_state import Conversations
from .transport import validate_url
from .timing import LocalTimings
from .observability import Diagnostics, GLOBAL, observe, failure_summary
from .capabilities import validate_capabilities, check_identity, stage_available

_LOGGER = logging.getLogger(__name__)
ACTION_SPEECH_FEED = ContextVar("openclaw_action_speech_feed", default=None)


def api_url(value: str) -> str:
    value = validate_url(value)
    return value if value.endswith(API_PATH) else value + API_PATH


class SpeechFeed:
    def __init__(self, diagnostics=None):
        self.diagnostics = diagnostics or GLOBAL
        self._bytes = self._frames = 0
        self._queue = asyncio.Queue(maxsize=32)
        self._closed = False
        self._error = None
        self._changed = asyncio.Event()

    async def put(self, text: str):
        self._bytes += len(text.encode())
        self._frames += 1
        if self._closed:
            raise VoiceTransportError("speech_cancelled")
        await self._queue.put(text)
        if self._closed:
            raise VoiceTransportError("speech_cancelled")
        self._changed.set()

    def finish(self, error=None):
        self._closed = True
        self._error = error
        if error:
            while not self._queue.empty():
                self._queue.get_nowait()
        self._changed.set()
        self.diagnostics.event("playback", "speech_feed", "failed" if error else "closed",
            {"bytes": self._bytes, "frames": self._frames,
             **(failure_summary(error, stage="playback") if error else {})}, "warning" if error else "debug")

    @observe("playback", "feed_stream")
    async def stream(self):
        while True:
            if self._error:
                raise self._error
            if not self._queue.empty():
                yield self._queue.get_nowait()
                continue
            if self._closed:
                break
            self._changed.clear()
            await self._changed.wait()
        if self._error:
            raise self._error

    @observe("playback", "feed_ready")
    async def ready(self):
        while self._queue.empty() and not self._closed:
            self._changed.clear()
            await self._changed.wait()
        if self._error:
            raise self._error
        return not self._queue.empty()


class Runtime:
    def __init__(self, hass, entry: ConfigEntry, conversations: Conversations, actions=None):
        self.diagnostics = Diagnostics()
        self.hass, self.entry = hass, entry
        self.modes = conversations
        self.actions = actions or ActionConversationBindings()
        self.actions.diagnostics = self.diagnostics
        self.connection_identity = (entry.data[CONF_URL], entry.data[CONF_CLIENT_ID], hashlib.sha256(entry.data[CONF_CLIENT_TOKEN].encode()).digest())
        self.client = VoiceClient(async_get_clientsession(hass), api_url(entry.data[CONF_URL]),
                                  entry.data[CONF_CLIENT_TOKEN], "initializing", diagnostics=self.diagnostics)
        self.capabilities = {}
        self.timings = LocalTimings(diagnostics=self.diagnostics)
        self.entities = {}
        self.speech_feeds: dict[str, SpeechFeed] = {}
        self.speech_jobs: dict[str, tuple[str, VoiceClient] | None] = {}
        self.pending_speech = {}
        self.action_conversations: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.coordinator = DataUpdateCoordinator(hass, _LOGGER, name="OpenClaw",
            config_entry=entry, update_method=lambda: self.refresh(), update_interval=timedelta(seconds=30))
        self.diagnostics.instrument(self, "connection", ["refresh", "close", "reserve_speech", "release_pending_speech"])
        self.diagnostics.instrument(self.modes, "conversation_state", ["begin", "end", "select", "recover", "close", "seed"])
        self.diagnostics.instrument(self.actions, "action_binding", ["start", "close", "pause", "resume", "shutdown"])

    @property
    def epoch(self):
        return self.entry.data[CONF_CLIENT_ID] + ":" + self.client.profile_revision

    @property
    def default_mode(self):
        return self.entry.options.get(CONF_SESSION_MODE, self.entry.data.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE))

    async def refresh(self):
        try:
            caps = await self.client.capabilities()
            validate_capabilities(caps)
            stable_identity = check_identity(caps, self.entry.data)
            languages = caps.get("languages")
            if (not isinstance(languages, list) or not languages or len(languages) > 100 or
                    any(not isinstance(value, str) or not value or len(value) > 32 for value in languages) or
                    caps.get("default_language") not in languages or not isinstance(caps.get("agent"), dict)):
                raise VoiceTransportError("invalid_capabilities")
            if caps["profile_revision"] != self.client.profile_revision:
                self.diagnostics.event("connection", "profile", "refreshed", {"reason": "profile_changed"}, "info")
                self.client = VoiceClient(async_get_clientsession(self.hass), api_url(self.entry.data[CONF_URL]),
                                          self.entry.data[CONF_CLIENT_TOKEN], caps["profile_revision"], diagnostics=self.diagnostics)
            self.capabilities = caps
            if stable_identity and self.entry.data.get("service_identity") != stable_identity:
                others = self.hass.config_entries.async_entries(self.entry.domain)
                if any(other.entry_id != self.entry.entry_id and other.unique_id == stable_identity for other in others):
                    raise VoiceTransportError("duplicate_backend")
                self.hass.config_entries.async_update_entry(self.entry,
                    data={**self.entry.data, "service_identity": stable_identity}, unique_id=stable_identity)
            self.actions.resume()
            return caps
        except VoiceTransportError as error:
            if error.code == "backend_auth_failed":
                raise ConfigEntryAuthFailed("OpenClaw pairing was revoked or its agent changed", translation_domain=DOMAIN, translation_key="pairing_revoked") from error
            raise UpdateFailed("OpenClaw connection unavailable: " + error.code) from error

    def stage_available(self, name):
        return self.coordinator.last_update_success and stage_available(self.capabilities, name)

    async def close(self):
        self.actions.pause()
        for action_id in tuple(self.pending_speech):
            self.release_pending_speech(action_id, expire=True)
        for feed in self.speech_feeds.values():
            feed.finish(VoiceTransportError("integration_unloaded"))
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            results = await asyncio.gather(*self.tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.diagnostics.event("connection", "owned_tasks", "cleanup_failed",
                        failure_summary(result, stage="shutdown"), "warning")
            self.diagnostics.event("connection", "owned_tasks", "cancelled", {"count": len(results), "confirmed": False}, "info")

    def reserve_speech(self, action_id, stream, feed):
        timer = self.hass.loop.call_later(60, lambda: self.release_pending_speech(action_id, expire=True))
        self.pending_speech[action_id] = (stream, feed, timer)

    def release_pending_speech(self, action_id, *, expire=False):
        pending = self.pending_speech.pop(action_id, None)
        if pending:
            stream, feed, timer = pending
            timer.cancel()
            if not expire:
                self.diagnostics.event("playback", "pending", "consumed")
            if expire:
                self.diagnostics.event("playback", "pending", "expired", {"reason": "pending_playback_expired", "confirmed": False}, "info")
                stream.delete()
                feed.finish(VoiceTransportError("speech_not_consumed"))
                self.speech_jobs.pop(action_id, None)

    def device_info(self):
        return {"identifiers": {("openclaw_voice", self.entry.entry_id)},
                "name": "OpenClaw", "manufacturer": "OpenClaw",
                "model": self.capabilities.get("agent", {}).get("id", "Configured agent")}
