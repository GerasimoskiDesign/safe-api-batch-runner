"""HTTP client: retries only where safe, Retry-After honoured, pacing, one at a time."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from batchrunner.client import (
    ConcurrentUseError,
    FailureKind,
    RetryPolicy,
    SequentialApiClient,
    parse_retry_after,
)
from tests.conftest import AUTO, FAST, NOT_FOUND, TOKEN

# --- pure helpers ------------------------------------------------------------


def test_backoff_doubles_and_is_capped():
    policy = RetryPolicy(backoff_base=1.0, backoff_max=5.0)
    assert [policy.backoff(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_parse_retry_after_seconds_date_and_garbage():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("3", now) == 3.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=10), usegmt=True), now) == 10
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("-5", now) == 0.0


# --- with a scripted in-memory transport (exact control, no real waiting) ----


class Recorder:
    """Fake sleep + monotonic clock: records waits instead of sleeping."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def scripted_client(responses, **kwargs):
    """Client whose transport returns ``responses`` in order (Response or exception)."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    rec = Recorder()
    client = SequentialApiClient(
        "http://127.0.0.1:1",
        TOKEN,
        transport=httpx.MockTransport(handler),
        sleep=rec.sleep,
        monotonic=rec.monotonic,
        **kwargs,
    )
    return client, rec, seen


def ok_plan():
    return httpx.Response(200, json={"plan_code": "POST_M"})


def test_5xx_is_retried_with_backoff_until_success():
    client, rec, seen = scripted_client(
        [httpx.Response(503), httpx.Response(500), ok_plan()],
        delay=0.0,
        policy=RetryPolicy(max_attempts=4, backoff_base=1.0),
    )
    result = client.get_plan(AUTO)
    assert result.ok and result.attempts == 3
    assert rec.sleeps == [1.0, 2.0]
    assert len(seen) == 3


def test_retry_after_header_wins_over_shorter_backoff():
    client, rec, _ = scripted_client(
        [httpx.Response(429, headers={"Retry-After": "7"}), ok_plan()],
        delay=0.0,
        policy=RetryPolicy(backoff_base=1.0),
    )
    assert client.get_plan(AUTO).ok
    assert rec.sleeps == [7.0]


def test_retry_after_longer_than_allowed_gives_up_without_waiting():
    client, rec, seen = scripted_client(
        [httpx.Response(429, headers={"Retry-After": "3600"})],
        delay=0.0,
        policy=RetryPolicy(max_retry_after=60),
    )
    result = client.get_plan(AUTO)
    assert not result.ok and result.failure_kind is FailureKind.TRANSIENT
    assert "too long" in result.error
    assert rec.sleeps == [] and len(seen) == 1


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_client_errors_are_never_retried(status):
    client, _, seen = scripted_client([httpx.Response(status)], delay=0.0)
    result = client.get_plan(AUTO)
    assert not result.ok and result.failure_kind is FailureKind.CLIENT_ERROR
    assert len(seen) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retried_and_flagged(status):
    client, _, seen = scripted_client([httpx.Response(status)], delay=0.0)
    result = client.get_plan(AUTO)
    assert result.failure_kind is FailureKind.AUTH
    assert len(seen) == 1


def test_timeouts_and_refused_connections_are_retried():
    client, _, seen = scripted_client(
        [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), ok_plan()],
        delay=0.0,
        policy=RetryPolicy(max_attempts=3, backoff_base=0.5),
    )
    assert client.get_plan(AUTO).ok
    assert len(seen) == 3


def test_gives_up_after_max_attempts():
    client, rec, seen = scripted_client(
        [httpx.Response(503)] * 3, delay=0.0, policy=RetryPolicy(max_attempts=3)
    )
    result = client.get_plan(AUTO)
    assert not result.ok and result.failure_kind is FailureKind.TRANSIENT
    assert result.attempts == 3 and len(seen) == 3
    assert len(rec.sleeps) == 2  # no pointless wait after the last attempt


def test_non_json_success_body_is_a_failure_not_a_crash():
    client, _, _ = scripted_client([httpx.Response(200, text="<html>")], delay=0.0)
    result = client.get_plan(AUTO)
    assert not result.ok and result.failure_kind is FailureKind.BAD_RESPONSE


def test_delay_is_enforced_between_consecutive_requests():
    client, rec, _ = scripted_client([ok_plan(), ok_plan(), ok_plan()], delay=2.0)
    for _ in range(3):
        client.get_plan(AUTO)
    assert rec.sleeps == [2.0, 2.0]  # none before the very first request


def test_redirects_are_not_followed_so_the_token_never_leaves_the_host():
    client, _, seen = scripted_client(
        [httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})],
        delay=0.0,
    )
    result = client.get_plan(AUTO)
    assert not result.ok and len(seen) == 1


def test_number_is_url_encoded_and_bearer_header_sent():
    client, _, seen = scripted_client([ok_plan()], delay=0.0)
    client.get_plan(AUTO)
    assert seen[0].url.raw_path.decode() == "/subscribers/%2B99970000001/plan"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_concurrent_use_of_one_client_is_refused():
    gate = threading.Event()
    release = threading.Event()

    def handler(request):
        gate.set()
        release.wait(5)
        return ok_plan()

    client = SequentialApiClient(
        "http://127.0.0.1:1", TOKEN, delay=0.0, transport=httpx.MockTransport(handler)
    )
    worker = threading.Thread(target=client.get_plan, args=(AUTO,))
    worker.start()
    assert gate.wait(5)
    try:
        with pytest.raises(ConcurrentUseError):
            client.get_plan(AUTO)
    finally:
        release.set()
        worker.join(5)


# --- against the real mock API over HTTP -----------------------------------


def real_client(mock, **overrides):
    settings = {"timeout": FAST.timeout, "delay": FAST.delay, "policy": FAST.policy}
    settings.update(overrides)
    return SequentialApiClient(mock.base_url, TOKEN, **settings)


def test_5xx_then_success_against_mock(start_mock):
    mock = start_mock(scripted_faults={f"plan:{AUTO}": ["5xx", "5xx", "ok"]})
    with real_client(mock) as client:
        result = client.get_plan(AUTO)
    assert result.ok and result.attempts == 3
    assert result.body["plan_code"] == "POST_M"
    assert mock.stats()["status_counts"] == {"200": 1, "503": 2}


def test_429_retry_after_is_honoured_against_mock(start_mock):
    # min_gap 0.5 s: the second call arrives immediately and gets 429 Retry-After: 1.
    mock = start_mock(min_gap_seconds=0.5)
    with real_client(mock) as client:
        client.get_plan(AUTO)
        started = time.monotonic()
        result = client.get_plan(AUTO)
        elapsed = time.monotonic() - started
    assert result.ok and result.attempts == 2
    assert elapsed >= 1.0  # waited the full Retry-After, not the 0.1 s backoff
    stats = mock.stats()
    assert stats["rate_limited"] == 1
    assert stats["status_counts"] == {"200": 2, "429": 1}


def test_timeout_is_retried_and_server_sees_the_client_hang_up(start_mock):
    mock = start_mock(scripted_faults={f"plan:{AUTO}": ["slow", "ok"]}, slow_seconds=2.0)
    with real_client(mock) as client:
        result = client.get_plan(AUTO)
    assert result.ok and result.attempts == 2
    assert result.attempt_log[0].error == "timeout"
    time.sleep(0.1)
    stats = mock.stats()
    assert stats["dropped_by_client"] == 1
    assert stats["max_concurrency"] == 1


def test_404_against_mock_is_a_single_call(start_mock):
    mock = start_mock()
    with real_client(mock) as client:
        result = client.get_plan(NOT_FOUND)
    assert result.status_code == 404 and result.attempts == 1
    assert mock.stats()["total_calls"] == 1


def test_wrong_token_is_rejected_by_mock(start_mock):
    mock = start_mock()
    with SequentialApiClient(mock.base_url, "wrong", delay=0.0) as client:
        result = client.get_plan(AUTO)
    assert result.failure_kind is FailureKind.AUTH
    assert mock.stats()["unauthorized"] == 1
