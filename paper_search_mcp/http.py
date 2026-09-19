"""Bounded HTTP attempts with origin-bound credentials and safe diagnostics."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import re
import time
from urllib.parse import quote, quote_plus

import httpx

from .provider_models import ProviderError
from .storage import data_directory, run_lock


class RequestFailure(RuntimeError):
    def __init__(self, error: ProviderError):
        self.error = error
        super().__init__(error.message)


@dataclass
class RequestAllowance:
    remaining: int = 4
    wait_seconds: float = 0
    used: int = 0
    response_usage: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.remaining < 0 or self.wait_seconds < 0 or self.used < 0:
            raise ValueError("Request and waiting allowances must be nonnegative.")

    def reserve(self):
        if self.remaining <= 0:
            raise RequestFailure(ProviderError(kind="budget", message="Request allowance exhausted."))
        self.remaining -= 1
        self.used += 1


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"api.?key|token|authorization|password|email", key, re.I)
                else sanitize(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
                value = value.replace(variant, "[REDACTED]")
    value = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
    return re.sub(r"([?&](?:api[_-]?key|access_token|token|email|password|authorization)=)[^&\s\"']+", r"\1[REDACTED]", value, flags=re.I)


_log_credentials = ContextVar("provider_log_credentials", default=())


def _safe_log(record):
    record.msg = sanitize(record.getMessage(), _log_credentials.get())
    record.args = ()
    return True


logging.getLogger("httpx").addFilter(_safe_log)


def retry_delay(value, default):
    try:
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else default
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return default


class ProviderHTTP:
    def __init__(self, source, origin, *, headers=None, params=None, interval=0):
        self.interval = interval
        self.source, self.origin = source, httpx.URL(origin)
        self.headers, self.params = headers or {}, params or {}
        self.secrets = tuple(self.headers.values()) + tuple(self.params.values())
        self.secrets += tuple(value[7:] for value in self.headers.values() if value.startswith("Bearer "))

    @contextmanager
    def request_slot(self):
        """Space every attempt across instances/processes, including retries and restarts."""
        if not self.interval:
            yield
            return
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = directory / f"{self.source}-request-time"
        with run_lock(directory / f"{self.source}-requests.lock"):
            try:
                next_request = float(timestamp.read_text())
            except (FileNotFoundError, ValueError):
                next_request = 0
            time.sleep(min(self.interval, max(0, next_request - time.time())))
            timestamp.write_text(str(time.time() + self.interval))
            try:
                yield
            finally:
                timestamp.write_text(str(time.time() + self.interval))

    def get(self, client, url, allowance, *, params=None, allow_redirect_response=False):
        target = httpx.URL(url)
        if (target.scheme, target.host, target.port) != (self.origin.scheme, self.origin.host, self.origin.port) or target.userinfo:
            raise RequestFailure(ProviderError(kind="unsafe_origin", message="Credential origin mismatch."))
        for attempt in range(4):  # Initial attempt plus at most three retries.
            response = None
            token = _log_credentials.set(tuple(str(secret) for secret in self.secrets))
            try:
                if allowance.remaining <= 0:
                    allowance.reserve()  # Fail without waiting for a request slot.
                with self.request_slot():
                    allowance.reserve()
                    response = client.get(url, params={**(params or {}), **self.params}, headers=self.headers,
                                          timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
            except httpx.RequestError as exc:
                error = ProviderError(kind="service", message=f"{self.source}: {type(exc).__name__}.")
            else:
                usage = {key: response.headers[key] for key in (
                    "X-RateLimit-Credits-Used", "X-RateLimit-Remaining", "X-RateLimit-Limit", "X-RateLimit-Reset")
                    if key in response.headers}
                if usage:
                    allowance.response_usage.append({"attempt": allowance.used, **usage})
                status = response.status_code
                if 200 <= status < 300 or allow_redirect_response and status in (301, 302, 303, 307, 308):
                    return response
                body = response.text.lower()
                exhausted = response.headers.get("X-RateLimit-Remaining") == "0" or any(
                    token in body + response.headers.get("X-ELS-Status", "").lower()
                    for token in ("quota exhausted", "quota_exceeded", "quota exceeded", "run out of searches"))
                kind = "quota" if exhausted else "authentication" if status in (401, 403) else "rate_limit" if status == 429 else "service"
                if status in (400, 404, 410) and any(word in body for word in ("cursor", "token")) and any(word in body for word in ("expired", "invalid", "expire")):
                    kind = "expired_cursor"
                error = ProviderError(kind=kind, message=f"{self.source}: HTTP {status} ({kind}).", status_code=status)
                if kind in ("authentication", "quota") or status not in (429, 500, 502, 503, 504):
                    raise RequestFailure(error)
            finally:
                _log_credentials.reset(token)
            if attempt == 3:
                raise RequestFailure(error)
            delay = retry_delay(response.headers.get("Retry-After", ""), 2 ** (attempt + 1)) if response is not None else 2 ** (attempt + 1)
            error.retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            if delay > allowance.wait_seconds or allowance.remaining <= 0:
                raise RequestFailure(error)
            allowance.wait_seconds -= delay
            time.sleep(delay)
