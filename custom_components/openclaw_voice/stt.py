"""Native HA PCM recognition through the selected OC speech capability."""
import asyncio

from homeassistant.components import stt
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import VoiceTransportError


async def async_setup_entry(hass, entry, async_add_entities):
    entity = OpenClawSpeechToText(entry.runtime_data)
    entry.runtime_data.entities["stt"] = entity
    async_add_entities([entity])


class OpenClawSpeechToText(CoordinatorEntity, stt.SpeechToTextEntity):
    _attr_has_entity_name = True
    _attr_name = "Speech recognition"

    def __init__(self, runtime):
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_unique_id = runtime.entry.entry_id + "_stt"
        self._attr_device_info = runtime.device_info()

    @property
    def supported_languages(self):
        return self.runtime.capabilities["languages"]

    @property
    def supported_formats(self):
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self):
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self):
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self):
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        if not self.check_metadata(metadata):
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        task = asyncio.create_task(self.runtime.client.transcribe(stream, metadata.language), name="OpenClaw transcription")
        self.runtime.tasks.add(task)
        try:
            result = await task
            return stt.SpeechResult(result["text"], stt.SpeechResultState.SUCCESS)
        except VoiceTransportError:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        finally:
            self.runtime.tasks.discard(task)
