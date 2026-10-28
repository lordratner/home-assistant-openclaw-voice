"""Real HA config flow, loader, config entries and platform registration."""
from pathlib import Path
import asyncio
import math
import struct
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from homeassistant.auth import auth_manager_from_config
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant import bootstrap, loader
from homeassistant.components.assist_pipeline import async_get_pipeline
from homeassistant.components import tts

from custom_components.openclaw_voice.client import VoiceClient
from custom_components.openclaw_voice.const import DOMAIN

CAPS = {"protocol_major": 1, "protocol_minor": 0, "profile_revision": "r1",
        "agent": {"id": "synthetic-agent"}, "languages": ["en"], "default_language": "en"}
PAIRED = {"client_id": "00000000-0000-4000-8000-000000000001", "client_token": "a" * 43,
          "capabilities": CAPS}


async def configure(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert form["type"] == "form"
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {
        "url": "http://127.0.0.1:1", "pairing_code": "synthetic-code", "session_mode": "persistent",
    })
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return result["result"]


@pytest_asyncio.fixture
async def hass(tmp_path):
    components = tmp_path / "custom_components"
    components.mkdir()
    (components / DOMAIN).symlink_to(Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN, target_is_directory=True)
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    assert await bootstrap.async_load_base_functionality(instance)
    instance.auth = await auth_manager_from_config(instance, [{"type": "homeassistant"}], [])
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)


@pytest.mark.asyncio
async def test_native_flow_registers_all_three_entities_and_actions_without_changing_pipeline(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)):
        assert await async_setup_component(hass, DOMAIN, {})
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert form["type"] == "form"
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {
            "url": "http://127.0.0.1:1", "pairing_code": "synthetic-code", "session_mode": "persistent",
        })
        assert result["type"] == "create_entry", result
        await hass.async_block_till_done()
        entry = result["result"]
        assert set(entry.runtime_data.entities) == {"stt", "conversation", "tts"}
        assert entry.runtime_data.entities["conversation"].supports_streaming
        assert entry.runtime_data.entities["tts"].async_supports_streaming_input()
        assert all(entity.entity_id in hass.states.async_entity_ids() for entity in entry.runtime_data.entities.values())
        assert hass.services.has_service(DOMAIN, "query")
        assert hass.services.has_service(DOMAIN, "start_conversation")
        assert async_get_pipeline(hass, None).conversation_engine != entry.runtime_data.entities["conversation"].entity_id


@pytest.mark.asyncio
async def test_native_query_override_followup_and_option_update_never_change_existing_mode(hass):
    opened = []

    async def open_conversation(self, conversation_id, mode):
        opened.append((conversation_id, mode))
        return {"conversation_handle": "00000000-0000-4000-8000-000000000002", "mode": mode, "profile_revision": "r1"}

    async def turn(self, handle, text, request_id=None, *, context=None):
        yield {"type": "text_delta", "message_id": 0, "text": "Synthetic reply", "job_id": PAIRED["client_id"]}
        yield {"type": "done", "job_id": PAIRED["client_id"], "continue_conversation": False}

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "open_conversation", open_conversation), patch.object(VoiceClient, "turn", turn):
        entry = await configure(hass)
        old_runtime = entry.runtime_data
        base = {"config_entry_id": entry.entry_id, "prompt": "Synthetic prompt"}
        initial = await hass.services.async_call(DOMAIN, "query", base, blocking=True, return_response=True)
        assert initial["session_mode"] == "persistent"
        isolated = await hass.services.async_call(DOMAIN, "query", {**base, "session_mode": "per_conversation"}, blocking=True, return_response=True)
        assert isolated["session_mode"] == "per_conversation"
        assert entry.runtime_data.default_mode == "persistent"
        options = await hass.config_entries.options.async_init(entry.entry_id)
        updated = await hass.config_entries.options.async_configure(options["flow_id"], {"session_mode": "per_conversation"})
        assert updated["type"] == "create_entry"
        await hass.async_block_till_done()
        assert entry.runtime_data is old_runtime  # A mode-only edit need not reload providers.
        followup = await hass.services.async_call(DOMAIN, "query", {**base, "conversation_id": initial["conversation_id"]}, blocking=True, return_response=True)
        assert followup["session_mode"] == "persistent"
        new = await hass.services.async_call(DOMAIN, "query", base, blocking=True, return_response=True)
        assert new["session_mode"] == "per_conversation"
        assert [mode for _, mode in opened] == ["persistent", "per_conversation", "per_conversation"]


@pytest.mark.asyncio
async def test_native_spoken_query_streams_audio_before_final_agent_text(hass):
    heard = asyncio.Event()
    finished = False
    audio = bytearray()

    async def open_conversation(self, conversation_id, mode):
        return {"conversation_handle": "00000000-0000-4000-8000-000000000002", "mode": mode, "profile_revision": "r1"}

    async def turn(self, handle, text, request_id=None, *, context=None):
        nonlocal finished
        yield {"type": "text_delta", "message_id": 0, "text": "Opening sentence.", "job_id": PAIRED["client_id"]}
        await heard.wait()
        finished = True
        yield {"type": "text_delta", "message_id": 0, "text": " The ending.", "job_id": PAIRED["client_id"]}
        yield {"type": "done", "job_id": PAIRED["client_id"], "continue_conversation": False}

    async def speech(self, source, language, *, on_job=None):
        if on_job:
            on_job(PAIRED["client_id"])
        yield {"type": "audio_start", "extension": "wav"}
        header = b"RIFF" + struct.pack("<I", 0xffffffff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16) + b"data" + struct.pack("<I", 0xffffffff)
        first = True
        async for _ in source:
            pcm = b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(7200))
            yield {"type": "audio_chunk", "audio": (header if first else b"") + pcm}
            first = False
        yield {"type": "done"}

    async def playback(call):
        token = call.data["media_content_id"].rsplit("/", 1)[-1]
        stream = tts.async_get_stream(hass, token)
        assert stream is not None
        async for chunk in stream.async_stream_result():
            audio.extend(chunk)
            if len(audio) >= 1024 and not heard.is_set():
                assert not finished
                heard.set()

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "open_conversation", open_conversation), patch.object(VoiceClient, "turn", turn), patch.object(VoiceClient, "speech", speech):
        entry = await configure(hass)
        hass.services.async_register("media_player", "play_media", playback)
        async with asyncio.timeout(10):
            result = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
                "prompt": "Synthetic spoken query", "speaker": "media_player.synthetic"}, blocking=True, return_response=True)
        assert heard.is_set()
        assert result["speech_requested"] is True
        assert result["response"]["speech"]["plain"]["speech"] == "Opening sentence. The ending."
        assert not entry.runtime_data.speech_jobs


@pytest.mark.asyncio
async def test_native_interactive_action_binds_only_its_chat_and_preserves_override_on_reload(hass):
    from homeassistant.components.assist_satellite import AssistSatelliteEntity, AssistSatelliteEntityFeature
    from homeassistant.components.assist_pipeline.pipeline import async_update_pipeline
    from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs
    from homeassistant.exceptions import ServiceValidationError

    created = []
    starts = []
    opened = []

    class Satellite(AssistSatelliteEntity):
        _attr_name = "Synthetic satellite"
        _attr_unique_id = "synthetic-satellite"
        _attr_supported_features = AssistSatelliteEntityFeature.START_CONVERSATION

        def async_get_configuration(self):
            return None

        async def async_set_configuration(self, config):
            pass

        async def async_start_conversation(self, announcement):
            starts.append(announcement)

        def on_pipeline_event(self, event):
            pass

    async def open_conversation(self, conversation_id, mode):
        opened.append((conversation_id, mode))
        return {"conversation_handle": "00000000-0000-4000-8000-000000000002", "mode": mode, "profile_revision": "r1"}

    async def turn(self, handle, text, request_id=None, *, context=None):
        yield {"type": "text_delta", "message_id": 0, "text": "Synthetic followup", "job_id": PAIRED["client_id"]}
        yield {"type": "done", "job_id": PAIRED["client_id"], "continue_conversation": False}

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "open_conversation", open_conversation), patch.object(VoiceClient, "turn", turn):
        entry = await configure(hass)
        assert await async_setup_component(hass, "assist_satellite", {})
        satellite = Satellite()
        await hass.data["assist_satellite"].async_add_entities([satellite])
        hass.config.internal_url = "http://127.0.0.1:8123"
        data = {"config_entry_id": entry.entry_id, "satellite_id": satellite.entity_id, "session_mode": "per_conversation"}
        before = async_get_pipeline(hass, None)
        with pytest.raises(ServiceValidationError, match="Select this OpenClaw"):
            await hass.services.async_call(DOMAIN, "start_conversation", data, blocking=True, return_response=True)
        assert not starts
        # This changes only the disposable HA fixture, never a live pipeline.
        await async_update_pipeline(hass, before, conversation_engine=entry.runtime_data.entities["conversation"].entity_id,
                                    tts_engine=entry.runtime_data.entities["tts"].entity_id, tts_language="en")
        stop = async_subscribe_chat_logs(hass, lambda identity, kind, payload: created.append(identity) if kind == "created" else None)
        try:
            result = await hass.services.async_call(DOMAIN, "start_conversation", data, blocking=True, return_response=True)
        finally:
            stop()
        assert len(starts) == 1
        assert created == [result["conversation_id"]]
        assert result["session_mode"] == "per_conversation"
        assert entry.runtime_data.default_mode == "persistent"
        old_runtime = entry.runtime_data
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not old_runtime
        assert entry.runtime_data.modes.select(result["conversation_id"], "persistent", entry.runtime_data.epoch).mode == "per_conversation"
        followup = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
            "conversation_id": result["conversation_id"], "prompt": "Synthetic followup"}, blocking=True, return_response=True)
        assert followup["session_mode"] == "per_conversation"
        assert opened == [(result["conversation_id"], "per_conversation")]


@pytest.mark.asyncio
async def test_overlapping_action_on_same_conversation_is_rejected_without_a_second_turn(hass):
    from homeassistant.exceptions import ServiceValidationError
    started = asyncio.Event()
    release = asyncio.Event()
    opened = []
    calls = 0

    async def open_conversation(self, conversation_id, mode):
        opened.append(conversation_id)
        return {"conversation_handle": "00000000-0000-4000-8000-000000000002", "mode": mode, "profile_revision": "r1"}

    async def turn(self, handle, text, request_id=None, *, context=None):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        yield {"type": "text_delta", "message_id": 0, "text": "Single reply", "job_id": PAIRED["client_id"]}
        yield {"type": "done", "job_id": PAIRED["client_id"], "continue_conversation": False}

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "open_conversation", open_conversation), patch.object(VoiceClient, "turn", turn):
        entry = await configure(hass)
        data = {"config_entry_id": entry.entry_id, "prompt": "First query"}
        first = asyncio.create_task(hass.services.async_call(DOMAIN, "query", data, blocking=True, return_response=True))
        try:
            async with asyncio.timeout(5):
                await started.wait()
                with pytest.raises(ServiceValidationError, match="active query"):
                    await hass.services.async_call(DOMAIN, "query", {**data, "conversation_id": opened[0]}, blocking=True, return_response=True)
                assert calls == 1
        finally:
            release.set()
            await first
        assert not entry.runtime_data.action_conversations


@pytest.mark.asyncio
async def test_unconsumed_speech_reservation_expires_without_retaining_capacity(hass):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from custom_components.openclaw_voice.runtime import SpeechFeed
    from custom_components.openclaw_voice.client import VoiceTransportError
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)):
        entry = await configure(hass)
        runtime = entry.runtime_data
        stream = SimpleNamespace(delete=Mock())
        feed = SpeechFeed()
        await feed.put("Synthetic pending output")
        runtime.speech_jobs["pending"] = None
        runtime.reserve_speech("pending", stream, feed)
        runtime.release_pending_speech("pending", expire=True)
        runtime.release_pending_speech("pending", expire=True)
        stream.delete.assert_called_once()
        assert not runtime.pending_speech
        assert not runtime.speech_jobs
        with pytest.raises(VoiceTransportError, match="speech_not_consumed"):
            await feed.ready()
