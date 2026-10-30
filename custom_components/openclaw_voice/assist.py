"""HA owns context, tool schemas, intent execution and exposure authorization."""
import json
import probatio

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.helpers import llm

from .const import DOMAIN
from .client import VoiceTransportError
from .observability import GLOBAL, observe


def selected_apis(entry):
    """No silent control grant to existing installations."""
    return entry.options.get(CONF_LLM_HASS_API, entry.data.get(CONF_LLM_HASS_API, []))


@observe("assist", "prepare")
async def prepare_assist(entry, capabilities, user_input, chat_log):
    apis = selected_apis(entry)
    if not apis:
        return None
    if apis != ["assist"]:
        raise VoiceTransportError("unsupported_assist_api")
    if capabilities.get("assist_tools", {}).get("version") != 1:
        raise VoiceTransportError("assist_bridge_unavailable")
    await chat_log.async_provide_llm_data(
        user_input.as_llm_context(DOMAIN), apis, None, user_input.extra_system_prompt,
    )
    api = chat_log.llm_api
    if api is None:
        raise VoiceTransportError("assist_bridge_unavailable")
    bundle = {"prompt": chat_log.content[0].content, "tools": [
        {"name": tool.name, "description": tool.description or "",
         "parameters": probatio.to_openapi(tool.parameters, custom_serializer=api.custom_serializer,
                                            openapi_version="3.1.0")}
        for tool in api.tools
    ]}
    if len(bundle["tools"]) > 256 or len(bundle["prompt"].encode()) > 131072 or len(json.dumps(bundle).encode()) > 1048576:
        raise VoiceTransportError("assist_bundle_limit")
    return bundle


class AssistExchange:
    """One turn's native ChatLog exchange; duplicate stream events never reexecute."""
    def __init__(self, bundle, client):
        self.diagnostics = getattr(client, "diagnostics", GLOBAL)
        self.names = {tool["name"] for tool in bundle["tools"]}
        self.client = client
        self.calls = {}

    @observe("assist", "execute")
    async def deltas(self, event):
        self.diagnostics.current().annotate(job_id=event.get("job_id"), call_id=event.get("call_id"))
        call_id = event["call_id"]
        identity = (event["job_id"], event["tool_name"], json.dumps(event["tool_args"], sort_keys=True))
        if previous := self.calls.get(call_id):
            if previous["identity"] != identity or "result" not in previous:
                raise VoiceTransportError("assist_call_conflict")
            await self.client.tool_result(event["job_id"], previous["result"])
            return
        if len(self.calls) >= 20 or event["tool_name"] not in self.names:
            raise VoiceTransportError("invalid_assist_call")
        await self.client.tool_claim(event["job_id"], call_id)
        self.diagnostics.current().phase("native_tool_claimed", admitted=True)
        self.calls[call_id] = {"identity": identity, "job_id": event["job_id"]}
        # The delta stream starts the HA task and flushes it at the role boundary.
        # Its native ToolResultContent is returned to OpenClaw by accept().
        yield {"tool_calls": [llm.ToolInput(id=call_id, tool_name=event["tool_name"], tool_args=event["tool_args"])]}
        yield {"role": "assistant"}

    @observe("assist", "accept_result")
    async def accept(self, content):
        if not isinstance(content, conversation.ToolResultContent):
            return
        call = self.calls.get(content.tool_call_id)
        if call is None:
            raise VoiceTransportError("unexpected_assist_result")
        result = {"call_id": content.tool_call_id, "data": content.result.data, "error": content.result.error}
        self.diagnostics.event("assist", "native_result", "failed" if content.result.error else "completed",
            {"job_id": call["job_id"], "call_id": content.tool_call_id, "tool_result_error": content.result.error,
             **({"reason": "native_tool_failed"} if content.result.error else {})}, "warning" if content.result.error else "debug")
        if len(json.dumps(result).encode()) > 60000:
            result = {"call_id": content.tool_call_id, "data": {"error": "Native HA result exceeds bridge limit; do not repeat the action."}, "error": True}
        call["result"] = result
        await self.client.tool_result(call["job_id"], result)
