"""Allowlisted operational diagnostics, with no identities or conversation data."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SESSION_MODE, DEFAULT_SESSION_MODE, SESSION_MODES


def _integer_fields(value, fields):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields if type(value.get(key)) is int and 0 <= value[key] <= 2**53}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    mode = entry.options.get(CONF_SESSION_MODE, entry.data.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE))
    runtime = getattr(entry, "runtime_data", None)
    result = {"config_version": entry.version, "loaded": runtime is not None,
              "default_mode": mode if mode in SESSION_MODES else "invalid"}
    if runtime is None:
        return result
    caps = runtime.capabilities
    # Never dump config entry data, URL, client/token/agent/session identifiers,
    # backend messages, arbitrary capability extensions, prompts or audio.
    result["protocol"] = _integer_fields(caps, ("protocol_major", "protocol_minor"))
    result["limits"] = _integer_fields(caps.get("limits"), (
        "text_bytes", "audio_chunk_bytes", "stt_audio_bytes", "replay_bytes", "receipt_ms", "result_ms"))
    result["registered_platforms"] = sorted(set(runtime.entities) & {"stt", "tts", "conversation"})
    result["connection_available"] = runtime.coordinator.last_update_success
    result["active_actions"] = len(runtime.tasks)
    result["active_speech_jobs"] = len(runtime.speech_jobs)
    result["stt_batch_available"] = "batch" in caps.get("stt", {}).get("modes", [])
    result["tts_incremental_text"] = caps.get("tts", {}).get("input") == "incremental_text"
    return result
