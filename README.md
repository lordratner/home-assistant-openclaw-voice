# OpenClaw Home Assistant integration

Native Home Assistant STT, streaming conversation and streaming-text TTS entities,
backed by the companion [OpenClaw plugin](https://github.com/lordratner/openclaw-home-assistant).
OC owns the selected agent, normal tools/memory/policy, actual persistent session,
model/thinking/fast and provider credentials. HA chooses mode and playback target.

**Development candidate, 0.0.1-dev.0. Not yet approved for live deployment.**
Config/options/reauth, all three entities and both actions are implemented and
exercised with real HA APIs in disposable runtimes. Physical playback and real
provider acceptance are still pending; HACS discovery/publication is not claimed.

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
- [SDK streaming-transcription gap](docs/SDK-GAPS.md)
- [Detailed planning baseline](planning/00-README.md)
- [Current development checkpoint](implementation/STATUS.md)

TTS uses a single canonical WAV stream. HA device-specific format preferences
still apply and can add conversion latency. Native microphone data uses bounded
finalized-utterance recognition until OC exposes a reliable streaming-finalization
contract. Unconsumed action playback expires after 60 seconds; it cannot retain
an action slot forever. Normal HA cache/URL retention remains HA-owned.

Diagnostics are allowlisted and exclude connection/agent/client/session identities,
credentials, conversation text and audio. Reauthentication creates a new scoped
identity; old conversations must not silently adopt a new agent or profile.

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
no public distribution, live installation or firmware change is implied.

Original source is MIT; see [LICENSE](LICENSE) and [provenance](THIRD_PARTY_NOTICES.md).
