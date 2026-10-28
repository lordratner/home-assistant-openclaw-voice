"""Task-local binding of an owned action to a native HA conversation.

No global 'next wake' slot and no mutation of another entity's private fields.
The framework adapter subscribes to the public chat-log creation callback and
runs the native start service within start(). Native HA runtime validation is
required in addition to this independent concurrency contract.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
from uuid import uuid4
from time import monotonic

SessionMode = Literal["persistent", "per_conversation"]


class InteractionBindingError(RuntimeError):
    """The native start did not create the expected owned conversation."""


@dataclass(frozen=True)
class Binding:
    interaction_id: str
    conversation_id: str
    satellite_id: str
    mode: SessionMode


@dataclass
class _Pending:
    owner: object
    interaction_id: str
    satellite_id: str
    mode: SessionMode
    active: bool = True
    conversation_id: str | None = None


class ActionConversationBindings:
    """Bind only events emitted in the current admitted action task context."""

    def __init__(self, *, max_bindings: int = 1024, idle_ttl: float = 900,
                 clock: Callable[[], float] = monotonic) -> None:
        if max_bindings < 1 or idle_ttl <= 0:
            raise ValueError("binding limits must be positive")
        self._owner = object()
        self._pending: ContextVar[_Pending | None] = ContextVar(
            "openclaw_voice_action", default=None
        )
        self._bindings: dict[str, Binding] = {}
        self._expires: dict[str, float] = {}
        self._starts: dict[str, _Pending] = {}
        self._limit = max_bindings
        self._ttl = idle_ttl
        self._clock = clock
        self._closed = False

    def _purge(self) -> None:
        now = self._clock()
        pending_ids = {value.conversation_id for value in self._starts.values() if value.active}
        for conversation_id, expires in tuple(self._expires.items()):
            if expires <= now and conversation_id not in pending_ids:
                self.close(conversation_id)

    async def start(
        self,
        satellite_id: str,
        mode: SessionMode,
        native_start: Callable[[], Awaitable[None]],
    ) -> Binding:
        if self._closed:
            raise InteractionBindingError("integration is unloaded")
        self._purge()
        if mode not in ("persistent", "per_conversation"):
            raise ValueError("invalid session mode")
        if not satellite_id.startswith("assist_satellite."):
            raise ValueError("a satellite target is required")
        if satellite_id in self._starts:
            raise InteractionBindingError("satellite already has a pending start")
        if len(self._bindings) + len(self._starts) >= self._limit:
            raise InteractionBindingError("conversation binding capacity exceeded")
        pending = _Pending(self._owner, uuid4().hex, satellite_id, mode)
        self._starts[satellite_id] = pending
        token = self._pending.set(pending)
        try:
            await native_start()
            if self._closed or pending.conversation_id is None:
                raise InteractionBindingError("native start created no correlated chat log")
            self._expires[pending.conversation_id] = self._clock() + self._ttl
            return self._bindings[pending.conversation_id]
        except BaseException:
            if pending.conversation_id is not None:
                binding = self._bindings.get(pending.conversation_id)
                if binding and binding.interaction_id == pending.interaction_id:
                    self.close(pending.conversation_id)
            raise
        finally:
            pending.active = False
            self._starts.pop(satellite_id, None)
            self._pending.reset(token)

    def on_chat_log_event(self, conversation_id: str, event_type: str, data: dict) -> None:
        pending = self._pending.get()
        if not pending or not pending.active or pending.owner is not self._owner:
            return
        if event_type != "created":
            return
        if not conversation_id or not isinstance(data.get("chat_log"), dict):
            raise InteractionBindingError("invalid chat-log creation event")
        if pending.conversation_id is not None:
            if pending.conversation_id != conversation_id:
                raise InteractionBindingError("ambiguous native conversation creation")
            return
        if conversation_id in self._bindings:
            raise InteractionBindingError("conversation already bound")
        self._bindings[conversation_id] = Binding(
            pending.interaction_id, conversation_id, pending.satellite_id, pending.mode
        )
        pending.conversation_id = conversation_id
        self._expires[conversation_id] = self._clock() + self._ttl

    def resolve_mode(self, conversation_id: str, default: SessionMode) -> SessionMode:
        self._purge()
        binding = self._bindings.get(conversation_id)
        if binding:
            self._expires[conversation_id] = self._clock() + self._ttl
        return binding.mode if binding else default

    def close(self, conversation_id: str) -> None:
        self._bindings.pop(conversation_id, None)
        self._expires.pop(conversation_id, None)

    def shutdown(self) -> None:
        """Fence copied contexts and clear mappings when the config entry unloads."""
        self._closed = True
        for pending in self._starts.values():
            pending.active = False
        self._bindings.clear()
        self._expires.clear()
