from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from pz_updater import cli
from pz_updater.core import UpdateError


class FakeUpdater:
    def __init__(self, state: str = "current") -> None:
        self.state = state
        self.ready: tuple[str | None, str | None] | None = None
        self.failure: str | None = None

    def mark_runtime_ready(self, version: str | None, revision: str | None) -> None:
        self.ready = (version, revision)

    def fail_readiness(self, detail: str) -> None:
        self.failure = detail

    def status(self) -> dict[str, object]:
        return {"state": self.state, "current_build": 200}


class CommandUpdater(FakeUpdater):
    def __init__(self) -> None:
        super().__init__()
        self.validate: bool | None = None
        self.failed_detail: str | None = None

    def prepare_start(self) -> dict[str, object]:
        return self.status()

    def active_release(self) -> Path:
        return Path("/opt/pzserver/releases/steam-200")

    def explicit_update(self, validate: bool = False) -> dict[str, object]:
        self.validate = validate
        return self.status()

    def mark_world_opened(self) -> bool:
        return True

    def accept(self) -> dict[str, object]:
        return self.status()

    def fail_readiness(self, detail: str) -> None:
        self.failed_detail = detail


def test_rcon_query_uses_environment_secret_and_exact_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RCON_PASSWORD", "super-secret")
    monkeypatch.setenv("RCON_PORT", "27015")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "super-secret" not in command
        assert kwargs["env"]["RCON_PASSWORD"] == "super-secret"  # type: ignore[index]
        return subprocess.CompletedProcess(command, 0, "Players connected (0):\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._query_players() == 0


def test_rcon_query_requires_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RCON_PASSWORD", raising=False)
    with pytest.raises(UpdateError, match="must not be empty"):
        cli._query_players()
    monkeypatch.setenv("RCON_PASSWORD", "secret")
    monkeypatch.setenv("RCON_PORT", "invalid")
    with pytest.raises(UpdateError, match="integer"):
        cli._query_players()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (subprocess.CompletedProcess([], 1, "", "authentication failed"), "status 1"),
        (subprocess.CompletedProcess([], 0, "Players connected (0):", "warning"), "reported"),
        (subprocess.CompletedProcess([], 0, "ambiguous", ""), "recognized"),
    ],
)
def test_rcon_query_fails_closed(
    result: subprocess.CompletedProcess[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RCON_PASSWORD", "secret")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(UpdateError, match=message):
        cli._query_players()


def test_log_fingerprint_changes_with_fresh_content(tmp_path: Path) -> None:
    log = tmp_path / "server-console.txt"
    assert cli._log_fingerprint(log) == "missing"
    log.write_text("first", encoding="utf-8")
    first = cli._log_fingerprint(log)
    log.write_text("second and larger", encoding="utf-8")
    assert first != cli._log_fingerprint(log)


def test_runtime_wait_records_exact_rcon_and_fresh_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "server-console.txt"
    log.write_text("version=42.20.3 abc123 demo=false\n", encoding="utf-8")
    updater = FakeUpdater()
    monkeypatch.setattr(cli, "_process_alive", lambda _: True)
    monkeypatch.setattr(cli, "_query_players", lambda: 0)
    args = argparse.Namespace(
        pid=123,
        timeout=30,
        log=str(log),
        log_fingerprint="old-fingerprint",
    )
    result = cli._wait_runtime_ready(args, updater)  # type: ignore[arg-type]
    assert updater.ready == ("42.20.3", "abc123")
    assert result["players"] == 0
    assert result["state"] == "current"


def test_runtime_wait_records_process_exit_as_readiness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(state="starting-candidate")
    monkeypatch.setattr(cli, "_process_alive", lambda _: False)
    args = argparse.Namespace(
        pid=123,
        timeout=30,
        log=str(tmp_path / "missing.log"),
        log_fingerprint="missing",
    )
    with pytest.raises(UpdateError, match="exited"):
        cli._wait_runtime_ready(args, updater)  # type: ignore[arg-type]
    assert updater.failure == "Project Zomboid exited before exact RCON readiness"


@pytest.mark.parametrize(("pid", "timeout"), [(0, 30), (1, 29), (1, 7201)])
def test_runtime_wait_validates_bounds(pid: int, timeout: int, tmp_path: Path) -> None:
    args = argparse.Namespace(
        pid=pid,
        timeout=timeout,
        log=str(tmp_path / "log"),
        log_fingerprint="missing",
    )
    with pytest.raises(UpdateError):
        cli._wait_runtime_ready(args, FakeUpdater())  # type: ignore[arg-type]


def test_cli_status_emits_one_json_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    updater = FakeUpdater()
    monkeypatch.setattr(
        cli.UpdateConfiguration,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(cli, "GameUpdater", lambda _: updater)
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"current_build": 200, "state": "current"}


@pytest.mark.parametrize(
    "arguments",
    [
        ["prepare-start"],
        ["active-release"],
        ["update", "--validate"],
        ["mark-world-opened"],
        ["accept"],
        ["fail-readiness", "--detail", "test failure"],
    ],
)
def test_cli_dispatches_lifecycle_commands(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    updater = CommandUpdater()
    monkeypatch.setattr(
        cli.UpdateConfiguration,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(cli, "GameUpdater", lambda _: updater)
    assert cli.main(arguments) == 0
    output = capsys.readouterr().out.strip()
    assert output
    if arguments[0] == "update":
        assert updater.validate is True
    if arguments[0] == "fail-readiness":
        assert updater.failed_detail == "test failure"


def test_cli_marks_runtime_from_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = tmp_path / "server-console.txt"
    log.write_text("version=42.20.3 abc123 demo=false\n", encoding="utf-8")
    updater = CommandUpdater()
    monkeypatch.setattr(
        cli.UpdateConfiguration,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(cli, "GameUpdater", lambda _: updater)
    assert cli.main(["mark-runtime-ready", "--log", str(log)]) == 0
    assert updater.ready == ("42.20.3", "abc123")
    assert json.loads(capsys.readouterr().out)["state"] == "current"


def test_cli_returns_nonzero_without_exposing_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.UpdateConfiguration,
        "from_environment",
        staticmethod(lambda: (_ for _ in ()).throw(UpdateError("safe failure"))),
    )
    assert cli.main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
