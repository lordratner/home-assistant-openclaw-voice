# OpenClaw Home Assistant integration

## Current product candidate

Bridge **dev.10** + HA **dev.6**, additive protocol **1.2**. New source candidates,
not a public compatibility certification or a claim they are installed. Start
with [the product setup guide](docs/PRODUCT.md), [compatibility](docs/COMPATIBILITY.md)
and [HA quality checklist](docs/QUALITY.md). Older versioned notes below are
historical feature evidence, not the current release status.

Use OpenClaw conversation with existing HA speech providers, all three OC
entities, or a mix. Missing OC speech dependencies do not disable text chat.
Streaming is preferred automatically and unsupported capabilities use native
batch/buffered paths; interrupted admitted work is never blindly repeated.


Native Home Assistant STT, streaming conversation and streaming-text TTS entities,
backed by the companion [OpenClaw plugin](https://github.com/lordratner/openclaw-home-assistant).
OC owns the selected agent, normal tools/memory/policy, actual persistent session,
model/thinking/fast and provider credentials. HA chooses mode and playback target.

**Integration 0.0.1-dev.6: development candidate, not a stable release.**
Config/options/reauth, all three entities and both actions are implemented and
exercised with real HA APIs in disposable runtimes. Real-provider and physical
listening behavior remain deployment-specific acceptance gates; HACS
discovery/publication is not claimed.

## Setup and use

After a separately reviewed installation, add **OpenClaw Home Assistant** in
Settings → Devices & services. Enter the Gateway base URL and a one-use pairing
code issued by its administrator. Choose persistent history or a fresh session
per conversation. Only the scoped voice token is stored in HA's config entry;
protect HA backups. Provider keys and Gateway owner credentials stay out of HA.

Select the three entities in an Assist pipeline yourself. Installation does not
change preferred/default pipelines. Existing conversations keep their selected
mode across options edits and integration reload; a fresh session retains normal
agent knowledge, memory, skills and tools.

### Ask and optionally speak

```yaml
action: openclaw_voice.query
data:
  config_entry_id: YOUR_ENTRY_ID
  prompt: "Give me a short update."
  session_mode: per_conversation
  speaker: media_player.YOUR_SPEAKER
response_variable: answer
```

Omit `speaker` for text only. `assist_satellite` targets are also supported.
The structured result contains conversation/mode/job/request IDs, response and
`speech_requested`. It is not an acknowledgment of audible playback. Pass the
returned `conversation_id` to continue that conversation without changing mode.
An overlapping query on the same HA conversation is rejected before another turn.

### Start an interactive satellite conversation

```yaml
action: openclaw_voice.start_conversation
data:
  config_entry_id: YOUR_ENTRY_ID
  satellite_id: assist_satellite.YOUR_SATELLITE
  start_message: "What would you like to do?"
  session_mode: per_conversation
response_variable: interaction
```

The satellite must support HA's native start-conversation service and already
select a pipeline using this connection's conversation entity. Otherwise the
action fails before starting playback/listening. Follow-ups keep the override;
no global settings or private satellite fields are changed.

## Documentation

- [Install/upgrade/rollback](docs/INSTALL.md)
- [Protocol, limits, retention and recovery](docs/PROTOCOL.md)
- [Evidence and remaining gates](docs/ACCEPTANCE.md)
- [Native streaming-transcription capability and fallback](docs/SDK-GAPS.md)
- [Detailed planning baseline](planning/00-README.md)
- [Current development checkpoint](implementation/STATUS.md)

TTS uses a single canonical WAV stream. HA device-specific format preferences
still apply and can add conversion latency. HA streams bounded PCM microphone
chunks to OC. The companion plugin can use provider-dependent realtime
STT when the host supplies the reviewed optional finite-transcription runtime
(including the separately prepared core backport); only a completed transcript
is returned for conversation processing. Older hosts or unsupported selections
retain native batch STT. A failed admitted stream is never automatically replayed
through batch or another provider. A bridge update alone does not install the
core capability, and not every provider/model supports it.

Existing HA integration dev.3 needs no code update or restart solely for this STT
capability or the bridge's clarification repair. Backend capabilities/profile
refresh every 30 seconds. Explicit per-turn continuation intent is forwarded to
HA's native conversation result; actual wake-word-free listening after playback
still needs a satellite observation. Unconsumed action playback expires after
60 seconds; normal HA cache/URL retention remains HA-owned.

Diagnostics are allowlisted and exclude connection/agent/client/session identities,
credentials, conversation text and audio. Reauthentication creates a new scoped
identity; old conversations must not silently adopt a new agent or profile.

Local diagnostics retain at most 20 conversation/TTS timing samples in memory,
including first text/audio received and completion/close/failure offsets. These
do not measure when the speaker becomes audible. Model/thinking/fast preferences
can be changed through the companion plugin's owner-only `inference.get` and
`inference.update` Gateway methods without reloading HA or losing its current
conversation. The HA client token does not receive model-administration rights.

## Development

Python >=3.14.2; the lock file pins the tested HA 2026.10.4 environment.

```sh
uv venv --python 3.14 .venv
uv pip sync --python .venv/bin/python requirements-test.lock
.venv/bin/python -m pytest -q
```

Run `npm run test:native-pair` in the sibling OC checkout for the full isolated
production-plugin/native-HA proof. Synthetic providers and satellite fixtures
are not real-provider latency or audible-playback evidence.

For a review archive, include only `custom_components/openclaw_voice/` source
and metadata, excluding `__pycache__`/`.pyc`. The repository also has HACS metadata;
public source availability does not imply a stable release, an official HACS listing,
or a change to satellite firmware.

Original source is MIT; see [LICENSE](LICENSE) and [provenance](THIRD_PARTY_NOTICES.md).

## Native HA Assist tools (candidate dev.3)

The [native Assist bridge](docs/NATIVE-ASSIST.md) forwards HA’s own prompt/tool API and executes callbacks through HA’s ChatLog. Enable Device control in HA options; allow the `home_assistant` tool in the selected agent’s policy. No MCP or SSH dependency. Existing entries keep control disabled until explicitly selected. Local-first pipeline choices remain untouched.

### Stale conversation recovery

Bridge dev.7 and HA dev.4 recover a retained native satellite chat ID after
voice-profile change or expiry, without replacing native session/history.
Recovery is bounded to one explicit pre-admission refresh/reopen; known or
uncertain admitted work never redispatches. See [protocol](docs/PROTOCOL.md).

### Structured diagnostics

The logging candidate adds consistent bounded lifecycle/error logs across both
products, persistent safe failure summaries, cross-layer request/job correlation,
and diagnostics readable during startup/storage failure. No transcript/audio/key
logging or automatic retry is enabled. See [logging contract](docs/LOGGING.md).
