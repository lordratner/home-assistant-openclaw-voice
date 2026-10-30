"""Native streaming ChatLog projection, with OC as the only history owner."""
import asyncio
from contextlib import aclosing

from homeassistant.components import conversation
from homeassistant.helpers import intent
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.translation import async_get_translations

from .client import VoiceTransportError
from .assist import prepare_assist, selected_apis, AssistExchange
from .runtime import ACTION_SPEECH_FEED
from .conversation_recovery import ConversationRequest
from .observability import observe, failure_summary


@observe("platform", "conversation_setup")
async def async_setup_entry(hass, entry, async_add_entities):
    entity = OpenClawConversation(entry.runtime_data)
    entry.runtime_data.entities["conversation"] = entity
    async_add_entities([entity])


class OpenClawConversation(CoordinatorEntity, conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "conversation"
    _attr_supports_streaming = True

    def __init__(self, runtime):
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_unique_id = runtime.entry.entry_id + "_conversation"
        self._attr_device_info = runtime.device_info()

    @property
    def supported_languages(self):
        return "*"

    @property
    def available(self):
        return self.runtime.stage_available("conversation")

    @property
    def supported_features(self):
        return conversation.ConversationEntityFeature.CONTROL if selected_apis(self.runtime.entry) else 0

    async def _async_handle_message(self, user_input, chat_log):
        task = asyncio.create_task(self._handle(user_input, chat_log), name="OpenClaw conversation")
        self.runtime.tasks.add(task)
        try:
            return await task
        finally:
            self.runtime.tasks.discard(task)

    @observe("conversation", "pipeline")
    async def _handle(self, user_input, chat_log):
        conversation_id = chat_log.conversation_id
        action = ACTION_SPEECH_FEED.get()
        feed = action[2] if action and action[0] is self.runtime and action[1] == conversation_id else None
        response = intent.IntentResponse(language=user_input.language)
        result_id = conversation_id
        failure = None
        timing = self.runtime.timings.start("conversation")
        try:
            request = ConversationRequest(self.runtime, conversation_id, timing)
            self.runtime.diagnostics.current().annotate(request_id=request.request_id)
            exchange = None

            async def prepare(client):
                nonlocal exchange
                bundle = await prepare_assist(self.runtime.entry, self.runtime.capabilities, user_input, chat_log)
                exchange = AssistExchange(bundle, client) if bundle else None
                return {"context": None if bundle else user_input.extra_system_prompt,
                        **({"assist": bundle} if bundle else {})}

            pieces = []
            continuing = False

            async def deltas():
                nonlocal continuing
                message_id = None
                async with aclosing(request.events(user_input.text, prepare)) as events:
                    async for event in events:
                        if event["type"] == "text_delta":
                            timing.mark("first_text_received")
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
                        elif event["type"] == "tool_call":
                            async for delta in exchange.deltas(event):
                                yield delta
                            message_id = None
                        elif event["type"] == "done":
                            continuing = event.get("continue_conversation", False)
            async with aclosing(deltas()) as source:
                async with aclosing(chat_log.async_add_delta_content_stream(self.entity_id, source)) as contents:
                    async for content in contents:
                        if exchange:
                            await exchange.accept(content)
            if pieces:
                response.async_set_speech("".join(pieces))
            return conversation.ConversationResult(response=response, conversation_id=conversation_id,
                                                    continue_conversation=continuing)
        except conversation.ConverseError as error:
            failure = error
            self.runtime.diagnostics.current().fail(error, stage="dispatch")
            return error.as_conversation_result()
        except VoiceTransportError as error:
            failure = error
            self.runtime.diagnostics.current().fail(error, stage="dispatch")
            if error.code in {"profile_changed", "conversation_expired", "persistent_binding_changed",
                    "session_missing_or_replaced", "conversation_not_found"}:
                # A native satellite may keep this HA ID. Retain the caller and
                # mode snapshot; only bounded pre-admission recovery may rotate
                # transport state. Dropping it could adopt another paired caller.
                result_id = None
                message = ("I could not refresh the OpenClaw conversation. Please try again."
                           if error.code in {"profile_changed", "conversation_expired", "conversation_not_found"}
                           else "OpenClaw's session changed. Its connection needs attention before I can continue.")
            elif error.code == "execution_outcome_unknown":
                message = "The connection was interrupted. I could not confirm the result, so I did not repeat the request."
            else:
                message = "I could not finish the OpenClaw request."
            key = ("conversation_refresh_failed" if error.code in {"profile_changed", "conversation_expired", "conversation_not_found"}
                   else "conversation_session_changed" if error.code in {"persistent_binding_changed", "session_missing_or_replaced"}
                   else "conversation_outcome_unknown" if error.code == "execution_outcome_unknown" else "conversation_failed")
            translations = await async_get_translations(self.hass, user_input.language, "exceptions", {"openclaw_voice"})
            message = translations.get("component.openclaw_voice.exceptions." + key + ".message", message)
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
            return conversation.ConversationResult(response=response, conversation_id=result_id)
        except BaseException as error:
            failure = error
            raise
        finally:
            timing.finish("failed" if failure else "completed")
            if feed:
                feed.finish(failure)
