"""Execute native HA service/chat-log/satellite code, without a live HA server.

Only hardware, pipeline selection, state publishing and announcement synthesis
are mocked. The service registry, log subscription and native conversation
creation run unmodified from homeassistant==2026.10.4.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.components.assist_satellite.entity import AssistSatelliteEntity
from homeassistant.components.conversation.chat_log import (
    async_subscribe_chat_logs, async_get_chat_log,
)
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.openclaw_voice.action_context import ActionConversationBindings


class FixtureSatellite(AssistSatelliteEntity):
    def __init__(self, hass, entity_id):
        self.hass = hass
        self.entity_id = entity_id

    def async_get_configuration(self):
        raise NotImplementedError

    async def async_set_configuration(self, config):
        raise NotImplementedError

    def on_pipeline_event(self, event):
        pass

    def _resolve_pipeline(self):
        return "fixture"

    async def _resolve_announcement_media_id(self, *args, **kwargs):
        return SimpleNamespace(message="", media_id="fixture")

    def _set_state(self, state):
        pass

    async def async_start_conversation(self, announcement):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "Please tell me your answer"])
async def test_native_satellite_start_binds_only_its_service_context(tmp_path, message):
    hass = HomeAssistant(str(tmp_path))
    bindings = ActionConversationBindings()
    unsubscribe = async_subscribe_chat_logs(hass, bindings.on_chat_log_event)
    satellites = {
        key: FixtureSatellite(hass, "assist_satellite." + key) for key in ("a", "b")
    }
    entered = asyncio.Event()
    release = asyncio.Event()

    async def service(call):
        if call.data["target"] == "a":
            entered.set()
            await release.wait()
        await satellites[call.data["target"]].async_internal_start_conversation(
            start_message=message, preannounce=False
        )

    hass.services.async_register("assist_satellite", "start_conversation", service)

    async def start(key, mode):
        return await bindings.start(
            "assist_satellite." + key, mode,
            lambda: hass.services.async_call(
                "assist_satellite", "start_conversation", {"target": key}, blocking=True
            ),
        )

    try:
        with patch(
            "homeassistant.components.assist_satellite.entity.async_get_pipeline",
            return_value=SimpleNamespace(conversation_engine="conversation.fixture"),
        ):
            first = asyncio.create_task(start("a", "per_conversation"))
            await entered.wait()
            # This is a separate wake-like task, not inside either action.
            with async_get_chat_session(hass) as session:
                unrelated_id = session.conversation_id
                with async_get_chat_log(hass, session):
                    pass
            second = await start("b", "persistent")
            release.set()
            first = await first
        assert first.conversation_id != second.conversation_id
        assert first.satellite_id == "assist_satellite.a"
        assert bindings.resolve_mode(first.conversation_id, "persistent") == "per_conversation"
        assert bindings.resolve_mode(second.conversation_id, "per_conversation") == "persistent"
        assert bindings.resolve_mode(unrelated_id, "persistent") == "persistent"
        # Empty native start emits DELETED; binding must survive for user reply.
        await asyncio.sleep(0)
        assert bindings.resolve_mode(first.conversation_id, "persistent") == "per_conversation"
    finally:
        unsubscribe()
        await hass.async_stop(force=True)
