from types import SimpleNamespace
import json

import pytest

from custom_components.openclaw_voice.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.asyncio
async def test_diagnostics_never_echo_private_config_or_backend_extensions():
    private = "DO_NOT_INCLUDE_PRIVATE_VALUE"
    entry = SimpleNamespace(version=1, options={}, data={
        "url": private, "client_token": private, "agent_id": private, "session_mode": "persistent"},
        runtime_data=SimpleNamespace(
            capabilities={"protocol_major": 1, "protocol_minor": 0, "agent": {"id": private},
                          "profile_revision": private, "extra": private,
                          "limits": {"text_bytes": 65536, "receipt_ms": private},
                          "stt": {"modes": ["batch", private]}, "tts": {"input": "incremental_text"}},
            entities={"stt": private, "tts": private, "conversation": private, private: private},
            coordinator=SimpleNamespace(last_update_success=True), tasks={private}, speech_jobs={private: private}))
    result = await async_get_config_entry_diagnostics(None, entry)
    assert private not in json.dumps(result)
    assert result["limits"] == {"text_bytes": 65536}
    assert result["active_actions"] == 1
    assert result["registered_platforms"] == ["conversation", "stt", "tts"]
    del entry.runtime_data
    assert (await async_get_config_entry_diagnostics(None, entry))["loaded"] is False
