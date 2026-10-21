"""Charts for the README, built from a run's results and audit log."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

DECISION_ORDER = [
    "REFRESHED",
    "WOULD_REFRESH",
    "FLAGGED_MANUAL",
    "SKIPPED_INELIGIBLE",
    "FAILED",
]
COLOURS = {
    "dry-run": "#7f9cc7",
    "live": "#1f3864",
    "ok": "#4c9a6a",
    "client_error": "#9e9e9e",
    "server_error": "#d9534f",
    "timeout": "#f0ad4e",
    "rate_limited": "#8e6bbf",
}


def decisions_chart(dry: dict[str, int], live: dict[str, int], path: Path) -> Path:
    """Grouped bars: decisions per number in the dry run vs the live run."""
    labels = [d for d in DECISION_ORDER if dry.get(d) or live.get(d)]
    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(8, 4))
    bars_dry = ax.bar(
        [i - width / 2 for i in x],
        [dry.get(d, 0) for d in labels],
        width,
        label="dry run",
        color=COLOURS["dry-run"],
    )
    bars_live = ax.bar(
        [i + width / 2 for i in x],
        [live.get(d, 0) for d in labels],
        width,
        label="live run",
        color=COLOURS["live"],
    )
    for bars in (bars_dry, bars_live):
        ax.bar_label(bars, padding=2, fontsize=9)
    ax.set_xticks(list(x), labels, fontsize=9)
    ax.set_ylabel("numbers")
    ax.set_title("Decision per number (synthetic data, seed 42)")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def _attempt_class(record: dict) -> str:
    status = record.get("status_code")
    if record.get("error") == "timeout":
        return "timeout"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "server_error"
    if status is not None and 200 <= status < 300:
        return "ok"
    return "client_error"


def timeline_chart(audit_log: Path, path: Path) -> Path:
    """Every HTTP attempt of a run on one time axis - one lane, no overlaps.

    If the runner ever had two requests in flight, two bars would overlap on
    the single lane; the second panel plots the in-flight count directly.
    """
    records = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    records.sort(key=lambda r: r["started_at"])
    t0 = datetime.fromisoformat(records[0]["started_at"])

    def secs(value: str) -> float:
        return (datetime.fromisoformat(value) - t0).total_seconds()

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(10, 3.6), sharex=True, gridspec_kw={"height_ratios": [1, 1.3]}
    )
    for rec in records:
        start, end = secs(rec["started_at"]), secs(rec["finished_at"])
        cls = _attempt_class(rec)
        ax.broken_barh([(start, max(end - start, 0.004))], (0, 1), color=COLOURS[cls])
    ax.set_yticks([])
    ax.set_title("Every HTTP attempt of the live run on one lane (synthetic data)", loc="left")
    handles = [
        Patch(color=COLOURS[k], label=k.replace("_", " "))
        for k in ("ok", "client_error", "server_error", "timeout", "rate_limited")
        if any(_attempt_class(r) == k for r in records)
    ]
    ax.legend(
        handles=handles,
        ncol=len(handles),
        frameon=False,
        fontsize=8,
        loc="lower right",
        bbox_to_anchor=(1, 1.0),
    )
    ax.spines[["top", "right", "left"]].set_visible(False)

    events: list[tuple[float, int]] = []
    for rec in records:
        events.append((secs(rec["started_at"]), 1))
        events.append((secs(rec["finished_at"]), -1))
    events.sort(key=lambda e: (e[0], e[1]))
    xs, ys, level = [0.0], [0], 0
    for t, delta in events:
        level += delta
        xs.append(t)
        ys.append(level)
    ax2.step(xs, ys, where="post", color=COLOURS["live"], linewidth=0.8)
    ax2.set_ylim(-0.1, 2.1)
    ax2.set_yticks([0, 1, 2])
    ax2.axhline(1, color="#999999", linestyle=":", linewidth=0.8)
    ax2.set_ylabel("in flight")
    ax2.set_xlabel("seconds since first request")
    ax2.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
