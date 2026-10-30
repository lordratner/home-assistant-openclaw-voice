"""Additive v1.2 capability checks; optional speech never gates conversation."""
from .transport import VoiceTransportError, canonical_id

STAGES = ("conversation", "stt", "tts")
STATES = {"unknown", "configured", "unavailable", "verified"}


def service_identity(caps):
    service = caps.get("service")
    if service is None:
        return None
    if not isinstance(service, dict) or not all(canonical_id(service.get(k)) for k in ("instance_id", "profile_id")):
        raise VoiceTransportError("invalid_capabilities")
    return service["instance_id"] + ":" + service["profile_id"]


def validate_capabilities(caps):
    if (not isinstance(caps, dict) or type(caps.get("protocol_major")) is not int or caps["protocol_major"] != 1
            or not isinstance(caps.get("profile_revision"), str) or not 1 <= len(caps["profile_revision"]) <= 128):
        raise VoiceTransportError("invalid_capabilities")
    service_identity(caps)
    minor = caps.get("protocol_minor", 0)
    if type(minor) is not int or minor < 0:
        raise VoiceTransportError("invalid_capabilities")
    if minor >= 2 and (not caps.get("service") or not isinstance(caps.get("stages"), dict)):
        raise VoiceTransportError("invalid_capabilities")
    stages = caps.get("stages", {})
    if not isinstance(stages, dict):
        raise VoiceTransportError("invalid_capabilities")
    for name in STAGES:
        stage = stages.get(name)
        if stage is None:
            continue
        if (not isinstance(stage, dict) or (stage.get("available") is not None and type(stage.get("available")) is not bool)
                or stage.get("state") not in STATES or type(stage.get("verified")) is not bool):
            raise VoiceTransportError("invalid_capabilities")
        if name != "conversation":
            language_facts(caps, name)
    return caps


def stage_available(caps, name):
    return caps.get("stages", {}).get(name, {}).get("available") is not False


def language_facts(caps, name):
    stage = caps.get("stages", {}).get(name, caps.get(name, {}))
    values = stage.get("languages", caps.get("languages"))
    default = stage.get("default_language", caps.get("default_language"))
    if (not isinstance(values, list) or not values or len(values) > 100
            or any(not isinstance(v, str) or not v or len(v) > 32 for v in values) or default not in values):
        raise VoiceTransportError("invalid_capabilities")
    return values, default


def check_identity(caps, data):
    """A new credential/URL cannot silently select a different backend or agent."""
    actual = service_identity(caps)
    if caps.get("agent", {}).get("id") != data.get("agent_id"):
        raise VoiceTransportError("wrong_backend")
    if data.get("service_identity") and data["service_identity"] != actual:
        raise VoiceTransportError("wrong_backend")
    return actual
