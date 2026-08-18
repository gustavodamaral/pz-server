from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import PlayerObservation

LOGGER = logging.getLogger(__name__)

ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PLAYER_HEADER_PATTERNS = (
    re.compile(r"Players\s+connected\s*\(\s*(\d+)\s*\)\s*:?\s*", re.IGNORECASE),
    re.compile(r"Players\s+connected\s*:\s*(\d+)", re.IGNORECASE),
)
SAVE_CONFIRMATION_PATTERN = re.compile(r"World\s+(?:is\s+)?saved", re.IGNORECASE)


class RconError(RuntimeError):
    """A safe, password-free RCON failure."""


class RconClient(Protocol):
    def run(self, *command: str) -> str: ...


@dataclass(slots=True)
class RconCliClient:
    command: Sequence[str]
    host: str
    port: int
    password: str
    timeout_seconds: int

    def run(self, *command: str) -> str:
        environment = os.environ.copy()
        environment["RCON_PASSWORD"] = self.password
        arguments = [*self.command, "--host", self.host, "--port", str(self.port), *command]
        try:
            result = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RconError(f"RCON command could not complete: {type(error).__name__}") from error
        if result.returncode != 0:
            raise RconError(f"rcon-cli exited with status {result.returncode}")
        if result.stderr.strip():
            raise RconError("rcon-cli reported an error despite a zero exit status")
        response = result.stdout.strip()
        if not response:
            raise RconError("rcon-cli returned an empty response")
        return response


def parse_player_count(response: str) -> int:
    lines = [
        line.strip() for line in ANSI_ESCAPE_PATTERN.sub("", response).splitlines() if line.strip()
    ]
    if not lines:
        raise RconError("RCON players response was empty")
    for pattern in PLAYER_HEADER_PATTERNS:
        if match := pattern.fullmatch(lines[0]):
            count = int(match.group(1))
            if len(lines[1:]) != count or any(not line.startswith("-") for line in lines[1:]):
                break
            return count
    raise RconError("RCON players response did not contain a recognized player count")


@dataclass(slots=True)
class RetryingPlayerCounter:
    client: RconClient
    attempts: int = 3
    retry_delay_seconds: float = 5
    sleeper: Callable[[float], None] = time.sleep

    def query(self) -> PlayerObservation:
        last_error = "RCON query failed"
        for attempt in range(1, self.attempts + 1):
            try:
                count = parse_player_count(self.client.run("players"))
            except (RconError, OSError, ValueError) as error:
                last_error = str(error)
                LOGGER.warning("RCON attempt %d/%d failed: %s", attempt, self.attempts, error)
                if attempt < self.attempts:
                    self.sleeper(self.retry_delay_seconds)
                continue
            return PlayerObservation.known(count)
        LOGGER.error("RCON state UNKNOWN after %d attempts; shutdown is prohibited", self.attempts)
        return PlayerObservation.unknown(last_error)


@dataclass(slots=True)
class RconAdmin:
    client: RconClient
    attempts: int = 3
    retry_delay_seconds: float = 5
    sleeper: Callable[[float], None] = time.sleep

    def save(self) -> bool:
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.run("save")
                normalized = ANSI_ESCAPE_PATTERN.sub("", response).strip()
                if SAVE_CONFIRMATION_PATTERN.fullmatch(normalized) is None:
                    raise RconError("RCON save response was not a recognized confirmation")
            except (RconError, OSError) as error:
                LOGGER.warning("RCON save attempt %d/%d failed: %s", attempt, self.attempts, error)
                if attempt < self.attempts:
                    self.sleeper(self.retry_delay_seconds)
                continue
            LOGGER.info("Project Zomboid accepted the world save command")
            return True
        LOGGER.error("World save could not be confirmed; graceful shutdown aborted")
        return False
