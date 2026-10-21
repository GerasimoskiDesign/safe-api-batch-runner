"""Shared fixtures: a real mock API over HTTP and fast client settings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from batchrunner.client import RetryPolicy
from batchrunner.config import ApiSettings, PlanRules, load_plan_rules
from batchrunner.mock_api import MockConfig, MockState, create_app
from batchrunner.mock_server import serve_in_thread
from batchrunner.pipeline import ClientSettings

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-credential-not-real"

# Fixed test numbers with fixed plans (via MockConfig.plan_overrides).
AUTO = "+99970000001"  # POST_M -> auto refresh
INELIGIBLE = "+99970000002"  # PREPAID_BASIC -> skip
MANUAL = "+99970000003"  # BUSINESS_PRO -> flag
UNKNOWN_PLAN = "+99970000004"  # PROMO_TRIAL, not in plans.yaml -> skip
NOT_FOUND = "+99970000005"  # 404
AUTO_2 = "+99970000006"  # POST_S -> auto refresh

PLAN_OVERRIDES: dict[str, str | None] = {
    AUTO: "POST_M",
    INELIGIBLE: "PREPAID_BASIC",
    MANUAL: "BUSINESS_PRO",
    UNKNOWN_PLAN: "PROMO_TRIAL",
    NOT_FOUND: None,
    AUTO_2: "POST_S",
}

# Short timings so the suite stays fast. Backoff stays >= 0.1 s so the mock has
# time to notice a timed-out client hanging up before the retry arrives.
FAST = ClientSettings(
    timeout=0.3,
    delay=0.0,
    policy=RetryPolicy(max_attempts=3, backoff_base=0.1, backoff_max=0.2, max_retry_after=5),
)


@dataclass
class MockHandle:
    base_url: str
    state: MockState
    # One reusable client: creating an httpx client per call is slow (TLS setup).
    http: httpx.Client

    @property
    def api(self) -> ApiSettings:
        return ApiSettings(base_url=self.base_url, token=TOKEN)

    def stats(self) -> dict:
        response = self.http.get("/admin/stats", headers={"Authorization": f"Bearer {TOKEN}"})
        response.raise_for_status()
        return response.json()


@pytest.fixture
def start_mock() -> Iterator[Callable[..., MockHandle]]:
    """Factory: ``start_mock(error_rate=..., scripted_faults=...)`` -> running mock."""
    with ExitStack() as stack:

        def _start(**overrides) -> MockHandle:
            settings = {
                "token": TOKEN,
                "seed": 42,
                "error_rate": 0.0,
                "slow_rate": 0.0,
                "slow_seconds": 1.0,
                "min_gap_seconds": 0.0,
                "plan_overrides": dict(PLAN_OVERRIDES),
            }
            settings.update(overrides)
            app = create_app(MockConfig(**settings))
            base_url = stack.enter_context(serve_in_thread(app))
            http = stack.enter_context(httpx.Client(base_url=base_url))
            return MockHandle(base_url=base_url, state=app.state.mock, http=http)

        yield _start


@pytest.fixture
def rules() -> PlanRules:
    return load_plan_rules(ROOT / "config" / "plans.yaml")


@pytest.fixture
def write_input(tmp_path: Path) -> Callable[..., Path]:
    def _write(*lines: str, name: str = "numbers.txt") -> Path:
        path = tmp_path / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write
