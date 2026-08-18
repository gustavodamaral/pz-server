from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pz_watchdog.engine import WatchdogEngine, WatchdogPolicy
from pz_watchdog.models import PlayerObservation, ServiceHealth, ShutdownOutcome


@dataclass
class SequencePlayers:
    observations: list[PlayerObservation]
    calls: int = 0

    def query(self) -> PlayerObservation:
        self.calls += 1
        if not self.observations:
            raise AssertionError("No player observation was prepared")
        return self.observations.pop(0)


@dataclass
class FakeService:
    health_value: ServiceHealth = ServiceHealth.HEALTHY
    restart_result: bool = True
    restart_calls: int = 0

    def health(self) -> ServiceHealth:
        return self.health_value

    def restart(self) -> bool:
        self.restart_calls += 1
        return self.restart_result

    def graceful_stop(self, timeout_seconds: int) -> bool:
        del timeout_seconds
        return True

    def is_stopped(self) -> bool:
        return True

    def start(self) -> bool:
        return True


@dataclass
class FakeShutdown:
    outcome: ShutdownOutcome = ShutdownOutcome.COMPLETED
    calls: list[bool] = field(default_factory=list)

    def execute(self, dry_run: bool) -> ShutdownOutcome:
        self.calls.append(dry_run)
        return self.outcome


def engine(
    observations: list[PlayerObservation],
    *,
    service: FakeService | None = None,
    shutdown: FakeShutdown | None = None,
    dry_run: bool = True,
) -> tuple[WatchdogEngine, SequencePlayers, FakeService, FakeShutdown, list[float]]:
    players = SequencePlayers(observations)
    fake_service = service or FakeService()
    fake_shutdown = shutdown or FakeShutdown(
        ShutdownOutcome.DRY_RUN if dry_run else ShutdownOutcome.COMPLETED
    )
    sleeps: list[float] = []
    watchdog = WatchdogEngine(
        players=players,
        service=fake_service,
        shutdown=fake_shutdown,
        policy=WatchdogPolicy(
            idle_timeout_seconds=2700,
            health_failure_timeout_seconds=720,
            final_check_count=3,
            final_check_delay_seconds=5,
            progress_interval_seconds=300,
            dry_run=dry_run,
        ),
        sleeper=sleeps.append,
    )
    return watchdog, players, fake_service, fake_shutdown, sleeps


def known(count: int) -> PlayerObservation:
    return PlayerObservation.known(count)


def unknown(detail: str = "failed") -> PlayerObservation:
    return PlayerObservation.unknown(detail)


def test_server_with_players_never_starts_idle_timer() -> None:
    watchdog, _, _, shutdown, _ = engine([known(3)])
    watchdog.tick(100)
    assert watchdog.state.empty_since is None
    assert shutdown.calls == []


def test_server_becomes_empty() -> None:
    watchdog, _, _, _, _ = engine([known(0)])
    watchdog.tick(100)
    assert watchdog.state.empty_since == 100


def test_empty_for_less_than_timeout_does_nothing() -> None:
    watchdog, _, _, shutdown, _ = engine([known(0), known(0)])
    watchdog.tick(100)
    watchdog.tick(2799)
    assert shutdown.calls == []


def test_full_idle_timeout_and_final_checks_trigger_dry_run_once() -> None:
    shutdown = FakeShutdown(ShutdownOutcome.DRY_RUN)
    watchdog, players, _, _, sleeps = engine(
        [known(0), known(0), known(0), known(0), known(0)], shutdown=shutdown
    )
    watchdog.tick(100)
    watchdog.tick(2800)
    watchdog.tick(2860)
    assert shutdown.calls == [True]
    assert sleeps == [5, 5]
    assert players.calls == 5
    assert watchdog.state.dry_run_reported is True


def test_player_reconnect_resets_idle_timer() -> None:
    watchdog, _, _, shutdown, _ = engine([known(0), known(2)])
    watchdog.tick(100)
    watchdog.tick(1000)
    assert watchdog.state.empty_since is None
    assert shutdown.calls == []


def test_unknown_resets_idle_timer_and_prohibits_shutdown() -> None:
    watchdog, _, service, shutdown, _ = engine([known(0), unknown()])
    watchdog.tick(100)
    watchdog.tick(2800)
    assert watchdog.state.empty_since is None
    assert service.restart_calls == 0
    assert shutdown.calls == []


def test_management_outage_restarts_service_after_continuous_threshold() -> None:
    service = FakeService(health_value=ServiceHealth.UNHEALTHY)
    watchdog, _, _, shutdown, _ = engine([unknown(), unknown(), unknown()], service=service)
    watchdog.tick(100)
    watchdog.tick(819)
    assert service.restart_calls == 0
    watchdog.tick(820)
    assert service.restart_calls == 1
    assert watchdog.state.management_failure_since is None
    assert shutdown.calls == []


def test_unknown_with_healthy_secondary_check_never_restarts() -> None:
    service = FakeService(health_value=ServiceHealth.HEALTHY)
    watchdog, _, _, _, _ = engine([unknown(), unknown()], service=service)
    watchdog.tick(100)
    watchdog.tick(1000)
    assert service.restart_calls == 0
    assert watchdog.state.management_failure_since is None


def test_failed_service_restart_keeps_host_online() -> None:
    service = FakeService(health_value=ServiceHealth.STOPPED, restart_result=False)
    watchdog, _, _, shutdown, _ = engine([unknown(), unknown()], service=service)
    watchdog.tick(100)
    watchdog.tick(820)
    assert service.restart_calls == 1
    assert shutdown.calls == []


def test_recovery_after_outage_starts_idle_state_fresh() -> None:
    service = FakeService(health_value=ServiceHealth.UNHEALTHY)
    watchdog, _, _, _, _ = engine([unknown(), known(0)], service=service)
    watchdog.tick(100)
    service.health_value = ServiceHealth.HEALTHY
    watchdog.tick(500)
    assert watchdog.state.management_failure_since is None
    assert watchdog.state.empty_since == 500


def test_player_arriving_during_final_checks_aborts_shutdown() -> None:
    watchdog, _, _, shutdown, sleeps = engine([known(0), known(0), known(1)])
    watchdog.tick(100)
    watchdog.tick(2800)
    assert shutdown.calls == []
    assert sleeps == [5]
    assert watchdog.state.empty_since is None


def test_error_during_final_checks_aborts_shutdown() -> None:
    watchdog, _, _, shutdown, sleeps = engine([known(0), known(0), unknown("timeout")])
    watchdog.tick(100)
    watchdog.tick(2800)
    assert shutdown.calls == []
    assert sleeps == [5]
    assert watchdog.state.empty_since is None


def test_aborted_production_shutdown_requires_fresh_idle_interval() -> None:
    shutdown = FakeShutdown(ShutdownOutcome.ABORTED)
    watchdog, _, _, _, _ = engine(
        [known(0), known(0), known(0), known(0)], shutdown=shutdown, dry_run=False
    )
    watchdog.tick(100)
    watchdog.tick(2800)
    assert shutdown.calls == [False]
    assert watchdog.state.empty_since is None


def test_completed_production_shutdown() -> None:
    shutdown = FakeShutdown(ShutdownOutcome.COMPLETED)
    watchdog, _, _, _, _ = engine(
        [known(0), known(0), known(0), known(0)], shutdown=shutdown, dry_run=False
    )
    watchdog.tick(100)
    watchdog.tick(2800)
    assert shutdown.calls == [False]
    assert watchdog.state.empty_since is None


@pytest.mark.parametrize(
    "policy",
    [
        WatchdogPolicy(idle_timeout_seconds=1, health_failure_timeout_seconds=1),
    ],
)
def test_valid_policy(policy: WatchdogPolicy) -> None:
    assert policy.final_check_count == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idle_timeout_seconds": 0},
        {"health_failure_timeout_seconds": 0},
        {"final_check_count": 1},
        {"final_check_delay_seconds": 0},
    ],
)
def test_invalid_policy(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        WatchdogPolicy(**kwargs)
