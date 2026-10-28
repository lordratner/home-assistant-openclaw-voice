"""Pair a scoped OC connection; agent/model/provider choices stay in OC."""
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector
import voluptuous as vol

from .client import VoiceClient, VoiceTransportError
from .const import DOMAIN, CONF_URL, CONF_CLIENT_ID, CONF_CLIENT_TOKEN, CONF_SESSION_MODE, DEFAULT_SESSION_MODE, SESSION_MODES
from .runtime import api_url


def schema(defaults):
    return vol.Schema({
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): selector.TextSelector({"type": selector.TextSelectorType.URL}),
        vol.Required("pairing_code"): selector.TextSelector({"type": selector.TextSelectorType.PASSWORD}),
        vol.Required(CONF_SESSION_MODE, default=defaults.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE)): selector.SelectSelector({
            "options": [{"value": "persistent", "label": "Persistent session"},
                        {"value": "per_conversation", "label": "Fresh session per conversation"}], "mode": selector.SelectSelectorMode.DROPDOWN}),
    })


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def _pair(self, user_input):
        if user_input[CONF_SESSION_MODE] not in SESSION_MODES:
            raise ValueError("invalid session mode")
        result = await VoiceClient.pair(async_get_clientsession(self.hass), api_url(user_input[CONF_URL]), user_input["pairing_code"])
        caps = result["capabilities"]
        VoiceClient(async_get_clientsession(self.hass), api_url(user_input[CONF_URL]), result["client_token"], caps["profile_revision"])
        if not isinstance(caps.get("agent"), dict) or not isinstance(caps["agent"].get("id"), str):
            raise VoiceTransportError("invalid_capabilities")
        return {CONF_URL: user_input[CONF_URL].rstrip("/"), CONF_CLIENT_ID: result["client_id"],
                CONF_CLIENT_TOKEN: result["client_token"], "agent_id": caps["agent"]["id"],
                CONF_SESSION_MODE: user_input[CONF_SESSION_MODE]}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            try:
                data = await self._pair(user_input)
                await self.async_set_unique_id(data[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="OpenClaw (" + data["agent_id"] + ")", data=data)
            except VoiceTransportError as error:
                errors["base"] = "pairing_failed" if error.code == "pairing_failed" else "cannot_connect"
            except (ValueError, KeyError):
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="user", data_schema=schema(user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input:
            try:
                data = await self._pair(user_input)
                return self.async_update_reload_and_abort(entry, data=data, unique_id=data[CONF_CLIENT_ID],
                    title="OpenClaw (" + data["agent_id"] + ")",
                    options={**entry.options, CONF_SESSION_MODE: data[CONF_SESSION_MODE]}, reason="reauth_successful")
            except VoiceTransportError:
                errors["base"] = "pairing_failed"
            except (ValueError, KeyError):
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema({**entry.data, **entry.options}), errors=errors)


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input:
            return self.async_create_entry(title="", data=user_input)
        default = self.config_entry.options.get(CONF_SESSION_MODE, self.config_entry.data.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE))
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_SESSION_MODE, default=default): selector.SelectSelector({
                "options": [{"value": "persistent", "label": "Persistent session"},
                            {"value": "per_conversation", "label": "Fresh session per conversation"}],
                "mode": selector.SelectSelectorMode.DROPDOWN}),
        }))
