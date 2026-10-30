import pytest
from custom_components.openclaw_voice.timing import LocalTimings


def test_local_timings_are_bounded_content_free_copied_and_first_occurrence():
    ring = LocalTimings()
    for _ in range(25):
        sample = ring.start("conversation")
        sample.mark("first_text_received")
        initial = sample.marks["first_text_received"]
        sample.mark("first_text_received")
        assert sample.marks["first_text_received"] == initial
        sample.finish("completed")
        sample.finish("failed")
    values = ring.snapshot()
    assert len(values) == 20
    assert all(set(v) == {"kind", "status", "started_at_ms", "stages_ms"} for v in values)
    assert all(v["stages_ms"]["finished"] >= v["stages_ms"]["first_text_received"] for v in values)
    values[0]["stages_ms"]["first_text_received"] = -1
    assert ring.snapshot()[0]["stages_ms"]["first_text_received"] >= 0
    with pytest.raises(ValueError):
        ring.start("private prompt")
    with pytest.raises(ValueError):
        sample.mark("private audio")


def test_recovery_and_failure_diagnostics_only_retain_allowlisted_reason_and_stage():
    ring = LocalTimings()
    sample = ring.start("conversation")
    sample.recovery("profile_changed")
    sample.failure("PRIVATE_BACKEND_ERROR_AND_PROMPT", "PRIVATE_STAGE")
    sample.finish("failed")
    value = ring.snapshot()[0]
    assert value["recovery_reason"] == "profile_changed"
    assert value["failure_reason"] == "request_failed"
    assert value["failure_stage"] == "unknown"
    assert "PRIVATE" not in str(value)
    with pytest.raises(ValueError):
        sample.recovery("PRIVATE_REASON")
