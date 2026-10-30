"""Small content-free local timing ring. These times do not prove audibility."""
from collections import deque
from time import monotonic, time

STAGES = frozenset({"conversation_opened", "first_text_received", "first_audio_received", "finished"})
REASONS = frozenset({"profile_changed", "conversation_expired", "conversation_not_found",
                    "persistent_binding_changed", "session_missing_or_replaced", "backend_auth_failed",
                    "conversation_mode_conflict", "conversation_busy", "conversation_capacity",
                    "execution_outcome_unknown", "request_conflict"})


class LocalTimings:
    def __init__(self, diagnostics=None):
        self._recent = deque(maxlen=20)
        self.diagnostics = diagnostics

    def start(self, kind):
        if kind not in {"conversation", "tts", "stt"}:
            raise ValueError("invalid timing kind")
        return _Sample(self, kind)

    def snapshot(self):
        return [{**value, "stages_ms": dict(value["stages_ms"])} for value in self._recent]


class _Sample:
    def __init__(self, owner, kind):
        self.owner, self.kind = owner, kind
        self.started = monotonic()
        self.started_at = round(time() * 1000)
        self.marks = {}
        self.done = False
        self.details = {}
        self.span = owner.diagnostics.current() if owner.diagnostics else None

    def recovery(self, reason):
        if reason not in {"profile_changed", "conversation_expired", "conversation_not_found"}:
            raise ValueError("invalid recovery reason")
        self.details["recovery_reason"] = reason
        if self.span:
            self.span.phase("recovery", reason=reason)

    def failure(self, reason, stage):
        self.details["failure_reason"] = reason if reason in REASONS else "request_failed"
        self.details["failure_stage"] = stage if stage in {"select", "open", "prepare", "turn"} else "unknown"

    def mark(self, stage):
        if stage not in STAGES:
            raise ValueError("invalid timing stage")
        first = stage not in self.marks
        self.marks.setdefault(stage, round((monotonic() - self.started) * 1000, 3))
        if first and self.span:
            self.span.phase(stage)

    def finish(self, status):
        if self.done:
            return
        if status not in {"completed", "failed", "closed"}:
            raise ValueError("invalid timing status")
        self.mark("finished")
        self.done = True
        self.owner._recent.append({"kind": self.kind, "status": status, "started_at_ms": self.started_at,
                                   "stages_ms": dict(self.marks), **self.details})
