"""Parse the operator's input file into a clean, de-duplicated list of numbers.

Input files are pasted together by hand from support tickets, so they contain
blank lines, comments, numbers in several notations and the same number twice.
The parser is deliberately strict about what it accepts: a number that cannot
be normalised to the canonical form is reported back with its line number and
never sent to the API, because a malformed identifier against a live backend
is at best a wasted call and at worst a write to the wrong subscriber.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Synthetic numbering plan: the fake country prefix +999 followed by 8 digits.
# +999 is not assigned to any country, so test data can never hit a real line.
PREFIX = "+999"
SUBSCRIBER_DIGITS = 8
MSISDN_PATTERN = re.compile(r"^\+999\d{8}$")

# Characters people commonly use to format numbers. Removing them is safe;
# anything else (letters, '*') makes the line invalid.
_SEPARATORS = re.compile(r"[\s\-./()]")


@dataclass(frozen=True)
class InvalidLine:
    """A non-empty, non-comment input line that is not a valid number."""

    line_no: int
    raw: str
    reason: str


@dataclass(frozen=True)
class DuplicateLine:
    """A valid number that already appeared earlier in the file."""

    line_no: int
    msisdn: str
    first_line_no: int


@dataclass
class ParsedInput:
    """Result of parsing an input file. ``numbers`` keeps first-seen order."""

    numbers: list[str] = field(default_factory=list)
    invalid: list[InvalidLine] = field(default_factory=list)
    duplicates: list[DuplicateLine] = field(default_factory=list)
    ignored_lines: int = 0
    total_lines: int = 0


def normalise(raw: str) -> str:
    """Bring a hand-typed number into canonical ``+999XXXXXXXX`` form.

    Accepts the international ``00`` prefix and a missing ``+`` so that the
    same subscriber written three different ways de-duplicates to one entry.
    The result is *not* guaranteed valid; call :func:`validation_error` on it.
    """
    value = _SEPARATORS.sub("", raw.strip())
    if value.startswith("00"):
        value = "+" + value[2:]
    elif value.startswith("999"):
        value = "+" + value
    return value


def validation_error(value: str) -> str | None:
    """Return a human-readable reason why ``value`` is invalid, or ``None``."""
    if MSISDN_PATTERN.match(value):
        return None
    if not re.fullmatch(r"\+?\d+", value):
        return "contains characters other than digits and separators"
    if not value.startswith(PREFIX):
        return f"wrong prefix (expected {PREFIX})"
    return f"wrong length (expected {SUBSCRIBER_DIGITS} digits after {PREFIX})"


def _strip_comment(line: str) -> str:
    """Drop everything after ``#`` so lines like ``+999... # VIP`` still parse."""
    return line.split("#", 1)[0].strip()


def parse_lines(lines: Iterable[str]) -> ParsedInput:
    """Parse raw lines: skip blanks/comments, normalise, validate, de-duplicate."""
    parsed = ParsedInput()
    first_seen: dict[str, int] = {}
    for line_no, line in enumerate(lines, start=1):
        parsed.total_lines += 1
        content = _strip_comment(line)
        if not content:
            parsed.ignored_lines += 1
            continue
        value = normalise(content)
        reason = validation_error(value)
        if reason is not None:
            parsed.invalid.append(InvalidLine(line_no=line_no, raw=content, reason=reason))
            continue
        if value in first_seen:
            parsed.duplicates.append(
                DuplicateLine(line_no=line_no, msisdn=value, first_line_no=first_seen[value])
            )
            continue
        first_seen[value] = line_no
        parsed.numbers.append(value)
    return parsed


def parse_file(path: Path) -> ParsedInput:
    """Parse an input file. ``utf-8-sig`` strips the BOM that Windows editors add."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_lines(text.splitlines())
