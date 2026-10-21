"""Reproducible end-to-end demo: mock API + three runs + charts.

Scenarios, each against a fresh mock started with the same seed:

* ``dry-run``  - plan checks only; the mock must record zero write calls.
* ``live``     - the real workflow with the default fault rates.
* ``outage``   - the backend returns 5xx for everything; the circuit breaker
  must stop the run after a few numbers instead of working through the list.

Timings are shortened (small delay and timeout) so the demo finishes in
seconds; the guardrails are the same ones a real run uses.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from batchrunner.charts import decisions_chart, timeline_chart
from batchrunner.client import RetryPolicy
from batchrunner.config import ApiSettings, PlanRules
from batchrunner.generate import write_numbers_file
from batchrunner.mock_api import MockConfig, create_app
from batchrunner.mock_server import serve_in_thread
from batchrunner.pipeline import ClientSettings, RunOutcome, execute
from batchrunner.runner import RunOptions

log = logging.getLogger(__name__)

# A throwaway credential shared by the in-process mock and the runner.
DEMO_TOKEN = "demo-only-not-a-real-credential"

DEMO_CLIENT = ClientSettings(
    timeout=0.5,
    delay=0.05,
    policy=RetryPolicy(max_attempts=4, backoff_base=0.1, backoff_max=1.0, max_retry_after=10),
)


@dataclass(frozen=True)
class Scenario:
    name: str
    dry_run: bool
    error_rate: float = 0.03
    slow_rate: float = 0.02


SCENARIOS = [
    Scenario("dry-run", dry_run=True),
    Scenario("live", dry_run=False),
    Scenario("outage", dry_run=False, error_rate=1.0, slow_rate=0.0),
]


def _fetch_stats(base_url: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {DEMO_TOKEN}"}
    return httpx.get(f"{base_url}/admin/stats", headers=headers, timeout=5).json()


def run_scenario(
    scenario: Scenario, numbers_file: Path, rules: PlanRules, out_root: Path, seed: int
) -> tuple[RunOutcome, dict[str, Any]]:
    mock = MockConfig(
        token=DEMO_TOKEN,
        seed=seed,
        error_rate=scenario.error_rate,
        slow_rate=scenario.slow_rate,
        slow_seconds=1.5,
        min_gap_seconds=0.0,
    )
    with serve_in_thread(create_app(mock)) as base_url:
        outcome = execute(
            numbers_file,
            ApiSettings(base_url=base_url, token=DEMO_TOKEN),
            rules,
            RunOptions(dry_run=scenario.dry_run),
            DEMO_CLIENT,
            out_root,
            run_id=f"demo-{scenario.name}",
        )
        stats = _fetch_stats(base_url)
    return outcome, stats


def results_markdown(results: dict[str, Any]) -> str:
    """Markdown tables for the README, generated from the demo's own numbers."""
    names = list(results["scenarios"])
    lines = ["| Metric | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]

    def row(label: str, values: list[Any]) -> None:
        lines.append(f"| {label} | " + " | ".join(str(v) for v in values) + " |")

    sc = results["scenarios"]
    for decision in (
        "REFRESHED",
        "WOULD_REFRESH",
        "FLAGGED_MANUAL",
        "SKIPPED_INELIGIBLE",
        "FAILED",
    ):
        row(decision, [sc[n]["summary"]["decisions"][decision] for n in names])
    row("numbers not processed", [sc[n]["summary"]["unprocessed"] for n in names])
    row("HTTP attempts (client)", [sc[n]["summary"]["http_attempts"] for n in names])
    row("retries", [sc[n]["summary"]["retries"] for n in names])
    row("calls received (server)", [sc[n]["server"]["total_calls"] for n in names])
    row("write calls received (server)", [sc[n]["server"]["write_calls"] for n in names])
    row(
        "injected 5xx / slow",
        [f"{sc[n]['server']['injected_5xx']} / {sc[n]['server']['injected_slow']}" for n in names],
    )
    row("max concurrency seen by server", [sc[n]["server"]["max_concurrency"] for n in names])
    row("exit code", [sc[n]["exit_code"] for n in names])
    stopped = [n for n in names if sc[n]["summary"]["stopped_reason"]]
    out = "\n".join(lines) + "\n"
    for n in stopped:
        out += f"\n`{n}` stopped early: {sc[n]['summary']['stopped_reason']}\n"
    return out


def run_demo(
    out_dir: Path, img_dir: Path | None, rules: PlanRules, seed: int = 42
) -> dict[str, Any]:
    """Run every scenario, write ``demo_results.json/.md`` and the charts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        shutil.rmtree(out_dir / f"demo-{scenario.name}", ignore_errors=True)
    numbers_file = write_numbers_file(out_dir / "numbers.txt", seed=seed)
    input_info: dict[str, Any] = {}
    results: dict[str, Any] = {"seed": seed, "scenarios": {}}
    for scenario in SCENARIOS:
        log.info("=== Demo scenario: %s ===", scenario.name)
        outcome, stats = run_scenario(scenario, numbers_file, rules, out_dir, seed)
        assert outcome.summary is not None
        input_info = outcome.summary["input"]
        results["scenarios"][scenario.name] = {
            "exit_code": outcome.exit_code,
            "summary": outcome.summary,
            "server": stats,
        }
    results["input"] = input_info
    (out_dir / "demo_results.json").write_text(json.dumps(results, indent=2) + "\n", "utf-8")
    (out_dir / "demo_results.md").write_text(results_markdown(results), encoding="utf-8")
    if img_dir is not None:
        sc = results["scenarios"]
        decisions_chart(
            sc["dry-run"]["summary"]["decisions"],
            sc["live"]["summary"]["decisions"],
            img_dir / "decisions.png",
        )
        timeline_chart(out_dir / "demo-live" / "audit_log.jsonl", img_dir / "request_timeline.png")
        log.info("Charts written to %s", img_dir)
    log.info("Demo results written to %s", out_dir)
    return results
