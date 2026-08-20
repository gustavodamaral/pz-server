from __future__ import annotations

import json
import socket
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from pz_watchdog.models import ServiceHealth
from pz_watchdog.service import DockerComposeService, TcpOnlyService


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def service() -> DockerComposeService:
    return DockerComposeService(Path("/project"), sleeper=lambda _: None)


def docker_state_run(state: dict[str, object]) -> object:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            return completed(stdout=json.dumps(state))
        return completed(stdout="container-id\n")

    return fake_run


def test_compose_command_includes_optional_env_file() -> None:
    target = DockerComposeService(Path("/project"), env_file=Path("/run/secrets.env"))
    assert target.compose_command[-2:] == ["--env-file", str(Path("/run/secrets.env"))]


def test_multiple_container_ids_are_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: completed(stdout="first\nsecond\n")
    )
    assert service().health() is ServiceHealth.UNKNOWN


def test_failed_compose_lookup_is_unknown_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed(1, stderr="failed"))
    target = service()
    assert target.health() is ServiceHealth.UNKNOWN
    assert target.is_stopped() is False


def test_confirmed_absent_container_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed())
    target = service()
    assert target.health() is ServiceHealth.STOPPED
    assert target.is_stopped() is True
    assert target.graceful_stop(120) is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"Running": True, "Health": {"Status": "healthy"}}, ServiceHealth.HEALTHY),
        ({"Running": True, "Health": {"Status": "unhealthy"}}, ServiceHealth.UNHEALTHY),
        ({"Running": True}, ServiceHealth.UNKNOWN),
        ({"Running": False}, ServiceHealth.STOPPED),
    ],
)
def test_health_maps_structured_docker_state(
    state: dict[str, object], expected: ServiceHealth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", docker_state_run(state))
    assert service().health() is expected


@pytest.mark.parametrize("inspection_output", ["not-json", "[]"])
def test_invalid_inspection_output_is_unknown(
    inspection_output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            return completed(stdout=inspection_output)
        return completed(stdout="container-id\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert service().health() is ServiceHealth.UNKNOWN


def test_wait_until_healthy_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerComposeService, "health", lambda self: ServiceHealth.HEALTHY)
    assert service().wait_until_healthy(1) is True
    monkeypatch.setattr(DockerComposeService, "health", lambda self: ServiceHealth.UNKNOWN)
    assert service().wait_until_healthy(-1) is False


def test_restart_and_start_require_command_success_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed())
    monkeypatch.setattr(DockerComposeService, "wait_until_healthy", lambda self: True)
    target = service()
    assert target.restart() is True
    assert target.start() is True

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed(1, stderr="failed"))
    assert target.restart() is False
    assert target.start() is False


@pytest.mark.parametrize(
    "state",
    [
        {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": True,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": False,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": 137,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": True,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "created",
            "Running": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": False,
            "Dead": True,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "dead",
            "Running": False,
            "Restarting": False,
            "Dead": True,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
        },
        {
            "Status": "exited",
            "Running": False,
            "Restarting": False,
            "Dead": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "daemon failure",
        },
    ],
)
def test_graceful_stop_requires_clean_exit_of_original_container(
    state: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            return completed(stdout=json.dumps(state))
        if "ps" in command:
            return completed(stdout="original-container\n")
        return completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert service().graceful_stop(120) is False


def test_graceful_stop_accepts_verified_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {
        "Status": "exited",
        "Running": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
    }

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            assert command[-1] == "original-container"
            return completed(stdout=json.dumps(state))
        if "ps" in command:
            return completed(stdout="original-container\n")
        return completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = service()
    assert target.graceful_stop(120) is True
    assert target.is_stopped() is True


@pytest.mark.parametrize("current_lookup", ["replacement-container\n", ""])
def test_verified_stop_rejects_changed_or_missing_container(
    current_lookup: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {
        "Status": "exited",
        "Running": False,
        "Restarting": False,
        "Dead": False,
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
    }
    lookups = iter(("original-container\n", "original-container\n", current_lookup))

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            return completed(stdout=json.dumps(state))
        if "ps" in command:
            return completed(stdout=next(lookups))
        return completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = service()
    assert target.graceful_stop(120) is True
    assert target.is_stopped() is False


def test_graceful_stop_fails_when_post_stop_inspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "inspect"]:
            return completed(1, stderr="daemon unavailable")
        if "ps" in command:
            return completed(stdout="original-container\n")
        return completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert service().graceful_stop(120) is False


def test_tcp_only_service_is_observational_and_never_controls_host_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext())
    target = TcpOnlyService("localhost", 27015)
    assert target.health() is ServiceHealth.HEALTHY
    assert target.is_stopped() is False
    assert target.restart() is False
    assert target.graceful_stop(120) is False
    assert target.start() is False

    def refused(*args: object, **kwargs: object) -> object:
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", refused)
    assert target.health() is ServiceHealth.UNHEALTHY
    assert target.is_stopped() is True
