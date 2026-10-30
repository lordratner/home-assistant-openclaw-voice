"""Pair a scoped OC connection; agent/model/provider choices stay in OC."""
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector
import voluptuous as vol

from .client import VoiceClient, VoiceTransportError
from .const import DOMAIN, CONF_URL, CONF_CLIENT_ID, CONF_CLIENT_TOKEN, CONF_SESSION_MODE, DEFAULT_SESSION_MODE, SESSION_MODES
from .runtime import api_url
from .observability import observe, GLOBAL, failure_summary
from .capabilities import validate_capabilities, service_identity, check_identity


def api_selector(hass):
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[selector.SelectOptionDict(label=api.name, value=api.id)
                 for api in llm.async_get_apis(hass) if api.id == "assist"], multiple=True))


def schema(defaults, hass=None):
    fields = {
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): selector.TextSelector({"type": selector.TextSelectorType.URL}),
        vol.Required("pairing_code"): selector.TextSelector({"type": selector.TextSelectorType.PASSWORD}),
        vol.Required(CONF_SESSION_MODE, default=defaults.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE)): selector.SelectSelector({
            "options": ["persistent", "per_conversation"], "translation_key": "session_mode", "mode": selector.SelectSelectorMode.DROPDOWN}),
    }
    if hass is not None:
        fields[vol.Optional(CONF_LLM_HASS_API, default=defaults.get(CONF_LLM_HASS_API, []))] = api_selector(hass)
    return vol.Schema(fields)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    @observe("configuration", "pair")
    async def _pair(self, user_input):
        if user_input[CONF_SESSION_MODE] not in SESSION_MODES:
            raise ValueError("invalid session mode")
        result = await VoiceClient.pair(async_get_clientsession(self.hass), api_url(user_input[CONF_URL]), user_input["pairing_code"])
        try:
            caps = validate_capabilities(result["capabilities"])
            VoiceClient(async_get_clientsession(self.hass), api_url(user_input[CONF_URL]), result["client_token"], caps["profile_revision"])
            if not isinstance(caps.get("agent"), dict) or not isinstance(caps["agent"].get("id"), str):
                raise VoiceTransportError("invalid_capabilities")
            return {CONF_URL: user_input[CONF_URL].rstrip("/"), CONF_CLIENT_ID: result["client_id"],
                    CONF_CLIENT_TOKEN: result["client_token"], "agent_id": caps["agent"]["id"],
                    CONF_SESSION_MODE: user_input[CONF_SESSION_MODE],
                    **({"service_identity": service_identity(caps)} if service_identity(caps) else {})}
        except (VoiceTransportError, ValueError, KeyError):
            if result.get("client_token"):
                await self._discard({CONF_URL: user_input[CONF_URL], CONF_CLIENT_TOKEN: result["client_token"]})
            raise

    async def _discard(self, data):
        """Revoke a newly issued credential if the flow cannot adopt it."""
        try:
            client = VoiceClient(async_get_clientsession(self.hass), api_url(data[CONF_URL]),
                                 data[CONF_CLIENT_TOKEN], "flow_cleanup")
            await client.control("DELETE", "/pair")
        except (VoiceTransportError, ValueError) as error:
            GLOBAL.event("configuration", "pair_cleanup", "cleanup_failed",
                         failure_summary(error, stage="cleanup"), "warning")

    def _check_duplicate(self, data, exclude=None):
        stable = data.get("service_identity")
        for entry in self._async_current_entries():
            if entry.entry_id == exclude:
                continue
            same_service = stable and entry.data.get("service_identity") == stable
            legacy_match = (not stable or not entry.data.get("service_identity")) and (
                api_url(entry.data[CONF_URL]) == api_url(data[CONF_URL])
                and entry.data.get("agent_id") == data["agent_id"])
            if same_service or legacy_match:
                raise AbortFlow("already_configured")

    @observe("configuration", "user_flow")
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            data = None
            try:
                data = await self._pair(user_input)
                self._check_duplicate(data)
                await self.async_set_unique_id(data.get("service_identity", data[CONF_CLIENT_ID]))
                self._abort_if_unique_id_configured()
                options = {CONF_SESSION_MODE: data.pop(CONF_SESSION_MODE),
                           CONF_LLM_HASS_API: user_input.get(CONF_LLM_HASS_API, [])}
                if options[CONF_LLM_HASS_API] not in ([], ["assist"]):
                    raise ValueError("invalid API")
                return self.async_create_entry(title="OpenClaw (" + data["agent_id"] + ")", data=data, options=options)
            except AbortFlow:
                if data:
                    await self._discard(data)
                raise
            except VoiceTransportError as error:
                if data:
                    await self._discard(data)
                GLOBAL.event("configuration", "pair", "failed", failure_summary(error, stage="authorization"), "warning")
                errors["base"] = "pairing_failed" if error.code == "pairing_failed" else "cannot_connect"
            except (ValueError, KeyError) as error:
                if data:
                    await self._discard(data)
                GLOBAL.event("configuration", "input", "failed", failure_summary(error, stage="validation"), "warning")
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="user", data_schema=schema(user_input or {}, self.hass), errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    @observe("configuration", "reauth")
    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input:
            data = None
            try:
                data = await self._pair(user_input)
                if data["agent_id"] != entry.data.get("agent_id") or (
                    entry.data.get("service_identity") and data.get("service_identity") != entry.data["service_identity"]):
                    raise VoiceTransportError("wrong_backend")
                if not entry.data.get("service_identity") and api_url(data[CONF_URL]) != api_url(entry.data[CONF_URL]):
                    raise VoiceTransportError("legacy_identity_required")
                self._check_duplicate(data, entry.entry_id)
                mode = data.pop(CONF_SESSION_MODE)
                old = dict(entry.data)
                result = self.async_update_reload_and_abort(entry, data=data,
                    unique_id=data.get("service_identity", data[CONF_CLIENT_ID]),
                    title="OpenClaw (" + data["agent_id"] + ")",
                    options={**entry.options, CONF_SESSION_MODE: mode}, reason="reauth_successful")
                if old[CONF_CLIENT_TOKEN] != data[CONF_CLIENT_TOKEN]:
                    await self._discard(old)
                return result
            except AbortFlow:
                if data:
                    await self._discard(data)
                raise
            except VoiceTransportError as error:
                if data:
                    await self._discard(data)
                GLOBAL.event("configuration", "reauth", "failed", failure_summary(error, stage="authorization"), "warning")
                errors["base"] = error.code if error.code in {"wrong_backend", "legacy_identity_required"} else "pairing_failed"
            except (ValueError, KeyError) as error:
                if data:
                    await self._discard(data)
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema({**entry.data, **entry.options}), errors=errors)

    @observe("configuration", "reconfigure")
    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input:
            try:
                client = VoiceClient(async_get_clientsession(self.hass), api_url(user_input[CONF_URL]),
                                     entry.data[CONF_CLIENT_TOKEN], "reconfigure")
                caps = validate_capabilities(await client.capabilities())
                stable = check_identity(caps, entry.data)
                data = {**entry.data, CONF_URL: user_input[CONF_URL].rstrip("/"),
                        **({"service_identity": stable} if stable else {})}
                self._check_duplicate(data, entry.entry_id)
                return self.async_update_reload_and_abort(entry, data=data,
                    unique_id=stable or entry.unique_id, reason="reconfigure_successful")
            except VoiceTransportError as error:
                GLOBAL.event("configuration", "reconfigure", "failed", failure_summary(error, stage="validation"), "warning")
                errors["base"] = "wrong_backend" if error.code == "wrong_backend" else "cannot_connect"
            except (ValueError, KeyError):
                errors["base"] = "invalid_input"
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({
            vol.Required(CONF_URL, default=entry.data[CONF_URL]): selector.TextSelector({"type": selector.TextSelectorType.URL})}), errors=errors)


class OptionsFlow(config_entries.OptionsFlow):
    @observe("configuration", "options")
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            if user_input.get(CONF_LLM_HASS_API, []) not in ([], ["assist"]):
                GLOBAL.event("configuration", "options", "rejected", {"reason": "unsupported_assist_api"}, "warning")
                return self.async_abort(reason="invalid_api")
            return self.async_create_entry(title="", data={**user_input, CONF_LLM_HASS_API: user_input.get(CONF_LLM_HASS_API, [])})
        default = self.config_entry.options.get(CONF_SESSION_MODE, self.config_entry.data.get(CONF_SESSION_MODE, DEFAULT_SESSION_MODE))
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Optional(CONF_LLM_HASS_API, description={"suggested_value": self.config_entry.options.get(
                CONF_LLM_HASS_API, self.config_entry.data.get(CONF_LLM_HASS_API, []))}): api_selector(self.hass),
            vol.Required(CONF_SESSION_MODE, default=default): selector.SelectSelector({
                "options": ["persistent", "per_conversation"], "translation_key": "session_mode",
                "mode": selector.SelectSelectorMode.DROPDOWN}),
        }))
