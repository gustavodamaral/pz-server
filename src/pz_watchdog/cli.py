from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence

from .config import ConfigurationError, Settings
from .engine import WatchdogEngine, WatchdogPolicy
from .metrics import MetricsCollector, format_status, snapshot_json
from .models import ServiceHealth
from .rcon import RconAdmin, RconCliClient, RetryingPlayerCounter
from .service import DockerComposeService, ServiceManager, TcpOnlyService
from .shutdown import ShutdownCoordinator, SystemdPowerController

LOGGER = logging.getLogger(__name__)


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        UTCFormatter("%(asctime)sZ %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def build_components(
    settings: Settings,
) -> tuple[
    RetryingPlayerCounter,
    RconAdmin,
    ServiceManager,
    ShutdownCoordinator,
]:
    client = RconCliClient(
        command=settings.rcon_cli_command,
        host=settings.rcon_host,
        port=settings.rcon_port,
        password=settings.rcon_password,
        timeout_seconds=settings.rcon_timeout_seconds,
    )
    players = RetryingPlayerCounter(
        client=client,
        attempts=settings.rcon_retry_count,
        retry_delay_seconds=settings.rcon_retry_delay_seconds,
    )
    admin = RconAdmin(
        client=client,
        attempts=settings.rcon_retry_count,
        retry_delay_seconds=settings.rcon_retry_delay_seconds,
    )
    if settings.service_control_mode == "docker":
        service: ServiceManager = DockerComposeService(
            project_directory=settings.compose_project_directory,
            service=settings.compose_service,
            env_file=settings.compose_env_file,
        )
    else:
        service = TcpOnlyService(settings.rcon_host, settings.rcon_port)
    host = SystemdPowerController(
        deployment_environment=settings.deployment_environment,
        enabled=settings.host_shutdown_enabled,
        guard_file=settings.host_shutdown_guard_file,
    )
    shutdown = ShutdownCoordinator(
        players=players,
        admin=admin,
        service=service,
        host=host,
        service_stop_timeout_seconds=settings.service_stop_timeout_seconds,
    )
    return players, admin, service, shutdown


def run_watchdog(settings: Settings, once: bool = False) -> int:
    players, _, service, shutdown = build_components(settings)
    engine = WatchdogEngine(
        players=players,
        service=service,
        shutdown=shutdown,
        policy=WatchdogPolicy(
            idle_timeout_seconds=settings.idle_timeout_seconds,
            health_failure_timeout_seconds=settings.health_failure_timeout_seconds,
            final_check_count=settings.final_check_count,
            final_check_delay_seconds=settings.final_check_delay_seconds,
            dry_run=settings.dry_run,
        ),
    )
    LOGGER.info(
        "Watchdog started: idle=%d min, outage=%d min, retries=%d, dry_run=%s",
        settings.idle_timeout_seconds // 60,
        settings.health_failure_timeout_seconds // 60,
        settings.rcon_retry_count,
        settings.dry_run,
    )
    while True:
        engine.tick(time.monotonic())
        if once:
            return 0
        time.sleep(settings.poll_interval_seconds)


def status(settings: Settings, as_json: bool) -> int:
    players, _, service, _ = build_components(settings)
    if not isinstance(service, DockerComposeService):
        LOGGER.error("Detailed status requires SERVICE_CONTROL_MODE=docker on the host")
        return 2
    snapshot = MetricsCollector(
        service,
        settings.data_path,
        settings.server_path,
        settings.update_policy,
        settings.steam_branch,
    ).collect(players.query())
    print(snapshot_json(snapshot) if as_json else format_status(snapshot))
    return 0


def graceful_stop(settings: Settings, allow_connected_players: bool = False) -> int:
    players, admin, service, _ = build_components(settings)
    before_save = players.query()
    if before_save.is_unknown or (before_save.count and not allow_connected_players):
        LOGGER.error("Connected-player state does not permit a manual stop")
        return 1
    if not admin.save():
        return 1
    time.sleep(10)
    after_save = players.query()
    if after_save.is_unknown or (after_save.count and not allow_connected_players):
        LOGGER.error("Connected-player state changed during save; manual stop aborted")
        return 1
    return 0 if service.graceful_stop(settings.service_stop_timeout_seconds) else 1


def wait_ready(settings: Settings, timeout_seconds: int) -> int:
    players, _, service, _ = build_components(settings)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if service.health() is ServiceHealth.HEALTHY and not players.query().is_unknown:
            LOGGER.info("Project Zomboid is healthy and RCON is responsive")
            return 0
        time.sleep(10)
    LOGGER.error("Project Zomboid did not become ready within %d seconds", timeout_seconds)
    return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Project Zomboid lifecycle watchdog")
    result.add_argument("--verbose", action="store_true")
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run the watchdog continuously")
    subparsers.add_parser("once", help="Execute one watchdog poll")
    status_parser = subparsers.add_parser("status", help="Report players and real host metrics")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("players", help="Print the confirmed player count")
    subparsers.add_parser("save", help="Save and confirm the Project Zomboid world")
    restart_parser = subparsers.add_parser("restart", help="Gracefully restart the PZ service")
    restart_parser.add_argument("--allow-connected-players", action="store_true")
    stop_parser = subparsers.add_parser("graceful-stop", help="Save and stop the PZ service")
    stop_parser.add_argument("--allow-connected-players", action="store_true")
    shutdown_parser = subparsers.add_parser(
        "shutdown-host", help="Save, stop PZ, and apply guarded host shutdown"
    )
    shutdown_parser.add_argument("--allow-connected-players", action="store_true")
    ready_parser = subparsers.add_parser("wait-ready", help="Wait for container health and RCON")
    ready_parser.add_argument("--timeout", type=int, default=900)
    return result


def execute(arguments: argparse.Namespace, settings: Settings) -> int:
    if arguments.command == "run":
        return run_watchdog(settings)
    if arguments.command == "once":
        return run_watchdog(settings, once=True)
    if arguments.command == "status":
        return status(settings, arguments.as_json)
    players, admin, service, shutdown = build_components(settings)
    if arguments.command == "players":
        observation = players.query()
        print("UNKNOWN" if observation.is_unknown else observation.count)
        return 2 if observation.is_unknown else 0
    if arguments.command == "save":
        return 0 if admin.save() else 1
    if arguments.command == "restart":
        observation = players.query()
        if observation.is_unknown or (observation.count and not arguments.allow_connected_players):
            LOGGER.error("Connected-player state does not permit a manual restart")
            return 1
        return 0 if service.restart() else 1
    if arguments.command == "graceful-stop":
        return graceful_stop(settings, arguments.allow_connected_players)
    if arguments.command == "shutdown-host":
        accepted = {"completed", "dry-run"}
        outcome = shutdown.execute(
            dry_run=settings.dry_run,
            allow_connected_players=arguments.allow_connected_players,
        )
        return 0 if outcome.value in accepted else 1
    if arguments.command == "wait-ready":
        return wait_ready(settings, arguments.timeout)
    raise AssertionError(f"Unhandled command: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    configure_logging(arguments.verbose)
    try:
        settings = Settings.from_environment()
        return execute(arguments, settings)
    except ConfigurationError as error:
        LOGGER.error("Configuration error: %s", error)
        return 64
    except KeyboardInterrupt:
        LOGGER.info("Watchdog interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
