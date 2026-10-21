"""CLI entry point and mock API behaviour."""

from __future__ import annotations

from batchrunner.cli import main
from batchrunner.mock_api import NOT_FOUND_SHARE, plan_for
from batchrunner.pipeline import EXIT_OK, EXIT_REFUSED, EXIT_STOPPED
from tests.conftest import AUTO, MANUAL, ROOT, TOKEN


def set_env(monkeypatch, base_url: str, token: str = TOKEN) -> None:
    monkeypatch.setenv("BASE_URL", base_url)
    monkeypatch.setenv("API_TOKEN", token)


FAST_ARGS = ["--delay", "0", "--timeout", "0.3", "--backoff-base", "0.1", "--max-attempts", "2"]


def test_cli_run_end_to_end(start_mock, write_input, tmp_path, monkeypatch):
    mock = start_mock()
    set_env(monkeypatch, mock.base_url)
    code = main(
        [
            "run",
            str(write_input(AUTO, MANUAL)),
            "--out-dir",
            str(tmp_path / "out"),
            "--run-id",
            "cli",
            "--plans",
            str(ROOT / "config" / "plans.yaml"),
            "--env-file",
            str(tmp_path / "none.env"),
            *FAST_ARGS,
        ]
    )
    assert code == EXIT_OK
    assert (tmp_path / "out" / "cli" / "results.xlsx").exists()
    assert mock.stats()["writes_applied"] == 1


def test_cli_dry_run_flag(start_mock, write_input, tmp_path, monkeypatch):
    mock = start_mock()
    set_env(monkeypatch, mock.base_url)
    code = main(
        [
            "run",
            str(write_input(AUTO)),
            "--dry-run",
            "--out-dir",
            str(tmp_path / "o"),
            "--plans",
            str(ROOT / "config" / "plans.yaml"),
            "--env-file",
            str(tmp_path / "x"),
            *FAST_ARGS,
        ]
    )
    assert code == EXIT_OK
    assert mock.stats()["write_calls"] == 0
    (run_folder,) = (tmp_path / "o").iterdir()
    assert run_folder.name.endswith("-dry-run")


def test_cli_cap_flag(start_mock, write_input, tmp_path, monkeypatch):
    mock = start_mock()
    set_env(monkeypatch, mock.base_url)
    args = [
        "run",
        str(write_input(AUTO, MANUAL)),
        "--max-numbers",
        "1",
        "--out-dir",
        str(tmp_path / "o"),
        "--plans",
        str(ROOT / "config" / "plans.yaml"),
        "--env-file",
        str(tmp_path / "x"),
        *FAST_ARGS,
    ]
    assert main(args) == EXIT_REFUSED
    assert main([*args, "--allow-over-cap"]) == EXIT_OK


def test_cli_refuses_plain_http_to_remote_host(write_input, tmp_path, monkeypatch):
    set_env(monkeypatch, "http://api.example.com")
    assert main(["run", str(write_input(AUTO)), "--env-file", str(tmp_path / "x")]) == EXIT_REFUSED


def test_cli_refuses_without_token(write_input, tmp_path, monkeypatch):
    set_env(monkeypatch, "http://127.0.0.1:8000", token="")
    assert main(["run", str(write_input(AUTO)), "--env-file", str(tmp_path / "x")]) == EXIT_REFUSED


def test_cli_breaker_exit_code(start_mock, write_input, tmp_path, monkeypatch):
    mock = start_mock(error_rate=1.0)
    set_env(monkeypatch, mock.base_url)
    batch = [f"+9997200000{i}" for i in range(6)]
    code = main(
        [
            "run",
            str(write_input(*batch)),
            "--out-dir",
            str(tmp_path / "o"),
            "--breaker-threshold",
            "2",
            "--plans",
            str(ROOT / "config" / "plans.yaml"),
            "--env-file",
            str(tmp_path / "x"),
            *FAST_ARGS,
        ]
    )
    assert code == EXIT_STOPPED
    assert mock.stats()["total_calls"] == 4  # 2 numbers x 2 attempts


def test_cli_generate_numbers_is_reproducible(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    assert main(["generate-numbers", "--out", str(a)]) == EXIT_OK
    assert main(["generate-numbers", "--out", str(b)]) == EXIT_OK
    assert a.read_bytes() == b.read_bytes()
    # Text comparison: a Windows checkout may have converted the sample to CRLF.
    sample = (ROOT / "data" / "numbers_sample.txt").read_text(encoding="utf-8")
    assert a.read_text(encoding="utf-8") == sample


# --- mock API ----------------------------------------------------------------


def test_mock_requires_bearer_token(start_mock):
    mock = start_mock()
    url = "/subscribers/%2B99970000001/plan"
    assert mock.http.get(url).status_code == 401
    assert mock.http.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert mock.http.get("/admin/stats").status_code == 401


def test_mock_rate_limit_returns_retry_after(start_mock):
    mock = start_mock(min_gap_seconds=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    url = "/subscribers/%2B99970000001/plan"
    assert mock.http.get(url, headers=headers).status_code == 200
    second = mock.http.get(url, headers=headers)
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 5


def test_mock_rejects_malformed_numbers(start_mock):
    mock = start_mock()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = mock.http.get("/subscribers/12345/plan", headers=headers)
    assert response.status_code == 400


def test_mock_plans_are_deterministic_and_roughly_weighted():
    numbers = [f"+9997{i:07d}" for i in range(4000)]
    first = [plan_for(n, 42) for n in numbers]
    assert first == [plan_for(n, 42) for n in numbers]
    missing = sum(p is None for p in first) / len(first)
    assert abs(missing - NOT_FOUND_SHARE) < 0.015
    assert first != [plan_for(n, 7) for n in numbers]


def test_mock_fault_sequence_is_seeded(start_mock):
    def statuses(seed):
        mock = start_mock(seed=seed, error_rate=0.3, plan_overrides={})
        headers = {"Authorization": f"Bearer {TOKEN}"}
        url = "/subscribers/%2B99970000001/plan"
        return [mock.http.get(url, headers=headers).status_code for _ in range(30)]

    assert statuses(1) == statuses(1)
    assert 503 in statuses(1)


def test_mock_reset_clears_counters(start_mock):
    mock = start_mock()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    mock.http.get("/subscribers/%2B99970000001/plan", headers=headers)
    assert mock.stats()["total_calls"] == 1
    mock.http.post("/admin/reset", headers=headers)
    assert mock.stats()["total_calls"] == 0
