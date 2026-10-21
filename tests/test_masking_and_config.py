"""Masking, redaction and configuration guards."""

import logging
from pathlib import Path

import pytest

from batchrunner.config import (
    ApiSettings,
    ConfigError,
    PlanCategory,
    load_api_settings,
    load_plan_rules,
    validate_base_url,
)
from batchrunner.masking import REDACTED, RedactingFormatter, mask_msisdn, redact

# --- masking -----------------------------------------------------------------


def test_mask_keeps_only_last_three_digits():
    assert mask_msisdn("+99970123456") == "+********456"


def test_redact_masks_numbers_in_free_text_and_urls():
    text = "GET /subscribers/%2B99970123456/plan for 99970123456 and +99970123456"
    out = redact(text)
    assert "70123456" not in out
    assert "%2B********456" in out
    assert out.count("456") == 3


def test_redact_leaves_short_digit_runs_alone():
    text = "port 8000, run 2026-10-21_170015Z, attempt 3/4, took 12.345s"
    assert redact(text) == text


def test_redact_removes_secrets():
    assert redact("Authorization: Bearer s3cr3t-value", ["s3cr3t-value"]) == (
        f"Authorization: Bearer {REDACTED}"
    )


def test_formatter_redacts_exception_text_too():
    formatter = RedactingFormatter("%(message)s", secrets=["tok-123"])
    try:
        raise RuntimeError("failed for +99970123456 with tok-123")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    out = formatter.format(record)
    assert "tok-123" not in out
    assert "+99970123456" not in out
    assert "+********456" in out


# --- plan rules --------------------------------------------------------------


def test_plan_config_has_eleven_codes_split_into_groups(rules):
    assert len(rules.catalog) == 11
    assert rules.categorise("POST_M") is PlanCategory.AUTO_REFRESH
    assert rules.categorise("BUSINESS_FLEET") is PlanCategory.MANUAL_ONLY
    assert rules.categorise("PREPAID_DATA") is PlanCategory.INELIGIBLE


def test_unknown_or_missing_plan_codes_are_ineligible(rules):
    assert rules.categorise("BRAND_NEW_PLAN") is PlanCategory.INELIGIBLE
    assert rules.categorise(None) is PlanCategory.INELIGIBLE
    assert not rules.is_known("BRAND_NEW_PLAN")


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "plans.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_plan_in_both_groups_is_rejected(tmp_path):
    path = _write_yaml(tmp_path, "plans: {A: a, B: b}\nauto_refresh: [A]\nmanual_only: [A]\n")
    with pytest.raises(ConfigError, match="both"):
        load_plan_rules(path)


def test_listed_code_missing_from_catalog_is_rejected(tmp_path):
    path = _write_yaml(tmp_path, "plans: {A: a}\nauto_refresh: [A, TYPO]\n")
    with pytest.raises(ConfigError, match="TYPO"):
        load_plan_rules(path)


# --- api settings ------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "http://localhost:8000/", "https://api.example.com"]
)
def test_allowed_base_urls(url):
    assert validate_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://api.example.com", "ftp://example.com", "not a url"])
def test_rejected_base_urls(url):
    with pytest.raises(ConfigError):
        validate_base_url(url)


def test_settings_repr_never_shows_token():
    settings = ApiSettings(base_url="https://api.example.com", token="very-private-value")
    assert "very-private-value" not in repr(settings)


def test_missing_token_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_api_settings(tmp_path / "missing.env")


def test_settings_load_from_env_file(monkeypatch, tmp_path):
    # setenv first so monkeypatch restores the environment that dotenv mutates.
    for name in ("BASE_URL", "API_TOKEN"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text("BASE_URL=http://127.0.0.1:9999\nAPI_TOKEN=from-file\n", encoding="utf-8")
    settings = load_api_settings(env)
    assert settings.base_url == "http://127.0.0.1:9999"
    assert settings.token == "from-file"
