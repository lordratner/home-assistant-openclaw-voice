"""Unmodified HA satellite/session retention plus native conversation processing."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.assist_satellite.entity import AssistSatelliteEntity
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.conversation import ConversationInput
from homeassistant.core import Context
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.openclaw_voice.client import VoiceClient
from test_ha_setup import hass, configure, CAPS, PAIRED


class NativeSatellite(AssistSatelliteEntity):
    def async_get_configuration(self):
        raise NotImplementedError

    async def async_set_configuration(self, config):
        raise NotImplementedError

    def on_pipeline_event(self, event):
        pass

    def _set_state(self, state):
        pass


@pytest.mark.asyncio
async def test_native_sticky_satellite_id_recovers_profile_and_continues_without_new_history(hass):
    caps = dict(CAPS)
    opens, turns = [], []

    async def capabilities(self):
        return dict(caps)

    async def open_conversation(self, identity, mode):
        opens.append((identity, mode, self.profile_revision))
        return {"conversation_handle": "00000000-0000-4000-8000-00000000000" + self.profile_revision[-1],
                "mode": mode, "profile_revision": self.profile_revision}

    async def turn(self, handle, text, request_id=None, *, context=None):
        turns.append((handle, text, request_id))
        yield {"type": "text_delta", "message_id": 0, "text": "Reply", "job_id": PAIRED["client_id"]}
        yield {"type": "done", "job_id": PAIRED["client_id"], "continue_conversation": text == "clarify"}

    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", capabilities), \
            patch.object(VoiceClient, "open_conversation", open_conversation), \
            patch.object(VoiceClient, "turn", turn):
        entry = await configure(hass)
        runtime = entry.runtime_data
        satellite = NativeSatellite()
        satellite.hass = hass
        with async_get_chat_session(hass) as session:
            satellite._conversation_id = session.conversation_id
        sticky_id = satellite._conversation_id
        runtime.modes.select(sticky_id, "per_conversation", runtime.epoch)

        async def request(text):
            with async_get_chat_session(hass, satellite._conversation_id) as session:
                assert session.conversation_id == sticky_id
            return await runtime.entities["conversation"].async_process(
                ConversationInput(text=text, context=Context(), conversation_id=sticky_id, language="en",
                                  device_id=None, satellite_id=None, agent_id=None))

        await request("before")
        caps["profile_revision"] = "r2"
        await runtime.refresh()
        # This is the old handler's actual error result. Native HA does not reset
        # the satellite ID. Recovery must therefore work without a new native ID.
        satellite._internal_on_pipeline_event(PipelineEvent(PipelineEventType.INTENT_END,
            {"intent_output": {"conversation_id": None, "continue_conversation": False}}))
        satellite._internal_on_pipeline_event(PipelineEvent(PipelineEventType.RUN_END))
        assert satellite._conversation_id == sticky_id
        result = await request("clarify")
        assert result.conversation_id == sticky_id and result.continue_conversation
        followup = await request("answer")
        assert followup.conversation_id == sticky_id and not followup.continue_conversation
        assert opens == [(sticky_id, "per_conversation", "r1"), (sticky_id, "per_conversation", "r2")]
        assert [text for _, text, _ in turns] == ["before", "clarify", "answer"]
        assert turns[1][0] == turns[2][0] != turns[0][0]
        assert all(v["status"] == "completed" for v in runtime.timings.snapshot())
        assert sum("recovery_reason" in v for v in runtime.timings.snapshot()) == 1
