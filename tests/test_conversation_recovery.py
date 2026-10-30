"""Sticky native IDs recover once; admitted or uncertain requests never repeat."""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.openclaw_voice.conversation_recovery import ConversationRequest
from custom_components.openclaw_voice.conversation_state import Conversations
from custom_components.openclaw_voice.timing import LocalTimings
from custom_components.openclaw_voice.transport import VoiceTransportError

JOB = "00000000-0000-4000-8000-000000000001"


class Client:
    def __init__(self, runtime, revision):
        self.runtime, self.revision = runtime, revision

    async def open_conversation(self, identity, mode):
        self.runtime.opens.append((identity, mode, self.revision))
        return {"conversation_handle": "handle-" + self.revision}

    async def turn(self, handle, text, request_id, **options):
        self.runtime.posts.append((handle, request_id))
        if self.runtime.failures:
            action = self.runtime.failures.pop(0)
            if action == "output_then_error":
                yield {"type": "text_delta", "text": "already acted", "job_id": JOB}
                raise VoiceTransportError("profile_changed")
            if action:
                raise action
        yield {"type": "done", "job_id": JOB}


class Runtime:
    def __init__(self):
        self.now = 0
        self.modes = Conversations(clock=lambda: self.now, ttl=10)
        self.default_mode = "persistent"
        self.actions = SimpleNamespace(get_mode=lambda _: None)
        self.client = Client(self, "r1")
        self.refreshes = 0
        self.posts, self.opens, self.failures = [], [], []
        self.caller = "paired-caller"

    @property
    def epoch(self):
        return self.caller + ":" + self.client.revision

    async def refresh(self):
        self.refreshes += 1
        self.client = Client(self, "r2")


async def prepare(client):
    return {}


async def run(runtime, text="safe fixture"):
    sample = LocalTimings().start("conversation")
    result = [event async for event in ConversationRequest(runtime, "sticky-id", sample).events(text, prepare)]
    return result, sample


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["epoch", "expired", "profile_changed", "conversation_not_found"])
async def test_sticky_id_recovers_once_and_preserves_mode(reason):
    runtime = Runtime()
    prior = runtime.modes.select("sticky-id", "per_conversation", runtime.epoch)
    prior.handle = "old-handle"
    if reason == "epoch":
        runtime.client = Client(runtime, "r2")
    elif reason == "expired":
        runtime.now = 11
    else:
        runtime.failures = [VoiceTransportError(reason)]
    result, sample = await run(runtime)
    assert result[-1]["type"] == "done"
    assert runtime.refreshes == 1
    assert all(identity == "sticky-id" and mode == "per_conversation" for identity, mode, _ in runtime.opens)
    assert len({request_id for _, request_id in runtime.posts}) == 1
    assert runtime.modes.select("sticky-id", "persistent", runtime.epoch).mode == "per_conversation"
    assert sample.details["recovery_reason"]
    await run(runtime, "follow-up")
    assert runtime.refreshes == 1
    assert len(runtime.opens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code,job", [
    ("profile_changed", JOB), ("conversation_expired", JOB),
    ("voice_transport_failed", None), ("execution_outcome_unknown", None),
    ("session_missing_or_replaced", None), ("persistent_binding_changed", None),
    ("backend_auth_failed", None), ("request_conflict", None),
])
async def test_admission_uncertainty_and_authority_failures_never_redispatch(code, job):
    runtime = Runtime()
    runtime.failures = [VoiceTransportError(code, job)]
    with pytest.raises(VoiceTransportError, match=code):
        await run(runtime)
    assert len(runtime.posts) == 1
    assert runtime.refreshes == 0


@pytest.mark.asyncio
async def test_output_even_without_error_job_id_fences_retry_and_budget_is_one():
    runtime = Runtime()
    runtime.failures = ["output_then_error"]
    with pytest.raises(VoiceTransportError):
        await run(runtime)
    assert len(runtime.posts) == 1 and runtime.refreshes == 0
    runtime = Runtime()
    runtime.failures = [VoiceTransportError("profile_changed"), VoiceTransportError("profile_changed")]
    with pytest.raises(VoiceTransportError):
        await run(runtime)
    assert len(runtime.posts) == 2 and runtime.refreshes == 1
    assert len({identity for _, identity in runtime.posts}) == 1


@pytest.mark.asyncio
async def test_repaired_pairing_does_not_adopt_old_conversation_or_change_action_mode():
    runtime = Runtime()
    runtime.modes.select("sticky-id", "per_conversation", runtime.epoch)
    runtime.caller = "different-paired-caller"
    with pytest.raises(VoiceTransportError, match="profile_changed"):
        await run(runtime)
    with pytest.raises(VoiceTransportError, match="profile_changed"):
        await run(runtime)
    assert not runtime.posts and not runtime.opens
    runtime = Runtime()
    runtime.modes.select("sticky-id", "per_conversation", runtime.epoch)
    runtime.client = Client(runtime, "r2")
    runtime.actions = SimpleNamespace(get_mode=lambda _: "persistent")
    with pytest.raises(VoiceTransportError, match="conversation_mode_conflict"):
        await run(runtime)
    assert not runtime.posts


@pytest.mark.asyncio
async def test_overlap_rejects_second_turn_and_cancellation_releases_claim():
    runtime = Runtime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(client):
        entered.set()
        await release.wait()
        return {}

    async def first():
        return [event async for event in ConversationRequest(runtime, "sticky-id", LocalTimings().start("conversation")).events("first", blocked)]

    task = asyncio.create_task(first())
    await entered.wait()
    with pytest.raises(VoiceTransportError, match="conversation_busy"):
        await run(runtime)
    assert not runtime.posts
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await run(runtime)
    assert len(runtime.posts) == 1
