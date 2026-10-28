"""Native HA streaming-text TTS entity backed by the OC transport.

Registration/config-entry lifecycle follows after the shared contract gate. This
entity can already be exercised through HA's real public TTSAudioRequest API.
"""
from collections.abc import AsyncGenerator

from homeassistant.components.tts import TextToSpeechEntity, TTSAudioRequest, TTSAudioResponse
from homeassistant.exceptions import HomeAssistantError

from .client import VoiceClient, VoiceTransportError


class OpenClawTextToSpeech(TextToSpeechEntity):
    _attr_has_entity_name = True
    _attr_name = "Speech"

    def __init__(self, client: VoiceClient, unique_id: str, languages: list[str], default_language: str) -> None:
        if default_language not in languages:
            raise ValueError("default language must be supported")
        self._client = client
        self._attr_unique_id = unique_id + "_tts"
        self._attr_supported_languages = languages.copy()
        self._attr_default_language = default_language

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        events = self._client.speech(request.message_gen, request.language)
        try:
            first = await anext(events)
            if first["type"] != "audio_start":
                await events.aclose()
                raise HomeAssistantError("OpenClaw returned no speech")
            if first["extension"] == "pcm":
                await events.aclose()
                raise HomeAssistantError("OpenClaw PCM audio requires normalization")
        except (StopAsyncIteration, VoiceTransportError) as error:
            await events.aclose()
            raise HomeAssistantError("OpenClaw speech is unavailable") from error

        extension = first["extension"]
        # Arbitrary container concatenation is unsafe. Only MP3's self-framed
        # stream is admitted for this first sentence-stream adapter. Other formats
        # need the planned normalizer before multi-segment release validation.
        async def audio() -> AsyncGenerator[bytes]:
            try:
                async for event in events:
                    if event["type"] == "audio_start":
                        if event["extension"] != extension or extension != "mp3":
                            raise HomeAssistantError("OpenClaw audio requires normalization")
                    elif event["type"] == "audio_chunk":
                        yield event["audio"]
            except VoiceTransportError as error:
                raise HomeAssistantError("OpenClaw speech stream was interrupted") from error
            finally:
                await events.aclose()

        return TTSAudioResponse(extension, audio())

    async def async_get_tts_audio(self, message: str, language: str, options: dict):
        async def text():
            yield message
        response = await self.async_stream_tts_audio(TTSAudioRequest(language, options, text()))
        content = bytearray()
        async for chunk in response.data_gen:
            content.extend(chunk)
        return response.extension, bytes(content)
