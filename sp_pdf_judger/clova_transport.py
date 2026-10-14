"""Bounded CLOVA transport retries, separate from document verdicts.

The gate is process-local (not an account-wide quota manager). Provider reset
headers take precedence over our adaptive pacing. Never log request text/keys.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
from pathlib import Path
import re
import threading
import time


MAX_ATTEMPTS = 3
RETRY_BUDGET_SECONDS = 150.0
REQUEST_TIMEOUT_SECONDS = 60.0
_CONTEXT = ContextVar("clova_request_context", default=None)
_GATE_LOCK = threading.Lock()
_GATES: dict[str, dict] = {}


class ClovaServiceError(RuntimeError):
    """Safe, durable operational failure; must never become a document HOLD."""

    def __init__(self, kind="service", status_code=None, provider_code="", retry_after=0):
        self.kind = kind if kind in _MESSAGES else "service"
        self.status_code = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
        self.provider_code = str(provider_code) if re.fullmatch(r"\d{5}", str(provider_code)) else ""
        self.retry_after = _positive_number(retry_after) or 0.0
        message = _MESSAGES[self.kind]
        codes = [f"HTTP {self.status_code}" if self.status_code else "", self.provider_code]
        if any(codes):
            message += " (" + ", ".join(c for c in codes if c) + ")"
        if self.retry_after:
            message += f". 재요청까지 최소 {math.ceil(self.retry_after)}초 대기가 필요합니다"
        message += ". 남은 API 호출을 중단했습니다. 오류를 충족/불충족/보류로 판정하지 않습니다. "
        message += "원인을 확인한 뒤 문서 목록의 '검수 진행'으로 명시적으로 재시도하세요."
        super().__init__(message)

    def as_dict(self):
        return {"kind": self.kind, "status_code": self.status_code,
                "provider_code": self.provider_code, "retry_after": self.retry_after}


_MESSAGES = {
    "rate_limit": "CLOVA 호출 한도 또는 서비스 혼잡으로 요청이 제한되었습니다",
    "timeout": "CLOVA 응답 대기 시간이 초과되었습니다",
    "connection": "CLOVA 서버에 연결하지 못했습니다",
    "authentication": "CLOVA 인증 또는 접근 권한을 확인해야 합니다",
    "request": "CLOVA 요청 설정 또는 입력 크기를 확인해야 합니다",
    "response": "CLOVA 응답 형식을 검증하지 못했습니다",
    "service": "CLOVA 서비스 요청을 완료하지 못했습니다",
}


@contextmanager
def clova_request_context(progress=None, cache_dir: Path | None = None, *, max_http_attempts=None):
    if max_http_attempts is not None and (type(max_http_attempts) is not int or max_http_attempts < 1):
        raise ValueError('max_http_attempts must be a positive integer')
    parent_budget = (_CONTEXT.get() or {}).get('attempt_budget')
    budget = parent_budget if parent_budget is not None else (
        {'remaining': max_http_attempts} if max_http_attempts is not None else None)
    token = _CONTEXT.set({"progress": progress, "cache_dir": cache_dir,
                         "metrics": {"http_attempts": 0, "retries": 0, "cache_hits": 0},
                         "diagnostics": _empty_diagnostics(), 'attempt_budget': budget})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def response_cache_dir():
    return (_CONTEXT.get() or {}).get("cache_dir")


def request_metrics():
    """Non-secret request-local counters; logical calls may include cache hits."""
    return dict((_CONTEXT.get() or {}).get("metrics") or
                {"http_attempts": 0, "retries": 0, "cache_hits": 0})


def record_request_event(event):
    metrics = (_CONTEXT.get() or {}).get("metrics")
    if metrics is not None and event in metrics:
        metrics[event] += 1


def _empty_diagnostics():
    return dict(gate_wait_seconds=0.0, lock_wait_seconds=0.0, response_wait_seconds=0.0,
                repairs=0, actual_input_tokens=None, actual_output_tokens=None,
                usage_reported_responses=0, usage_missing_responses=0)


def request_diagnostics():
    """Counters only. Unknown usage is None; cached historical usage is excluded."""
    return {**request_metrics(), **((_CONTEXT.get() or {}).get('diagnostics') or _empty_diagnostics())}


def record_diagnostic(name, value=1):
    counters = (_CONTEXT.get() or {}).get('diagnostics')
    if counters is not None and name in counters:
        counters[name] = (counters[name] or 0) + value


def report_response_usage(response):
    usage = getattr(response, 'usage', None)
    def field(name):
        return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    input_tokens, output_tokens = field('prompt_tokens'), field('completion_tokens')
    if all(type(v) is int and v >= 0 for v in (input_tokens, output_tokens)):
        record_diagnostic('actual_input_tokens', input_tokens)
        record_diagnostic('actual_output_tokens', output_tokens)
        record_diagnostic('usage_reported_responses')
    else:
        record_diagnostic('usage_missing_responses')


def diagnostic_delta(before, after):
    result = {key: (None if value is None else value - (before.get(key) or 0))
              for key, value in after.items()}
    if result['usage_reported_responses'] == 0:
        result['actual_input_tokens'] = result['actual_output_tokens'] = None
    return result


def report_response_cache_hit():
    record_request_event("cache_hits")
    _progress("CLOVA 동일 요청의 저장 응답 재사용 · API 호출 없음")


def _progress(message):
    callback = (_CONTEXT.get() or {}).get("progress")
    if callback:
        callback(message)


def _positive_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def _duration(value):
    """Provider duration, e.g. 23s, 1m2s, 500ms, or seconds."""
    text = str(value or "").strip().lower()
    number = _positive_number(text)
    if number is not None:
        return number
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h))+", text):
        return 0.0
    return sum(float(n) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[u]
               for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text))


def retry_delay(headers, *, limited=False):
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    delays = []
    retry = headers.get("retry-after", "")
    if retry:
        seconds = _positive_number(retry)
        if seconds is None:
            try:
                date = parsedate_to_datetime(str(retry))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        delays.append(seconds)
    for unit in ("requests", "tokens"):
        remaining = _positive_number(headers.get(f"x-ratelimit-remaining-{unit}"))
        if limited or remaining == 0:
            delays.append(_duration(headers.get(f"x-ratelimit-reset-{unit}")))
    return max(delays, default=0.0)


def allows_schema_fallback(exc):
    """Only a format validation error or explicit unsupported schema merits it."""
    if isinstance(exc, ClovaServiceError):
        return False
    if isinstance(exc, ValueError):
        return True  # JSON/Pydantic validation; never a transport exception.
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    # Inspect locally, never put the provider body (possibly input text) in logs.
    text = str(getattr(exc, "body", "") or str(exc)).lower()
    return (any(word in text for word in ("response_format", "json_schema"))
            and any(word in text for word in ("unsupported", "not support", "not supported")))


def service_error(exc):
    if isinstance(exc, ClovaServiceError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    kind = ("rate_limit" if status == 429 else
            "authentication" if status in (401, 403) else
            "request" if isinstance(status, int) and 400 <= status < 500 else
            "timeout" if isinstance(exc, TimeoutError) or "timeout" in name else
            "connection" if isinstance(exc, ConnectionError) or "connection" in name else
            "response" if isinstance(exc, ValueError) else "service")
    body = getattr(exc, "body", {})
    provider_code = ""
    if isinstance(body, dict):
        for node in (body, body.get("status"), body.get("error")):
            if isinstance(node, dict) and re.fullmatch(r"\d{5}", str(node.get("code", ""))):
                provider_code = str(node["code"])
                break
    return ClovaServiceError(kind, status, provider_code,
        retry_delay(getattr(getattr(exc, "response", None), "headers", {}), limited=status == 429))


def _gate_for(client, model):
    # Hash identities in memory; no key, URL or provider body in error records.
    identity = f"{getattr(client, 'base_url', '')}|{getattr(client, 'api_key', '')}|{model}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _GATE_LOCK:
        return _GATES.setdefault(key, {"lock": threading.Lock(), "next": 0.0,
                                      "interval": 2.0, "cooldown": 0.0})


def completion_with_backoff(client, request, *, max_attempts: int | None = None):
    attempt_limit = MAX_ATTEMPTS if max_attempts is None else min(MAX_ATTEMPTS, max_attempts)
    if attempt_limit < 1:
        raise ValueError("max_attempts must be at least 1")
    gate = _gate_for(client, request.get("model", ""))
    deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    last_error = ClovaServiceError("rate_limit")
    for attempt in range(attempt_limit):
        budget = (_CONTEXT.get() or {}).get('attempt_budget')
        if budget is not None and budget['remaining'] <= 0:
            raise ClovaServiceError('request')
        # Serialize provider traffic for this identity, not just start times.
        remaining = deadline - time.monotonic()
        lock_started = time.monotonic()
        acquired = remaining > 0 and gate["lock"].acquire(timeout=max(0, remaining))
        record_diagnostic('lock_wait_seconds', time.monotonic() - lock_started)
        if not acquired:
            raise last_error
        try:
            wait = max(0, gate["next"] - time.monotonic())
            if wait >= deadline - time.monotonic():
                raise ClovaServiceError("rate_limit", 429, retry_after=wait)
            end = time.monotonic() + wait
            gate_started = time.monotonic()
            while time.monotonic() < end:
                remaining_wait = end - time.monotonic()
                if wait > 2:
                    _progress(f"CLOVA 호출 간격 조절 · 재요청까지 {math.ceil(remaining_wait)}초 · 시도 {attempt + 1}/{attempt_limit}")
                time.sleep(min(1, remaining_wait))
            record_diagnostic('gate_wait_seconds', time.monotonic() - gate_started)
            _progress(f"CLOVA 응답 대기 · 시도 {attempt + 1}/{attempt_limit}")
            gate["next"] = time.monotonic() + gate["interval"]
            kwargs = dict(request, timeout=min(REQUEST_TIMEOUT_SECONDS, max(.1, deadline - time.monotonic())))
            response_started = time.monotonic()
            try:
                if budget is not None:
                    if budget['remaining'] <= 0:
                        raise ClovaServiceError('request')
                    budget['remaining'] -= 1
                record_request_event("http_attempts")
                if attempt:
                    record_request_event("retries")
                completions = client.chat.completions
                raw_api = getattr(completions, "with_raw_response", None)
                if raw_api is not None:
                    raw = raw_api.create(**kwargs)
                    response = raw.parse()
                    headers = raw.headers
                else:  # Compatible clients and test doubles without raw headers.
                    response = completions.create(**kwargs)
                    headers = getattr(response, "headers", {})
                report_response_usage(response)
                gate["next"] = max(gate["next"], time.monotonic() + retry_delay(headers))
                # Keep slower pacing through the quota window instead of
                # resetting to 2s immediately after one successful retry.
                if time.monotonic() > gate["cooldown"]:
                    gate["interval"] = max(2.0, gate["interval"] * .9)
                return response
            except Exception as exc:
                if allows_schema_fallback(exc):
                    raise
                last_error = service_error(exc)
                retryable = last_error.kind in {"rate_limit", "timeout", "connection"} or (
                    last_error.status_code is not None and last_error.status_code >= 500)
                delay = max(last_error.retry_after, 10 * 2 ** attempt)
                if last_error.kind == "rate_limit":
                    gate["interval"] = min(20.0, max(5.0, gate["interval"] * 2))
                    gate["cooldown"] = time.monotonic() + 60
                if retryable:
                    gate["next"] = max(gate["next"], time.monotonic() + delay)
                safe = last_error.as_dict()
                print(f"[CLOVA_REQUEST_ERROR] kind={safe['kind']} http={safe['status_code']} "
                      f"code={safe['provider_code'] or '-'} attempt={attempt + 1}/{attempt_limit}", flush=True)
                if not retryable or attempt + 1 >= attempt_limit or gate["next"] >= deadline:
                    raise last_error from exc
                _progress(f"CLOVA {last_error.kind} · {math.ceil(delay)}초 후 제한 재시도 {attempt + 2}/{attempt_limit}")
            finally:
                record_diagnostic('response_wait_seconds', time.monotonic() - response_started)
        finally:
            gate["lock"].release()
    raise last_error
