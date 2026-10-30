"""Native HA actions with per-conversation, not global, mode overrides."""
import asyncio
from uuid import uuid7
import voluptuous as vol

from homeassistant.components import conversation, tts
from homeassistant.components.assist_pipeline import async_get_pipeline, async_get_pipelines, OPTION_PREFERRED
from homeassistant.components.assist_satellite import DOMAIN as SATELLITE_DOMAIN
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, chat_session

from .const import DOMAIN, SESSION_MODES
from .client import VoiceTransportError
from .runtime import SpeechFeed, ACTION_SPEECH_FEED
from .observability import observe, GLOBAL, failure_summary
from .capabilities import language_facts


def validation_error(reason, message):
    error = ServiceValidationError(message, translation_domain=DOMAIN, translation_key=reason)
    error.diagnostic = {"reason": reason, "category": "validation", "stage": "validation"}
    return error


def _runtime(hass, call):
    runtime = hass.data[DOMAIN]["runtimes"].get(call.data["config_entry_id"])
    if not runtime or not runtime.coordinator.last_update_success:
        raise validation_error("connection_unavailable", "This OpenClaw connection is not available")
    if not runtime.stage_available("conversation"):
        raise validation_error("native_conversation_unavailable", "OpenClaw conversation is unavailable")
    return runtime


def _satellite(hass, entity_id):
    component = hass.data.get(SATELLITE_DOMAIN)
    entity = component.get_entity(entity_id) if component else None
    if entity is None:
        raise validation_error("satellite_unavailable", "The Assist satellite is not available")
    return entity


def _check_interactive_pipeline(hass, satellite, engine):
    selected = None
    if satellite.pipeline_entity_id:
        state = hass.states.get(satellite.pipeline_entity_id)
        if state is None:
            raise validation_error("pipeline_unavailable", "The satellite's pipeline selection is unavailable")
        if state.state != OPTION_PREFERRED:
            selected = next((pipeline.id for pipeline in async_get_pipelines(hass) if pipeline.name == state.state), None)
            if selected is None:
                raise validation_error("pipeline_unavailable", "The satellite's selected pipeline was not found")
    if async_get_pipeline(hass, selected).conversation_engine != engine:
        raise validation_error("pipeline_mismatch", "Select this OpenClaw conversation entity in the satellite's Assist pipeline first")


def register(hass):
    if hass.services.has_service(DOMAIN, "query"):
        return

    @observe("action", "query")
    async def query(call):
        runtime = _runtime(hass, call)
        entity = runtime.entities["conversation"]
        language = call.data.get("language", runtime.capabilities["default_language"])
        speaker = call.data.get("speaker")
        tts_entity = call.data.get("tts_entity", runtime.entities["tts"].entity_id)
        own_tts = tts_entity == runtime.entities["tts"].entity_id
        if speaker and own_tts:
            if not runtime.stage_available("tts"):
                raise validation_error("speech_provider_not_configured", "OpenClaw speech is unavailable; select another TTS entity")
            language = call.data.get("language", language_facts(runtime.capabilities, "tts")[1])
        if speaker and not speaker.startswith(("assist_satellite.", "media_player.")):
            raise validation_error("speaker_invalid", "Choose an Assist satellite or media player")
        if speaker and speaker.startswith("assist_satellite."):
            _satellite(hass, speaker)
        if speaker and len(runtime.speech_jobs) >= 8:
            raise validation_error("speech_capacity", "Too many spoken OpenClaw requests")
        with chat_session.async_get_chat_session(hass, call.data.get("conversation_id")) as session:
            conversation_id = session.conversation_id
        if conversation_id in runtime.action_conversations:
            raise validation_error("query_overlap", "This OpenClaw conversation already has an active query")
        state = runtime.modes.select(conversation_id, runtime.default_mode, runtime.epoch, call.data.get("session_mode"))
        prior_request_id = state.request_id
        runtime.action_conversations.add(conversation_id)
        stream = action_id = feed = None
        feed_token = None
        owned = []
        failure = None
        try:
            if speaker:
                feed = SpeechFeed(runtime.diagnostics)
                runtime.speech_feeds[conversation_id] = feed
                action_id = str(uuid7())
                runtime.speech_jobs[action_id] = None
                stream = tts.async_create_stream(hass, tts_entity, language=language,
                    options={"profile_revision": runtime.client.profile_revision, "action_id": action_id} if own_tts else {})
                runtime.reserve_speech(action_id, stream, feed)
                feed_token = ACTION_SPEECH_FEED.set((runtime, conversation_id, feed))
            async with asyncio.TaskGroup() as group:
                async def converse():
                    result = await conversation.async_converse(hass, call.data["prompt"], conversation_id,
                        call.context, language=language, agent_id=entity.entity_id)
                    # Snapshot in the same task before playback/follow-up tasks
                    # can replace state. No stale handle or previous action IDs.
                    receipt = runtime.modes.receipt(conversation_id)
                    if receipt and receipt["request_id"] == prior_request_id:
                        receipt = None
                    return result, receipt
                task = group.create_task(converse())
                owned.append(task); runtime.tasks.add(task)
                if speaker:
                    async def playback():
                        runtime.diagnostics.event("playback", "request", "waiting", {}, "info")
                        if not await feed.ready():
                            runtime.diagnostics.event("playback", "request", "skipped", {"reason": "empty_speech"}, "info")
                            runtime.release_pending_speech(action_id)
                            stream.delete()
                            runtime.speech_jobs.pop(action_id, None)
                            return
                        async def external_text():
                            runtime.release_pending_speech(action_id)
                            try:
                                async for text in feed.stream():
                                    yield text
                            finally:
                                runtime.speech_jobs.pop(action_id, None)
                        stream.async_set_message_stream(feed.stream() if own_tts else external_text())
                        runtime.diagnostics.event("playback", "request", "submitted", {"confirmed": False}, "info")
                        if speaker.startswith("assist_satellite."):
                            await hass.services.async_call(SATELLITE_DOMAIN, "announce",
                                {"media_id": stream.media_source_id, "preannounce": False},
                                target={"entity_id": speaker}, blocking=True, context=call.context)
                        else:
                            await hass.services.async_call("media_player", "play_media",
                                {"media_content_id": stream.media_source_id, "media_content_type": "music", "announce": True},
                                target={"entity_id": speaker}, blocking=True, context=call.context)
                    output = group.create_task(playback())
                    owned.append(output); runtime.tasks.add(output)
            result, receipt = task.result()
            return {"conversation_id": result.conversation_id, "session_mode": state.mode,
                    "job_id": receipt["job_id"] if receipt else None,
                    "request_id": receipt["request_id"] if receipt else None,
                    "speech_requested": bool(speaker), "response": result.as_dict()["response"]}
        except BaseException as error:
            failure = error
            raise
        finally:
            if feed_token is not None:
                ACTION_SPEECH_FEED.reset(feed_token)
            runtime.action_conversations.discard(conversation_id)
            for task in owned:
                runtime.tasks.discard(task)
            runtime.speech_feeds.pop(conversation_id, None)
            if feed and failure:
                feed.finish(VoiceTransportError("speech_cancelled"))
            if failure and stream:
                stream.delete()
            if failure and action_id:
                runtime.release_pending_speech(action_id)
                job = runtime.speech_jobs.pop(action_id, None)
                if job:
                    try:
                        await job[1].cancel_job(job[0])
                    except VoiceTransportError as error:
                        runtime.diagnostics.event("action", "cancel", "cleanup_failed",
                            failure_summary(error, stage="cleanup", job_id=job[0], confirmed=False), "warning")

    @observe("action", "start_conversation")
    async def start_conversation(call):
        runtime = _runtime(hass, call)
        entity_id = call.data["satellite_id"]
        satellite = _satellite(hass, entity_id)
        _check_interactive_pipeline(hass, satellite, runtime.entities["conversation"].entity_id)
        task = asyncio.create_task(runtime.actions.start(entity_id, call.data.get("session_mode", runtime.default_mode),
            lambda: hass.services.async_call(SATELLITE_DOMAIN, "start_conversation",
                {"start_message": call.data.get("start_message", ""), "preannounce": False},
                target={"entity_id": entity_id}, blocking=True, context=call.context)))
        runtime.tasks.add(task)
        try:
            binding = await task
        finally:
            runtime.tasks.discard(task)
        return {"conversation_id": binding.conversation_id, "session_mode": binding.mode, "interaction_id": binding.interaction_id}

    common = {vol.Required("config_entry_id"): str, vol.Optional("session_mode"): vol.In(SESSION_MODES)}
    hass.services.async_register(DOMAIN, "query", query, schema=vol.Schema({**common,
        vol.Required("prompt"): cv.string, vol.Optional("language"): cv.string,
        vol.Optional("tts_entity"): cv.entity_domain("tts"),
        vol.Optional("conversation_id"): cv.string, vol.Optional("speaker"): cv.entity_id,
    }), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "start_conversation", start_conversation, schema=vol.Schema({**common,
        vol.Required("satellite_id"): cv.entity_id, vol.Optional("start_message", default=""): cv.string,
    }), supports_response=SupportsResponse.OPTIONAL)
