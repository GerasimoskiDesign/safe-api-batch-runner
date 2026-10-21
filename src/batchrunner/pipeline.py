"""Glue for one complete run: parse input, guard, call the API, write outputs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from batchrunner.client import Clock, RetryPolicy, SequentialApiClient, utc_now
from batchrunner.config import ApiSettings, ConfigError, PlanRules
from batchrunner.logging_setup import file_log, quiet_noisy_loggers
from batchrunner.numbers import ParsedInput, parse_file
from batchrunner.reports import AuditLog, OutputPaths, format_summary, write_all
from batchrunner.runner import (
    CapExceededError,
    Decision,
    RunOptions,
    RunReport,
    check_cap,
    run_batch,
)

log = logging.getLogger(__name__)

EXIT_OK = 0  # every number processed, none FAILED
EXIT_FAILURES = 1  # run completed, some numbers FAILED
EXIT_REFUSED = 2  # refused to start (config, input, cap)
EXIT_STOPPED = 3  # stopped early (circuit breaker / auth)


@dataclass(frozen=True)
class ClientSettings:
    timeout: float = 5.0
    delay: float = 0.5
    policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class RunOutcome:
    exit_code: int
    parsed: ParsedInput | None = None
    report: RunReport | None = None
    summary: dict[str, Any] | None = None
    paths: OutputPaths | None = None
    error: str | None = None


def make_run_id(clock: Clock, dry_run: bool) -> str:
    # Dashes keep every digit run short, so log masking never touches the id.
    stamp = clock().strftime("%Y-%m-%d_%H%M%SZ")
    return f"{stamp}-dry-run" if dry_run else stamp


def _log_input(parsed: ParsedInput) -> None:
    log.info(
        "Input: %d lines, %d valid unique numbers, %d duplicates, %d invalid, %d blank/comment",
        parsed.total_lines,
        len(parsed.numbers),
        len(parsed.duplicates),
        len(parsed.invalid),
        parsed.ignored_lines,
    )
    for item in parsed.invalid:
        # Only the line number and the reason: the raw content may be a
        # (malformed) phone number and is written to invalid_lines.csv instead.
        log.warning("Invalid input on line %d: %s", item.line_no, item.reason)
    for dup in parsed.duplicates:
        log.info("Duplicate on line %d (first seen on line %d)", dup.line_no, dup.first_line_no)


def execute(
    input_path: Path,
    api: ApiSettings,
    rules: PlanRules,
    options: RunOptions,
    client_settings: ClientSettings,
    out_root: Path,
    run_id: str | None = None,
    clock: Clock = utc_now,
    transport: httpx.BaseTransport | None = None,
) -> RunOutcome:
    """Run the full workflow and return what happened plus an exit code."""
    quiet_noisy_loggers()
    try:
        parsed = parse_file(input_path)
    except FileNotFoundError:
        log.error("Input file not found: %s", input_path)
        return RunOutcome(EXIT_REFUSED, error="input file not found")
    _log_input(parsed)
    if not parsed.numbers:
        log.error("No valid numbers in %s; nothing to do", input_path)
        return RunOutcome(EXIT_REFUSED, parsed=parsed, error="no valid numbers")
    try:
        check_cap(len(parsed.numbers), options.max_numbers, options.allow_over_cap)
    except CapExceededError as exc:
        log.error("Refusing to run: %s", exc)
        return RunOutcome(EXIT_REFUSED, parsed=parsed, error=str(exc))
    if options.allow_over_cap and len(parsed.numbers) > options.max_numbers:
        log.warning(
            "Running %d numbers, above the cap of %d, because --allow-over-cap was given",
            len(parsed.numbers),
            options.max_numbers,
        )

    run_id = run_id or make_run_id(clock, options.dry_run)
    paths = OutputPaths(out_root / run_id)
    if paths.folder.exists():
        raise ConfigError(f"output folder already exists: {paths.folder}")

    with file_log(paths.run_log, secrets=[api.token]):
        audit = AuditLog(paths.audit_log)
        try:
            with SequentialApiClient(
                api.base_url,
                api.token,
                timeout=client_settings.timeout,
                delay=client_settings.delay,
                policy=client_settings.policy,
                clock=clock,
                transport=transport,
                on_attempt=audit,
            ) as client:
                report = run_batch(parsed.numbers, client, rules, options, clock)
        finally:
            audit.close()
        summary = write_all(report, parsed, paths, run_id)
        for line in format_summary(summary).splitlines():
            log.info("%s", line)
        log.info("Outputs written to %s", paths.folder)

    if report.stopped_reason:
        code = EXIT_STOPPED
    elif report.count(Decision.FAILED):
        code = EXIT_FAILURES
    else:
        code = EXIT_OK
    return RunOutcome(code, parsed=parsed, report=report, summary=summary, paths=paths)
