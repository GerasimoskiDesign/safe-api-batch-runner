"""The remediation workflow: check the plan, refresh if allowed, record why.

For each number, in input order and strictly one at a time:

1. ``GET`` the subscriber's plan.
2. Look the plan code up in the allow-list (``config/plans.yaml``):
   ``auto_refresh`` -> force an entitlement refresh (unless dry-run);
   ``manual_only`` -> flag for a human, no write;
   anything else -> skip, no write.
3. Record the decision, HTTP status per step, attempts and timestamps.

The run stops early - leaving the remaining numbers untouched and listed for a
re-run - when the circuit breaker opens or the API rejects the credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from batchrunner.breaker import CircuitBreaker
from batchrunner.client import Clock, FailureKind, SequentialApiClient, StepResult, utc_now
from batchrunner.config import PlanCategory, PlanRules
from batchrunner.masking import mask_msisdn

log = logging.getLogger(__name__)

DEFAULT_MAX_NUMBERS = 50


class Decision(StrEnum):
    REFRESHED = "REFRESHED"
    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"
    FLAGGED_MANUAL = "FLAGGED_MANUAL"
    FAILED = "FAILED"
    # Dry-run only: the number *would* have been refreshed. Kept distinct from
    # REFRESHED so a dry-run report can never be mistaken for a live one.
    WOULD_REFRESH = "WOULD_REFRESH"


class CapExceededError(Exception):
    """The input has more numbers than the per-run cap allows."""


@dataclass
class NumberResult:
    msisdn: str
    decision: Decision
    reason: str
    plan_code: str | None = None
    plan_category: PlanCategory | None = None
    plan_status: int | None = None
    plan_attempts: int = 0
    refresh_status: int | None = None
    refresh_attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    max_numbers: int = DEFAULT_MAX_NUMBERS
    allow_over_cap: bool = False
    breaker_threshold: int = 3


@dataclass
class RunReport:
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    results: list[NumberResult] = field(default_factory=list)
    unprocessed: list[str] = field(default_factory=list)
    stopped_reason: str | None = None

    def count(self, decision: Decision) -> int:
        return sum(1 for r in self.results if r.decision == decision)

    @property
    def completed(self) -> bool:
        return self.stopped_reason is None


def check_cap(count: int, max_numbers: int, allow_over_cap: bool) -> None:
    """Refuse oversized batches unless the operator explicitly opts in.

    The cap bounds the blast radius of a wrong input file: a list pasted from
    the wrong export can hold thousands of numbers, and a sequential run would
    happily work through all of them. Going above the cap has to be a
    deliberate decision, not a default.
    """
    if count > max_numbers and not allow_over_cap:
        raise CapExceededError(
            f"{count} numbers exceeds the per-run cap of {max_numbers}; "
            "split the file or pass --allow-over-cap"
        )


def _from_failure(step: StepResult, label: str) -> str:
    return f"{label} failed: {step.error}"


def process_number(
    msisdn: str, client: SequentialApiClient, rules: PlanRules, dry_run: bool
) -> tuple[NumberResult, FailureKind | None]:
    """Run the workflow for one number. Returns the result and, if it failed, why."""
    masked = mask_msisdn(msisdn)
    plan = client.get_plan(msisdn)
    result = NumberResult(
        msisdn=msisdn,
        decision=Decision.FAILED,
        reason="",
        plan_status=plan.status_code,
        plan_attempts=plan.attempts,
        started_at=plan.started_at,
        finished_at=plan.finished_at,
    )
    if not plan.ok:
        result.reason = _from_failure(plan, "plan check")
        log.error("%s FAILED: %s", masked, result.reason)
        return result, plan.failure_kind

    plan_code = plan.body.get("plan_code") if plan.body else None
    result.plan_code = plan_code
    category = rules.categorise(plan_code)
    result.plan_category = category

    if category is PlanCategory.INELIGIBLE:
        result.decision = Decision.SKIPPED_INELIGIBLE
        result.reason = (
            "plan not eligible"
            if rules.is_known(plan_code)
            else "plan code not in config (treated as ineligible)"
        )
        if not rules.is_known(plan_code):
            log.warning("%s has unknown plan code %r; skipped", masked, plan_code)
    elif category is PlanCategory.MANUAL_ONLY:
        result.decision = Decision.FLAGGED_MANUAL
        result.reason = "plan requires manual handling"
    elif dry_run:
        result.decision = Decision.WOULD_REFRESH
        result.reason = "dry run: refresh not sent"
    else:
        refresh = client.refresh_entitlements(msisdn)
        result.refresh_status = refresh.status_code
        result.refresh_attempts = refresh.attempts
        result.finished_at = refresh.finished_at
        if not refresh.ok:
            result.reason = _from_failure(refresh, "refresh")
            log.error("%s FAILED: %s", masked, result.reason)
            return result, refresh.failure_kind
        result.decision = Decision.REFRESHED
        result.reason = "entitlements refreshed"
    log.info("%s %s (%s)", masked, result.decision, plan_code)
    return result, None


def run_batch(
    numbers: list[str],
    client: SequentialApiClient,
    rules: PlanRules,
    options: RunOptions,
    clock: Clock = utc_now,
) -> RunReport:
    """Process ``numbers`` in order with cap, breaker and auth-failure guards."""
    check_cap(len(numbers), options.max_numbers, options.allow_over_cap)
    breaker = CircuitBreaker(options.breaker_threshold)
    report = RunReport(dry_run=options.dry_run, started_at=clock())
    mode = "DRY RUN (plan checks only, no writes)" if options.dry_run else "LIVE"
    log.info("Starting %s run over %d numbers", mode, len(numbers))

    for index, msisdn in enumerate(numbers):
        result, failure = process_number(msisdn, client, rules, options.dry_run)
        report.results.append(result)
        if failure is FailureKind.AUTH:
            report.stopped_reason = (
                f"API rejected the credentials ({result.reason}); check API_TOKEN"
            )
        elif failure in (FailureKind.TRANSIENT, FailureKind.BAD_RESPONSE):
            # Only backend-health failures count. A 404 for one number says
            # nothing about the backend - it answered correctly - so it resets
            # the breaker like any other properly handled response.
            breaker.record_failure(result.reason)
            if breaker.is_open:
                report.stopped_reason = breaker.describe()
        else:
            breaker.record_success()
        if report.stopped_reason:
            report.unprocessed = numbers[index + 1 :]
            log.error(
                "Stopping run: %s. %d numbers left unprocessed.",
                report.stopped_reason,
                len(report.unprocessed),
            )
            break

    report.finished_at = clock()
    return report
