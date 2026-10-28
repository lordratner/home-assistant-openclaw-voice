# Home Assistant OpenClaw Voice — development foundation

Native Home Assistant client for an agent-neutral OpenClaw voice backend.
The intended integration supplies STT, conversation and streaming-input TTS
entities, plus text-query and interactive-satellite actions. HA chooses the
session **mode**; OC chooses the actual agent, persistent target, model and
speech providers. Provider credentials remain in OC.

**Pre-release source checkpoint; not HACS/install ready.** Config entry,
manifest, full entity/action registration and production backend connection
remain unfinished. Do not copy this directory into a live HA installation yet.

## Read first

- [Current execution ledger and ordered next work](implementation/STATUS.md)
- [Full paired-product design index](planning/00-README.md)
- [HA development plan](planning/04-HOME-ASSISTANT-DEVELOPMENT-PLAN.md)
- [Companion OC plan](planning/03-OPENCLAW-PLUGIN-DEVELOPMENT-PLAN.md)
- [Requirements](planning/01-REQUIREMENTS-AND-DECISIONS.md)
- [Provenance](THIRD_PARTY_NOTICES.md)

## Implemented here

- Task-local binding of an action-created native HA conversation to its mode,
  without a global next-wake slot or private satellite field writes.
- Bounded voice-credential HTTP speech client, incremental upload, strict
  request/profile/job/sequence checks, no redirected credential forwarding.
- Native `TextToSpeechEntity.async_stream_tts_audio` implementation and buffered
  announcement-compatible method.
- Actual HA service/chat-log/satellite-creation tests and cross-repository
  unfinished-text-to-audio proof.

Still unfinished: config/options/pairing UI, production lifecycle, capability
projection, STT and conversation entities, both registered automation actions,
cache/recovery/diagnostics, general codec normalization and release packaging.
MP3 sentence stitching has decoded successfully with synthetic fixtures; other
multi-segment containers are not presented as solved.

## Development checks

Requires Python >=3.14.2. The pinned test environment includes HA 2026.10.4.
An isolated setup with uv is:

```sh
uv venv --python 3.14 .venv
uv pip sync --python .venv/bin/python requirements-test.lock
.venv/bin/python -m pytest -q
```

For the native cross-repository proof, prepare the OC companion as documented in
its README and run `npm run test:native-pair` there. It invokes this repository's
`tests/native_pair_proof.py` with synthetic short-lived test credentials via the
child process environment. No real provider credential is needed.

The native TTS proof withholds final input text until the first audio bytes
arrive. This establishes early audio delivery, **not audible satellite playback**.
Separate household testing follows production integration registration and a
controlled live-install proposal.

## Configuration ownership

The eventual HA options must not become a second provider/model/agent/session
catalog. They select persistent or per-conversation mode; actions can override
mode for that conversation and follow-ups only. Isolated history does not remove
the OC agent's normal tools, skills or durable knowledge.

Original source is MIT; see [LICENSE](LICENSE). HA and other runtime dependencies
retain their own licenses.
