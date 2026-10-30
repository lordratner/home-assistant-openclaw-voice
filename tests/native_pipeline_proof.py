"""Disposable real HA config flow/entities/actions -> production OC plugin.

The companion Gateway serves ONLY synthetic model/audio endpoints. No live
configuration, user audio, household hardware or provider account is touched.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

component_root = Path(os.environ.get("VOICE_PROOF_HA_PACKAGE_ROOT", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(component_root))
from homeassistant import bootstrap, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import async_get_pipeline
from homeassistant.components.assist_pipeline.pipeline import async_update_pipeline
from homeassistant.components.assist_satellite import AssistSatelliteEntity, AssistSatelliteEntityFeature
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.setup import async_setup_component
from custom_components.openclaw_voice.const import DOMAIN


class Satellite(AssistSatelliteEntity):
    _attr_name = "Synthetic pipeline satellite"
    _attr_unique_id = "synthetic-native-pipeline"
    _attr_supported_features = AssistSatelliteEntityFeature.START_CONVERSATION
    starts = 0

    def async_get_configuration(self):
        return None

    async def async_set_configuration(self, config):
        pass

    def on_pipeline_event(self, event):
        pass

    async def async_start_conversation(self, announcement):
        self.starts += 1


async def main():
    with tempfile.TemporaryDirectory(prefix="ha-openclaw-native-") as directory:
        components = Path(directory) / "custom_components"
        components.mkdir()
        (components / DOMAIN).symlink_to(component_root / "custom_components" / DOMAIN, target_is_directory=True)
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        assert await bootstrap.async_load_base_functionality(hass)
        hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
        hass.config.internal_url = "http://127.0.0.1:8123"
        try:
            assert await async_setup_component(hass, DOMAIN, {})
            flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
            paired = await hass.config_entries.flow.async_configure(flow["flow_id"], {
                "url": os.environ["VOICE_PROOF_URL"], "pairing_code": os.environ["VOICE_PROOF_PAIRING_CODE"],
                "session_mode": "persistent"})
            assert paired["type"] == "create_entry", paired["type"]
            await hass.async_block_till_done()
            entry = paired["result"]
            assert set(entry.runtime_data.entities) == {"stt", "conversation", "tts"}
            preferred_before = async_get_pipeline(hass, None)
            metadata = stt.SpeechMetadata(language="en", format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
                bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                channel=stt.AudioChannels.CHANNEL_MONO)

            async def microphone():
                yield b"\0\0" * 1600

            recognized = await entry.runtime_data.entities["stt"].async_process_audio_stream(metadata, microphone())
            assert recognized.result == stt.SpeechResultState.SUCCESS
            assert recognized.text == "SYNTHETIC_TRANSCRIPT_OK"
            audio = bytearray()
            moments = {}

            async def playback(call):
                token = call.data["media_content_id"].rsplit("/", 1)[-1]
                stream = tts.async_get_stream(hass, token)
                assert stream is not None and stream.extension == "wav"
                async for chunk in stream.async_stream_result():
                    if len(audio) + len(chunk) > 44 and "first_audio" not in moments:
                        moments["first_audio"] = time.time() * 1000
                    audio.extend(chunk)

            hass.services.async_register("media_player", "play_media", playback)
            moments["request_started"] = time.time() * 1000
            spoken = await hass.services.async_call(DOMAIN, "query", {
                "config_entry_id": entry.entry_id, "prompt": "Use voice_proof and answer with the fixture text.",
                "session_mode": "per_conversation", "speaker": "media_player.synthetic"}, blocking=True, return_response=True)
            assert spoken["response"]["speech"]["plain"]["speech"] == "Opening sentence. This is the ending."
            assert audio[:4] == b"RIFF" and len(audio) > 44
            assert async_get_pipeline(hass, None) == preferred_before
            assert await async_setup_component(hass, "assist_satellite", {})
            satellite = Satellite()
            await hass.data["assist_satellite"].async_add_entities([satellite])
            # Isolated fixture setup only: the integration itself never mutates pipelines.
            await async_update_pipeline(hass, preferred_before,
                conversation_engine=entry.runtime_data.entities["conversation"].entity_id,
                tts_engine=entry.runtime_data.entities["tts"].entity_id, tts_language="en")
            interactive = await hass.services.async_call(DOMAIN, "start_conversation", {
                "config_entry_id": entry.entry_id, "satellite_id": satellite.entity_id,
                "session_mode": "per_conversation"}, blocking=True, return_response=True)
            assert satellite.starts == 1
            followup = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
                "conversation_id": interactive["conversation_id"], "prompt": "Use voice_proof and answer with the fixture text."},
                blocking=True, return_response=True)
            assert followup["session_mode"] == "per_conversation"
            assert entry.runtime_data.default_mode == "persistent"
            assert await async_setup_component(hass, "homeassistant", {})
            assert await async_setup_component(hass, "intent", {})
            hass.states.async_set("light.fixture", "off", {"friendly_name": "Fixture Light"})
            hass.states.async_set("light.hidden", "off", {"friendly_name": "Hidden Light"})
            async_expose_entity(hass, "conversation", "light.fixture", True)
            async_expose_entity(hass, "conversation", "light.hidden", False)
            effects = []
            async def turn_on(call):
                effects.append(call.data)
                hass.states.async_set("light.fixture", "on", {"friendly_name": "Fixture Light"})
            hass.services.async_register("light", "turn_on", turn_on)
            options = await hass.config_entries.options.async_init(entry.entry_id)
            updated = await hass.config_entries.options.async_configure(options["flow_id"],
                {"session_mode": "persistent", CONF_LLM_HASS_API: ["assist"]})
            assert updated["type"] == "create_entry"
            await hass.async_block_till_done()
            for name in ("Fixture Light", "Hidden Light"):
                native = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
                    "session_mode": "per_conversation", "prompt": "native-assist-proof: turn on " + name},
                    blocking=True, return_response=True)
                assert native["response"]["speech"]["plain"]["speech"] == "Native Assist result received.", native
            assert len(effects) == 1, effects
            diagnostics = entry.runtime_data.diagnostics.snapshot()
            assert diagnostics["schema"] == 1
            assert len(diagnostics["recent"]) <= 128 and len(diagnostics["failures"]) <= 32
            assert any(e.get("request_id") and e.get("job_id") for e in diagnostics["recent"])
            logged = json.dumps(diagnostics)
            for private in ["Fixture Light", "Hidden Light", "SYNTHETIC_TRANSCRIPT_OK", "native-assist-proof"]:
                assert private not in logged
            assert not entry.runtime_data.tasks and not entry.runtime_data.speech_jobs
            assert await hass.config_entries.async_unload(entry.entry_id)
            print(json.dumps({"status": "passed", "native_config_flow": True, "registered_entities": 3,
                "stt": "native_batch", "query_action": True, "interactive_action": True,
                "followup_mode_preserved": True, "unloaded": True, "audio_bytes": len(audio),
                "native_assist": True, "exposed_effects": len(effects), "hidden_effects": 0,
                "audible_playback_tested": False, "structured_logging_checked": True, **moments}))
        finally:
            await hass.async_stop(force=True)


async def bounded():
    async with asyncio.timeout(45):
        await main()


asyncio.run(bounded())
