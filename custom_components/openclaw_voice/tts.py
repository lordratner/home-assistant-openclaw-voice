"""Native HA streaming-text TTS entity backed by the OC transport.

Uses HA's public TTSAudioRequest API and config-entry lifecycle.
"""
from collections.abc import AsyncGenerator
import asyncio
from contextlib import aclosing

from homeassistant.components.tts import TextToSpeechEntity, TTSAudioRequest, TTSAudioResponse
from homeassistant.exceptions import HomeAssistantError

from .client import VoiceClient, VoiceTransportError
from .const import DOMAIN
from .observability import observe, GLOBAL, failure_summary
from .capabilities import language_facts


def speech_error(reason, message, stage="synthesis_request"):
    error = HomeAssistantError(message, translation_domain=DOMAIN, translation_key=reason)
    error.diagnostic = {"reason": reason, "stage": stage,
                        "category": "policy" if reason == "profile_changed" else "cancelled" if reason == "speech_cancelled" else "validation"}
    return error


@observe("platform", "tts_setup")
async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data
    languages, default = language_facts(runtime.capabilities, "tts")
    entity = OpenClawTextToSpeech(runtime.client, entry.entry_id, languages, default, runtime=runtime)
    runtime.entities["tts"] = entity
    async_add_entities([entity])


class OpenClawTextToSpeech(TextToSpeechEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "speech"

    def __init__(self, client: VoiceClient, unique_id: str, languages: list[str], default_language: str, *, runtime=None) -> None:
        if default_language not in languages:
            raise ValueError("default language must be supported")
        self._client = client
        self._runtime = runtime
        self.diagnostics = runtime.diagnostics if runtime and hasattr(runtime, "diagnostics") else GLOBAL
        self._jobs = {}
        if runtime:
            self._attr_device_info = runtime.device_info()
        self._attr_unique_id = unique_id + "_tts"
        self._attr_supported_languages = languages.copy()
        self._attr_default_language = default_language

    @property
    def available(self):
        return self._runtime.stage_available("tts") if self._runtime else True

    @property
    def supported_languages(self):
        return language_facts(self._runtime.capabilities, "tts")[0] if self._runtime else self._attr_supported_languages

    @property
    def default_language(self):
        return language_facts(self._runtime.capabilities, "tts")[1] if self._runtime else self._attr_default_language

    @property
    def supported_options(self):
        return ["profile_revision", "action_id"]

    @property
    def default_options(self):
        client = self._runtime.client if self._runtime else self._client
        # Preserve the canonical live container unless a caller/device asks
        # otherwise. HA defaults to MP3, whose encoder can buffer a short first
        # sentence. Native satellite preferred-format options override this.
        return {"profile_revision": client.profile_revision, "preferred_format": "wav"}

    @observe("tts", "added_to_hass")
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self._runtime:
            self.async_on_remove(self._runtime.coordinator.async_add_listener(self.async_write_ha_state))

    @observe("tts", "unload")
    async def async_will_remove_from_hass(self):
        async def cancel(job_id, client):
            try:
                async with asyncio.timeout(5):
                    await client.cancel_job(job_id)
            except (VoiceTransportError, TimeoutError) as error:
                self.diagnostics.event("tts", "unload_cancel", "cleanup_failed",
                    failure_summary(error, stage="cleanup", job_id=job_id, confirmed=False), "warning")
        await asyncio.gather(*(cancel(job_id, client) for job_id, client in tuple(self._jobs.items())))
        await super().async_will_remove_from_hass()

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        # The v1 transport negotiates one canonical PCM/WAV stream. Acquire it
        # only when the caller starts consuming data, so closing an unstarted
        # response cannot strand an already-open HTTP/provider operation.
        async def audio() -> AsyncGenerator[bytes]:
            outcome = "closed"
            client = self._runtime.client if self._runtime else self._client
            if request.options.get("profile_revision", client.profile_revision) != client.profile_revision:
                raise speech_error("profile_changed", "OpenClaw's voice profile changed", "validation")
            action_id = request.options.get("action_id")
            job_id = None
            timing = self._runtime.timings.start("tts") if self._runtime else None

            def admitted(value):
                nonlocal job_id
                job_id = value
                self.diagnostics.current().phase("admitted", job_id=value, admitted=True)
                self._jobs[value] = client
                if action_id:
                    if not self._runtime or action_id not in self._runtime.speech_jobs:
                        raise speech_error("speech_cancelled", "OpenClaw speech action is no longer active", "playback")
                    self._runtime.release_pending_speech(action_id)
                    self._runtime.speech_jobs[action_id] = (value, client)

            events = client.speech(request.message_gen, request.language, on_job=admitted)
            try:
                async for event in events:
                    if event["type"] == "audio_start":
                        if event["extension"] != "wav":
                            raise speech_error("unsupported_audio_format", "OpenClaw audio requires normalization", "normalization")
                    elif event["type"] == "audio_chunk":
                        timing and timing.mark("first_audio_received")
                        yield event["audio"]
                outcome = "completed"
            except VoiceTransportError as error:
                outcome = "failed"
                raise HomeAssistantError("OpenClaw speech stream was interrupted", translation_domain=DOMAIN, translation_key="speech_stream_interrupted") from error
            except Exception:
                outcome = "failed"
                raise
            finally:
                timing and timing.finish(outcome)
                await events.aclose()
                if job_id:
                    self._jobs.pop(job_id, None)
                if action_id and self._runtime:
                    self._runtime.speech_jobs.pop(action_id, None)

        @observe("tts", "pipeline")
        async def observed_audio(owner):
            span = self.diagnostics.current()
            total = 0
            async with aclosing(audio()) as source:
                async for chunk in source:
                    total += len(chunk)
                    span.annotate(bytes=total)
                    span.phase("first_audio_received")
                    yield chunk

        return TTSAudioResponse("wav", observed_audio(self))

    @observe("tts", "buffered_announcement")
    async def async_get_tts_audio(self, message: str, language: str, options: dict):
        async def text():
            yield message
        response = await self.async_stream_tts_audio(TTSAudioRequest(language, options, text()))
        content = bytearray()
        async for chunk in response.data_gen:
            content.extend(chunk)
        return response.extension, bytes(content)
