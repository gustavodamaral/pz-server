from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
ACTIVE_RELEASE_FILE = "active-release"
PREVIOUS_RELEASE_FILE = "previous-release"
STATE_FILE = "update-state.json"
TRANSACTION_LOCK_FILE = ".update-transaction.lock"
REPOSITORY_DEPLOYMENT_MARKER = ".repository-deployment-pending"
RELEASES_DIRECTORY = "releases"
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PLAYER_HEADER_PATTERNS = (
    re.compile(r"Players\s+connected\s*\(\s*(\d+)\s*\)\s*:?\s*", re.IGNORECASE),
    re.compile(r"Players\s+connected\s*:\s*(\d+)", re.IGNORECASE),
)
VERSION_PATTERN = re.compile(
    r"\bversion=(\d+\.\d+(?:\.\d+)?)\s+([0-9a-f]+)\s+demo=(?:true|false)\b",
    re.IGNORECASE,
)


class UpdateError(RuntimeError):
    """An update could not proceed without violating a safety invariant."""


class UpdatePolicy(StrEnum):
    STABLE_ON_START = "stable-on-start"
    MANUAL = "manual"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise UpdateError(f"{name} must be true or false")


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(values.get(name, str(default)))
    except ValueError as error:
        raise UpdateError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise UpdateError(f"{name} must be between {minimum} and {maximum}")
    return result


def _heap_size(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    if not re.fullmatch(r"[1-9]\d*[gGmM]", value):
        raise UpdateError(f"{name} must be a positive integer followed by g or m")
    return value.lower()


def _heap_mebibytes(value: str) -> int:
    amount = int(value[:-1])
    return amount * 1024 if value[-1] == "g" else amount


@dataclass(frozen=True, slots=True)
class UpdateConfiguration:
    install_root: Path
    data_root: Path
    backup_root: Path
    steamcmd: Path
    app_id: int
    branch: str
    branch_password: str
    policy: UpdatePolicy
    allow_auto_update_with_mods: bool
    mods: str
    workshop_items: str
    backup_retention: int
    pz_xms: str
    pz_xmx: str
    deployment_environment: str
    repository_deployment_marker: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> UpdateConfiguration:
        values = dict(os.environ if environment is None else environment)
        policy_raw = values.get("PZ_UPDATE_POLICY", "").strip()
        if not policy_raw:
            if "UPDATE_ON_START" in values:
                policy_raw = (
                    UpdatePolicy.STABLE_ON_START.value
                    if _boolean(values, "UPDATE_ON_START", False)
                    else UpdatePolicy.MANUAL.value
                )
            else:
                policy_raw = UpdatePolicy.STABLE_ON_START.value
        try:
            policy = UpdatePolicy(policy_raw)
        except ValueError as error:
            raise UpdateError("PZ_UPDATE_POLICY must be stable-on-start or manual") from error
        branch = values.get("STEAM_BRANCH", "").strip()
        normalized_branch = branch.lower()
        if policy is UpdatePolicy.STABLE_ON_START and normalized_branch:
            raise UpdateError(
                "PZ_UPDATE_POLICY=stable-on-start requires an empty STEAM_BRANCH; "
                "use manual for an explicit non-public branch"
            )
        branch_password = values.get("STEAM_BRANCH_PASSWORD", "")
        if branch_password and not branch:
            raise UpdateError("STEAM_BRANCH_PASSWORD requires an explicit STEAM_BRANCH")
        app_id = _integer(values, "STEAM_APP_ID", 380870, 1, 2_147_483_647)
        if app_id != 380870:
            raise UpdateError(
                "This updater supports only Project Zomboid dedicated-server App 380870"
            )
        deployment_environment = values.get("DEPLOYMENT_ENVIRONMENT", "local").strip().lower()
        if deployment_environment not in {"local", "aws"}:
            raise UpdateError("DEPLOYMENT_ENVIRONMENT must be local or aws")
        steamcmd_directory = Path(values.get("STEAMCMD_DIR", "/opt/steamcmd"))
        pz_xms = _heap_size(values, "PZ_XMS", "2g")
        pz_xmx = _heap_size(values, "PZ_XMX", "8g")
        if _heap_mebibytes(pz_xms) > _heap_mebibytes(pz_xmx):
            raise UpdateError("PZ_XMS must not exceed PZ_XMX")
        return cls(
            install_root=Path(values.get("PZ_INSTALL_ROOT", "/opt/pzserver")),
            data_root=Path(values.get("ZOMBOID_DIR", "/home/pz/Zomboid")),
            backup_root=Path(values.get("PZ_BACKUP_DIR", "/backups")),
            steamcmd=steamcmd_directory / "steamcmd.sh",
            app_id=app_id,
            branch=branch,
            branch_password=branch_password,
            policy=policy,
            allow_auto_update_with_mods=_boolean(values, "ALLOW_AUTO_UPDATE_WITH_MODS", False),
            mods=values.get("MODS", "").strip(),
            workshop_items=values.get("WORKSHOP_ITEMS", "").strip(),
            backup_retention=_integer(values, "PZ_PRE_UPDATE_BACKUP_RETENTION", 3, 2, 20),
            pz_xms=pz_xms,
            pz_xmx=pz_xmx,
            deployment_environment=deployment_environment,
            repository_deployment_marker=Path(
                values.get(
                    "PZ_REPOSITORY_DEPLOYMENT_MARKER",
                    "/run/pz-deploy/deployment-pending",
                )
            ),
        )

    @property
    def steam_branch(self) -> str:
        return "public" if not self.branch else self.branch

    @property
    def mods_configured(self) -> bool:
        return bool(self.mods or self.workshop_items)

    @property
    def state_path(self) -> Path:
        return self.install_root / STATE_FILE


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, object]:
        try:
            if self.path.is_symlink():
                raise UpdateError(f"Update state is not a safe regular file: {self.path}")
            if not self.path.exists():
                return {"schema_version": STATE_SCHEMA_VERSION, "state": "uninitialized"}
            if not self.path.is_file() or self.path.stat().st_size > 1024 * 1024:
                raise UpdateError(f"Update state is not a safe regular file: {self.path}")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateError(f"Update state is unreadable: {self.path}") from error
        if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise UpdateError(f"Update state has an unsupported schema: {self.path}")
        return value

    def write(self, state: Mapping[str, object]) -> dict[str, object]:
        value = {**state, "schema_version": STATE_SCHEMA_VERSION}
        try:
            atomic_write(
                self.path,
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                mode=0o640,
            )
        except OSError as error:
            raise UpdateError(f"Could not persist update state: {self.path}") from error
        return value


class TransactionLock:
    """An advisory lock that remains valid across container PID namespaces."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def __enter__(self) -> TransactionLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.chmod(self.path, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if "descriptor" in locals():
                os.close(descriptor)
            raise UpdateError(
                "Another Project Zomboid game update transaction is active or unavailable"
            ) from error
        self.descriptor = descriptor
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = None


def _vdf_tokens(text: str) -> list[str]:
    clean = ANSI_ESCAPE_PATTERN.sub("", text)
    tokens: list[str] = []
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"|([{}])', clean):
        if match.group(2):
            tokens.append(match.group(2))
        else:
            value = match.group(1).replace(r"\"", '"').replace(r"\\", "\\")
            tokens.append(value)
    return tokens


def _parse_vdf_object(tokens: Sequence[str], index: int) -> tuple[dict[str, object], int]:
    if index >= len(tokens) or tokens[index] != "{":
        raise UpdateError("Valve metadata object is malformed")
    result: dict[str, object] = {}
    index += 1
    while index < len(tokens) and tokens[index] != "}":
        key = tokens[index]
        index += 1
        if key in {"{", "}"} or index >= len(tokens):
            raise UpdateError("Valve metadata key/value structure is malformed")
        if tokens[index] == "{":
            value, index = _parse_vdf_object(tokens, index)
        else:
            value = tokens[index]
            index += 1
        if key in result:
            raise UpdateError(f"Valve metadata contains duplicate key {key}")
        result[key] = value
    if index >= len(tokens) or tokens[index] != "}":
        raise UpdateError("Valve metadata object is unterminated")
    return result, index + 1


def parse_vdf_root(text: str, root_key: str) -> dict[str, object]:
    tokens = _vdf_tokens(text)
    for index, token in enumerate(tokens[:-1]):
        if token == root_key and tokens[index + 1] == "{":
            result, _ = _parse_vdf_object(tokens, index + 1)
            return result
    raise UpdateError(f"Valve metadata did not contain root {root_key}")


def _nested_string(value: object, *path: str) -> str:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise UpdateError(f"Valve metadata is missing {'/'.join(path)}")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise UpdateError(f"Valve metadata field {'/'.join(path)} is malformed")
    return current


def parse_app_info_build(text: str, app_id: int, branch: str) -> int:
    app = parse_vdf_root(text, str(app_id))
    raw = _nested_string(app, "depots", "branches", branch, "buildid")
    if not raw.isdecimal() or int(raw) <= 0:
        raise UpdateError("Steam branch build ID is malformed")
    return int(raw)


@dataclass(frozen=True, slots=True)
class SteamBuildMetadata:
    build_id: int
    size_on_disk: int | None


def read_installed_metadata(release: Path, app_id: int) -> SteamBuildMetadata:
    manifest = release / "steamapps" / f"appmanifest_{app_id}.acf"
    try:
        app_state = parse_vdf_root(manifest.read_text(encoding="utf-8"), "AppState")
    except OSError as error:
        raise UpdateError(f"Installed Steam manifest is missing: {manifest}") from error
    if _nested_string(app_state, "appid") != str(app_id):
        raise UpdateError("Installed Steam manifest App ID does not match 380870")
    raw_build = _nested_string(app_state, "buildid")
    if not raw_build.isdecimal() or int(raw_build) <= 0:
        raise UpdateError("Installed Steam build ID is malformed")
    if _nested_string(app_state, "StateFlags") != "4":
        raise UpdateError("Installed Steam manifest is not in the fully-installed state")
    if app_state.get("UpdateResult") not in {None, "0"}:
        raise UpdateError("Installed Steam manifest reports an unsuccessful update")
    raw_size = app_state.get("SizeOnDisk")
    size = int(raw_size) if isinstance(raw_size, str) and raw_size.isdecimal() else None
    return SteamBuildMetadata(int(raw_build), size)


class SteamProvider(Protocol):
    def latest_build(self, branch: str) -> int: ...

    def install(
        self, destination: Path, branch: str, branch_password: str, validate: bool
    ) -> None: ...


@dataclass(slots=True)
class SteamClient:
    executable: Path
    app_id: int

    def latest_build(self, branch: str) -> int:
        command = [
            str(self.executable),
            "+login",
            "anonymous",
            "+app_info_update",
            "1",
            "+app_info_print",
            str(self.app_id),
            "+quit",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(
                f"Steam build metadata query failed: {type(error).__name__}"
            ) from error
        if result.returncode != 0:
            raise UpdateError(f"Steam build metadata query exited with status {result.returncode}")
        return parse_app_info_build(f"{result.stdout}\n{result.stderr}", self.app_id, branch)

    def install(self, destination: Path, branch: str, branch_password: str, validate: bool) -> None:
        command = [
            str(self.executable),
            "+force_install_dir",
            str(destination),
            "+login",
            "anonymous",
            "+app_update",
            str(self.app_id),
        ]
        if branch != "public":
            command.extend(("-beta", branch))
            if branch_password:
                command.extend(("-betapassword", branch_password))
        if validate:
            command.append("validate")
        command.append("+quit")
        try:
            result = subprocess.run(command, check=False, timeout=6300)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(
                f"Steam candidate installation failed: {type(error).__name__}"
            ) from error
        if result.returncode != 0:
            raise UpdateError(
                f"Steam candidate installation exited with status {result.returncode}"
            )


class LauncherValidator:
    REQUIRED_PATHS = (
        "start-server.sh",
        "ProjectZomboid64",
        "ProjectZomboid64.json",
        "jre64/bin/java",
        "java/projectzomboid.jar",
    )

    def __init__(self, syntax_checker: Callable[[Path], None] | None = None) -> None:
        self.syntax_checker = syntax_checker or self._check_bash_syntax

    def prepare(self, release: Path, xms: str, xmx: str) -> SteamBuildMetadata:
        metadata, launcher_json, wrapper, launcher_mode, wrapper_mode = self._inspect(
            release, xms, xmx
        )
        temporary_wrapper = self._validated_wrapper(release, wrapper, wrapper_mode)
        try:
            atomic_write(release / "ProjectZomboid64.json", launcher_json, mode=launcher_mode)
            os.replace(temporary_wrapper, release / ".pz-start-server.sh")
            _fsync_directory(release)
        finally:
            temporary_wrapper.unlink(missing_ok=True)
        return metadata

    def inspect(self, release: Path, xms: str, xmx: str) -> SteamBuildMetadata:
        metadata, _, wrapper, _, wrapper_mode = self._inspect(release, xms, xmx)
        temporary_wrapper = self._validated_wrapper(release, wrapper, wrapper_mode)
        temporary_wrapper.unlink(missing_ok=True)
        return metadata

    def _validated_wrapper(self, release: Path, content: str, mode: int) -> Path:
        temporary = release / f".pz-start-server.{uuid.uuid4().hex}.validate"
        try:
            atomic_write(temporary, content, mode=mode)
            self.syntax_checker(temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _inspect(
        self, release: Path, xms: str, xmx: str
    ) -> tuple[SteamBuildMetadata, str, str, int, int]:
        for relative in self.REQUIRED_PATHS:
            if not (release / relative).is_file():
                raise UpdateError(f"Candidate is incomplete; missing {relative}")
        if os.name == "posix":
            for relative in ("start-server.sh", "ProjectZomboid64", "jre64/bin/java"):
                if not os.access(release / relative, os.X_OK):
                    raise UpdateError(f"Candidate executable bit is missing on {relative}")
        metadata = read_installed_metadata(release, 380870)
        launcher_path = release / "ProjectZomboid64.json"
        wrapper_path = release / "start-server.sh"
        launcher_json = self._configured_launcher_json(launcher_path, xms, xmx)
        wrapper = self._status_preserving_wrapper(wrapper_path)
        return (
            metadata,
            launcher_json,
            wrapper,
            launcher_path.stat().st_mode & 0o777,
            wrapper_path.stat().st_mode & 0o777,
        )

    @staticmethod
    def _configured_launcher_json(path: Path, xms: str, xmx: str) -> str:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateError("ProjectZomboid64.json is missing or malformed") from error
        if (
            not isinstance(document, dict)
            or document.get("mainClass") != "zombie/network/GameServer"
        ):
            raise UpdateError("ProjectZomboid64.json no longer identifies the dedicated GameServer")
        arguments = document.get("vmArgs")
        if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
            raise UpdateError("ProjectZomboid64.json vmArgs is malformed")
        xms_indexes = [
            index for index, item in enumerate(arguments) if re.fullmatch(r"-Xms\d+[gGmM]", item)
        ]
        xmx_indexes = [
            index for index, item in enumerate(arguments) if re.fullmatch(r"-Xmx\d+[gGmM]", item)
        ]
        if len(xms_indexes) > 1 or len(xmx_indexes) != 1:
            raise UpdateError("ProjectZomboid64.json memory structure is incompatible")
        xmx_index = xmx_indexes[0]
        arguments[xmx_index] = f"-Xmx{xmx}"
        if xms_indexes:
            arguments[xms_indexes[0]] = f"-Xms{xms}"
        else:
            arguments.insert(xmx_index, f"-Xms{xms}")
        return json.dumps(document, indent=2) + "\n"

    @staticmethod
    def _status_preserving_wrapper(source: Path) -> str:
        try:
            lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as error:
            raise UpdateError("Could not read start-server.sh") from error
        stripped = [line.strip() for line in lines]
        condition = [
            index
            for index, line in enumerate(stripped)
            if re.fullmatch(r"if\s+.*jre64/bin/java.*-version.*;\s*then", line)
        ]
        invocation = [
            index
            for index, line in enumerate(stripped)
            if re.fullmatch(
                r'(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S+)\s+)*'
                r'\./ProjectZomboid64\s+"\$@"',
                line,
            )
        ]
        unsupported = [
            index
            for index, line in enumerate(stripped)
            if re.fullmatch(r"echo\s+([\"'])Only 64bit is supported\1", line)
        ]
        terminal_exit = [
            index for index, line in enumerate(stripped) if re.fullmatch(r"exit\s+0", line)
        ]
        else_lines = [index for index, line in enumerate(stripped) if line == "else"]
        fi_lines = [index for index, line in enumerate(stripped) if line == "fi"]
        counts = (condition, invocation, unsupported, terminal_exit, else_lines, fi_lines)
        if any(len(matches) != 1 for matches in counts):
            raise UpdateError("start-server.sh lifecycle structure is ambiguous or incompatible")
        condition_index = condition[0]
        invocation_index = invocation[0]
        else_index = else_lines[0]
        unsupported_index = unsupported[0]
        fi_index = fi_lines[0]
        exit_index = terminal_exit[0]
        if not (
            condition_index
            < invocation_index
            < else_index
            < unsupported_index
            < fi_index
            < exit_index
        ):
            raise UpdateError("start-server.sh lifecycle commands are in an incompatible order")
        trailing_commands = [
            line for line in stripped[exit_index + 1 :] if line and not line.startswith("#")
        ]
        if trailing_commands:
            raise UpdateError("start-server.sh has commands after its terminal exit")
        indent = lines[unsupported_index][
            : len(lines[unsupported_index]) - len(lines[unsupported_index].lstrip())
        ]
        newline = "\r\n" if lines[unsupported_index].endswith("\r\n") else "\n"
        lines[unsupported_index] = f'{indent}echo "Only 64bit is supported" >&2; exit 1{newline}'
        invocation_indent = lines[invocation_index][
            : len(lines[invocation_index]) - len(lines[invocation_index].lstrip())
        ]
        invocation_newline = "\r\n" if lines[invocation_index].endswith("\r\n") else "\n"
        exit_indent = lines[exit_index][: len(lines[exit_index]) - len(lines[exit_index].lstrip())]
        exit_newline = "\r\n" if lines[exit_index].endswith("\r\n") else "\n"
        lines[exit_index] = f'{exit_indent}exit "${{PZ_EXIT_CODE}}"{exit_newline}'
        lines.insert(
            invocation_index + 1, f"{invocation_indent}PZ_EXIT_CODE=$?{invocation_newline}"
        )
        return "".join(lines)

    @staticmethod
    def _check_bash_syntax(path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
            result = subprocess.run(
                ["bash", "-n"],
                input=content,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("Could not syntax-check the generated start wrapper") from error
        if result.returncode != 0:
            detail = " ".join(result.stderr.splitlines())[:300] or "unknown syntax error"
            raise UpdateError(f"Generated start wrapper failed Bash syntax validation: {detail}")


class BackupProvider(Protocol):
    def create(self, old_build: int | None, new_build: int) -> Path: ...


@dataclass(slots=True)
class BackupManager:
    data_root: Path
    backup_root: Path
    retention: int

    def create(self, old_build: int | None, new_build: int) -> Path:
        if not self.data_root.is_dir():
            raise UpdateError(f"Project Zomboid data directory is missing: {self.data_root}")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        old_label = str(old_build) if old_build is not None else "unknown"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"pz-pre-update-{old_label}-to-{new_build}-{timestamp}.tar.gz"
        archive = self.backup_root / name
        partial = self.backup_root / f".{name}.{os.getpid()}.partial"
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        metadata = archive.with_suffix(archive.suffix + ".json")
        if archive.exists():
            raise UpdateError(f"Pre-update backup filename collision: {archive}")
        succeeded = False
        try:
            with tarfile.open(partial, mode="w:gz") as bundle:
                bundle.add(self.data_root, arcname="data", recursive=True)
            with partial.open("r+b") as stream:
                os.fsync(stream.fileno())
            with tarfile.open(partial, mode="r:gz") as bundle:
                if not any(
                    member.name == "data" or member.name.startswith("data/") for member in bundle
                ):
                    raise UpdateError("Pre-update backup verification found no data tree")
            digest = self._sha256(partial)
            os.chmod(partial, 0o640)
            os.replace(partial, archive)
            _fsync_directory(self.backup_root)
            atomic_write(checksum, f"{digest}  {archive.name}\n", mode=0o640)
            atomic_write(
                metadata,
                json.dumps(
                    {
                        "created_at": utc_now(),
                        "new_steam_build": new_build,
                        "old_steam_build": old_build,
                        "type": "pre-update-world",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                mode=0o640,
            )
            if self._sha256(archive) != digest:
                raise UpdateError("Pre-update backup checksum verification failed")
            self._prune()
            succeeded = True
            return archive
        except (OSError, tarfile.TarError) as error:
            raise UpdateError(f"Pre-update backup failed: {type(error).__name__}") from error
        finally:
            partial.unlink(missing_ok=True)
            if not succeeded:
                archive.unlink(missing_ok=True)
                checksum.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _prune(self) -> None:
        archives = sorted(
            self.backup_root.glob("pz-pre-update-*.tar.gz"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for archive in archives[self.retention :]:
            archive.unlink()
            archive.with_suffix(archive.suffix + ".sha256").unlink(missing_ok=True)
            archive.with_suffix(archive.suffix + ".json").unlink(missing_ok=True)


def parse_player_count(response: str) -> int:
    lines = [
        line.strip() for line in ANSI_ESCAPE_PATTERN.sub("", response).splitlines() if line.strip()
    ]
    if not lines:
        raise UpdateError("RCON player response was empty")
    for pattern in PLAYER_HEADER_PATTERNS:
        if match := pattern.fullmatch(lines[0]):
            count = int(match.group(1))
            if len(lines[1:]) == count and all(line.startswith("-") for line in lines[1:]):
                return count
    raise UpdateError("RCON player response was not an exact recognized count")


def require_no_players(count: int | None) -> None:
    if count is None:
        raise UpdateError("Player state is UNKNOWN; game update refused")
    if count > 0:
        raise UpdateError(f"{count} player(s) are connected; game update refused")


class ReleaseLayout:
    MANAGED_ROOT_NAMES = {
        ACTIVE_RELEASE_FILE,
        PREVIOUS_RELEASE_FILE,
        RELEASES_DIRECTORY,
        REPOSITORY_DEPLOYMENT_MARKER,
        STATE_FILE,
        TRANSACTION_LOCK_FILE,
    }

    def __init__(self, root: Path, state_store: StateStore) -> None:
        self.root = root
        self.releases = root / RELEASES_DIRECTORY
        self.state_store = state_store

    def active(self) -> Path | None:
        return self._read_pointer(ACTIVE_RELEASE_FILE)

    def previous(self) -> Path | None:
        return self._read_pointer(PREVIOUS_RELEASE_FILE)

    def relative(self, release: Path) -> str:
        try:
            relative = release.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise UpdateError("Release path escaped the managed installation root") from error
        if len(relative.parts) != 2 or relative.parts[0] != RELEASES_DIRECTORY:
            raise UpdateError("Release pointer must identify one direct managed release")
        return relative.as_posix()

    def write_active(self, release: Path) -> None:
        atomic_write(
            self.root / ACTIVE_RELEASE_FILE,
            self.relative(release) + "\n",
            mode=0o640,
        )

    def write_previous(self, release: Path | None) -> None:
        path = self.root / PREVIOUS_RELEASE_FILE
        if release is None:
            path.unlink(missing_ok=True)
            return
        atomic_write(path, self.relative(release) + "\n", mode=0o640)

    def ensure(self, launcher: LauncherValidator, xms: str, xmx: str) -> Path | None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.releases.mkdir(parents=True, exist_ok=True)
        active = self.active()
        if active is not None:
            return active
        state = self.state_store.load()
        migration_relative = state.get("migration_release")
        if state.get("state") == "migrating-flat-install" and isinstance(migration_relative, str):
            if not re.fullmatch(r"releases/[A-Za-z0-9._-]+", migration_relative):
                raise UpdateError("Flat-install migration release is malformed")
            destination = self.root / migration_relative
            self.relative(destination)
        elif (self.root / "start-server.sh").is_file():
            metadata = launcher.inspect(self.root, xms, xmx)
            destination = self._unique_release(f"steam-{metadata.build_id}-imported")
            destination.mkdir()
            state = self.state_store.write(
                {
                    **state,
                    "state": "migrating-flat-install",
                    "migration_release": self.relative(destination),
                    "current_build": metadata.build_id,
                    "updated_at": utc_now(),
                }
            )
        else:
            return None
        destination.mkdir(parents=True, exist_ok=True)
        for entry in list(self.root.iterdir()):
            if entry.name in self.MANAGED_ROOT_NAMES:
                continue
            os.replace(entry, destination / entry.name)
        metadata = launcher.prepare(destination, xms, xmx)
        self.write_active(destination)
        self.state_store.write(
            {
                **state,
                "state": "current",
                "current_build": metadata.build_id,
                "last_result": "imported-existing-installation",
                "migration_release": None,
                "updated_at": utc_now(),
            }
        )
        return destination

    def unique_candidate(self, build_id: int) -> Path:
        return self.releases / f".candidate-{build_id}-{uuid.uuid4().hex[:12]}"

    def final_release(self, build_id: int) -> Path:
        return self._unique_release(
            f"steam-{build_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        )

    def promote_candidate(self, candidate: Path, final: Path) -> None:
        self.relative(candidate)
        self.relative(final)
        os.replace(candidate, final)
        _fsync_directory(self.releases)

    def discard_old_previous(self, active: Path) -> None:
        previous = self.previous()
        if previous is not None and previous != active and previous.exists():
            shutil.rmtree(previous)
        self.write_previous(None)

    def _read_pointer(self, name: str) -> Path | None:
        pointer = self.root / name
        try:
            if pointer.is_symlink():
                raise UpdateError(f"{name} is not a safe regular file")
            if not pointer.exists():
                return None
            if not pointer.is_file() or pointer.stat().st_size > 4096:
                raise UpdateError(f"{name} is not a safe regular file")
            value = pointer.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise UpdateError(f"Could not read {name}") from error
        if not re.fullmatch(r"releases/[A-Za-z0-9._-]+", value):
            raise UpdateError(f"{name} is malformed")
        release = self.root / value
        if not release.is_dir():
            raise UpdateError(f"{name} points to a missing release")
        self.relative(release)
        return release

    def _unique_release(self, prefix: str) -> Path:
        candidate = self.releases / prefix
        if not candidate.exists():
            return candidate
        return self.releases / f"{prefix}-{uuid.uuid4().hex[:8]}"


def _default_game_server_running() -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            continue
        if "zombie.network.GameServer" in command or "ProjectZomboid64" in command:
            return True
    return False


class GameUpdater:
    CANDIDATE_STATES = {
        "activating",
        "downloading-candidate",
        "pending-readiness",
        "promoting-candidate",
        "starting-candidate",
        "runtime-ready",
        "failed-after-world-open",
    }

    def __init__(
        self,
        configuration: UpdateConfiguration,
        steam: SteamProvider | None = None,
        launcher: LauncherValidator | None = None,
        backup: BackupProvider | None = None,
        game_server_running: Callable[[], bool] = _default_game_server_running,
    ) -> None:
        self.configuration = configuration
        self.state_store = StateStore(configuration.state_path)
        self.layout = ReleaseLayout(configuration.install_root, self.state_store)
        self.steam = steam or SteamClient(configuration.steamcmd, configuration.app_id)
        self.launcher = launcher or LauncherValidator()
        self.backup = backup or BackupManager(
            configuration.data_root,
            configuration.backup_root,
            configuration.backup_retention,
        )
        self.game_server_running = game_server_running

    def prepare_start(self) -> dict[str, object]:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self._recover_interrupted(self.state_store.load())
            active = self.layout.active()
            if state.get("state") in {
                "pending-readiness",
                "starting-candidate",
                "runtime-ready",
                "failed-after-world-open",
            }:
                if active is None:
                    raise UpdateError("Candidate update state has no active release")
                current = self.launcher.prepare(
                    active, self.configuration.pz_xms, self.configuration.pz_xmx
                )
                LOGGER.warning(
                    "Resuming Steam build %s without automatic downgrade; update state is %s",
                    current.build_id,
                    state.get("state"),
                )
                return state
            if self.configuration.repository_deployment_marker.exists():
                selected = active
                if (
                    selected is None
                    and (self.configuration.install_root / "start-server.sh").is_file()
                ):
                    selected = self.configuration.install_root
                if selected is None:
                    raise UpdateError(
                        "Repository deployment suppressed updates but no game release exists"
                    )
                current = read_installed_metadata(selected, self.configuration.app_id)
                LOGGER.info(
                    "Repository deployment is pending; game update and migration are suppressed."
                )
                return self._write_observation(state, current, "repository-deploy-suppressed")
            active = self.layout.ensure(
                self.launcher, self.configuration.pz_xms, self.configuration.pz_xmx
            )
            state = self.state_store.load()
            active = self.layout.active() or active
            if active is not None:
                current = self.launcher.prepare(
                    active, self.configuration.pz_xms, self.configuration.pz_xmx
                )
            else:
                current = None
            if active is not None and self.configuration.policy is UpdatePolicy.MANUAL:
                if state.get("state") == "failed-before-world-open":
                    LOGGER.warning(
                        "Manual policy is preserving the recorded pre-world update failure."
                    )
                    return state
                LOGGER.info(
                    "Game update policy is manual; starting the current release without a check."
                )
                return self._write_observation(state, current, "manual-policy")
            if (
                active is not None
                and self.configuration.mods_configured
                and not self.configuration.allow_auto_update_with_mods
            ):
                LOGGER.warning(
                    "Mods or Workshop items are configured; automatic Stable update is blocked "
                    "until ALLOW_AUTO_UPDATE_WITH_MODS=true or an explicit update is used."
                )
                return self._write_observation(state, current, "blocked-mods")
            try:
                return self._check_and_update(
                    active, current, state, validate=False, allow_blocked=False
                )
            except UpdateError:
                failed = self.state_store.load()
                selected = self.layout.active()
                if failed.get("state") in {"downloading-candidate", "promoting-candidate"}:
                    try:
                        failed = self._recover_interrupted(failed)
                    except UpdateError:
                        LOGGER.exception("Could not finalize interrupted candidate cleanup.")
                if (
                    active is not None
                    and selected == active
                    and failed.get("state")
                    not in {
                        "activating",
                        "pending-readiness",
                        "starting-candidate",
                        "runtime-ready",
                        "failed-after-world-open",
                    }
                ):
                    LOGGER.exception(
                        "Automatic game update failed before world access; "
                        "starting the known-good release."
                    )
                    return failed
                raise

    def explicit_update(self, validate: bool = False) -> dict[str, object]:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self._recover_interrupted(self.state_store.load())
            if self.configuration.repository_deployment_marker.exists():
                raise UpdateError("Repository deployment is pending; explicit game update refused")
            active = self.layout.ensure(
                self.launcher, self.configuration.pz_xms, self.configuration.pz_xmx
            )
            state = self.state_store.load()
            if state.get("state") in self.CANDIDATE_STATES:
                raise UpdateError(
                    "A candidate has already touched or is awaiting the real world; "
                    "another update is prohibited"
                )
            current = (
                self.launcher.prepare(active, self.configuration.pz_xms, self.configuration.pz_xmx)
                if active is not None
                else None
            )
            return self._check_and_update(
                active, current, state, validate=validate, allow_blocked=True
            )

    def active_release(self) -> Path:
        active = self.layout.active()
        if active is None and (self.configuration.install_root / "start-server.sh").is_file():
            active = self.configuration.install_root
        if active is None:
            raise UpdateError("No active Project Zomboid release is selected")
        return active

    def mark_world_opened(self) -> bool:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self.state_store.load()
            if state.get("state") not in {"pending-readiness", "starting-candidate"}:
                return False
            self._verify_candidate_active(state)
            self.state_store.write(
                {
                    **state,
                    "state": "starting-candidate",
                    "world_opened": True,
                    "world_opened_at": state.get("world_opened_at") or utc_now(),
                    "updated_at": utc_now(),
                }
            )
            return True

    def mark_runtime_ready(self, pz_version: str | None, pz_revision: str | None) -> None:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self.state_store.load()
            if state.get("state") == "current":
                self.state_store.write(
                    {
                        **state,
                        "pz_version": pz_version,
                        "pz_revision": pz_revision,
                        "runtime_ready_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                )
                return
            if state.get("state") not in {
                "pending-readiness",
                "starting-candidate",
                "runtime-ready",
            }:
                return
            if state.get("world_opened") is not True:
                raise UpdateError(
                    "Candidate readiness cannot be recorded before the world-open boundary"
                )
            self._verify_candidate_active(state)
            value = {
                **state,
                "state": "runtime-ready",
                "pz_version": pz_version,
                "pz_revision": pz_revision,
                "runtime_ready_at": utc_now(),
                "updated_at": utc_now(),
            }
            if self.configuration.deployment_environment == "local":
                self._complete_acceptance(value)
            else:
                self.state_store.write(value)

    def accept(self) -> dict[str, object]:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self.state_store.load()
            if state.get("state") != "runtime-ready":
                return state
            if state.get("world_opened") is not True:
                raise UpdateError("Candidate acceptance requires a recorded world-open boundary")
            self._verify_candidate_active(state)
            return self._complete_acceptance(state)

    def fail_readiness(self, detail: str) -> None:
        with TransactionLock(self.configuration.install_root / TRANSACTION_LOCK_FILE):
            state = self.state_store.load()
            if state.get("state") not in {
                "pending-readiness",
                "starting-candidate",
                "runtime-ready",
            }:
                return
            if state.get("world_opened") is not True:
                raise UpdateError(
                    "Readiness failure cannot cross the world-open boundary implicitly"
                )
            self._verify_candidate_active(state)
            self.state_store.write(
                {
                    **state,
                    "state": "failed-after-world-open",
                    "last_result": "readiness-failed",
                    "detail": detail[:500],
                    "updated_at": utc_now(),
                }
            )

    def status(self) -> dict[str, object]:
        state = self.state_store.load()
        active = self.layout.active()
        if active is None and (self.configuration.install_root / "start-server.sh").is_file():
            active = self.configuration.install_root
        if active is not None:
            try:
                metadata = read_installed_metadata(active, self.configuration.app_id)
                state = {**state, "current_build": metadata.build_id}
            except UpdateError:
                state = {**state, "current_build": None, "state": "unknown"}
        return {
            "state": state.get("state", "unknown"),
            "current_build": state.get("current_build"),
            "previous_build": state.get("previous_build"),
            "candidate_build": state.get("candidate_build"),
            "blocked_build": state.get("blocked_build"),
            "last_result": state.get("last_result"),
            "last_update_from_build": state.get("last_update_from_build"),
            "last_update_to_build": state.get("last_update_to_build"),
            "detail": state.get("detail"),
            "last_check_at": state.get("last_check_at"),
            "last_successful_update_at": state.get("last_successful_update_at"),
            "pz_version": state.get("pz_version"),
            "pz_revision": state.get("pz_revision"),
            "world_opened": state.get("world_opened"),
            "runtime_ready_at": state.get("runtime_ready_at"),
            "update_policy": self.configuration.policy.value,
            "steam_branch": self.configuration.steam_branch,
        }

    def _check_and_update(
        self,
        active: Path | None,
        current: SteamBuildMetadata | None,
        state: dict[str, object],
        validate: bool,
        allow_blocked: bool,
    ) -> dict[str, object]:
        branch = self.configuration.steam_branch
        LOGGER.info("Checking Project Zomboid Steam branch '%s'.", branch)
        try:
            latest = self.steam.latest_build(branch)
        except UpdateError as error:
            self._record_pre_world_failure(
                state, current, None, "metadata-query-failed", str(error)
            )
            raise
        current_build = current.build_id if current else None
        LOGGER.info("Current dedicated-server Steam build: %s", current_build or "not installed")
        LOGGER.info("Latest %s Steam build: %s", branch, latest)
        if current_build == latest and not validate:
            LOGGER.info("No Project Zomboid update required.")
            return self.state_store.write(
                {
                    **state,
                    "state": "current",
                    "current_build": current_build,
                    "candidate_build": None,
                    "blocked_build": None,
                    "detail": None,
                    "last_check_at": utc_now(),
                    "last_result": "no-update",
                    "update_policy": self.configuration.policy.value,
                    "steam_branch": branch,
                    "updated_at": utc_now(),
                }
            )
        if current_build is not None and latest < current_build:
            value = self._record_pre_world_failure(
                state,
                current,
                latest,
                "downgrade-blocked",
                "Steam branch build is older than the active build; automatic downgrade refused",
            )
            raise UpdateError(str(value["detail"]))
        if current_build is not None and state.get("blocked_build") == latest and not allow_blocked:
            raise UpdateError(
                f"Steam build {latest} was previously rejected before world open; "
                "use manual policy or a reviewed repair"
            )
        return self._stage_and_activate(active, current, latest, state, validate)

    def _stage_and_activate(
        self,
        active: Path | None,
        current: SteamBuildMetadata | None,
        latest: int,
        state: dict[str, object],
        validate: bool,
    ) -> dict[str, object]:
        if self.game_server_running():
            detail = "A Project Zomboid GameServer process is active; update refused"
            self._record_pre_world_failure(
                state, current, latest, "update-precondition-failed", detail
            )
            raise UpdateError(detail)
        try:
            if active is not None:
                self.layout.discard_old_previous(active)
            self._require_candidate_space(current)
        except (OSError, UpdateError) as error:
            detail = str(error) if isinstance(error, UpdateError) else type(error).__name__
            self._record_pre_world_failure(
                state, current, latest, "update-precondition-failed", detail
            )
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(f"Candidate precondition failed: {detail}") from error
        candidate = self.layout.unique_candidate(latest)
        try:
            candidate.mkdir(parents=True)
        except OSError as error:
            self._record_pre_world_failure(
                state,
                current,
                latest,
                "update-precondition-failed",
                type(error).__name__,
            )
            raise UpdateError("Could not create the isolated candidate directory") from error
        try:
            self.state_store.write(
                {
                    **state,
                    "state": "downloading-candidate",
                    "current_build": current.build_id if current else None,
                    "candidate_build": latest,
                    "candidate_release": self.layout.relative(candidate),
                    "last_check_at": utc_now(),
                    "update_policy": self.configuration.policy.value,
                    "steam_branch": self.configuration.steam_branch,
                    "updated_at": utc_now(),
                }
            )
        except UpdateError:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        try:
            self.steam.install(
                candidate,
                self.configuration.steam_branch,
                self.configuration.branch_password,
                validate,
            )
            candidate_metadata = self.launcher.prepare(
                candidate, self.configuration.pz_xms, self.configuration.pz_xmx
            )
            if candidate_metadata.build_id != latest:
                raise UpdateError(
                    f"Candidate manifest build {candidate_metadata.build_id} "
                    f"does not match expected {latest}"
                )
        except Exception as error:
            shutil.rmtree(candidate, ignore_errors=True)
            detail = str(error) if isinstance(error, UpdateError) else type(error).__name__
            self._record_pre_world_failure(state, current, latest, "candidate-rejected", detail)
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(f"Candidate installation failed: {detail}") from error
        LOGGER.info(
            "Project Zomboid update detected: %s -> %s",
            current.build_id if current else "none",
            latest,
        )
        backup_path: Path | None = None
        if active is not None or self._world_has_state():
            try:
                backup_path = self.backup.create(current.build_id if current else None, latest)
            except Exception as error:
                shutil.rmtree(candidate, ignore_errors=True)
                detail = str(error) if isinstance(error, UpdateError) else type(error).__name__
                self._record_pre_world_failure(state, current, latest, "backup-failed", detail)
                if isinstance(error, UpdateError):
                    raise
                raise UpdateError(f"Pre-update backup failed: {detail}") from error
            LOGGER.info("Pre-update world backup verified: %s", backup_path)
        final = self.layout.final_release(latest)
        promoting = self.state_store.write(
            {
                **state,
                "state": "promoting-candidate",
                "current_build": current.build_id if current else None,
                "previous_build": current.build_id if current else None,
                "candidate_build": latest,
                "previous_release": self.layout.relative(active) if active else None,
                "candidate_release": self.layout.relative(candidate),
                "promoted_release": self.layout.relative(final),
                "pre_update_backup": str(backup_path) if backup_path else None,
                "world_opened": False,
                "updated_at": utc_now(),
            }
        )
        try:
            self.layout.promote_candidate(candidate, final)
            activating = self.state_store.write(
                {
                    **promoting,
                    "state": "activating",
                    "candidate_release": self.layout.relative(final),
                    "promoted_release": None,
                    "updated_at": utc_now(),
                }
            )
        except (OSError, UpdateError) as error:
            for path in (candidate, final):
                if path.exists() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
            self._record_pre_world_failure(
                state, current, latest, "promotion-failed", type(error).__name__
            )
            if isinstance(error, UpdateError):
                raise
            raise UpdateError("Candidate promotion failed before activation") from error
        try:
            self.layout.write_previous(active)
            self.layout.write_active(final)
        except OSError as error:
            recovered = self._recover_activation(activating)
            if recovered.get("state") != "pending-readiness":
                raise UpdateError(
                    "Atomic candidate activation failed; the previous release remains selected"
                ) from error
        result = self.state_store.write(
            {
                **activating,
                "state": "pending-readiness",
                "current_build": latest,
                "last_result": "candidate-activated",
                "updated_at": utc_now(),
            }
        )
        LOGGER.info(
            "Candidate validation and atomic activation succeeded; runtime readiness is pending."
        )
        return result

    def _recover_interrupted(self, state: dict[str, object]) -> dict[str, object]:
        if state.get("state") not in {"downloading-candidate", "promoting-candidate"}:
            return self._recover_activation(state)
        paths: list[Path] = []
        for field in ("candidate_release", "promoted_release"):
            relative = state.get(field)
            if relative is None:
                continue
            if not isinstance(relative, str) or not re.fullmatch(
                r"releases/[A-Za-z0-9._-]+", relative
            ):
                raise UpdateError("Interrupted candidate state contains an invalid release path")
            path = self.configuration.install_root / relative
            self.layout.relative(path)
            paths.append(path)
        if not paths:
            raise UpdateError("Interrupted candidate state does not identify its staged release")
        for path in paths:
            if path.is_symlink():
                raise UpdateError("Interrupted candidate path is a symbolic link; cleanup refused")
            if path.exists():
                try:
                    shutil.rmtree(path)
                except OSError as error:
                    raise UpdateError("Could not clean an interrupted candidate release") from error
        return self.state_store.write(
            {
                **state,
                "state": "failed-before-world-open",
                "candidate_release": None,
                "promoted_release": None,
                "blocked_build": state.get("candidate_build"),
                "last_result": "candidate-interrupted",
                "detail": "Candidate preparation was interrupted before activation",
                "updated_at": utc_now(),
            }
        )

    def _recover_activation(self, state: dict[str, object]) -> dict[str, object]:
        if state.get("state") != "activating":
            return state
        active = self.layout.active()
        candidate_relative = state.get("candidate_release")
        previous_relative = state.get("previous_release")
        if active is not None and isinstance(candidate_relative, str):
            if self.layout.relative(active) == candidate_relative:
                return self.state_store.write(
                    {
                        **state,
                        "state": "pending-readiness",
                        "current_build": state.get("candidate_build"),
                        "updated_at": utc_now(),
                    }
                )
        if (active is None and previous_relative is None) or (
            active is not None
            and previous_relative is not None
            and self.layout.relative(active) == previous_relative
        ):
            if isinstance(candidate_relative, str) and re.fullmatch(
                r"releases/[A-Za-z0-9._-]+", candidate_relative
            ):
                candidate = self.configuration.install_root / candidate_relative
                self.layout.relative(candidate)
                if candidate.exists() and not candidate.is_symlink():
                    shutil.rmtree(candidate)
            return self.state_store.write(
                {
                    **state,
                    "state": "failed-before-world-open",
                    "candidate_release": None,
                    "last_result": "activation-failed",
                    "blocked_build": state.get("candidate_build"),
                    "detail": "Activation was interrupted before the candidate became active",
                    "updated_at": utc_now(),
                }
            )
        raise UpdateError("Interrupted activation left an ambiguous active release pointer")

    def _write_observation(
        self,
        state: dict[str, object],
        current: SteamBuildMetadata | None,
        result: str,
    ) -> dict[str, object]:
        return self.state_store.write(
            {
                **state,
                "state": "current" if current else "uninitialized",
                "current_build": current.build_id if current else None,
                "candidate_build": None,
                "last_result": result,
                "update_policy": self.configuration.policy.value,
                "steam_branch": self.configuration.steam_branch,
                "updated_at": utc_now(),
            }
        )

    def _record_pre_world_failure(
        self,
        state: dict[str, object],
        current: SteamBuildMetadata | None,
        candidate: int | None,
        result: str,
        detail: str,
    ) -> dict[str, object]:
        return self.state_store.write(
            {
                **state,
                "state": "failed-before-world-open",
                "current_build": current.build_id if current else None,
                "candidate_build": candidate,
                "candidate_release": None,
                "promoted_release": None,
                "blocked_build": candidate,
                "last_result": result,
                "detail": detail[:500],
                "last_check_at": utc_now(),
                "updated_at": utc_now(),
            }
        )

    def _verify_candidate_active(self, state: Mapping[str, object]) -> None:
        candidate_relative = state.get("candidate_release")
        candidate_build = state.get("candidate_build")
        active = self.layout.active()
        if (
            not isinstance(candidate_relative, str)
            or not isinstance(candidate_build, int)
            or active is None
            or self.layout.relative(active) != candidate_relative
        ):
            raise UpdateError("Candidate state does not match the active release pointer")
        metadata = read_installed_metadata(active, self.configuration.app_id)
        if metadata.build_id != candidate_build:
            raise UpdateError("Candidate state does not match the active Steam manifest")

    def _complete_acceptance(self, state: Mapping[str, object]) -> dict[str, object]:
        previous_build = state.get("previous_build")
        candidate_build = state.get("candidate_build")
        if previous_build is not None and not isinstance(previous_build, int):
            raise UpdateError("Candidate state contains an invalid previous Steam build")
        if not isinstance(candidate_build, int):
            raise UpdateError("Candidate state contains an invalid candidate Steam build")
        result = "installed" if previous_build is None else "updated"
        return self.state_store.write(
            {
                **state,
                "state": "current",
                "current_build": candidate_build,
                "candidate_build": None,
                "candidate_release": None,
                "promoted_release": None,
                "last_result": result,
                "last_update_from_build": previous_build,
                "last_update_to_build": candidate_build,
                "last_successful_update_at": utc_now(),
                "world_opened": True,
                "detail": None,
                "blocked_build": None,
                "updated_at": utc_now(),
            }
        )

    def _world_has_state(self) -> bool:
        for name in ("Saves", "db", "Server"):
            path = self.configuration.data_root / name
            try:
                if path.is_dir() and next(path.iterdir(), None) is not None:
                    return True
            except OSError:
                return True
        return False

    def _require_candidate_space(self, current: SteamBuildMetadata | None) -> None:
        try:
            free = shutil.disk_usage(self.configuration.install_root).free
        except OSError as error:
            raise UpdateError("Could not determine free space for the candidate release") from error
        estimated_size = current.size_on_disk if current and current.size_on_disk else 7 * 1024**3
        required = int(estimated_size * 1.35) + 2 * 1024**3
        if free < required:
            raise UpdateError(
                f"Insufficient free space for isolated candidate: "
                f"need {required} bytes, have {free}"
            )


def read_pz_version(log_path: Path) -> tuple[str | None, str | None]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    matches = list(VERSION_PATTERN.finditer(text))
    if not matches:
        return None, None
    return matches[-1].group(1), matches[-1].group(2)
