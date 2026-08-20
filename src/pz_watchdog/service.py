from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import ServiceHealth

LOGGER = logging.getLogger(__name__)


class ServiceInspectionError(RuntimeError):
    """Docker state could not be established safely."""


class ServiceManager(Protocol):
    def health(self) -> ServiceHealth: ...

    def restart(self) -> bool: ...

    def graceful_stop(self, timeout_seconds: int) -> bool: ...

    def is_stopped(self) -> bool: ...

    def start(self) -> bool: ...


@dataclass(slots=True)
class DockerComposeService:
    project_directory: Path
    service: str = "server"
    env_file: Path | None = None
    ready_timeout_seconds: int = 900
    sleeper: Callable[[float], None] = time.sleep
    _verified_stopped_identifier: str | None = field(default=None, init=False, repr=False)

    @property
    def compose_command(self) -> list[str]:
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.project_directory),
            "--file",
            str(self.project_directory / "compose.yaml"),
        ]
        if self.env_file is not None:
            command.extend(("--env-file", str(self.env_file)))
        return command

    def _run(self, arguments: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*self.compose_command, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            LOGGER.error("Docker Compose command failed: %s", type(error).__name__)
            return subprocess.CompletedProcess([], 1, "", str(error))

    def container_id(self, all_containers: bool = True) -> str | None:
        arguments = ["ps", "--quiet"]
        if all_containers:
            arguments.append("--all")
        arguments.append(self.service)
        result = self._run(arguments)
        if result.returncode != 0:
            LOGGER.error("Could not identify the Compose service container")
            raise ServiceInspectionError("Docker Compose container lookup failed")
        identifier = result.stdout.strip().splitlines()
        if len(identifier) > 1:
            raise ServiceInspectionError("Compose returned multiple service containers")
        return identifier[0] if identifier else None

    @staticmethod
    def inspect_container_state(identifier: str) -> dict[str, object]:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", identifier],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceInspectionError(
                f"Docker container inspection failed: {type(error).__name__}"
            ) from error
        if result.returncode != 0:
            raise ServiceInspectionError("Docker container inspection returned an error")
        try:
            state = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ServiceInspectionError("Docker returned invalid container state JSON") from error
        if not isinstance(state, dict):
            raise ServiceInspectionError("Docker returned an unexpected container state")
        return state

    def inspect_state(self) -> dict[str, object] | None:
        identifier = self.container_id()
        return None if identifier is None else self.inspect_container_state(identifier)

    def health(self) -> ServiceHealth:
        try:
            state = self.inspect_state()
        except ServiceInspectionError as error:
            LOGGER.error("Service health is unknown: %s", error)
            return ServiceHealth.UNKNOWN
        if state is None:
            return ServiceHealth.STOPPED
        if state.get("Running") is not True:
            return ServiceHealth.STOPPED
        health = state.get("Health")
        if not isinstance(health, dict):
            return ServiceHealth.UNKNOWN
        status = health.get("Status")
        if status == "healthy":
            return ServiceHealth.HEALTHY
        if status == "unhealthy":
            return ServiceHealth.UNHEALTHY
        return ServiceHealth.UNKNOWN

    def wait_until_healthy(self, timeout_seconds: int | None = None) -> bool:
        deadline = time.monotonic() + (timeout_seconds or self.ready_timeout_seconds)
        while time.monotonic() < deadline:
            if self.health() is ServiceHealth.HEALTHY:
                return True
            self.sleeper(5)
        return False

    def restart(self) -> bool:
        LOGGER.warning("Restarting the Project Zomboid service after a sustained management outage")
        result = self._run(("restart", "--timeout", "120", self.service), timeout=180)
        if result.returncode != 0:
            LOGGER.error("Docker Compose restart failed: %s", result.stderr.strip())
            return False
        if not self.wait_until_healthy():
            LOGGER.error("Project Zomboid did not become healthy after restart")
            return False
        LOGGER.info("Project Zomboid recovered after service restart; idle state is fresh")
        return True

    def graceful_stop(self, timeout_seconds: int) -> bool:
        self._verified_stopped_identifier = None
        try:
            identifier = self.container_id()
        except ServiceInspectionError as error:
            LOGGER.error("Cannot safely stop an unidentified container: %s", error)
            return False
        if identifier is None:
            LOGGER.error("No Project Zomboid container exists to stop and verify")
            return False
        result = self._run(
            ("stop", "--timeout", str(timeout_seconds), self.service),
            timeout=timeout_seconds + 30,
        )
        if result.returncode != 0:
            LOGGER.error("Docker Compose graceful stop failed: %s", result.stderr.strip())
            return False
        try:
            state = self.inspect_container_state(identifier)
        except ServiceInspectionError as error:
            LOGGER.error("Could not verify the stopped container: %s", error)
            return False
        clean_exit = self._is_clean_exit(state)
        if not clean_exit:
            LOGGER.error("The exact Project Zomboid container did not exit cleanly")
            return False
        try:
            current_identifier = self.container_id()
        except ServiceInspectionError as error:
            LOGGER.error("Could not reverify the stopped container identity: %s", error)
            return False
        if current_identifier != identifier:
            LOGGER.error("The Project Zomboid container identity changed during shutdown")
            return False
        self._verified_stopped_identifier = identifier
        return True

    @staticmethod
    def _is_clean_exit(state: dict[str, object]) -> bool:
        return (
            state.get("Status") == "exited"
            and state.get("Running") is False
            and state.get("Restarting") is False
            and state.get("Dead") is False
            and state.get("OOMKilled") is False
            and type(state.get("ExitCode")) is int
            and state.get("ExitCode") == 0
            and state.get("Error") == ""
        )

    def is_stopped(self) -> bool:
        try:
            identifier = self.container_id()
        except ServiceInspectionError as error:
            LOGGER.error("Service stop state is unknown: %s", error)
            return False
        if self._verified_stopped_identifier is not None:
            if identifier != self._verified_stopped_identifier:
                LOGGER.error("The verified stopped container identity changed")
                return False
        elif identifier is None:
            return True
        if identifier is None:
            LOGGER.error("The verified stopped container disappeared")
            return False
        try:
            state = self.inspect_container_state(identifier)
        except ServiceInspectionError as error:
            LOGGER.error("Service stop state is unknown: %s", error)
            return False
        return self._is_clean_exit(state)

    def start(self) -> bool:
        result = self._run(("up", "--detach", self.service), timeout=180)
        return result.returncode == 0 and self.wait_until_healthy()


@dataclass(slots=True)
class TcpOnlyService:
    host: str
    port: int
    timeout_seconds: float = 3

    def health(self) -> ServiceHealth:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_seconds):
                return ServiceHealth.HEALTHY
        except OSError:
            return ServiceHealth.UNHEALTHY

    def restart(self) -> bool:
        LOGGER.error("Service restart is unavailable in tcp-only mode")
        return False

    def graceful_stop(self, timeout_seconds: int) -> bool:
        del timeout_seconds
        LOGGER.error("Service stop is unavailable in tcp-only mode")
        return False

    def is_stopped(self) -> bool:
        return self.health() is not ServiceHealth.HEALTHY

    def start(self) -> bool:
        LOGGER.error("Service start is unavailable in tcp-only mode")
        return False
