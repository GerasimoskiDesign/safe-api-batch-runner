"""Seeded generator for a realistic, messy input file.

Real input lists are collected by hand from tickets, so the sample mixes
notations (spaces, dashes, ``00`` prefix, missing ``+``), adds repeats,
comments, blank lines and a few lines that are not numbers at all. Every
number uses the unassigned ``+999`` prefix.
"""

from __future__ import annotations

import random
from pathlib import Path

from batchrunner.numbers import PREFIX

INVALID_SAMPLES = [
    "+999 12 34",  # too short
    "+99970123456789",  # too long
    "call back tomorrow",  # free text pasted from a ticket
]


def _format_variant(msisdn: str, rng: random.Random) -> str:
    """Write the same number the way different agents type it."""
    digits = msisdn[len(PREFIX) :]
    style = rng.randrange(5)
    if style == 0:
        return f"{PREFIX} {digits[:2]} {digits[2:5]} {digits[5:]}"
    if style == 1:
        return f"00999-{digits[:4]}-{digits[4:]}"
    if style == 2:
        return f"999{digits}"
    return msisdn


def generate_lines(count: int = 45, duplicates: int = 4, seed: int = 42) -> list[str]:
    """Return input lines with ``count`` unique valid numbers plus noise."""
    rng = random.Random(seed)
    numbers: list[str] = []
    seen: set[str] = set()
    while len(numbers) < count:
        candidate = f"{PREFIX}7{rng.randrange(10_000_000):07d}"
        if candidate not in seen:
            seen.add(candidate)
            numbers.append(candidate)

    lines = [f"{_format_variant(n, rng)}" for n in numbers]
    for dup in rng.sample(numbers, k=min(duplicates, len(numbers))):
        lines.insert(rng.randrange(len(lines) + 1), _format_variant(dup, rng))
    for bad in INVALID_SAMPLES:
        lines.insert(rng.randrange(len(lines) + 1), bad)
    for _ in range(3):
        lines.insert(rng.randrange(len(lines) + 1), "")
    # An inline comment, as agents sometimes annotate lines.
    lines[rng.randrange(len(lines))] += "  # customer called twice"
    header = [
        "# Numbers from open tickets: perks missing after plan change.",
        f"# Synthetic sample (seed={seed}). All numbers use the unassigned {PREFIX} prefix.",
        "",
    ]
    return header + lines


def write_numbers_file(path: Path, count: int = 45, duplicates: int = 4, seed: int = 42) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = generate_lines(count=count, duplicates=duplicates, seed=seed)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path
