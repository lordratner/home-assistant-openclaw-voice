"""Native HA PCM recognition through the selected OC speech capability."""
import asyncio

from homeassistant.components import stt
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import VoiceTransportError
from .observability import observe, GLOBAL
from .capabilities import language_facts


@observe("platform", "stt_setup")
async def async_setup_entry(hass, entry, async_add_entities):
    entity = OpenClawSpeechToText(entry.runtime_data)
    entry.runtime_data.entities["stt"] = entity
    async_add_entities([entity])


class OpenClawSpeechToText(CoordinatorEntity, stt.SpeechToTextEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "recognition"

    def __init__(self, runtime):
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_unique_id = runtime.entry.entry_id + "_stt"
        self._attr_device_info = runtime.device_info()

    @property
    def supported_languages(self):
        return language_facts(self.runtime.capabilities, "stt")[0]

    @property
    def available(self):
        return self.runtime.stage_available("stt")

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

    @observe("stt", "pipeline")
    async def async_process_audio_stream(self, metadata, stream):
        diagnostics = getattr(self.runtime, "diagnostics", GLOBAL)
        span = diagnostics.current()
        timing = self.runtime.timings.start("stt") if hasattr(self.runtime, "timings") else None
        outcome = "failed"
        if not self.check_metadata(metadata):
            span.fail(VoiceTransportError("invalid_audio_metadata"), stage="validation")
            if timing:
                timing.finish("failed")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        task = asyncio.create_task(self.runtime.client.transcribe(stream, metadata.language), name="OpenClaw transcription")
        self.runtime.tasks.add(task)
        try:
            result = await task
            outcome = "completed"
            span.phase("no_speech" if result.get("no_speech") else "recognized",
                       **({"reason": "no_speech"} if result.get("no_speech") else {}))
            return stt.SpeechResult(result["text"], stt.SpeechResultState.SUCCESS)
        except VoiceTransportError as error:
            span.fail(error, stage="stt_transcription")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except asyncio.CancelledError:
            outcome = "closed"
            raise
        finally:
            if timing:
                timing.finish(outcome)
            self.runtime.tasks.discard(task)
