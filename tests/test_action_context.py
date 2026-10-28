"""Concurrency contract tests, not a replacement for native HA proof."""
import asyncio
import unittest

from custom_components.openclaw_voice.action_context import (
    ActionConversationBindings, InteractionBindingError,
)


def created(bindings, conversation_id):
    bindings.on_chat_log_event(conversation_id, "created", {"chat_log": {}})


class ActionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_expiry_and_followup_refresh_are_bounded(self):
        now = [0.0]
        bindings = ActionConversationBindings(max_bindings=1, idle_ttl=10, clock=lambda: now[0])

        async def native():
            created(bindings, "a")

        await bindings.start("assist_satellite.a", "per_conversation", native)
        now[0] = 9
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "per_conversation")
        now[0] = 18
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "per_conversation")
        now[0] = 29
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "persistent")

    async def test_capacity_and_same_satellite_busy_fail_before_native_start(self):
        bindings = ActionConversationBindings(max_bindings=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def native():
            calls.append("a")
            entered.set()
            await release.wait()
            created(bindings, "a")

        task = asyncio.create_task(bindings.start("assist_satellite.a", "persistent", native))
        await entered.wait()
        with self.assertRaisesRegex(InteractionBindingError, "pending start"):
            await bindings.start("assist_satellite.a", "persistent", native)
        with self.assertRaisesRegex(InteractionBindingError, "capacity"):
            await bindings.start("assist_satellite.b", "persistent", native)
        release.set()
        await task
        self.assertEqual(calls, ["a"])

    async def test_shutdown_fences_pending_and_late_creation(self):
        bindings = ActionConversationBindings()

        async def native():
            bindings.shutdown()
            created(bindings, "a")

        with self.assertRaises(InteractionBindingError):
            await bindings.start("assist_satellite.a", "per_conversation", native)
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "persistent")
        with self.assertRaisesRegex(InteractionBindingError, "unloaded"):
            await bindings.start("assist_satellite.b", "per_conversation", native)

    async def test_parallel_actions_bind_their_own_mode_and_keep_defaults(self):
        bindings = ActionConversationBindings()
        ready = asyncio.Event()

        async def native_a():
            await ready.wait()
            created(bindings, "conversation-a")

        async def native_b():
            created(bindings, "conversation-b")
            ready.set()

        a, b = await asyncio.gather(
            bindings.start("assist_satellite.a", "per_conversation", native_a),
            bindings.start("assist_satellite.b", "persistent", native_b),
        )
        self.assertEqual(a.conversation_id, "conversation-a")
        self.assertEqual(b.conversation_id, "conversation-b")
        self.assertEqual(bindings.resolve_mode("conversation-a", "persistent"), "per_conversation")
        self.assertEqual(bindings.resolve_mode("conversation-b", "per_conversation"), "persistent")
        self.assertEqual(bindings.resolve_mode("unrelated", "persistent"), "persistent")

    async def test_unrelated_wake_cannot_consume_an_action_override(self):
        bindings = ActionConversationBindings()
        active = asyncio.Event()
        continue_action = asyncio.Event()

        async def native():
            active.set()
            await continue_action.wait()
            created(bindings, "intended")

        task = asyncio.create_task(bindings.start("assist_satellite.a", "per_conversation", native))
        await active.wait()
        created(bindings, "unrelated")
        continue_action.set()
        result = await task
        self.assertEqual(result.conversation_id, "intended")
        self.assertEqual(bindings.resolve_mode("unrelated", "persistent"), "persistent")

    async def test_followups_keep_mode_and_close_restores_default(self):
        bindings = ActionConversationBindings()

        async def native():
            created(bindings, "a")

        await bindings.start("assist_satellite.a", "per_conversation", native)
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "per_conversation")
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "per_conversation")
        bindings.close("a")
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "persistent")

    async def test_failed_start_rolls_back_only_its_owned_binding(self):
        bindings = ActionConversationBindings()

        async def native():
            created(bindings, "a")
            raise RuntimeError("start failed")

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            await bindings.start("assist_satellite.a", "per_conversation", native)
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "persistent")

    async def test_missing_correlation_fails_instead_of_using_next_wake(self):
        bindings = ActionConversationBindings()

        async def native():
            pass

        with self.assertRaises(InteractionBindingError):
            await bindings.start("assist_satellite.a", "per_conversation", native)
        created(bindings, "later")
        self.assertEqual(bindings.resolve_mode("later", "persistent"), "persistent")

    async def test_ambiguous_creation_is_rejected_and_binding_removed(self):
        bindings = ActionConversationBindings()

        async def native():
            created(bindings, "a")
            created(bindings, "b")

        with self.assertRaises(InteractionBindingError):
            await bindings.start("assist_satellite.a", "per_conversation", native)
        self.assertEqual(bindings.resolve_mode("a", "persistent"), "persistent")

    async def test_context_copied_to_late_child_is_fenced_after_start_returns(self):
        bindings = ActionConversationBindings()
        release = asyncio.Event()
        child = None

        async def native():
            nonlocal child
            created(bindings, "a")

            async def late():
                await release.wait()
                created(bindings, "late")

            child = asyncio.create_task(late())

        await bindings.start("assist_satellite.a", "per_conversation", native)
        release.set()
        await child
        self.assertEqual(bindings.resolve_mode("late", "persistent"), "persistent")

    async def test_native_service_task_inherits_action_context(self):
        bindings = ActionConversationBindings()

        async def native_service():
            created(bindings, "a")

        async def native():
            await asyncio.create_task(native_service())

        result = await bindings.start("assist_satellite.a", "per_conversation", native)
        self.assertEqual(result.conversation_id, "a")
