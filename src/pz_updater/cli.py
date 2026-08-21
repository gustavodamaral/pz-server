from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .core import GameUpdater, UpdateConfiguration, UpdateError, parse_player_count, read_pz_version

LOGGER = logging.getLogger(__name__)


def _json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _log_fingerprint(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return "missing"
        metadata = path.stat()
    except OSError:
        return "missing"
    return f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:{metadata.st_mtime_ns}"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise UpdateError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise UpdateError(f"{name} must be between {minimum} and {maximum}")
    return value


def _query_players() -> int:
    password = os.environ.get("RCON_PASSWORD", "")
    if not password:
        raise UpdateError("RCON_PASSWORD must not be empty for runtime readiness")
    port = _environment_integer("RCON_PORT", 27015, 1024, 65535)
    timeout = _environment_integer("RCON_TIMEOUT_SECONDS", 10, 1, 60)
    environment = os.environ.copy()
    environment["RCON_PASSWORD"] = password
    try:
        result = subprocess.run(
            ["rcon-cli", "--host", "127.0.0.1", "--port", str(port), "players"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"RCON readiness query failed: {type(error).__name__}") from error
    if result.returncode != 0:
        raise UpdateError(f"RCON readiness query exited with status {result.returncode}")
    if result.stderr.strip():
        raise UpdateError("RCON readiness query reported an error")
    return parse_player_count(result.stdout.strip())


def _wait_runtime_ready(args: argparse.Namespace, updater: GameUpdater) -> dict[str, object]:
    if args.pid <= 0:
        raise UpdateError("--pid must identify a positive process ID")
    if not 30 <= args.timeout <= 3600:
        raise UpdateError("--timeout must be between 30 and 3600 seconds")
    log_path = Path(args.log)
    deadline = time.monotonic() + args.timeout
    last_detail = "RCON has not been queried"
    next_progress = time.monotonic()
    while time.monotonic() < deadline:
        if not _process_alive(args.pid):
            detail = "Project Zomboid exited before exact RCON readiness"
            updater.fail_readiness(detail)
            raise UpdateError(detail)
        try:
            players = _query_players()
        except UpdateError as error:
            last_detail = str(error)
        else:
            version: str | None = None
            revision: str | None = None
            if _log_fingerprint(log_path) != args.log_fingerprint:
                version, revision = read_pz_version(log_path)
            updater.mark_runtime_ready(version, revision)
            return {
                "players": players,
                "pz_revision": revision,
                "pz_version": version,
                "state": updater.status().get("state"),
            }
        now = time.monotonic()
        if now >= next_progress:
            LOGGER.info("Waiting for exact Project Zomboid RCON readiness: %s", last_detail)
            next_progress = now + 60
        time.sleep(min(5, max(0, deadline - now)))
    detail = f"Runtime readiness timed out: {last_detail}"
    updater.fail_readiness(detail)
    raise UpdateError(detail)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pz-updater",
        description="Conservative Project Zomboid dedicated-server release updater",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-start", help="stage updates and print the selected release")
    subparsers.add_parser("active-release", help="print the selected release")
    subparsers.add_parser("status", help="print update status JSON")
    update = subparsers.add_parser("update", help="explicitly check and stage a game update")
    update.add_argument(
        "--validate",
        action="store_true",
        help="run Steam validation as an explicit repair operation",
    )
    subparsers.add_parser(
        "mark-world-opened",
        help="record the irreversible boundary immediately before candidate launch",
    )
    runtime = subparsers.add_parser(
        "mark-runtime-ready", help="record runtime readiness from a fresh server log"
    )
    runtime.add_argument("--log", required=True)
    subparsers.add_parser("accept", help="accept an AWS candidate after external readiness")
    failure = subparsers.add_parser(
        "fail-readiness", help="record candidate failure after possible world access"
    )
    failure.add_argument("--detail", required=True)
    fingerprint = subparsers.add_parser(
        "log-fingerprint", help="print an opaque fingerprint for freshness checks"
    )
    fingerprint.add_argument("--log", required=True)
    wait = subparsers.add_parser(
        "wait-runtime-ready", help="wait for exact RCON and record candidate readiness"
    )
    wait.add_argument("--pid", required=True, type=int)
    wait.add_argument("--log", required=True)
    wait.add_argument("--log-fingerprint", required=True)
    wait.add_argument("--timeout", required=True, type=int)
    return parser


def _run(args: argparse.Namespace) -> None:
    if args.command == "log-fingerprint":
        print(_log_fingerprint(Path(args.log)))
        return
    updater = GameUpdater(UpdateConfiguration.from_environment())
    if args.command == "prepare-start":
        updater.prepare_start()
        print(updater.active_release())
    elif args.command == "active-release":
        print(updater.active_release())
    elif args.command == "status":
        _json(updater.status())
    elif args.command == "update":
        _json(updater.explicit_update(validate=args.validate))
    elif args.command == "mark-world-opened":
        _json({"marked": updater.mark_world_opened()})
    elif args.command == "mark-runtime-ready":
        version, revision = read_pz_version(Path(args.log))
        updater.mark_runtime_ready(version, revision)
        _json(updater.status())
    elif args.command == "accept":
        _json(updater.accept())
    elif args.command == "fail-readiness":
        updater.fail_readiness(args.detail)
        _json(updater.status())
    elif args.command == "wait-runtime-ready":
        _json(_wait_runtime_ready(args, updater))
    else:
        raise UpdateError(f"Unsupported updater command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stderr,
    )
    logging.Formatter.converter = time.gmtime
    try:
        _run(_parser().parse_args(argv))
    except UpdateError as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
