"""Native streaming ChatLog projection, with OC as the only history owner."""
import asyncio
from uuid import uuid7

from homeassistant.components import conversation
from homeassistant.helpers import intent
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import VoiceTransportError
from .runtime import ACTION_SPEECH_FEED


async def async_setup_entry(hass, entry, async_add_entities):
    entity = OpenClawConversation(entry.runtime_data)
    entry.runtime_data.entities["conversation"] = entity
    async_add_entities([entity])


class OpenClawConversation(CoordinatorEntity, conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = "Conversation"
    _attr_supports_streaming = True

    def __init__(self, runtime):
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_unique_id = runtime.entry.entry_id + "_conversation"
        self._attr_device_info = runtime.device_info()

    @property
    def supported_languages(self):
        return "*"

    async def _async_handle_message(self, user_input, chat_log):
        task = asyncio.create_task(self._handle(user_input, chat_log), name="OpenClaw conversation")
        self.runtime.tasks.add(task)
        try:
            return await task
        finally:
            self.runtime.tasks.discard(task)

    async def _handle(self, user_input, chat_log):
        conversation_id = chat_log.conversation_id
        action = ACTION_SPEECH_FEED.get()
        feed = action[2] if action and action[0] is self.runtime and action[1] == conversation_id else None
        response = intent.IntentResponse(language=user_input.language)
        result_id = conversation_id
        failure = None
        try:
            state = self.runtime.modes.select(conversation_id, self.runtime.default_mode, self.runtime.epoch,
                                              self.runtime.actions.get_mode(conversation_id))
            client = self.runtime.client  # Keep one profile revision for this turn.
            if state.handle is None:
                binding = await client.open_conversation(conversation_id, state.mode)
                state.handle = binding["conversation_handle"]
            state.request_id = str(uuid7())
            pieces = []
            continuing = False

            async def deltas():
                nonlocal continuing
                message_id = None
                async for event in client.turn(state.handle, user_input.text, state.request_id,
                        context=user_input.extra_system_prompt):
                    state.job_id = event["job_id"]
                    if event["type"] == "text_delta":
                        if message_id != event["message_id"]:
                            if pieces:
                                pieces.append("\n\n")
                                if feed:
                                    await feed.put("\n\n")
                            message_id = event["message_id"]
                            yield {"role": "assistant"}
                        pieces.append(event["text"])
                        if feed:
                            await feed.put(event["text"])
                        yield {"content": event["text"]}
                    elif event["type"] == "done":
                        continuing = event.get("continue_conversation", False)

            async for _ in chat_log.async_add_delta_content_stream(self.entity_id, deltas()):
                pass
            if pieces:
                response.async_set_speech("".join(pieces))
            return conversation.ConversationResult(response=response, conversation_id=conversation_id,
                                                    continue_conversation=continuing)
        except VoiceTransportError as error:
            failure = error
            if error.code in {"profile_changed", "conversation_expired", "persistent_binding_changed",
                    "session_missing_or_replaced", "conversation_not_found"}:
                self.runtime.modes.close(conversation_id)
                result_id = None
                message = "OpenClaw's conversation changed or expired. Please start a new conversation."
            elif error.code == "execution_outcome_unknown":
                message = "The connection was interrupted. I could not confirm the result, so I did not repeat the request."
            else:
                message = "I could not finish the OpenClaw request."
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
            return conversation.ConversationResult(response=response, conversation_id=result_id)
        except BaseException as error:
            failure = error
            raise
        finally:
            if feed:
                feed.finish(failure)
