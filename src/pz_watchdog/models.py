from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class PlayerObservation:
    """A confirmed player count or an explicitly unknown result."""

    count: int | None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.count is not None and self.count < 0:
            raise ValueError("Player count cannot be negative")

    @property
    def is_unknown(self) -> bool:
        return self.count is None

    @classmethod
    def known(cls, count: int) -> PlayerObservation:
        return cls(count=count)

    @classmethod
    def unknown(cls, detail: str) -> PlayerObservation:
        return cls(count=None, detail=detail)


class ServiceHealth(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class ShutdownOutcome(StrEnum):
    COMPLETED = "completed"
    DRY_RUN = "dry-run"
    ABORTED = "aborted"
