"""Command-line interface: ``python -m batchrunner <command>``."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from batchrunner.client import RetryPolicy
from batchrunner.config import (
    DEFAULT_PLANS_FILE,
    ConfigError,
    load_api_settings,
    load_plan_rules,
)
from batchrunner.logging_setup import configure_console
from batchrunner.pipeline import EXIT_OK, EXIT_REFUSED, ClientSettings, execute
from batchrunner.runner import DEFAULT_MAX_NUMBERS, RunOptions

log = logging.getLogger("batchrunner")


def _positive_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batchrunner",
        description="Sequential, rate-limited batch runner for API remediation workflows.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process a file of numbers against the API")
    run.add_argument("input", type=Path, help="text file, one number per line")
    run.add_argument("--dry-run", action="store_true", help="plan checks only, zero writes")
    run.add_argument(
        "--delay",
        type=_positive_float,
        default=0.5,
        help="seconds between the end of one request and the next (default 0.5)",
    )
    run.add_argument(
        "--timeout",
        type=_positive_float,
        default=5.0,
        help="per-request timeout in seconds (default 5)",
    )
    run.add_argument(
        "--max-attempts",
        type=_positive_int,
        default=4,
        help="attempts per call incl. the first (default 4)",
    )
    run.add_argument(
        "--backoff-base",
        type=_positive_float,
        default=1.0,
        help="first retry delay in seconds, doubled each retry (default 1)",
    )
    run.add_argument(
        "--backoff-max",
        type=_positive_float,
        default=30.0,
        help="upper bound for one backoff wait (default 30)",
    )
    run.add_argument(
        "--max-retry-after",
        type=_positive_float,
        default=120.0,
        help="give up on a call if the API asks to wait longer (default 120)",
    )
    run.add_argument(
        "--max-numbers",
        type=_positive_int,
        default=DEFAULT_MAX_NUMBERS,
        help=f"per-run cap (default {DEFAULT_MAX_NUMBERS})",
    )
    run.add_argument(
        "--allow-over-cap",
        action="store_true",
        help="explicitly allow more numbers than --max-numbers",
    )
    run.add_argument(
        "--breaker-threshold",
        type=_positive_int,
        default=3,
        help="stop after this many consecutive failed numbers (default 3)",
    )
    run.add_argument("--plans", type=Path, default=DEFAULT_PLANS_FILE, help="plan rules YAML")
    run.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file")
    run.add_argument(
        "--out-dir",
        type=Path,
        default=Path("out"),
        help="outputs go to <out-dir>/<run-id>/ (default out)",
    )
    run.add_argument("--run-id", default=None, help="override the timestamp-based run id")

    mock = sub.add_parser("mock-api", help="start the mock subscriber API")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=8000)
    mock.add_argument("--seed", type=int, default=42)
    mock.add_argument("--error-rate", type=float, default=0.03, help="share of 5xx (default 0.03)")
    mock.add_argument(
        "--slow-rate", type=float, default=0.02, help="share of slow responses (default 0.02)"
    )
    mock.add_argument(
        "--slow-seconds",
        type=float,
        default=8.0,
        help="how slow a slow response is (default 8, above the client timeout)",
    )
    mock.add_argument(
        "--min-gap",
        type=float,
        default=0.25,
        help="429 if calls arrive closer together than this (default 0.25)",
    )
    mock.add_argument("--env-file", type=Path, default=Path(".env"))

    gen = sub.add_parser("generate-numbers", help="write a seeded synthetic input file")
    gen.add_argument("--out", type=Path, default=Path("data/numbers_sample.txt"))
    gen.add_argument("--count", type=_positive_int, default=45)
    gen.add_argument("--duplicates", type=int, default=4)
    gen.add_argument("--seed", type=int, default=42)

    demo = sub.add_parser("demo", help="mock API + dry-run/live/outage runs + charts")
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--out-dir", type=Path, default=Path("out/demo"))
    demo.add_argument("--img-dir", type=Path, default=Path("docs/img"))
    demo.add_argument("--no-charts", action="store_true")
    demo.add_argument("--plans", type=Path, default=DEFAULT_PLANS_FILE)
    return parser


def cmd_run(args: argparse.Namespace, level: int) -> int:
    try:
        settings = load_api_settings(args.env_file)
        rules = load_plan_rules(args.plans)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_REFUSED
    configure_console(level, secrets=[settings.token])
    client = ClientSettings(
        timeout=args.timeout,
        delay=args.delay,
        policy=RetryPolicy(
            max_attempts=args.max_attempts,
            backoff_base=args.backoff_base,
            backoff_max=args.backoff_max,
            max_retry_after=args.max_retry_after,
        ),
    )
    options = RunOptions(
        dry_run=args.dry_run,
        max_numbers=args.max_numbers,
        allow_over_cap=args.allow_over_cap,
        breaker_threshold=args.breaker_threshold,
    )
    try:
        outcome = execute(args.input, settings, rules, options, client, args.out_dir, args.run_id)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_REFUSED
    return outcome.exit_code


def cmd_mock_api(args: argparse.Namespace, level: int) -> int:
    import uvicorn
    from dotenv import load_dotenv

    from batchrunner.mock_api import MockConfig, create_app

    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        log.error("API_TOKEN is not set; the mock uses the same token as the runner")
        return EXIT_REFUSED
    configure_console(level, secrets=[token])
    config = MockConfig(
        token=token,
        seed=args.seed,
        error_rate=args.error_rate,
        slow_rate=args.slow_rate,
        slow_seconds=args.slow_seconds,
        min_gap_seconds=args.min_gap,
    )
    log.info("Mock API on %s:%d (seed=%d)", args.host, args.port, args.seed)
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def cmd_generate(args: argparse.Namespace, level: int) -> int:
    from batchrunner.generate import write_numbers_file

    path = write_numbers_file(
        args.out, count=args.count, duplicates=args.duplicates, seed=args.seed
    )
    log.info("Wrote %s", path)
    return EXIT_OK


def cmd_demo(args: argparse.Namespace, level: int) -> int:
    from batchrunner.demo import DEMO_TOKEN, run_demo

    configure_console(level, secrets=[DEMO_TOKEN])
    try:
        rules = load_plan_rules(args.plans)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_REFUSED
    run_demo(args.out_dir, None if args.no_charts else args.img_dir, rules, seed=args.seed)
    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "mock-api": cmd_mock_api,
    "generate-numbers": cmd_generate,
    "demo": cmd_demo,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    configure_console(level)
    return COMMANDS[args.command](args, level)
