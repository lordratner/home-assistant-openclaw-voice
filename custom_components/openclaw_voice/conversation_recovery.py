"""One bounded transport recovery, only before a turn is known to be admitted."""
from contextlib import aclosing
from uuid import uuid7

from .transport import VoiceTransportError, canonical_id

REOPEN_ERRORS = frozenset({"profile_changed", "conversation_expired", "conversation_not_found"})


class ConversationRequest:
    def __init__(self, runtime, conversation_id, timing):
        self.runtime, self.conversation_id, self.timing = runtime, conversation_id, timing
        # Keep one idempotency key even across the safe pre-admission retry.
        self.request_id = str(uuid7())

    async def events(self, text, prepare):
        runtime, identity = self.runtime, self.conversation_id
        claimed = False
        observed = False
        stage = "select"
        state = None
        try:
            runtime.modes.begin(identity)
            claimed = True
            for attempt in range(2):
                try:
                    stage = "select"
                    state = runtime.modes.select(identity, runtime.default_mode, runtime.epoch,
                                                 runtime.actions.get_mode(identity))
                    client = runtime.client
                    state.request_id, state.job_id = self.request_id, None
                    stage = "open"
                    if state.handle is None:
                        binding = await client.open_conversation(identity, state.mode)
                        state.handle = binding["conversation_handle"]
                    stage = "prepare"
                    options = await prepare(client)
                    self.timing.mark("conversation_opened")
                    stage = "turn"
                    async with aclosing(client.turn(state.handle, text, self.request_id, **options)) as source:
                        async for event in source:
                            observed = True
                            state.job_id = event["job_id"]
                            yield event
                    return
                except VoiceTransportError as error:
                    if state is not None and canonical_id(error.job_id):
                        state.job_id = error.job_id
                    if (attempt or observed or error.job_id is not None or
                            error.code not in REOPEN_ERRORS or stage == "prepare"):
                        raise
                    self.timing.recovery(error.code)
                    if hasattr(runtime, "diagnostics"):
                        runtime.diagnostics.event("conversation", "recovery", "started",
                            {"request_id": self.request_id, "attempt": 1, "admitted": False, "reason": error.code}, "info")
                    # Refresh only after a definite rejection. Network/authority,
                    # native-target and uncertain-execution failures never retry.
                    await runtime.refresh()
                    runtime.modes.recover(identity, runtime.default_mode, runtime.epoch,
                                          runtime.actions.get_mode(identity))
        except VoiceTransportError as error:
            self.timing.failure(error.code, stage)
            raise
        finally:
            if claimed:
                runtime.modes.end(identity)
