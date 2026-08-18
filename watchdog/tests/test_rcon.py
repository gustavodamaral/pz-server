from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from pz_watchdog.rcon import (
    RconAdmin,
    RconCliClient,
    RconError,
    RetryingPlayerCounter,
    parse_player_count,
)


@dataclass
class FakeClient:
    responses: list[str | Exception]
    calls: list[tuple[str, ...]]

    def run(self, *command: str) -> str:
        self.calls.append(command)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("Players connected (0):", 0),
        ("Players connected ( 1 )\n-alice", 1),
        ("Players connected: 2\n-alice\n-bob", 2),
    ],
)
def test_parse_player_count(response: str, expected: int) -> None:
    assert parse_player_count(response) == expected


def test_unrecognized_player_response_is_not_zero() -> None:
    with pytest.raises(RconError):
        parse_player_count("Connected players unavailable")


@pytest.mark.parametrize(
    "response",
    [
        "Weird. This response is for another request.\nPlayers connected (0):",
        "Players connected (0):\nPlayers connected (3):",
        "ERROR: stale reply: Players connected (0):",
    ],
)
def test_ambiguous_player_response_is_unknown(response: str) -> None:
    with pytest.raises(RconError):
        parse_player_count(response)


def test_transient_rcon_failure_recovers() -> None:
    client = FakeClient([RconError("timeout"), "Players connected (2):\n-alice\n-bob"], [])
    sleeps: list[float] = []
    counter = RetryingPlayerCounter(
        client, attempts=3, retry_delay_seconds=5, sleeper=sleeps.append
    )
    result = counter.query()
    assert result.count == 2
    assert sleeps == [5]
    assert len(client.calls) == 2


def test_three_rcon_failures_return_unknown_never_zero() -> None:
    client = FakeClient([RconError("one"), OSError("two"), ValueError("three")], [])
    sleeps: list[float] = []
    result = RetryingPlayerCounter(client, 3, 5, sleeps.append).query()
    assert result.is_unknown
    assert result.count is None
    assert sleeps == [5, 5]


def test_rcon_admin_retries_save() -> None:
    client = FakeClient([RconError("transient"), "World saved"], [])
    sleeps: list[float] = []
    assert RconAdmin(client, 3, 5, sleeps.append).save() is True
    assert client.calls == [("save",), ("save",)]
    assert sleeps == [5]


def test_rcon_admin_fails_closed() -> None:
    client = FakeClient([RconError("a"), RconError("b")], [])
    assert RconAdmin(client, 2, 0, lambda _: None).save() is False


def test_rcon_admin_rejects_unrecognized_success_response() -> None:
    client = FakeClient(["Command completed", ""], [])
    assert RconAdmin(client, 2, 0, lambda _: None).save() is False


def test_rcon_admin_rejects_save_failure_containing_success_words() -> None:
    client = FakeClient(["World saved FAILED"], [])
    assert RconAdmin(client, 1, 0, lambda _: None).save() is False


def test_rcon_cli_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "--password" not in args[0]  # type: ignore[operator]
        assert kwargs["env"]["RCON_PASSWORD"] == "secret"  # type: ignore[index]
        return subprocess.CompletedProcess([], 0, "Players connected (1):\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = RconCliClient(("rcon-cli",), "localhost", 27015, "secret", 10)
    assert client.run("players") == "Players connected (1):"


def test_rcon_cli_nonzero_exit_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "auth failed\n"),
    )
    client = RconCliClient(("rcon-cli",), "localhost", 27015, "secret", 10)
    with pytest.raises(RconError, match="status 1") as caught:
        client.run("players")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [("", ""), ("Players connected (0):", "connection closed")],
)
def test_rcon_cli_rejects_ambiguous_zero_exit(
    stdout: str, stderr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout, stderr),
    )
    client = RconCliClient(("rcon-cli",), "localhost", 27015, "secret", 10)
    with pytest.raises(RconError):
        client.run("players")


def test_rcon_cli_timeout_becomes_rcon_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("rcon-cli", 10)

    monkeypatch.setattr(subprocess, "run", timeout)
    client = RconCliClient(("rcon-cli",), "localhost", 27015, "secret", 10)
    with pytest.raises(RconError, match="TimeoutExpired"):
        client.run("players")
