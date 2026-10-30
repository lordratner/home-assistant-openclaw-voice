"""Native Home Assistant pipelines backed by OpenClaw Home Assistant."""
from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS, CONF_CLIENT_TOKEN, CONF_URL
from .conversation_state import Conversations
from .runtime import Runtime, api_url
from .client import VoiceClient, VoiceTransportError
from . import services
from .action_context import ActionConversationBindings
import hashlib
from .const import CONF_CLIENT_ID
from .observability import observe, GLOBAL, failure_summary
from .const import CONF_SESSION_MODE

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_migrate_entry(hass, entry):
    """Move nonconnection defaults without replacing entry/entities or credentials."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        data = dict(entry.data)
        options = dict(entry.options)
        if CONF_SESSION_MODE in data:
            options.setdefault(CONF_SESSION_MODE, data.pop(CONF_SESSION_MODE))
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)
    return True


@observe("integration", "setup")
async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {"runtimes": {}, "modes": {}, "actions": {}})
    services.register(hass)
    return True


@observe("integration", "setup_entry")
async def async_setup_entry(hass, entry):
    data = hass.data.setdefault(DOMAIN, {"runtimes": {}, "modes": {}, "actions": {}})
    actions = data.setdefault("actions", {}).setdefault(entry.entry_id, ActionConversationBindings())
    runtime = Runtime(hass, entry, data["modes"].setdefault(entry.entry_id, Conversations()), actions)
    await runtime.coordinator.async_config_entry_first_refresh()
    entry.runtime_data = runtime
    data["runtimes"][entry.entry_id] = runtime
    def on_chat_log(conversation_id, kind, payload):
        runtime.actions.on_chat_log_event(conversation_id, kind, payload)
        if kind == "created" and (mode := runtime.actions.get_mode(conversation_id)) is not None:
            runtime.modes.seed(conversation_id, mode, runtime.epoch)
    entry.async_on_unload(async_subscribe_chat_logs(hass, on_chat_log))
    entry.async_on_unload(entry.add_update_listener(async_reload))
    services.register(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.diagnostics.event("integration", "setup_entry", "ready", {"count": len(PLATFORMS)}, "info")
    return True


@observe("integration", "reload")
async def async_reload(hass, entry):
    runtime = hass.data[DOMAIN]["runtimes"].get(entry.entry_id)
    identity = (entry.data[CONF_URL], entry.data[CONF_CLIENT_ID], hashlib.sha256(entry.data[CONF_CLIENT_TOKEN].encode()).digest())
    if runtime and runtime.connection_identity == identity:
        # Mode-only options are read live; existing conversation snapshots stay
        # unchanged, and no active native pipeline needs to be interrupted.
        return
    await hass.config_entries.async_reload(entry.entry_id)


@observe("integration", "unload")
async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.close()
        hass.data[DOMAIN]["runtimes"].pop(entry.entry_id, None)
    return unloaded


@observe("integration", "remove")
async def async_remove_entry(hass, entry):
    client = VoiceClient(async_get_clientsession(hass), api_url(entry.data[CONF_URL]), entry.data[CONF_CLIENT_TOKEN], "removal")
    try:
        await client.control("DELETE", "/pair")
    except VoiceTransportError as error:
        GLOBAL.event("authority", "remove_revoke", "cleanup_failed", failure_summary(error, stage="cleanup"), "warning")
    hass.data.get(DOMAIN, {}).get("modes", {}).pop(entry.entry_id, None)
    actions = hass.data.get(DOMAIN, {}).get("actions", {}).pop(entry.entry_id, None)
    if actions:
        actions.shutdown()
