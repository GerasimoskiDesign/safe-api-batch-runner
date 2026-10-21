"""Keep subscriber numbers and credentials out of logs.

Logs get copied into tickets, chat threads and log aggregators that far more
people can read than the batch output itself. So logs only ever show the last
three digits of a number (enough to match a line to a ticket) and never the
API token. Masking is done twice on purpose: explicitly at every call site and
again in the log formatter as a safety net for messages nobody thought about,
such as exception text from the HTTP library that contains a full URL.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

VISIBLE_DIGITS = 3
REDACTED = "***REDACTED***"

# Any run of 7+ digits (optionally with a leading "+") is treated as a possible
# subscriber number. Ports, years and durations are shorter and stay readable.
# The URL-encoded form "%2B999..." is handled without eating the escape itself.
_NUMBER_IN_TEXT = re.compile(r"(?<![\w%])(%2B|\+)?(\d{7,})(?!\d)", re.IGNORECASE)


def mask_msisdn(msisdn: str) -> str:
    """Replace every digit except the last three with ``*``.

    >>> mask_msisdn("+99970123456")
    '+********456'
    """
    total_digits = sum(ch.isdigit() for ch in msisdn)
    keep_from = total_digits - VISIBLE_DIGITS
    seen = 0
    out = []
    for ch in msisdn:
        if ch.isdigit():
            out.append(ch if seen >= keep_from else "*")
            seen += 1
        else:
            out.append(ch)
    return "".join(out)


def _mask_match(match: re.Match[str]) -> str:
    prefix = match.group(1) or ""
    return prefix + mask_msisdn(match.group(2))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove secrets and mask number-like digit runs in arbitrary text."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return _NUMBER_IN_TEXT.sub(_mask_match, text)


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the *fully formatted* record, tracebacks included.

    A ``logging.Filter`` only sees the message template and args; exception
    text is added later by the formatter, so redaction has to happen here.
    """

    def __init__(self, fmt: str, secrets: Iterable[str] = (), datefmt: str | None = None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)
