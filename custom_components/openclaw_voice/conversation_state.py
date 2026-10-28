"""Bound mode/epoch per HA conversation; options never retarget a follow-up."""
from dataclasses import dataclass
from time import monotonic

from .transport import VoiceTransportError


@dataclass
class ConversationState:
    mode: str
    epoch: str
    expires: float
    handle: str | None = None
    job_id: str | None = None
    request_id: str | None = None


class Conversations:
    def __init__(self, *, limit=1024, ttl=900, clock=monotonic):
        self._states: dict[str, ConversationState] = {}
        self._limit, self._ttl, self._clock = limit, ttl, clock

    def select(self, conversation_id: str, default: str, epoch: str, override: str | None = None):
        now = self._clock()
        state = self._states.get(conversation_id)
        if state:
            if state.expires <= now:
                self._states.pop(conversation_id)
                raise VoiceTransportError("conversation_expired")
            if state.epoch != epoch:
                raise VoiceTransportError("profile_changed")
            if override is not None and override != state.mode:
                raise VoiceTransportError("conversation_mode_conflict")
            state.expires = now + self._ttl
            return state
        for key, value in tuple(self._states.items()):
            if value.expires <= now:
                self._states.pop(key)
        if len(self._states) >= self._limit:
            raise VoiceTransportError("conversation_capacity")
        mode = override or default
        if mode not in ("persistent", "per_conversation"):
            raise VoiceTransportError("invalid_session_mode")
        state = ConversationState(mode, epoch, now + self._ttl)
        self._states[conversation_id] = state
        return state

    def close(self, conversation_id: str):
        self._states.pop(conversation_id, None)

    def seed(self, conversation_id: str, mode: str, epoch: str):
        """Remember an action's epoch before the native device starts listening."""
        if conversation_id not in self._states:
            self.select(conversation_id, mode, epoch, mode)
