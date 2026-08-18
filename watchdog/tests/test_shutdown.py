from __future__ import annotations

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from pz_watchdog.models import PlayerObservation, ServiceHealth, ShutdownOutcome
from pz_watchdog.shutdown import (
    GUARD_CONTENT,
    ShutdownCoordinator,
    SystemdPowerController,
)


@dataclass
class FakeAdmin:
    result: bool = True
    calls: int = 0

    def save(self) -> bool:
        self.calls += 1
        return self.result


@dataclass
class FakeService:
    stop_result: bool = True
    stopped: bool = True
    start_result: bool = True
    stop_calls: int = 0
    start_calls: int = 0

    def health(self) -> ServiceHealth:
        return ServiceHealth.HEALTHY

    def restart(self) -> bool:
        return True

    def graceful_stop(self, timeout_seconds: int) -> bool:
        assert timeout_seconds == 120
        self.stop_calls += 1
        return self.stop_result

    def is_stopped(self) -> bool:
        return self.stopped

    def start(self) -> bool:
        self.start_calls += 1
        return self.start_result


@dataclass
class FakeHost:
    eligible_result: bool = True
    shutdown_result: bool = True
    shutdown_calls: int = 0

    def eligible(self) -> bool:
        return self.eligible_result

    def shutdown(self) -> bool:
        self.shutdown_calls += 1
        return self.shutdown_result


@dataclass
class FakePlayers:
    observations: list[PlayerObservation]
    calls: int = 0

    def query(self) -> PlayerObservation:
        self.calls += 1
        return self.observations.pop(0)


def coordinator(
    admin: FakeAdmin | None = None,
    service: FakeService | None = None,
    host: FakeHost | None = None,
    players: FakePlayers | None = None,
) -> tuple[ShutdownCoordinator, FakeAdmin, FakeService, FakeHost, FakePlayers, list[float]]:
    fake_admin = admin or FakeAdmin()
    fake_service = service or FakeService()
    fake_host = host or FakeHost()
    fake_players = players or FakePlayers([PlayerObservation.known(0), PlayerObservation.known(0)])
    sleeps: list[float] = []
    result = ShutdownCoordinator(
        players=fake_players,
        admin=fake_admin,  # type: ignore[arg-type]
        service=fake_service,
        host=fake_host,
        service_stop_timeout_seconds=120,
        save_settle_seconds=10,
        sleeper=sleeps.append,
    )
    return result, fake_admin, fake_service, fake_host, fake_players, sleeps


def test_dry_run_has_no_side_effects() -> None:
    action, admin, service, host, players, sleeps = coordinator()
    assert action.execute(dry_run=True) is ShutdownOutcome.DRY_RUN
    assert admin.calls == service.stop_calls == host.shutdown_calls == 0
    assert sleeps == []
    assert players.calls == 0


def test_production_shutdown_eligibility_is_required() -> None:
    action, admin, service, host, players, _ = coordinator(host=FakeHost(eligible_result=False))
    assert action.execute(dry_run=False) is ShutdownOutcome.ABORTED
    assert admin.calls == service.stop_calls == host.shutdown_calls == 0
    assert players.calls == 0


def test_save_failure_aborts_before_stop() -> None:
    action, admin, service, host, _, _ = coordinator(admin=FakeAdmin(result=False))
    assert action.execute(False) is ShutdownOutcome.ABORTED
    assert admin.calls == 1
    assert service.stop_calls == host.shutdown_calls == 0


def test_graceful_stop_failure_never_shuts_host_down() -> None:
    action, _, service, host, _, sleeps = coordinator(service=FakeService(stop_result=False))
    assert action.execute(False) is ShutdownOutcome.ABORTED
    assert service.stop_calls == 1
    assert host.shutdown_calls == 0
    assert sleeps == [10]


def test_unverified_stop_never_shuts_host_down() -> None:
    action, _, service, host, _, _ = coordinator(service=FakeService(stopped=False))
    assert action.execute(False) is ShutdownOutcome.ABORTED
    assert service.stop_calls == 1
    assert host.shutdown_calls == 0


def test_production_shutdown_completes_in_order() -> None:
    action, admin, service, host, players, sleeps = coordinator()
    assert action.execute(False) is ShutdownOutcome.COMPLETED
    assert admin.calls == service.stop_calls == host.shutdown_calls == 1
    assert sleeps == [10]
    assert players.calls == 2


def test_host_shutdown_failure_attempts_service_recovery() -> None:
    action, _, service, host, _, _ = coordinator(host=FakeHost(shutdown_result=False))
    assert action.execute(False) is ShutdownOutcome.ABORTED
    assert host.shutdown_calls == 1
    assert service.start_calls == 1


@pytest.mark.parametrize(
    "observations",
    [
        [PlayerObservation.unknown("rcon failed")],
        [PlayerObservation.known(1)],
        [PlayerObservation.known(0), PlayerObservation.unknown("rcon failed")],
        [PlayerObservation.known(0), PlayerObservation.known(1)],
    ],
)
def test_player_uncertainty_or_connection_aborts_shutdown(
    observations: list[PlayerObservation],
) -> None:
    rejected_before_save = observations[0].is_unknown or bool(observations[0].count)
    players = FakePlayers(observations)
    action, admin, service, host, _, _ = coordinator(players=players)
    assert action.execute(False) is ShutdownOutcome.ABORTED
    assert service.stop_calls == host.shutdown_calls == 0
    assert admin.calls == (0 if rejected_before_save else 1)


def test_explicit_override_allows_connected_players() -> None:
    players = FakePlayers([PlayerObservation.known(2), PlayerObservation.known(2)])
    action, _, service, host, _, _ = coordinator(players=players)
    assert action.execute(False, allow_connected_players=True) is ShutdownOutcome.COMPLETED
    assert service.stop_calls == host.shutdown_calls == 1


def test_systemd_policy_requires_linux_aws_enable_and_exact_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = tmp_path / "guard"
    guard.write_text(GUARD_CONTENT, encoding="utf-8")
    guard.chmod(0o600)
    guard_mode = [0o600]
    monkeypatch.setattr("pz_watchdog.shutdown.os.name", "posix")
    monkeypatch.setattr("pz_watchdog.shutdown.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFREG | guard_mode[0], st_uid=0),
    )
    assert SystemdPowerController("aws", True, guard).eligible() is True
    assert SystemdPowerController("local", True, guard).eligible() is False
    assert SystemdPowerController("aws", False, guard).eligible() is False
    guard.write_text("wrong", encoding="utf-8")
    assert SystemdPowerController("aws", True, guard).eligible() is False
    guard.write_text(GUARD_CONTENT, encoding="utf-8")
    guard_mode[0] = 0o644
    assert SystemdPowerController("aws", True, guard).eligible() is False
    assert SystemdPowerController("aws", True, tmp_path / "missing").eligible() is False


def test_windows_is_never_eligible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = tmp_path / "guard"
    guard.write_text(GUARD_CONTENT, encoding="utf-8")
    guard.chmod(0o600)
    monkeypatch.setattr("pz_watchdog.shutdown.os.name", "nt")
    assert SystemdPowerController("aws", True, guard).eligible() is False


def test_systemd_shutdown_initiates_poweroff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = tmp_path / "guard"
    guard.write_text(GUARD_CONTENT, encoding="utf-8")
    guard.chmod(0o600)
    monkeypatch.setattr("pz_watchdog.shutdown.os.name", "posix")
    monkeypatch.setattr("pz_watchdog.shutdown.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0),
    )

    def successful_poweroff(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["/usr/bin/systemctl", "--no-block", "poweroff"]
        return subprocess.CompletedProcess([], 0, "accepted", "")

    monkeypatch.setattr(subprocess, "run", successful_poweroff)
    assert SystemdPowerController("aws", True, guard).shutdown() is True


def test_systemd_shutdown_command_failure_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = tmp_path / "guard"
    guard.write_text(GUARD_CONTENT, encoding="utf-8")
    guard.chmod(0o600)
    monkeypatch.setattr("pz_watchdog.shutdown.os.name", "posix")
    monkeypatch.setattr("pz_watchdog.shutdown.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    assert SystemdPowerController("aws", True, guard).shutdown() is False


def test_ineligible_systemd_shutdown_does_not_run_command(tmp_path: Path) -> None:
    assert SystemdPowerController("local", False, tmp_path / "missing").shutdown() is False
