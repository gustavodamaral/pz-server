from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .models import PlayerObservation, ServiceHealth, ShutdownOutcome
from .service import ServiceManager

LOGGER = logging.getLogger(__name__)


class PlayerCounter(Protocol):
    def query(self) -> PlayerObservation: ...


class ShutdownAction(Protocol):
    def execute(self, dry_run: bool) -> ShutdownOutcome: ...


@dataclass(frozen=True, slots=True)
class WatchdogPolicy:
    idle_timeout_seconds: float = 45 * 60
    health_failure_timeout_seconds: float = 12 * 60
    final_check_count: int = 3
    final_check_delay_seconds: float = 5
    progress_interval_seconds: float = 5 * 60
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.idle_timeout_seconds <= 0 or self.health_failure_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive")
        if self.final_check_count < 2 or self.final_check_delay_seconds <= 0:
            raise ValueError("Final validation requires at least two delayed checks")


@dataclass(slots=True)
class WatchdogState:
    empty_since: float | None = None
    management_failure_since: float | None = None
    last_progress_at: float | None = None
    dry_run_reported: bool = False


@dataclass(slots=True)
class WatchdogEngine:
    players: PlayerCounter
    service: ServiceManager
    shutdown: ShutdownAction
    policy: WatchdogPolicy = field(default_factory=WatchdogPolicy)
    sleeper: Callable[[float], None] = time.sleep
    state: WatchdogState = field(default_factory=WatchdogState)

    def tick(self, now: float) -> None:
        observation = self.players.query()
        if observation.is_unknown:
            self._handle_unknown(now, observation.detail)
            return

        self.state.management_failure_since = None
        if observation.count and observation.count > 0:
            if self.state.empty_since is not None:
                LOGGER.info("Player connected; idle timer reset")
            LOGGER.info("Players: %d", observation.count)
            self._reset_empty()
            return

        self._handle_empty(now)

    def _handle_empty(self, now: float) -> None:
        if self.state.empty_since is None:
            self.state.empty_since = now
            self.state.last_progress_at = now
            self.state.dry_run_reported = False
            LOGGER.info("Players: 0 - idle timer started")
            return

        elapsed = max(0.0, now - self.state.empty_since)
        if (
            self.state.last_progress_at is None
            or now - self.state.last_progress_at >= self.policy.progress_interval_seconds
        ):
            LOGGER.info(
                "Players: 0 - idle %.0f/%.0f min",
                elapsed / 60,
                self.policy.idle_timeout_seconds / 60,
            )
            self.state.last_progress_at = now

        if elapsed < self.policy.idle_timeout_seconds or self.state.dry_run_reported:
            return
        self._final_shutdown_validation()

    def _handle_unknown(self, now: float, detail: str) -> None:
        if self.state.empty_since is not None:
            LOGGER.warning("Player state UNKNOWN - idle timer reset and shutdown prohibited")
        else:
            LOGGER.warning("Player state UNKNOWN - shutdown prohibited: %s", detail)
        self._reset_empty()

        health = self.service.health()
        if health not in {ServiceHealth.UNHEALTHY, ServiceHealth.STOPPED}:
            self.state.management_failure_since = None
            LOGGER.warning("Secondary service health is %s; no restart is justified", health.value)
            return
        if self.state.management_failure_since is None:
            self.state.management_failure_since = now
            LOGGER.warning("RCON and local service health are unavailable; outage timer started")
            return

        elapsed = now - self.state.management_failure_since
        if elapsed < self.policy.health_failure_timeout_seconds:
            LOGGER.warning(
                "Management outage persists %.0f/%.0f min",
                elapsed / 60,
                self.policy.health_failure_timeout_seconds / 60,
            )
            return

        if not self.service.restart():
            LOGGER.error("Service restart attempt failed; host remains online")
        self.state.management_failure_since = None
        self._reset_empty()

    def _final_shutdown_validation(self) -> None:
        LOGGER.info("Final shutdown validation started")
        for check in range(2, self.policy.final_check_count + 1):
            self.sleeper(self.policy.final_check_delay_seconds)
            observation = self.players.query()
            if observation.is_unknown:
                LOGGER.warning("Shutdown aborted - final check %d returned UNKNOWN", check)
                self._reset_empty()
                return
            if observation.count and observation.count > 0:
                LOGGER.warning("Shutdown aborted - player detected during final checks")
                self._reset_empty()
                return
            LOGGER.info("Final empty check %d/%d confirmed", check, self.policy.final_check_count)

        outcome = self.shutdown.execute(dry_run=self.policy.dry_run)
        if outcome is ShutdownOutcome.DRY_RUN:
            self.state.dry_run_reported = True
            return
        self._reset_empty()
        if outcome is ShutdownOutcome.ABORTED:
            LOGGER.warning("Shutdown sequence aborted; a fresh idle interval is required")

    def _reset_empty(self) -> None:
        self.state.empty_since = None
        self.state.last_progress_at = None
        self.state.dry_run_reported = False
