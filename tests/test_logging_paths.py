"""Inject failures at native integration boundaries and inspect safe diagnostics."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import stt
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.openclaw_voice.client import VoiceClient, VoiceTransportError
from custom_components.openclaw_voice.observability import GLOBAL
from custom_components.openclaw_voice.const import DOMAIN
from test_ha_setup import hass, configure, CAPS, PAIRED


def metadata(rate=16000):
    return stt.SpeechMetadata(language="en", format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=rate, channel=stt.AudioChannels.CHANNEL_MONO)


async def audio():
    yield b"PRIVATE_AUDIO"


async def text():
    yield "PRIVATE_SPOKEN_TEXT"


@pytest.mark.asyncio
async def test_native_stt_metadata_and_provider_failures_are_not_silent(hass):
    error = VoiceTransportError("backend_job_failed", PAIRED["client_id"],
        {"http_status": 429, "category": "rate_limit", "provider_code": "quota_exceeded", "message": "PRIVATE_TOKEN"})
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "transcribe", AsyncMock(side_effect=error)) as transcribe:
        entry = await configure(hass)
        runtime = entry.runtime_data
        entity = runtime.entities["stt"]
        invalid = await entity.async_process_audio_stream(metadata(48000), audio())
        assert invalid.result == stt.SpeechResultState.ERROR
        transcribe.assert_not_called()
        failed = await entity.async_process_audio_stream(metadata(), audio())
        assert failed.result == stt.SpeechResultState.ERROR
        failures = runtime.diagnostics.snapshot()["failures"]
        assert any(v.get("reason") == "invalid_audio_metadata" for v in failures)
        assert any(v.get("http_status") == 429 and v.get("provider_code") == "quota_exceeded" for v in failures)
        assert "PRIVATE" not in json.dumps(runtime.diagnostics.snapshot())
        assert all(v["status"] == "failed" for v in runtime.timings.snapshot())


@pytest.mark.asyncio
async def test_lazy_tts_profile_failure_before_audio_has_a_logged_reason(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "speech") as speech:
        entry = await configure(hass)
        response = await entry.runtime_data.entities["tts"].async_stream_tts_audio(
            TTSAudioRequest("en", {"profile_revision": "old"}, text()))
        with pytest.raises(HomeAssistantError):
            _ = [chunk async for chunk in response.data_gen]
        speech.assert_not_called()
        failures = entry.runtime_data.diagnostics.snapshot()["failures"]
        assert any(v.get("reason") == "profile_changed" and v.get("stage") == "validation" for v in failures)
        assert "PRIVATE" not in json.dumps(failures)


@pytest.mark.asyncio
async def test_action_validation_and_best_effort_unload_cancel_are_logged(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)):
        entry = await configure(hass)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
                "prompt": "PRIVATE_PROMPT", "speaker": "light.private"}, blocking=True, return_response=True)
        assert any(v.get("reason") == "speaker_invalid" for v in GLOBAL.snapshot()["failures"])
        runtime = entry.runtime_data
        entity = runtime.entities["tts"]
        client = AsyncMock()
        client.cancel_job.side_effect = VoiceTransportError("backend_unavailable", diagnostic={"http_status": 503})
        entity._jobs[PAIRED["client_id"]] = client
        await entity.async_will_remove_from_hass()
        assert any(v["operation"] == "unload_cancel" and v.get("http_status") == 503 for v in runtime.diagnostics.snapshot()["failures"])
        assert "PRIVATE" not in json.dumps(runtime.diagnostics.snapshot())


@pytest.mark.asyncio
async def test_startup_auth_failure_keeps_cause_without_logging_pairing_data(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(side_effect=VoiceTransportError(
                "backend_auth_failed", diagnostic={"http_status": 401, "message": "PRIVATE_CREDENTIAL"}))):
        await configure(hass)
        assert any(v.get("http_status") == 401 and v.get("category") == "authentication" for v in GLOBAL.snapshot()["failures"])
        assert "PRIVATE_CREDENTIAL" not in json.dumps(GLOBAL.snapshot())


@pytest.mark.asyncio
async def test_action_returns_current_job_and_request_receipt_after_pre_admission_recovery(hass):
    caps = dict(CAPS)
    attempts, accepted = [], []
    job = "00000000-0000-4000-8000-000000000099"

    async def turn(self, handle, prompt, request_id=None, **kwargs):
        attempts.append(request_id)
        if len(attempts) == 1:
            caps["profile_revision"] = "r2"
            raise VoiceTransportError("profile_changed")
        accepted.append(request_id)
        yield {"type": "text_delta", "message_id": 0, "text": "Safe reply", "job_id": job}
        yield {"type": "done", "job_id": job}

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(side_effect=lambda: dict(caps))), \
            patch.object(VoiceClient, "open_conversation", AsyncMock(return_value={"conversation_handle": PAIRED["client_id"]})), \
            patch.object(VoiceClient, "turn", turn):
        entry = await configure(hass)
        result = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
            "prompt": "One synthetic action"}, blocking=True, return_response=True)
        assert result["job_id"] == job
        assert result["request_id"] == attempts[0] == attempts[1]
        assert accepted == [result["request_id"]]
