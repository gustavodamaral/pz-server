from __future__ import annotations

from pathlib import Path

import pytest

from pz_watchdog.config import ConfigurationError, Settings


def environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "PZ_ENV_FILE": str(tmp_path / "missing.env"),
        "RCON_PASSWORD": "secure-test-password",
        "DRY_RUN": "true",
        "DEPLOYMENT_ENVIRONMENT": "local",
        "COMPOSE_PROJECT_DIRECTORY": str(tmp_path),
        "RCON_CLI_COMMAND": "rcon-cli",
    }
    values.update(overrides)
    return values


def test_safe_defaults(tmp_path: Path) -> None:
    settings = Settings.from_environment(environment(tmp_path))
    assert settings.idle_timeout_seconds == 2700
    assert settings.health_failure_timeout_seconds == 720
    assert settings.rcon_retry_count == 3
    assert settings.dry_run is True
    assert settings.rcon_cli_command == ("rcon-cli",)
    assert settings.update_policy == "stable-on-start"
    assert settings.steam_branch == "public"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DRY_RUN", "perhaps"),
        ("RCON_PORT", "not-a-number"),
        ("RCON_PORT", "1"),
        ("SERVICE_CONTROL_MODE", "magic"),
        ("DEPLOYMENT_ENVIRONMENT", "desktop"),
        ("FINAL_CHECK_COUNT", "1"),
        ("PZ_UPDATE_POLICY", "automatic"),
    ],
)
def test_invalid_settings_fail(name: str, value: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_environment(environment(tmp_path, **{name: value}))


@pytest.mark.parametrize("password", ["", "change-me-rcon-password"])
def test_placeholder_or_empty_rcon_password_is_rejected(password: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_environment(environment(tmp_path, RCON_PASSWORD=password))


def test_non_aws_cannot_disable_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pz_watchdog.config._running_on_windows", lambda: False)
    with pytest.raises(ConfigurationError, match="aws environment"):
        Settings.from_environment(
            environment(tmp_path, DRY_RUN="false", DEPLOYMENT_ENVIRONMENT="local")
        )


def test_windows_cannot_disable_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pz_watchdog.config._running_on_windows", lambda: True)
    with pytest.raises(ConfigurationError, match="Windows"):
        Settings.from_environment(
            environment(tmp_path, DRY_RUN="false", DEPLOYMENT_ENVIRONMENT="aws")
        )


def test_aws_linux_can_be_explicitly_armed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pz_watchdog.config._running_on_windows", lambda: False)
    settings = Settings.from_environment(
        environment(
            tmp_path,
            DRY_RUN="false",
            DEPLOYMENT_ENVIRONMENT="aws",
            HOST_SHUTDOWN_ENABLED="true",
        )
    )
    assert settings.dry_run is False
    assert settings.host_shutdown_enabled is True


def test_dotenv_is_loaded_but_explicit_environment_wins(tmp_path: Path) -> None:
    dotenv = tmp_path / "test.env"
    dotenv.write_text(
        "# test\nRCON_PASSWORD=from-file\nIDLE_TIMEOUT_MINUTES='30'\n",
        encoding="utf-8",
    )
    values = environment(tmp_path, PZ_ENV_FILE=str(dotenv), RCON_PASSWORD="explicit")
    settings = Settings.from_environment(values)
    assert settings.rcon_password == "explicit"
    assert settings.idle_timeout_seconds == 1800


def test_invalid_dotenv_line_fails(tmp_path: Path) -> None:
    dotenv = tmp_path / "bad.env"
    dotenv.write_text("not-an-assignment\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="line 1"):
        Settings.from_environment(environment(tmp_path, PZ_ENV_FILE=str(dotenv)))


def test_stable_policy_rejects_non_public_branch(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="empty STEAM_BRANCH"):
        Settings.from_environment(
            environment(
                tmp_path,
                PZ_UPDATE_POLICY="stable-on-start",
                STEAM_BRANCH="unstable",
            )
        )
