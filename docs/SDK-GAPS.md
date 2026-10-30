# Native SDK capability notes

## Optional finite-input STT with the companion dev.6 bridge

The companion OpenClaw dev.6 plugin consumes the optional public injected
`api.runtime.mediaUnderstanding.transcribeAudioStream` method. The reviewed core
change is PR187371 at `79b395a14b0a03bf31c66a88e950a7c13af83232`, with a separate
OpenClaw 2026.10.3 backport being prepared. This document does not claim that
the backport or bridge candidate has been activated live.

The existing HA dev.3 client already streams bounded PCM16 mono 16 kHz input
and waits for one finalized transcript. It does not choose OC providers/models,
implement vendor protocols or dispatch from partial recognition. No HA code,
manifest, protocol-major change or HA restart is required solely for this bridge
capability. Capabilities/profile refresh on the normal 30-second coordinator.

OC owns provider/model policy and finite-input completion. Runtime-method
availability advertises provider-dependent capability, not support for every
configured model. An older host, unsupported selection or unavailable capability
retains native batch fallback. Unsupported must be determined before input
consumption. Once streaming is admitted, failure/cancellation does not trigger
batch or another-provider replay. Both paths keep the existing v1 PCM upload and
final transcript framing; only the backend's actual reported mode differs.

This is source/contract qualification, not new paired-runtime, live-provider,
latency or microphone evidence. Physical clarification -> answer -> stop and
wake-word-free listening remain hardware-observed acceptance gates.

## Stock OpenClaw 2026.10.3 limitation (without the core change)

The tested OpenClaw 2026.10.3 realtime-transcription surface exposes connection,
audio send and close/connectivity operations. It does not expose a usable
finalized-utterance commit/flush acknowledgment or watermark tied to the last
accepted sample, nor a complete sample-format/backpressure contract and explicit
selected-agent creation context for this use case.

Do not infer finalization by waiting for silence, socket close or the first
partial transcript. That can omit late words or execute a tool-bearing turn from
unfinished user speech. This is a **host SDK contract gap**, not a blanket claim
that OpenAI/ElevenLabs or every provider lacks realtime transcription.

On that stock host, the bridge uploads microphone chunks, then calls OC's native batch
transcription capability once at finalized input. Capabilities honestly report
`batch` and `native_realtime_finalization_contract_unavailable`. It is functional
fallback under the accepted streaming-first policy; it is not realtime STT.

The reviewed finite-input core change addresses these requirements:

- Selected-agent/auth context and explicit supported audio formats at creation.
- Bounded/backpressured audio writes and a caller-owned abort signal.
- `finishInput()` or equivalent commit returning an acknowledgment tied to all
  accepted input, followed by an unambiguous final transcript for that utterance.
- Documented provider cancellation/release semantics and capability discovery.
- Contract tests for delayed finals, provider errors, unsupported commit, abort,
  reconnect, multiple channels/utterances and no accidental agent dispatch.

Installing only HA does not add this core/runtime capability. Core installation,
bridge activation and real-provider testing remain separate from this HA
documentation update; it changes no installed runtime or provider configuration.

## Other evidence limits

Native OpenAI TTS on the tested host reports unsupported progressive output;
sentence-buffered fallback is exercised. Native ElevenLabs progressive audio is
exercised against a synthetic local provider endpoint. Neither proves current
real-account access, latency or speaker behavior. Model/thinking overrides use
public native dispatch; saved session settings are never patched/restored by the
production plugin. Native approval UI and alternate runtime/account combinations
still need their own compatibility evidence.
