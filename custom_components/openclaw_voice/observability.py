"""Bounded structured diagnostics; no raw exception strings or user content."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from time import monotonic, time
from uuid import UUID, uuid4
import asyncio
import inspect
import json
import logging
import math
import re
import errno
import hmac
import secrets
import socket
import ssl

LOGGER = logging.getLogger(__package__)
SCHEMA = json.loads(Path(__file__).with_name("diagnostic-schema.json").read_text())
REASONS = frozenset(json.loads(Path(__file__).with_name("diagnostic-codes.json").read_text()))
PROVIDERS = frozenset({"openai", "elevenlabs", "google", "deepgram", "groq", "mistral", "azure", "aws", "local"})
PROVIDER_CODES = frozenset({"authentication_error", "auth_error", "invalid_api_key", "quota_exceeded",
    "rate_limit_exceeded", "resource_exhausted", "insufficient_audio_activity", "input_error", "audio_too_short",
    "commit_throttled", "transcriber_error", "session_time_limit_exceeded"})
CATEGORIES = frozenset({"authentication", "timeout", "cancelled", "no_speech", "rate_limit", "capacity",
    "protocol", "transport", "provider", "storage", "policy", "validation", "internal", "unknown"})
STAGES = frozenset(SCHEMA["stages"])
CURRENT = ContextVar("openclaw_voice_diagnostic_span", default=None)
FINGERPRINT_KEY = secrets.token_bytes(32)
SYSTEM_CODES = frozenset(SCHEMA["system_codes"])
EXCEPTION_TYPES = frozenset({"TimeoutError", "CancelledError", "OSError", "ConnectionError", "ClientError",
    "ClientConnectorError", "ClientResponseError", "ClientPayloadError", "ServerDisconnectedError", "UnicodeDecodeError",
    "ValueError", "KeyError", "TypeError", "VoiceTransportError", "ConfigEntryAuthFailed", "UpdateFailed",
    "ServiceValidationError", "HomeAssistantError", "ExceptionGroup", "BaseExceptionGroup", "GeneratorExit", "RuntimeError", "Exception", "OverflowError", "other"})


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value) else "unknown"


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def safe_fields(raw=None, depth=0):
    raw = raw if isinstance(raw, dict) else {}
    result = {k: raw[k] for k in ("trace_id", "span_id", "parent_span_id", "request_id", "job_id", "call_id") if _uuid(raw.get(k))}
    for key in ("elapsed_ms", "bytes", "frames", "segments", "attempt", "count", "failure_count", "exit_code", "suppressed"):
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 2**53:
            result[key] = round(value, 3)
    for key in ("partial_output", "admitted", "continuing", "confirmed", "truncated", "detail_present", "tool_result_error", "error_reply"):
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    for key, low, high in (("http_status", 100, 599), ("transport_code", 1000, 4999)):
        if type(raw.get(key)) is int and low <= raw[key] <= high:
            result[key] = raw[key]
    for key, allowed in (("reason", REASONS), ("provider_code", PROVIDER_CODES), ("category", CATEGORIES), ("stage", STAGES)):
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            result[key] = raw[key]
    for key, allowed in (("system_code", SYSTEM_CODES), ("exception_type", EXCEPTION_TYPES)):
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            result[key] = raw[key]
    if isinstance(raw.get("classification_source"), str) and raw["classification_source"] in SCHEMA["classification_sources"]:
        result["classification_source"] = raw["classification_source"]
    if isinstance(raw.get("error_fingerprint"), str) and re.fullmatch(r"[a-f0-9]{16}", raw["error_fingerprint"]):
        result["error_fingerprint"] = raw["error_fingerprint"]
    if isinstance(raw.get("container"), str) and raw["container"] in SCHEMA["containers"]:
        result["container"] = raw["container"]
    if "provider" in raw:
        result["provider"] = raw["provider"] if isinstance(raw["provider"], str) and raw["provider"] in PROVIDERS else "other"
    if isinstance(raw.get("signal"), str) and raw["signal"] in SCHEMA["signals"]:
        result["signal"] = raw["signal"]
    if isinstance(raw.get("mode"), str) and raw["mode"] in {"realtime", "batch", "progressive", "buffered", "persistent", "per_conversation"}:
        result["mode"] = raw["mode"]
    if depth < 2 and isinstance(raw.get("causes"), list):
        result["causes"] = [safe_fields(value, depth+1) for value in raw["causes"][:3]]
    if isinstance(raw.get("causes"), list) and len(raw["causes"]) > (3 if depth < 2 else 0):
        result["truncated"] = True
    return result


def safe_failure(raw):
    result = safe_fields(raw)
    while len(json.dumps(result, separators=(",", ":")).encode()) > 1024 and result.get("causes"):
        result["causes"].pop()
        result["truncated"] = True
    for key in ("trace_id", "span_id", "parent_span_id", "call_id", "bytes", "frames", "segments", "attempt", "count", "elapsed_ms", "exit_code", "suppressed", "job_id", "request_id"):
        if len(json.dumps(result, separators=(",", ":")).encode()) > 1024 and key in result:
            result.pop(key)
            result["truncated"] = True
    return result


def failure_summary(error, _depth=0, **facts):
    try:
        inherited = safe_fields(getattr(error, "diagnostic", None))
        children = []
        if _depth < 2:
            if isinstance(error, BaseExceptionGroup):
                children = list(error.exceptions[:3])
            elif error.__cause__ is not None and error.__cause__ is not error:
                children = [error.__cause__]
        causes = [failure_summary(child, _depth=_depth+1) for child in children]
        truncated = isinstance(error, BaseExceptionGroup) and len(error.exceptions) > len(children) or _depth >= 2 and error.__cause__ is not None and error.__cause__ is not error
        if causes:
            useful = next((v for v in causes if v.get("category") != "cancelled"), causes[0])
            inherited = {**useful, "causes": causes, "failure_count": len(error.exceptions) if isinstance(error, BaseExceptionGroup) else 1, **inherited}
        code = getattr(error, "code", None)
        reason = code if isinstance(code, str) and code in REASONS else "unknown_error"
        status = getattr(error, "status", None) or getattr(error, "status_code", None) or inherited.get("http_status")
        detail = reason + " " + inherited.get("provider_code", "")
        category = "unknown"
        if "auth" in detail or "invalid_api_key" in detail or reason in {"scope_denied", "pairing_failed"} or status in (401, 403):
            category = "authentication"
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)) or "timeout" in reason or getattr(error, "errno", None) == errno.ETIMEDOUT:
            category = "timeout"
        elif isinstance(error, (asyncio.CancelledError, GeneratorExit)) or any(v in reason for v in ("cancel", "closed", "stopping")):
            category = "cancelled"
        elif any(v in detail for v in ("no_speech", "insufficient_audio_activity", "audio_too_short")):
            category = "no_speech"
        elif any(v in detail for v in ("rate_limit", "quota", "throttled", "resource_exhausted")) or status == 429:
            category = "rate_limit"
        elif "capacity" in reason or "limit" in reason:
            category = "capacity"
        elif isinstance(error, ssl.SSLError):
            category = "transport"
        elif isinstance(error, OSError):
            category = "transport" if isinstance(error, (ConnectionError, socket.gaierror)) or getattr(error, "errno", None) in {errno.ECONNRESET, errno.ECONNREFUSED, errno.EPIPE} else "storage"
        elif any(v in reason for v in ("frame", "protocol", "sequence", "ownership", "json")):
            category = "protocol"
        elif any(v in reason for v in ("transport", "backend_unavailable", "replay", "stream_interrupted")):
            category = "transport"
        elif any(v in reason for v in ("profile_changed", "binding", "mode_conflict", "session_")) or reason in {"request_conflict", "stale_inference_settings"}:
            category = "policy"
        elif reason.startswith(("invalid_", "missing_", "unsupported_")):
            category = "validation"
        elif any(v in reason for v in ("provider", "transcription", "synthesis")) or isinstance(status, int) and status >= 400:
            category = "provider"
        elif isinstance(error, (TypeError, ValueError, KeyError)):
            category = "internal"
        message = error.args[0][:4096] if getattr(error, "args", ()) and isinstance(error.args[0], str) else ""
        system = errno.errorcode.get(getattr(error, "errno", None))
        if isinstance(error, ssl.SSLError):
            system = "TLS_CERTIFICATE_VERIFY_FAILED" if isinstance(error, ssl.SSLCertVerificationError) else "TLS_ERROR"
        if isinstance(error, socket.gaierror):
            system = "EAI_AGAIN" if error.errno == socket.EAI_AGAIN else "ENOTFOUND"
        if inherited.get("reason") == "unknown_error" and reason != "unknown_error":
            inherited.pop("reason")
        if inherited.get("category") == "unknown" and category != "unknown":
            inherited.pop("category")
        classification_source = "structured" if isinstance(code, str) and code in REASONS or isinstance(status, int) else "system" if system in SYSTEM_CODES else "exception" if type(error).__name__ in EXCEPTION_TYPES else "unknown"
        return safe_failure({"stage": "unknown", "reason": reason, "category": category,
                            "http_status": status, "system_code": system,
                            "exception_type": type(error).__name__ if type(error).__name__ in EXCEPTION_TYPES else "other",
                            "classification_source": classification_source,
                            **({"truncated": True} if truncated else {}),
                            "detail_present": bool(message),
                            **({"error_fingerprint": hmac.new(FINGERPRINT_KEY, message.encode(), "sha256").hexdigest()[:16]} if message else {}),
                            **inherited, **facts})
    except Exception:
        return {"stage": "unknown", "reason": "unknown_error", "category": "unknown"}


class Diagnostics:
    def __init__(self, logger=LOGGER, limit=128, failure_limit=32, clock=monotonic):
        self.logger, self.clock = logger, clock
        self.instance_id = str(uuid4())
        self.recent = deque(maxlen=max(1, min(limit, 512)) if type(limit) is int else 128)
        self.failures = deque(maxlen=max(1, min(failure_limit, 64)) if type(failure_limit) is int else 32)
        self.rates = {}
        self.sequence = 0
        self.stats = {"events_total": 0, "recent_evicted": 0, "failures_evicted": 0, "native_suppressed": 0, "observer_failures": 0}

    def current(self):
        span = CURRENT.get()
        return span if span and span.owner is self else None

    def event(self, component, operation, phase, facts=None, level="debug"):
        try:
            self.sequence += 1
            span = CURRENT.get()
            item = {"schema": 1, "side": "ha", "instance_id": self.instance_id, "sequence": self.sequence, "at_ms": round(time()*1000),
                    "component": _label(component), "operation": _label(operation), "phase": _label(phase),
                    **safe_fields(span.fields if span else {}), **safe_fields(facts)}
            self.stats["events_total"] += 1
            if len(self.recent) == self.recent.maxlen:
                self.stats["recent_evicted"] += 1
            self.recent.append(item)
            if phase in {"failed", "partial_failure", "cleanup_failed", "interrupted"}:
                if len(self.failures) == self.failures.maxlen:
                    self.stats["failures_evicted"] += 1
                self.failures.append(item)
            now = monotonic()
            self.rates = {k: v for k, v in self.rates.items() if v[0] > now}
            key = (item["component"], item["operation"], item["phase"], item.get("reason"))
            if key not in self.rates and len(self.rates) < 256:
                self.rates[key] = [now+60, 0]
            rate = self.rates.get(key)
            if rate:
                rate[1] += 1
                if rate[1] == 9:
                    item = {**item, "phase": "suppressed", "suppressed": 1}
                if rate[1] <= 9:
                    getattr(self.logger, level)("voice_event %s", json.dumps(item, separators=(",", ":")))
                if rate[1] > 8:
                    self.stats["native_suppressed"] += 1
            else:
                self.stats["native_suppressed"] += 1
            return item
        except Exception:
            self.stats["observer_failures"] += 1
            return None

    def start(self, component, operation, **facts):
        return Span(self, component, operation, facts)

    def snapshot(self):
        return json.loads(json.dumps({"schema": 1, "recent": list(self.recent), "failures": list(self.failures), "stats": self.stats}))

    def instrument(self, target, component, methods):
        for name in methods:
            original = getattr(target, name, None)
            if not callable(original):
                continue
            original = getattr(original, "_voice_original", original)
            def make(fn, operation):
                if inspect.iscoroutinefunction(fn):
                    @wraps(fn)
                    async def async_call(*args, **kwargs):
                        with self.start(component, operation):
                            return await fn(*args, **kwargs)
                    wrapper = async_call
                else:
                    @wraps(fn)
                    def sync_call(*args, **kwargs):
                        with self.start(component, operation):
                            return fn(*args, **kwargs)
                    wrapper = sync_call
                wrapper._voice_original = fn
                return wrapper
            try:
                setattr(target, name, make(original, name))
            except (AttributeError, TypeError):
                self.stats["observer_failures"] += 1
        return target


class Span:
    def __init__(self, owner, component, operation, facts):
        self.owner, self.component, self.operation = owner, component, operation
        self.started, self.finished = owner.clock(), False
        parent = CURRENT.get()
        self.fields = {**(parent.fields if parent else {}), **safe_fields(facts), "span_id": str(uuid4())}
        self.fields.setdefault("trace_id", str(uuid4()))
        if parent:
            self.fields["parent_span_id"] = parent.fields["span_id"]
        self.steps = []
        owner.event(component, operation, "started", self.fields)

    def __enter__(self):
        self.token = CURRENT.set(self)
        return self

    @contextmanager
    def activate(self):
        token = CURRENT.set(self)
        try:
            yield self
        finally:
            CURRENT.reset(token)

    def __exit__(self, kind, error, tb):
        try:
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                self.finish("cancelled", **failure_summary(error))
            elif error is not None:
                self.fail(error)
            else:
                self.finish("completed")
        finally:
            CURRENT.reset(self.token)

    def annotate(self, **facts):
        self.fields.update(safe_fields(facts))

    def phase(self, name, **facts):
        name = _label(name)
        if name in self.steps:
            return
        if len(self.steps) >= 48:
            self.annotate(truncated=True)
            return
        self.steps.append(name)
        self.owner.event(self.component, self.operation, name,
                         {**self.fields, **facts, "elapsed_ms": (self.owner.clock()-self.started)*1000}, "info")

    def fail(self, error, **facts):
        self.failure = failure_summary(error, **facts)
        if self.failure.get("stage", "unknown") == "unknown":
            defaults = {"stt": "stt_transcription", "tts": "synthesis_request", "conversation": "dispatch",
                        "http": "http_request", "authority": "authorization", "bindings": "binding",
                        "configuration": "validation", "assist": "tool_execution", "action": "dispatch",
                        "playback": "playback", "connection": "refresh", "integration": "startup",
                        "action_binding": "binding", "conversation_state": "binding"}
            self.failure["stage"] = defaults.get(self.component, "unknown")
        self.finish("failed", **self.failure)
        return self.failure

    def finish(self, outcome, **facts):
        if self.finished:
            return
        self.finished = True
        important = self.component in {"integration", "configuration", "action", "authority"} or self.operation in {"pipeline", "wire_events"}
        self.owner.event(self.component, self.operation, outcome,
                         {**self.fields, **facts, "elapsed_ms": (self.owner.clock()-self.started)*1000},
                         "warning" if outcome in {"failed", "partial_failure", "interrupted"} else "info" if important else "debug")


GLOBAL = Diagnostics()


def observe(component, operation):
    """Instrument a boundary without inspecting its arguments, output or errors."""
    def decorate(fn):
        def diagnostics(args):
            first = args[0] if args else None
            return (getattr(first, "diagnostics", None) or
                    getattr(getattr(first, "runtime", None), "diagnostics", None) or
                    getattr(getattr(first, "runtime_data", None), "diagnostics", None) or GLOBAL)

        if inspect.isasyncgenfunction(fn):
            @wraps(fn)
            async def generator(*args, **kwargs):
                owner = diagnostics(args)
                span = owner.start(component, operation)
                source = fn(*args, **kwargs)
                primary_failure = None
                try:
                    count = 0
                    while True:
                        with span.activate():
                            try:
                                value = await anext(source)
                            except StopAsyncIteration:
                                span.finish("completed")
                                return
                            count += 1
                            if isinstance(value, dict):
                                span.annotate(job_id=value.get("job_id"), frames=count)
                            if count == 1:
                                span.phase("first_output")
                        # Do not hold a ContextVar token across a yield: native
                        # TTS consumers may close the iterator from another task.
                        yield value
                except (asyncio.CancelledError, GeneratorExit) as error:
                    primary_failure = error
                    span.finish("cancelled", **failure_summary(error))
                    raise
                except BaseException as error:
                    primary_failure = error
                    span.fail(error)
                    raise
                finally:
                    with span.activate():
                        try:
                            await source.aclose()
                        except BaseException as error:
                            owner.event(component, operation, "cleanup_failed", failure_summary(error, stage="cleanup"), "warning")
                            if primary_failure is None:
                                raise
            return generator
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                with diagnostics(args).start(component, operation):
                    return await fn(*args, **kwargs)
            return asynchronous
        @wraps(fn)
        def synchronous(*args, **kwargs):
            with diagnostics(args).start(component, operation):
                return fn(*args, **kwargs)
        return synchronous
    return decorate
