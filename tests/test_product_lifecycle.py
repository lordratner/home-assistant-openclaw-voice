"""Native flow/migration/degradation checks; no real provider or service calls."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import asyncio
import struct

import pytest

from homeassistant import config_entries
from homeassistant.setup import async_setup_component
from homeassistant.components import tts
from custom_components.openclaw_voice.client import VoiceClient, VoiceTransportError
from custom_components.openclaw_voice.capabilities import validate_capabilities, language_facts
from custom_components.openclaw_voice.const import DOMAIN
from test_ha_setup import hass, configure, CAPS


def capabilities():
    return {**CAPS, "protocol_minor": 2, "service": {
        "instance_id": "11111111-1111-4111-8111-111111111111",
        "profile_id": "22222222-2222-4222-8222-222222222222"},
        "stages": {
            "conversation": {"state": "configured", "available": None, "verified": False},
            "stt": {"state": "unknown", "available": None, "verified": False,
                    "languages": ["de-DE"], "default_language": "de-DE"},
            "tts": {"state": "unavailable", "available": False, "verified": False,
                    "reason": "audio_normalizer_unavailable", "languages": ["fr-FR"], "default_language": "fr-FR"}}}


def paired(caps=None):
    return {"client_id": str(uuid4()), "client_token": "a" * 43, "capabilities": caps or capabilities()}


async def flow(hass, source="user", entry=None, data=None):
    context = {"source": source}
    if entry:
        context["entry_id"] = entry.entry_id
    return await hass.config_entries.flow.async_init(DOMAIN, context=context, data=data)


INPUT = {"url": "http://127.0.0.1:1", "pairing_code": "synthetic-code", "session_mode": "persistent"}


@pytest.mark.asyncio
async def test_duplicate_pairing_is_rejected_and_only_new_credential_revoked(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(side_effect=[paired(caps), paired(caps)])), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as control:
        first = await configure(hass)
        entry_id = first.entry_id
        form = await flow(hass)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "abort" and result["reason"] == "already_configured"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        assert hass.config_entries.async_entries(DOMAIN)[0].entry_id == entry_id
        control.assert_awaited_once_with("DELETE", "/pair")


@pytest.mark.asyncio
async def test_missing_oc_tts_keeps_native_conversation_and_stt_available_with_separate_languages(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)):
        entry = await configure(hass)
        entities = entry.runtime_data.entities
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert entities["conversation"].available
        assert entities["stt"].available
        assert not entities["tts"].available
        assert entities["stt"].supported_languages == ["de-DE"]
        assert entities["tts"].default_language == "fr-FR"
        assert "session_mode" not in entry.data
        assert entry.options["session_mode"] == "persistent"


@pytest.mark.asyncio
async def test_reconfigure_moves_same_backend_without_replacing_entry_or_entities_and_rejects_wrong_backend(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)) as read:
        entry = await configure(hass)
        entities = {k: v.entity_id for k, v in entry.runtime_data.entities.items()}
        original_id, identity = entry.entry_id, entry.unique_id
        form = await flow(hass, "reconfigure", entry)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": "http://new-backend.invalid"})
        assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data["url"] == "http://new-backend.invalid"
        assert entry.entry_id == original_id and entry.unique_id == identity
        assert {k: v.entity_id for k, v in entry.runtime_data.entities.items()} == entities
        wrong = deepcopy(caps)
        wrong["service"]["profile_id"] = str(uuid4())
        read.return_value = wrong
        form = await flow(hass, "reconfigure", entry)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": "http://wrong.invalid"})
        assert result["type"] == "form" and result["errors"]["base"] == "wrong_backend"
        assert entry.data["url"] == "http://new-backend.invalid"


@pytest.mark.asyncio
async def test_v1_migration_preserves_entry_entities_token_and_options_and_adopts_stable_identity(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)):
        entry = await configure(hass)
        ids = {k: v.entity_id for k, v in entry.runtime_data.entities.items()}
        token = entry.data["client_token"]
        data = {k: v for k, v in entry.data.items() if k != "service_identity"}
        data["session_mode"] = "persistent"
        hass.config_entries.async_update_entry(entry, data=data, options={"session_mode": "per_conversation"},
                                               unique_id=data["client_id"], version=1)
        await hass.config_entries.async_reload(entry.entry_id)
        assert entry.version == 2
        assert "session_mode" not in entry.data
        assert entry.options["session_mode"] == "per_conversation"
        assert entry.data["client_token"] == token
        assert entry.unique_id == entry.data["service_identity"]
        assert {k: v.entity_id for k, v in entry.runtime_data.entities.items()} == ids


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["agent", "profile"])
async def test_reauth_rejects_different_agent_and_discards_new_credential(hass, changed):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))) as pair, \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as control:
        entry = await configure(hass)
        old = dict(entry.data)
        wrong = deepcopy(caps)
        if changed == "agent":
            wrong["agent"] = {"id": "another-agent"}
        else:
            wrong["service"]["profile_id"] = str(uuid4())
        pair.return_value = paired(wrong)
        form = await flow(hass, "reauth", entry, dict(entry.data))
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "form" and result["errors"]["base"] == "wrong_backend"
        assert dict(entry.data) == old
        control.assert_awaited_once_with("DELETE", "/pair")


def test_capabilities_reject_malformed_availability_identity_and_language_without_breaking_legacy():
    assert validate_capabilities(CAPS) == CAPS
    for change in [lambda c: c["stages"]["tts"].update(available=1),
                   lambda c: c["service"].update(instance_id="arbitrary"),
                   lambda c: c["stages"]["stt"].update(default_language="en"),
                   lambda c: c.update(protocol_major=True),
                   lambda c: c.pop("service"),
                   lambda c: c.update(profile_revision="")]:
        caps = capabilities()
        change(caps)
        with pytest.raises(VoiceTransportError, match="invalid_capabilities"):
            validate_capabilities(caps)
    assert language_facts(CAPS, "tts") == (["en"], "en")


@pytest.mark.asyncio
async def test_invalid_pair_capabilities_revoke_issued_token_without_creating_entry(hass):
    bad = paired(capabilities())
    bad["capabilities"]["service"]["profile_id"] = "invalid"
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=bad)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as cleanup:
        assert await async_setup_component(hass, DOMAIN, {})
        form = await flow(hass)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "form" and result["errors"]["base"] == "cannot_connect"
        assert not hass.config_entries.async_entries(DOMAIN)
        cleanup.assert_awaited_once_with("DELETE", "/pair")


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("backend_unavailable", "cannot_connect"), ("pairing_failed", "pairing_failed")])
async def test_pair_connection_failures_leave_form_retryable_without_entries(hass, code, expected):
    with patch.object(VoiceClient, "pair", AsyncMock(side_effect=VoiceTransportError(code))):
        assert await async_setup_component(hass, DOMAIN, {})
        form = await flow(hass)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "form" and result["errors"]["base"] == expected
        assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.asyncio
async def test_invalid_url_is_rejected_before_pairing_request(hass):
    with patch.object(VoiceClient, "pair", AsyncMock()) as pair:
        assert await async_setup_component(hass, DOMAIN, {})
        form = await flow(hass)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {**INPUT, "url": "ftp://invalid.example"})
        assert result["type"] == "form" and result["errors"]["base"] == "invalid_input"
        pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_backend_duplicate_guard_and_reauth_move_require_original_identity(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(side_effect=[paired(CAPS), paired(CAPS), paired(capabilities())])), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as cleanup:
        entry = await configure(hass)
        form = await flow(hass)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "abort" and result["reason"] == "already_configured"
        original = dict(entry.data)
        form = await flow(hass, "reauth", entry, dict(entry.data))
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {**INPUT, "url": "http://moved.invalid"})
        assert result["type"] == "form" and result["errors"]["base"] == "legacy_identity_required"
        assert dict(entry.data) == original
        assert cleanup.await_count == 2


@pytest.mark.asyncio
async def test_reconfigure_connection_failure_does_not_replace_url_or_credential(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)) as read:
        entry = await configure(hass)
        original = dict(entry.data)
        read.side_effect = VoiceTransportError("backend_unavailable")
        form = await flow(hass, "reconfigure", entry)
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": "http://offline.invalid"})
        assert result["type"] == "form" and result["errors"]["base"] == "cannot_connect"
        assert dict(entry.data) == original


@pytest.mark.asyncio
async def test_future_config_schema_is_not_downgraded_or_reset(hass):
    from custom_components.openclaw_voice import async_migrate_entry
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)):
        entry = await configure(hass)
        original = dict(entry.data)
        hass.config_entries.async_update_entry(entry, version=99)
        assert not await async_migrate_entry(hass, entry)
        assert entry.version == 99 and dict(entry.data) == original


@pytest.mark.asyncio
async def test_concurrent_pairing_flows_create_one_entry_and_discard_the_other_token(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(side_effect=[paired(caps), paired(caps)])), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as cleanup:
        assert await async_setup_component(hass, DOMAIN, {})
        one, two = await flow(hass), await flow(hass)
        results = await asyncio.gather(*(hass.config_entries.flow.async_configure(f["flow_id"], INPUT) for f in [one, two]))
        assert sorted(r["type"] for r in results) == ["abort", "create_entry"]
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        cleanup.assert_awaited_once_with("DELETE", "/pair")


@pytest.mark.asyncio
async def test_successful_reauth_rotates_only_credential_preserving_stable_identity_and_entities(hass):
    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))) as pair, \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "control", AsyncMock(return_value={"revoked": True})) as cleanup:
        entry = await configure(hass)
        stable, entry_id = entry.unique_id, entry.entry_id
        ids = {k: v.entity_id for k, v in entry.runtime_data.entities.items()}
        new = paired(caps)
        new["client_token"] = "b" * 43
        pair.return_value = new
        form = await flow(hass, "reauth", entry, dict(entry.data))
        result = await hass.config_entries.flow.async_configure(form["flow_id"], INPUT)
        assert result["type"] == "abort" and result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
        assert entry.entry_id == entry_id and entry.unique_id == stable
        assert entry.data["client_token"] == new["client_token"]
        assert {k: v.entity_id for k, v in entry.runtime_data.entities.items()} == ids
        cleanup.assert_awaited_once_with("DELETE", "/pair")


@pytest.mark.asyncio
async def test_native_buffered_ha_tts_can_speak_query_when_oc_tts_is_unavailable(hass):
    calls, heard = [], bytearray()
    class ExistingSpeech(tts.TextToSpeechEntity):
        _attr_name = "Existing local speech"
        _attr_unique_id = "existing-local-speech"
        _attr_supported_languages = ["en"]
        _attr_default_language = "en"
        _attr_default_options = {"preferred_format": "wav"}

        async def async_get_tts_audio(self, message, language, options):
            calls.append(message)
            pcm = b"\0\0" * 7200
            header = b"RIFF" + struct.pack("<I", len(pcm) + 36) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16) + b"data" + struct.pack("<I", len(pcm))
            return "wav", header + pcm

    async def turn(self, handle, text, request_id=None, **kwargs):
        yield {"type": "text_delta", "message_id": 0, "text": "A complete buffered reply.", "job_id": "11111111-1111-4111-8111-111111111111"}
        yield {"type": "done", "job_id": "11111111-1111-4111-8111-111111111111"}

    async def playback(call):
        stream = tts.async_get_stream(hass, call.data["media_content_id"].rsplit("/", 1)[-1])
        async for chunk in stream.async_stream_result():
            heard.extend(chunk)

    caps = capabilities()
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=paired(caps))), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "open_conversation", AsyncMock(return_value={"conversation_handle": str(uuid4())})), \
            patch.object(VoiceClient, "turn", turn), \
            patch.object(VoiceClient, "speech", side_effect=AssertionError("OC TTS must not be used")):
        entry = await configure(hass)
        external = ExistingSpeech()
        await hass.data[tts.DATA_COMPONENT].async_add_entities([external])
        hass.services.async_register("media_player", "play_media", playback)
        async with asyncio.timeout(10):
            result = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
                "prompt": "Use existing HA speech", "speaker": "media_player.synthetic",
                "tts_entity": external.entity_id}, blocking=True, return_response=True)
        assert result["speech_requested"]
        assert calls == ["A complete buffered reply."]
        assert len(heard) > 44
        assert not entry.runtime_data.entities["tts"].available
        assert not entry.runtime_data.pending_speech
