"""Export real HA schemas using synthetic entities; no server or hardware."""
import asyncio
import json
import sys
import tempfile

import probatio
from homeassistant import bootstrap, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component


async def main():
    with tempfile.TemporaryDirectory(prefix="ha-assist-schema-") as directory:
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        assert await bootstrap.async_load_base_functionality(hass)
        hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
        try:
            for component in ("homeassistant", "intent", "llm"):
                assert await async_setup_component(hass, component, {})
            hass.states.async_set("light.fixture", "off", {"friendly_name": "Fixture Light"})
            api = await llm.async_get_api(hass, "assist", llm.LLMContext(
                platform="openclaw_voice", context=Context(), language="en",
                assistant="conversation", device_id=None,
            ))
            bundle = {"prompt": api.api_prompt, "tools": [
                {"name": tool.name, "description": tool.description or "",
                 "parameters": probatio.to_openapi(tool.parameters,
                    custom_serializer=api.custom_serializer, openapi_version="3.1.0")}
                for tool in api.tools
            ]}
            print(json.dumps(bundle, indent=2))
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
