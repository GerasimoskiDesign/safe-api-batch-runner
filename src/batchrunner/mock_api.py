"""A stand-in for the subscriber backend, with seeded fault injection.

It exposes the two calls the remediation workflow needs (look up the plan,
force an entitlement refresh) and misbehaves the way real backends do:
occasional 5xx, occasional responses slower than any sane client timeout, and
429 with ``Retry-After`` when calls arrive faster than its rate limit allows.
``/admin/stats`` reports what the server actually saw - including the highest
number of requests it was handling at the same moment - so tests can verify
the runner's behaviour from the outside instead of trusting the runner's own
bookkeeping.

All randomness comes from one seeded ``random.Random``. Because the runner is
strictly sequential, the sequence of draws - and therefore which call fails -
is identical on every run with the same seed and input.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import random
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from batchrunner.numbers import MSISDN_PATTERN

# The backend's view of the plan mix. PROMO_TRIAL is deliberately missing from
# config/plans.yaml: it models a product launched after the config was written.
PLAN_WEIGHTS: dict[str, int] = {
    "POST_S": 14,
    "POST_M": 18,
    "POST_L": 12,
    "POST_XL": 6,
    "YOUTH_PLUS": 8,
    "FAMILY_SHARE": 9,
    "BUSINESS_PRO": 6,
    "BUSINESS_FLEET": 4,
    "PREPAID_BASIC": 10,
    "PREPAID_DATA": 7,
    "LEGACY_VOICE": 3,
    "PROMO_TRIAL": 3,
}
NOT_FOUND_SHARE = 0.04
DISCONNECT_POLL_SECONDS = 0.01

# Scripted fault names usable in MockConfig.scripted_faults.
FAULT_OK = "ok"
FAULT_5XX = "5xx"
FAULT_SLOW = "slow"
FAULT_429 = "429"


@dataclass
class MockConfig:
    """Behaviour knobs. Rates are probabilities per call (0..1)."""

    token: str
    seed: int = 42
    error_rate: float = 0.03
    slow_rate: float = 0.02
    slow_seconds: float = 8.0
    min_gap_seconds: float = 0.25
    # msisdn -> plan code, or None for "subscriber not found" (404).
    plan_overrides: dict[str, str | None] = field(default_factory=dict)
    # "plan:<msisdn>" or "refresh:<msisdn>" -> faults for successive calls,
    # e.g. ["5xx", "ok"]. Scripted entries replace the random draw.
    scripted_faults: dict[str, list[str]] = field(default_factory=dict)


def plan_for(msisdn: str, seed: int) -> str | None:
    """Deterministic plan lookup: the same number always has the same plan.

    A hash rather than a stored table keeps the mock stateless and lets it
    answer for any valid number, including ones generated in tests.
    """
    digest = hashlib.sha256(f"{seed}:{msisdn}".encode()).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    if u < NOT_FOUND_SHARE:
        return None
    u = (u - NOT_FOUND_SHARE) / (1 - NOT_FOUND_SHARE)
    total = sum(PLAN_WEIGHTS.values())
    cumulative = 0.0
    for code, weight in PLAN_WEIGHTS.items():
        cumulative += weight / total
        if u < cumulative:
            return code
    return next(reversed(PLAN_WEIGHTS))


class MockState:
    """Counters and the fault RNG. Only touched from the event loop thread."""

    def __init__(self, config: MockConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.config.seed)
        self.scripts = {key: list(faults) for key, faults in self.config.scripted_faults.items()}
        self.in_flight = 0
        self.max_concurrency = 0
        self.total_calls = 0
        self.plan_calls = 0
        self.write_calls = 0
        self.writes_applied = 0
        self.rate_limited = 0
        self.injected_5xx = 0
        self.injected_slow = 0
        self.dropped_by_client = 0
        self.unauthorized = 0
        self.status_counts: Counter[int] = Counter()
        self.refreshed: Counter[str] = Counter()
        self.last_accepted_at: float | None = None

    @contextmanager
    def track(self) -> Iterator[None]:
        self.in_flight += 1
        self.max_concurrency = max(self.max_concurrency, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1

    def next_fault(self, step: str, msisdn: str) -> str:
        script = self.scripts.get(f"{step}:{msisdn}")
        if script:
            return script.pop(0)
        draw = self.rng.random()
        if draw < self.config.error_rate:
            return FAULT_5XX
        if draw < self.config.error_rate + self.config.slow_rate:
            return FAULT_SLOW
        return FAULT_OK

    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "plan_calls": self.plan_calls,
            "write_calls": self.write_calls,
            "writes_applied": self.writes_applied,
            "max_concurrency": self.max_concurrency,
            "in_flight": self.in_flight,
            "rate_limited": self.rate_limited,
            "injected_5xx": self.injected_5xx,
            "injected_slow": self.injected_slow,
            "dropped_by_client": self.dropped_by_client,
            "unauthorized": self.unauthorized,
            "status_counts": {str(k): v for k, v in sorted(self.status_counts.items())},
        }


def _authorised(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.encode(), token.encode())


async def _sleep_unless_disconnected(request: Request, seconds: float) -> bool:
    """Sleep, but stop early if the client gave up. Returns True if it did.

    Without this, a handler would keep "working" after the client timed out
    and the concurrency counter would see the retry overlap with a request
    the client has already abandoned.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await request.is_disconnected():
            return True
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)
    return False


def create_app(config: MockConfig) -> FastAPI:
    """Build the mock FastAPI app. State lives on ``app.state.mock``."""
    app = FastAPI(title="Mock SubscriberService", docs_url=None, redoc_url=None)
    state = MockState(config)
    app.state.mock = state

    def respond(status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        state.status_counts[status] += 1
        return JSONResponse(status_code=status, content=body, headers=headers)

    async def handle(request: Request, step: str, msisdn: str) -> Response:
        with state.track():
            state.total_calls += 1
            if step == "plan":
                state.plan_calls += 1
            else:
                state.write_calls += 1
            if not _authorised(request, config.token):
                state.unauthorized += 1
                return respond(401, {"error": "unauthorized"})
            now = time.monotonic()
            if config.min_gap_seconds > 0 and state.last_accepted_at is not None:
                elapsed = now - state.last_accepted_at
                if elapsed < config.min_gap_seconds:
                    state.rate_limited += 1
                    wait = max(1, math.ceil(config.min_gap_seconds - elapsed))
                    return respond(429, {"error": "rate limited"}, {"Retry-After": str(wait)})
            state.last_accepted_at = now
            if not MSISDN_PATTERN.match(msisdn):
                return respond(400, {"error": "malformed msisdn"})

            fault = state.next_fault(step, msisdn)
            if fault == FAULT_5XX:
                state.injected_5xx += 1
                return respond(503, {"error": "service unavailable"})
            if fault == FAULT_429:
                state.rate_limited += 1
                return respond(429, {"error": "rate limited"}, {"Retry-After": "1"})
            if fault == FAULT_SLOW:
                state.injected_slow += 1
                if await _sleep_unless_disconnected(request, config.slow_seconds):
                    # The client hung up; nothing is applied and nothing is sent.
                    state.dropped_by_client += 1
                    return Response(status_code=499)

            plan = config.plan_overrides.get(msisdn, "__hash__")
            if plan == "__hash__":
                plan = plan_for(msisdn, config.seed)
            if plan is None:
                return respond(404, {"error": "subscriber not found"})
            if step == "plan":
                return respond(200, {"msisdn": msisdn, "plan_code": plan})
            state.writes_applied += 1
            state.refreshed[msisdn] += 1
            return respond(200, {"msisdn": msisdn, "status": "refresh_queued"})

    @app.get("/subscribers/{msisdn}/plan")
    async def get_plan(msisdn: str, request: Request) -> Response:
        return await handle(request, "plan", msisdn)

    @app.post("/subscribers/{msisdn}/refresh-entitlements")
    async def refresh_entitlements(msisdn: str, request: Request) -> Response:
        return await handle(request, "refresh", msisdn)

    @app.get("/admin/stats")
    async def admin_stats(request: Request) -> Response:
        if not _authorised(request, config.token):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return JSONResponse(content=state.stats())

    @app.post("/admin/reset")
    async def admin_reset(request: Request) -> Response:
        if not _authorised(request, config.token):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        state.reset()
        return JSONResponse(content={"status": "reset"})

    return app
