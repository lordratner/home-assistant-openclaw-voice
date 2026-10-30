"""Native HA Assist prompt/schema/intent execution, not a copied policy stub."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity

from custom_components.openclaw_voice.client import VoiceClient, VoiceTransportError
from custom_components.openclaw_voice.const import DOMAIN
from test_ha_setup import hass, configure, CAPS, PAIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("exposed,revoked", [(True, False), (False, False), (True, True)])
async def test_native_assist_round_trip_and_exposure_enforcement(hass, exposed, revoked):
    caps = {**CAPS, "assist_tools": {"version": 1}}
    effects, results = [], []
    ready = asyncio.Event()
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "intent", {})
    hass.states.async_set("light.fixture", "off", {"friendly_name": "Fixture Light"})
    # A second exposed light keeps the turn-on intent available in the negative case.
    hass.states.async_set("light.other", "off", {"friendly_name": "Other Light"})

    async def turn_on(call):
        effects.append(call)
        hass.states.async_set("light.fixture", "on")

    hass.services.async_register("light", "turn_on", turn_on)

    async def turn(self, handle, text, request_id=None, *, context=None, assist=None):
        assert assist is not None
        assert "intent__HassTurnOn" in {tool["name"] for tool in assist["tools"]}
        assert "When controlling Home Assistant" in assist["prompt"]
        assert ("Fixture Light" in assist["prompt"]) is exposed
        if revoked:
            async_expose_entity(hass, "conversation", "light.fixture", False)
        event = {"type": "tool_call", "job_id": PAIRED["client_id"],
                 "call_id": "00000000-0000-4000-8000-000000000003",
                 "tool_name": "intent__HassTurnOn", "tool_args": {"name": "Fixture Light", "domain": ["light"]}}
        yield event
        await asyncio.wait_for(ready.wait(), 3)
        # Replay must only resend the cached result, not run the native intent again.
        yield event
        yield {"type": "text_delta", "job_id": PAIRED["client_id"], "message_id": 0, "text": "Native result received."}
        yield {"type": "done", "job_id": PAIRED["client_id"]}

    async def result(self, job_id, value):
        results.append(value)
        ready.set()

    with patch.object(VoiceClient, "pair", AsyncMock(return_value={**PAIRED, "capabilities": caps})), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=caps)), \
            patch.object(VoiceClient, "open_conversation", AsyncMock(return_value={"conversation_handle": PAIRED["client_id"]})), \
            patch.object(VoiceClient, "turn", turn), patch.object(VoiceClient, "tool_result", result), \
            patch.object(VoiceClient, "tool_claim", AsyncMock()):
        entry = await configure(hass)
        async_expose_entity(hass, "conversation", "light.fixture", exposed)
        async_expose_entity(hass, "conversation", "light.other", True)
        # Explicit native API selection, as in the HA options flow.
        options = await hass.config_entries.options.async_init(entry.entry_id)
        updated = await hass.config_entries.options.async_configure(options["flow_id"],
            {"session_mode": "persistent", CONF_LLM_HASS_API: ["assist"]})
        assert updated["type"] == "create_entry"
        await hass.async_block_till_done()
        ctx = Context()
        response = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
            "prompt": "Turn on Fixture Light"}, blocking=True, return_response=True, context=ctx)
        assert response["response"]["speech"]["plain"]["speech"] == "Native result received."
        assert len(results) == 2 and results[0] == results[1]
        assert len(effects) == (1 if exposed and not revoked else 0)
        if exposed and not revoked:
            assert effects[0].context is ctx
        else:
            assert results[0]["error"] or results[0]["data"].get("error")


@pytest.mark.asyncio
async def test_control_is_not_silently_granted_and_missing_bridge_fails_before_dispatch(hass):
    with patch.object(VoiceClient, "pair", AsyncMock(return_value=PAIRED)), \
            patch.object(VoiceClient, "capabilities", AsyncMock(return_value=CAPS)), \
            patch.object(VoiceClient, "open_conversation", AsyncMock(return_value={"conversation_handle": PAIRED["client_id"]})), \
            patch.object(VoiceClient, "turn") as turn:
        entry = await configure(hass)
        entity = entry.runtime_data.entities["conversation"]
        assert not entity.supported_features
        hass.config_entries.async_update_entry(entry, options={CONF_LLM_HASS_API: ["assist"]})
        assert entity.supported_features
        response = await hass.services.async_call(DOMAIN, "query", {"config_entry_id": entry.entry_id,
            "prompt": "Must not fall back"}, blocking=True, return_response=True)
        assert response["response"]["response_type"] == "error"
        turn.assert_not_called()
