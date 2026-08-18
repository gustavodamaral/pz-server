from __future__ import annotations

import logging
import os
import platform
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import PlayerObservation, ShutdownOutcome
from .rcon import RconAdmin
from .service import ServiceManager

LOGGER = logging.getLogger(__name__)
GUARD_CONTENT = "PZ_HOST_SHUTDOWN=ENABLED"


class HostPowerController(Protocol):
    def eligible(self) -> bool: ...

    def shutdown(self) -> bool: ...


class PlayerCounter(Protocol):
    def query(self) -> PlayerObservation: ...


@dataclass(slots=True)
class SystemdPowerController:
    deployment_environment: str
    enabled: bool
    guard_file: Path

    def eligible(self) -> bool:
        if os.name == "nt" or platform.system() != "Linux":
            return False
        if self.deployment_environment != "aws" or not self.enabled:
            return False
        try:
            metadata = self.guard_file.lstat()
            content = self.guard_file.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == 0
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and content == GUARD_CONTENT
        )

    def shutdown(self) -> bool:
        if not self.eligible():
            LOGGER.error("Host shutdown policy is not eligible; refusing to power off")
            return False
        try:
            result = subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--no-block",
                    "poweroff",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            LOGGER.error("Could not schedule host shutdown: %s", type(error).__name__)
            return False
        if result.returncode != 0:
            LOGGER.error("Could not schedule host shutdown: %s", result.stderr.strip())
            return False
        LOGGER.info("Linux host poweroff initiated; EC2 is configured to enter stopped state")
        return True


@dataclass(slots=True)
class ShutdownCoordinator:
    players: PlayerCounter
    admin: RconAdmin
    service: ServiceManager
    host: HostPowerController
    service_stop_timeout_seconds: int = 120
    save_settle_seconds: float = 10
    sleeper: Callable[[float], None] = time.sleep

    def execute(self, dry_run: bool, allow_connected_players: bool = False) -> ShutdownOutcome:
        if dry_run:
            LOGGER.warning("[DRY RUN] Server has been empty for the configured idle timeout.")
            LOGGER.warning(
                "[DRY RUN] Would gracefully stop Project Zomboid and shut down EC2 host."
            )
            return ShutdownOutcome.DRY_RUN

        if not self.host.eligible():
            LOGGER.error("Production shutdown eligibility check failed; no side effects performed")
            return ShutdownOutcome.ABORTED
        if not self._players_allow_shutdown(allow_connected_players, "before save"):
            return ShutdownOutcome.ABORTED
        if not self.admin.save():
            return ShutdownOutcome.ABORTED
        self.sleeper(self.save_settle_seconds)
        if not self._players_allow_shutdown(allow_connected_players, "after save"):
            return ShutdownOutcome.ABORTED
        if not self.service.graceful_stop(self.service_stop_timeout_seconds):
            LOGGER.error("Graceful Project Zomboid stop failed; host shutdown aborted")
            return ShutdownOutcome.ABORTED
        if not self.service.is_stopped():
            LOGGER.error("Project Zomboid stop could not be verified; host shutdown aborted")
            return ShutdownOutcome.ABORTED
        if self.host.shutdown():
            return ShutdownOutcome.COMPLETED

        LOGGER.critical("Host shutdown scheduling failed after PZ stopped; attempting recovery")
        if not self.service.start():
            LOGGER.critical(
                "Project Zomboid recovery start also failed; manual intervention required"
            )
        return ShutdownOutcome.ABORTED

    def _players_allow_shutdown(self, allow_connected_players: bool, phase: str) -> bool:
        observation = self.players.query()
        if observation.is_unknown:
            LOGGER.error("Player state is UNKNOWN %s; shutdown aborted", phase)
            return False
        if observation.count and not allow_connected_players:
            LOGGER.warning("%d player(s) connected %s; shutdown aborted", observation.count, phase)
            return False
        return True
