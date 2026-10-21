"""HTTP client that can only ever have one request in flight.

Every guardrail that protects the backend from *this* process lives here:

* one synchronous ``httpx.Client`` with a connection pool of size 1, plus a
  non-blocking lock that raises if anything tries to use it concurrently;
* a fixed minimum pause between the end of one request and the start of the
  next, so the run is paced even when every call succeeds instantly;
* retries only for failures where retrying is safe - timeouts, connection
  refused, 5xx and 429 - with exponential backoff and ``Retry-After`` honoured;
* no redirects, so the bearer token is never forwarded to another host.

Retrying the refresh POST is safe only because "refresh entitlements" is
idempotent: refreshing twice has the same effect as once. A timed-out write
may or may not have been applied on the server, so a non-idempotent write
must never be put behind this retry loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx

from batchrunner.masking import mask_msisdn

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Wall-clock timestamp for audit records (never used for decisions)."""
    return datetime.now(UTC)


class Step(StrEnum):
    PLAN = "plan_check"
    REFRESH = "refresh"


class FailureKind(StrEnum):
    TRANSIENT = "transient"  # timeouts / 5xx / 429 still failing after all retries
    CLIENT_ERROR = "client_error"  # 4xx: the request itself is wrong; retrying won't help
    AUTH = "auth"  # 401/403: every following call would fail the same way
    BAD_RESPONSE = "bad_response"  # 2xx with a body we cannot use


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try before giving up on one call."""

    max_attempts: int = 4
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    max_retry_after: float = 120.0

    def backoff(self, attempt: int) -> float:
        """Delay after failed attempt ``attempt`` (1-based): base * 2^(n-1), capped.

        No jitter: jitter spreads out *many* clients retrying at once. This
        runner is a single sequential client, and a fixed schedule keeps runs
        reproducible.
        """
        return min(self.backoff_base * 2 ** (attempt - 1), self.backoff_max)


@dataclass
class AttemptRecord:
    """One HTTP attempt, as written to the audit log."""

    step: str
    msisdn_masked: str
    attempt: int
    started_at: datetime
    finished_at: datetime
    status_code: int | None
    error: str | None
    wait_before_next: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "msisdn": self.msisdn_masked,
            "attempt": self.attempt,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "finished_at": self.finished_at.isoformat(timespec="milliseconds"),
            "status_code": self.status_code,
            "error": self.error,
            "wait_before_next_s": self.wait_before_next,
        }


@dataclass
class StepResult:
    """Outcome of one logical call after retries."""

    step: Step
    ok: bool
    status_code: int | None
    attempts: int
    started_at: datetime
    finished_at: datetime
    body: dict[str, Any] | None = None
    error: str | None = None
    failure_kind: FailureKind | None = None
    attempt_log: list[AttemptRecord] = field(default_factory=list)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date) into seconds to wait."""
    if value is None:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class ConcurrentUseError(RuntimeError):
    """Raised if two threads try to use the same client at once."""


class SequentialApiClient:
    """Client for the subscriber API. One request at a time, always."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
        delay: float = 0.5,
        policy: RetryPolicy | None = None,
        clock: Clock = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
        on_attempt: Callable[[AttemptRecord], None] | None = None,
    ):
        self.delay = delay
        self.policy = policy or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._on_attempt = on_attempt
        self._last_request_end: float | None = None
        self._in_use = threading.Lock()
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> SequentialApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get_plan(self, msisdn: str) -> StepResult:
        """``GET /subscribers/{msisdn}/plan``. Read-only."""
        return self._call(Step.PLAN, "GET", f"/subscribers/{quote(msisdn, safe='')}/plan", msisdn)

    def refresh_entitlements(self, msisdn: str) -> StepResult:
        """``POST /subscribers/{msisdn}/refresh-entitlements``. The only write."""
        path = f"/subscribers/{quote(msisdn, safe='')}/refresh-entitlements"
        return self._call(Step.REFRESH, "POST", path, msisdn)

    def _pace(self) -> None:
        """Wait until ``delay`` seconds have passed since the previous request ended."""
        if self._last_request_end is None:
            return
        remaining = self.delay - (self._monotonic() - self._last_request_end)
        if remaining > 0:
            self._sleep(remaining)

    def _call(self, step: Step, method: str, path: str, msisdn: str) -> StepResult:
        if not self._in_use.acquire(blocking=False):
            raise ConcurrentUseError("SequentialApiClient used from two threads at once")
        try:
            return self._call_with_retries(step, method, path, msisdn)
        finally:
            self._in_use.release()

    def _call_with_retries(self, step: Step, method: str, path: str, msisdn: str) -> StepResult:
        masked = mask_msisdn(msisdn)
        attempts: list[AttemptRecord] = []
        first_start: datetime | None = None
        attempt = 0
        # The loop always returns; it exits via one of the self._finish(...) calls.
        while True:
            attempt += 1
            self._pace()
            started = self._clock()
            first_start = first_start or started
            status: int | None = None
            error: str | None = None
            response: httpx.Response | None = None
            try:
                response = self._http.request(method, path)
                status = response.status_code
            except httpx.TimeoutException:
                error = "timeout"
            except httpx.ConnectError:
                error = "connection refused"
            except httpx.TransportError as exc:
                error = f"transport error: {type(exc).__name__}"
            finally:
                self._last_request_end = self._monotonic()
            finished = self._clock()
            record = AttemptRecord(step, masked, attempt, started, finished, status, error)
            attempts.append(record)

            if status is not None and 200 <= status < 300:
                try:
                    body = response.json() if response is not None else None
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    return self._finish(
                        step,
                        attempts,
                        first_start,
                        False,
                        FailureKind.BAD_RESPONSE,
                        error="response is not JSON",
                    )
                return self._finish(step, attempts, first_start, True, body=body)
            if status in (401, 403):
                return self._finish(
                    step,
                    attempts,
                    first_start,
                    False,
                    FailureKind.AUTH,
                    error=f"HTTP {status}: not authorised",
                )

            retryable = error in ("timeout", "connection refused") or (
                status is not None and (status == 429 or status >= 500)
            )
            reason = error or f"HTTP {status}"
            if not retryable:
                kind = FailureKind.CLIENT_ERROR if status is not None else FailureKind.TRANSIENT
                return self._finish(step, attempts, first_start, False, kind, error=reason)
            if attempt >= self.policy.max_attempts:
                return self._finish(
                    step,
                    attempts,
                    first_start,
                    False,
                    FailureKind.TRANSIENT,
                    error=f"{reason} after {attempt} attempts",
                )

            wait = self.policy.backoff(attempt)
            if status == 429 and response is not None:
                retry_after = parse_retry_after(response.headers.get("Retry-After"), finished)
                if retry_after is not None:
                    if retry_after > self.policy.max_retry_after:
                        return self._finish(
                            step,
                            attempts,
                            first_start,
                            False,
                            FailureKind.TRANSIENT,
                            error=f"HTTP 429 with Retry-After {retry_after:.0f}s (too long)",
                        )
                    wait = max(wait, retry_after)
            record.wait_before_next = round(wait, 3)
            log.warning(
                "%s %s attempt %d/%d failed (%s); retrying in %.2fs",
                step,
                masked,
                attempt,
                self.policy.max_attempts,
                reason,
                wait,
            )
            self._sleep(wait)

    def _finish(
        self,
        step: Step,
        attempts: list[AttemptRecord],
        started_at: datetime,
        ok: bool,
        kind: FailureKind | None = None,
        **extra: Any,
    ) -> StepResult:
        """Build the StepResult and hand every attempt to the audit callback."""
        if self._on_attempt is not None:
            for record in attempts:
                self._on_attempt(record)
        last = attempts[-1]
        return StepResult(
            step=step,
            ok=ok,
            status_code=last.status_code,
            attempts=len(attempts),
            started_at=started_at,
            finished_at=last.finished_at,
            failure_kind=kind,
            attempt_log=attempts,
            **extra,
        )
