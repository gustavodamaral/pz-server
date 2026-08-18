from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when watchdog configuration is unsafe or malformed."""


def _running_on_windows() -> bool:
    return os.name == "nt"


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"Invalid environment line {number} in {path}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigurationError(f"Empty environment name on line {number} in {path}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        result = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    rcon_host: str
    rcon_port: int
    rcon_password: str
    rcon_timeout_seconds: int
    rcon_retry_count: int
    rcon_retry_delay_seconds: int
    idle_timeout_seconds: int
    poll_interval_seconds: int
    health_failure_timeout_seconds: int
    final_check_count: int
    final_check_delay_seconds: int
    service_stop_timeout_seconds: int
    dry_run: bool
    deployment_environment: str
    host_shutdown_enabled: bool
    host_shutdown_guard_file: Path
    service_control_mode: str
    compose_project_directory: Path
    compose_env_file: Path | None
    compose_service: str
    data_path: Path
    rcon_cli_command: tuple[str, ...]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Settings:
        supplied = dict(os.environ if environment is None else environment)
        env_file = Path(supplied.get("PZ_ENV_FILE", ".env"))
        values = {**_load_dotenv(env_file), **supplied}

        project_directory = Path(values.get("COMPOSE_PROJECT_DIRECTORY", ".")).resolve()
        compose_env_raw = values.get("COMPOSE_ENV_FILE", "")
        compose_env_file = Path(compose_env_raw).resolve() if compose_env_raw else None
        service_mode = values.get("SERVICE_CONTROL_MODE", "docker").strip().lower()
        if service_mode not in {"docker", "tcp-only"}:
            raise ConfigurationError("SERVICE_CONTROL_MODE must be docker or tcp-only")

        configured_rcon_cli = values.get("RCON_CLI_COMMAND", "").strip()
        if configured_rcon_cli:
            rcon_cli_command = tuple(shlex.split(configured_rcon_cli, posix=os.name != "nt"))
        elif shutil.which("rcon-cli"):
            rcon_cli_command = ("rcon-cli",)
        elif service_mode == "docker":
            rcon_cli_command = (
                "docker",
                "compose",
                "--project-directory",
                str(project_directory),
                "exec",
                "-T",
                values.get("COMPOSE_SERVICE", "server"),
                "rcon-cli",
            )
        else:
            rcon_cli_command = ("rcon-cli",)

        dry_run = _boolean(values, "DRY_RUN", True)
        deployment_environment = values.get("DEPLOYMENT_ENVIRONMENT", "local").strip().lower()
        if deployment_environment not in {"local", "aws"}:
            raise ConfigurationError("DEPLOYMENT_ENVIRONMENT must be local or aws")
        if _running_on_windows() and not dry_run:
            raise ConfigurationError("DRY_RUN=false is prohibited on Windows")
        if deployment_environment != "aws" and not dry_run:
            raise ConfigurationError("DRY_RUN=false is permitted only in the aws environment")

        password = values.get("RCON_PASSWORD", "")
        if not password or password.startswith("change-me-"):
            raise ConfigurationError("RCON_PASSWORD must be set to a non-placeholder value")

        return cls(
            rcon_host=values.get("RCON_HOST", "127.0.0.1"),
            rcon_port=_integer(values, "RCON_PORT", 27015, 1024, 65535),
            rcon_password=password,
            rcon_timeout_seconds=_integer(values, "RCON_TIMEOUT_SECONDS", 10, 1, 120),
            rcon_retry_count=_integer(values, "RCON_RETRY_COUNT", 3, 1, 10),
            rcon_retry_delay_seconds=_integer(values, "RCON_RETRY_DELAY_SECONDS", 5, 0, 300),
            idle_timeout_seconds=_integer(values, "IDLE_TIMEOUT_MINUTES", 45, 1, 1440) * 60,
            poll_interval_seconds=_integer(values, "POLL_INTERVAL_SECONDS", 60, 5, 3600),
            health_failure_timeout_seconds=_integer(
                values, "HEALTH_FAILURE_TIMEOUT_MINUTES", 12, 1, 120
            )
            * 60,
            final_check_count=_integer(values, "FINAL_CHECK_COUNT", 3, 2, 10),
            final_check_delay_seconds=_integer(values, "FINAL_CHECK_DELAY_SECONDS", 5, 1, 300),
            service_stop_timeout_seconds=_integer(
                values, "SERVICE_STOP_TIMEOUT_SECONDS", 120, 30, 600
            ),
            dry_run=dry_run,
            deployment_environment=deployment_environment,
            host_shutdown_enabled=_boolean(values, "HOST_SHUTDOWN_ENABLED", False),
            host_shutdown_guard_file=Path(
                values.get("HOST_SHUTDOWN_GUARD_FILE", "/etc/pz-server/allow-host-shutdown")
            ),
            service_control_mode=service_mode,
            compose_project_directory=project_directory,
            compose_env_file=compose_env_file,
            compose_service=values.get("COMPOSE_SERVICE", "server"),
            data_path=Path(values.get("PZ_DATA_PATH", "./runtime/zomboid")).resolve(),
            rcon_cli_command=rcon_cli_command,
        )
