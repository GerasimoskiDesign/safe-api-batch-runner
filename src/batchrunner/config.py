"""Runtime settings (from ``.env``) and plan rules (from ``config/plans.yaml``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

DEFAULT_PLANS_FILE = Path("config/plans.yaml")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(Exception):
    """Raised when settings are missing or unsafe. The run must not start."""


class PlanCategory(StrEnum):
    AUTO_REFRESH = "auto_refresh"
    MANUAL_ONLY = "manual_only"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class PlanRules:
    """Which plan codes may be refreshed automatically, and which need a human."""

    catalog: frozenset[str]
    auto_refresh: frozenset[str]
    manual_only: frozenset[str]

    def categorise(self, plan_code: str | None) -> PlanCategory:
        """Allow-list lookup: anything not explicitly listed is ineligible.

        This includes codes that are missing from the catalog entirely. A plan
        the config has never heard of is far more likely to be a new product
        nobody has checked than one that should be written to automatically.
        """
        if plan_code in self.auto_refresh:
            return PlanCategory.AUTO_REFRESH
        if plan_code in self.manual_only:
            return PlanCategory.MANUAL_ONLY
        return PlanCategory.INELIGIBLE

    def is_known(self, plan_code: str | None) -> bool:
        return plan_code in self.catalog


def load_plan_rules(path: Path = DEFAULT_PLANS_FILE) -> PlanRules:
    """Load and validate ``plans.yaml``.

    Validation is strict because a typo here silently changes who gets
    written to: a code listed in both groups, or a listed code that is not in
    the catalog, is rejected instead of guessed at.
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"plan config not found: {path}") from exc
    catalog = frozenset((data.get("plans") or {}).keys())
    auto = frozenset(data.get("auto_refresh") or [])
    manual = frozenset(data.get("manual_only") or [])
    if not catalog:
        raise ConfigError("plan config has no 'plans' catalog")
    overlap = auto & manual
    if overlap:
        raise ConfigError(
            f"plan codes listed as both auto_refresh and manual_only: {sorted(overlap)}"
        )
    unknown = (auto | manual) - catalog
    if unknown:
        raise ConfigError(f"plan codes not in the catalog: {sorted(unknown)}")
    return PlanRules(catalog=catalog, auto_refresh=auto, manual_only=manual)


@dataclass(frozen=True)
class ApiSettings:
    """Connection settings. ``repr`` never shows the token."""

    base_url: str
    token: str = field(repr=False)


def validate_base_url(base_url: str) -> str:
    """Require https, except for a mock running on this machine.

    The bearer token travels in a header on every call; over plain http to a
    remote host it would be readable by anything on the path.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("BASE_URL must look like https://host[:port]")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ConfigError("BASE_URL must use https unless it points at localhost")
    return base_url.rstrip("/")


def load_api_settings(env_file: Path | None = Path(".env")) -> ApiSettings:
    """Read ``BASE_URL`` and ``API_TOKEN`` from the environment / ``.env``.

    Real environment variables win over the file, so CI or a scheduler can
    inject secrets without writing them to disk.
    """
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    base_url = os.environ.get("BASE_URL", "").strip()
    token = os.environ.get("API_TOKEN", "").strip()
    if not base_url:
        raise ConfigError("BASE_URL is not set (copy .env.example to .env)")
    if not token:
        raise ConfigError("API_TOKEN is not set (copy .env.example to .env)")
    return ApiSettings(base_url=validate_base_url(base_url), token=token)
