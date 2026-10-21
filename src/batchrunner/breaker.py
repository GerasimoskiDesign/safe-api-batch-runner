"""Stop the whole run when the backend is clearly unhealthy.

Retries handle a single flaky call. When several numbers *in a row* fail even
after their retries, the problem is the backend, not the numbers - and every
further call just adds load to a system that is already struggling. The
breaker turns "keep hammering until the list is done" into "stop, say why,
and leave the rest of the list for a later run".

There is no half-open state as in a long-running service: a batch run that
trips simply ends, and the operator re-runs the leftover numbers once the
backend has recovered.
"""

from __future__ import annotations


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; any success resets it."""

    def __init__(self, threshold: int = 3):
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self.consecutive_failures = 0
        self.last_failure_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.consecutive_failures >= self.threshold

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_failure_reason = None

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_failure_reason = reason

    def describe(self) -> str:
        return (
            f"circuit breaker opened after {self.consecutive_failures} consecutive failed "
            f"numbers (last error: {self.last_failure_reason})"
        )
