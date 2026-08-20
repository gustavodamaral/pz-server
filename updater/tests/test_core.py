from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pz_updater.core import (
    ACTIVE_RELEASE_FILE,
    PREVIOUS_RELEASE_FILE,
    REPOSITORY_DEPLOYMENT_MARKER,
    STATE_FILE,
    BackupManager,
    GameUpdater,
    LauncherValidator,
    StateStore,
    SteamClient,
    TransactionLock,
    UpdateConfiguration,
    UpdateError,
    UpdatePolicy,
    parse_app_info_build,
    parse_player_count,
    read_installed_metadata,
    read_pz_version,
    require_no_players,
)

START_SCRIPT = """#!/bin/bash
INSTDIR="`dirname $0`" ; cd "${INSTDIR}" ; INSTDIR="`pwd`"
if "${INSTDIR}/jre64/bin/java" -version > /dev/null 2>&1; then
\texport PATH="${INSTDIR}/jre64/bin:$PATH"
\texport LD_LIBRARY_PATH="${INSTDIR}/linux64:${INSTDIR}:${LD_LIBRARY_PATH}"
\tJSIG="libjsig.so"
\tLD_PRELOAD="${LD_PRELOAD}:${JSIG}" ./ProjectZomboid64 "$@"
else
\techo "Only 64bit is supported"
fi
exit 0
"""


def write_release(path: Path, build: int, size: int = 1024) -> Path:
    (path / "jre64" / "bin").mkdir(parents=True, exist_ok=True)
    (path / "java").mkdir(parents=True, exist_ok=True)
    (path / "steamapps").mkdir(parents=True, exist_ok=True)
    start_script = path / "start-server.sh"
    start_script.write_text(START_SCRIPT, encoding="utf-8")
    game_binary = path / "ProjectZomboid64"
    game_binary.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    java = path / "jre64" / "bin" / "java"
    java.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for executable in (start_script, game_binary, java):
        executable.chmod(0o755)
    (path / "java" / "projectzomboid.jar").write_bytes(b"jar")
    (path / "ProjectZomboid64.json").write_text(
        json.dumps(
            {
                "mainClass": "zombie/network/GameServer",
                "classpath": ["java/.", "java/projectzomboid.jar"],
                "vmArgs": ["-Djava.awt.headless=true", "-Xms1g", "-Xmx2g"],
            }
        ),
        encoding="utf-8",
    )
    (path / "steamapps" / "appmanifest_380870.acf").write_text(
        f'''"AppState"
{{
    "appid" "380870"
    "StateFlags" "4"
    "SizeOnDisk" "{size}"
    "buildid" "{build}"
    "UpdateResult" "0"
}}
''',
        encoding="utf-8",
    )
    return path


def configuration(
    tmp_path: Path,
    *,
    policy: UpdatePolicy = UpdatePolicy.STABLE_ON_START,
    deployment_environment: str = "local",
    mods: str = "",
    workshop_items: str = "",
    allow_mods: bool = False,
) -> UpdateConfiguration:
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    backup_root = tmp_path / "backups"
    install_root.mkdir(exist_ok=True)
    data_root.mkdir(exist_ok=True)
    return UpdateConfiguration(
        install_root=install_root,
        data_root=data_root,
        backup_root=backup_root,
        steamcmd=tmp_path / "steamcmd.sh",
        app_id=380870,
        branch="",
        branch_password="",
        policy=policy,
        allow_auto_update_with_mods=allow_mods,
        mods=mods,
        workshop_items=workshop_items,
        backup_retention=3,
        pz_xms="2g",
        pz_xmx="6g",
        deployment_environment=deployment_environment,
        repository_deployment_marker=install_root / REPOSITORY_DEPLOYMENT_MARKER,
    )


@dataclass
class FakeSteam:
    latest: int | Exception
    installed_build: int | None = None
    install_error: Exception | None = None
    latest_calls: list[str] = field(default_factory=list)
    install_calls: list[tuple[Path, str, str, bool]] = field(default_factory=list)

    def latest_build(self, branch: str) -> int:
        self.latest_calls.append(branch)
        if isinstance(self.latest, Exception):
            raise self.latest
        return self.latest

    def install(self, destination: Path, branch: str, branch_password: str, validate: bool) -> None:
        self.install_calls.append((destination, branch, branch_password, validate))
        if self.install_error is not None:
            raise self.install_error
        build = self.installed_build if self.installed_build is not None else self.latest
        assert isinstance(build, int)
        write_release(destination, build)


@dataclass
class FakeBackup:
    root: Path
    error: Exception | None = None
    calls: list[tuple[int | None, int]] = field(default_factory=list)

    def create(self, old_build: int | None, new_build: int) -> Path:
        self.calls.append((old_build, new_build))
        if self.error is not None:
            raise self.error
        self.root.mkdir(parents=True, exist_ok=True)
        result = self.root / f"fake-{old_build}-{new_build}.tar.gz"
        result.write_bytes(b"verified")
        return result


def select_release(config: UpdateConfiguration, build: int, name: str | None = None) -> Path:
    release = config.install_root / "releases" / (name or f"steam-{build}")
    write_release(release, build)
    (config.install_root / ACTIVE_RELEASE_FILE).write_text(
        f"releases/{release.name}\n", encoding="utf-8"
    )
    StateStore(config.state_path).write(
        {"state": "current", "current_build": build, "schema_version": 1}
    )
    return release


def updater(
    config: UpdateConfiguration,
    steam: FakeSteam,
    backup: FakeBackup | None = None,
) -> GameUpdater:
    result = GameUpdater(
        config,
        steam=steam,
        launcher=LauncherValidator(syntax_checker=lambda _: None),
        backup=backup or FakeBackup(config.backup_root),
        game_server_running=lambda: False,
    )
    result._require_candidate_space = lambda _: None  # type: ignore[method-assign]
    return result


def test_configuration_defaults_to_stable_and_migrates_legacy_flag(tmp_path: Path) -> None:
    base = {
        "PZ_INSTALL_ROOT": str(tmp_path / "install"),
        "ZOMBOID_DIR": str(tmp_path / "data"),
        "PZ_BACKUP_DIR": str(tmp_path / "backups"),
    }
    assert UpdateConfiguration.from_environment(base).policy is UpdatePolicy.STABLE_ON_START
    assert (
        UpdateConfiguration.from_environment({**base, "UPDATE_ON_START": "false"}).policy
        is UpdatePolicy.MANUAL
    )
    assert (
        UpdateConfiguration.from_environment({**base, "UPDATE_ON_START": "true"}).policy
        is UpdatePolicy.STABLE_ON_START
    )
    explicit = UpdateConfiguration.from_environment(
        {**base, "PZ_UPDATE_POLICY": "stable-on-start", "UPDATE_ON_START": "false"}
    )
    assert explicit.policy is UpdatePolicy.STABLE_ON_START


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"PZ_UPDATE_POLICY": "stable-on-start", "STEAM_BRANCH": "unstable"}, "empty"),
        ({"STEAM_BRANCH_PASSWORD": "secret"}, "requires"),
        ({"PZ_XMS": "8g", "PZ_XMX": "6g"}, "must not exceed"),
        ({"PZ_XMS": "0g"}, "positive integer"),
        ({"STEAM_APP_ID": "108600"}, "380870"),
    ],
)
def test_configuration_rejects_unsafe_values(
    tmp_path: Path, values: dict[str, str], message: str
) -> None:
    values["PZ_INSTALL_ROOT"] = str(tmp_path / "install")
    with pytest.raises(UpdateError, match=message):
        UpdateConfiguration.from_environment(values)


def test_manual_policy_allows_an_explicit_branch(tmp_path: Path) -> None:
    result = UpdateConfiguration.from_environment(
        {
            "PZ_INSTALL_ROOT": str(tmp_path / "install"),
            "PZ_UPDATE_POLICY": "manual",
            "STEAM_BRANCH": "unstable",
            "STEAM_BRANCH_PASSWORD": "secret",
        }
    )
    assert result.steam_branch == "unstable"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"ALLOW_AUTO_UPDATE_WITH_MODS": "maybe"}, "true or false"),
        ({"PZ_PRE_UPDATE_BACKUP_RETENTION": "many"}, "integer"),
        ({"PZ_PRE_UPDATE_BACKUP_RETENTION": "1"}, "between 2 and 20"),
        ({"DEPLOYMENT_ENVIRONMENT": "production"}, "local or aws"),
        ({"PZ_UPDATE_POLICY": "automatic"}, "stable-on-start or manual"),
    ],
)
def test_configuration_rejects_malformed_policy_controls(
    tmp_path: Path, values: dict[str, str], message: str
) -> None:
    values["PZ_INSTALL_ROOT"] = str(tmp_path / "install")
    with pytest.raises(UpdateError, match=message):
        UpdateConfiguration.from_environment(values)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("Players connected (0):", 0),
        ("Players connected ( 1 ):\n-alice", 1),
        ("Players connected: 2\n-alice\n-bob", 2),
    ],
)
def test_player_count_is_exact(response: str, expected: int) -> None:
    assert parse_player_count(response) == expected


@pytest.mark.parametrize(
    "response",
    ["", "unknown", "Players connected (0):\n-alice", "prefix Players connected (0):"],
)
def test_player_count_never_guesses_zero(response: str) -> None:
    with pytest.raises(UpdateError):
        parse_player_count(response)
    with pytest.raises(UpdateError):
        require_no_players(None)


def test_player_gate_rejects_connected_players() -> None:
    require_no_players(0)
    with pytest.raises(UpdateError, match="2 player"):
        require_no_players(2)


def test_app_info_parser_uses_the_requested_branch() -> None:
    text = """"380870"
{
  "depots"
  {
    "branches"
    {
      "public" { "buildid" "24775771" }
      "unstable" { "buildid" "24770000" }
    }
  }
}
"""
    assert parse_app_info_build(text, 380870, "public") == 24775771
    assert parse_app_info_build(text, 380870, "unstable") == 24770000


def test_vdf_parser_rejects_duplicate_or_missing_builds() -> None:
    duplicate = """"380870" { "depots" { "branches" {
      "public" { "buildid" "1" "buildid" "2" }
    } } }"""
    with pytest.raises(UpdateError, match="duplicate"):
        parse_app_info_build(duplicate, 380870, "public")
    with pytest.raises(UpdateError, match="missing"):
        parse_app_info_build('"380870" { "depots" { } }', 380870, "public")


def test_steam_client_queries_structured_app_info(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [
            str(Path("/steam/steamcmd.sh")),
            "+login",
            "anonymous",
            "+app_info_update",
            "1",
            "+app_info_print",
            "380870",
            "+quit",
        ]
        assert kwargs["timeout"] == 300
        output = '"380870" { "depots" { "branches" { "public" { "buildid" "200" } } } }'
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SteamClient(Path("/steam/steamcmd.sh"), 380870).latest_build("public") == 200


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_steam_metadata_query_failure_is_safe(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if failure == "timeout":
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("steamcmd", 300)
            ),
        )
        message = "TimeoutExpired"
    else:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 9, "", "failed"),
        )
        message = "status 9"
    with pytest.raises(UpdateError, match=message):
        SteamClient(Path("steamcmd"), 380870).latest_build("public")


@pytest.mark.parametrize(
    ("branch", "password", "validate", "expected_tail"),
    [
        ("public", "", False, ["380870", "+quit"]),
        (
            "unstable",
            "branch-secret",
            True,
            ["380870", "-beta", "unstable", "-betapassword", "branch-secret", "validate", "+quit"],
        ),
    ],
)
def test_steam_candidate_command_is_explicit(
    tmp_path: Path,
    branch: str,
    password: str,
    validate: bool,
    expected_tail: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[-len(expected_tail) :] == expected_tail
        assert command[1:3] == ["+force_install_dir", str(tmp_path)]
        assert kwargs["timeout"] == 6300
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SteamClient(Path("steamcmd"), 380870).install(tmp_path, branch, password, validate)


def test_steam_candidate_nonzero_exit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 8, "", "failed"),
    )
    with pytest.raises(UpdateError, match="status 8"):
        SteamClient(Path("steamcmd"), 380870).install(tmp_path, "public", "", False)


def test_launcher_is_structurally_prepared_and_propagates_exit(tmp_path: Path) -> None:
    release = write_release(tmp_path / "release", 100)
    validator = LauncherValidator(syntax_checker=lambda _: None)
    assert validator.prepare(release, "2g", "6g").build_id == 100
    document = json.loads((release / "ProjectZomboid64.json").read_text(encoding="utf-8"))
    assert "-Xms2g" in document["vmArgs"]
    assert "-Xmx6g" in document["vmArgs"]
    wrapper = (release / ".pz-start-server.sh").read_text(encoding="utf-8")
    assert "PZ_EXIT_CODE=$?" in wrapper
    assert 'exit "${PZ_EXIT_CODE}"' in wrapper
    assert 'echo "Only 64bit is supported" >&2; exit 1' in wrapper
    if os.name != "nt" and shutil.which("bash"):
        result = subprocess.run(
            ["bash", str(release / ".pz-start-server.sh")],
            cwd=release,
            check=False,
            capture_output=True,
        )
        assert result.returncode == 7


def test_launcher_rejection_does_not_mutate_json(tmp_path: Path) -> None:
    release = write_release(tmp_path / "release", 100)
    original = (release / "ProjectZomboid64.json").read_bytes()
    with (release / "start-server.sh").open("a", encoding="utf-8") as stream:
        stream.write('./ProjectZomboid64 "$@"\n')
    validator = LauncherValidator(syntax_checker=lambda _: None)
    with pytest.raises(UpdateError, match="ambiguous"):
        validator.prepare(release, "2g", "6g")
    assert (release / "ProjectZomboid64.json").read_bytes() == original
    assert not (release / ".pz-start-server.sh").exists()


def test_installed_manifest_must_be_complete(tmp_path: Path) -> None:
    release = write_release(tmp_path / "release", 100)
    manifest = release / "steamapps" / "appmanifest_380870.acf"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('"4"', '"6"'), encoding="utf-8"
    )
    with pytest.raises(UpdateError, match="fully-installed"):
        read_installed_metadata(release, 380870)


def test_missing_or_wrong_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="missing"):
        read_installed_metadata(tmp_path, 380870)
    release = write_release(tmp_path / "release", 100)
    manifest = release / "steamapps" / "appmanifest_380870.acf"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('"380870"', '"108600"'),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError, match="does not match"):
        read_installed_metadata(release, 380870)


def test_backup_is_verified_restricted_and_retained(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "Saves").mkdir()
    (data / "Saves" / "world.bin").write_bytes(b"world")
    backups = tmp_path / "backups"
    manager = BackupManager(data, backups, retention=2)
    archives = [manager.create(index, index + 1) for index in range(1, 4)]
    remaining = list(backups.glob("pz-pre-update-*.tar.gz"))
    assert len(remaining) == 2
    assert archives[0] not in remaining
    latest = archives[-1]
    if os.name != "nt":
        assert stat.S_IMODE(latest.stat().st_mode) == 0o640
    with tarfile.open(latest, "r:gz") as bundle:
        assert "data/Saves/world.bin" in bundle.getnames()
    digest = hashlib.sha256(latest.read_bytes()).hexdigest()
    checksum = latest.with_suffix(latest.suffix + ".sha256").read_text(encoding="utf-8")
    assert checksum == f"{digest}  {latest.name}\n"


def test_backup_verification_failure_removes_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "world").write_text("state", encoding="utf-8")
    backups = tmp_path / "backups"
    manager = BackupManager(data, backups, retention=2)
    digests = iter(("a", "b"))
    monkeypatch.setattr(BackupManager, "_sha256", staticmethod(lambda _: next(digests)))
    with pytest.raises(UpdateError, match="checksum"):
        manager.create(100, 200)
    assert not list(backups.iterdir())


def test_backup_requires_an_existing_data_tree(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="missing"):
        BackupManager(tmp_path / "missing", tmp_path / "backups", 2).create(100, 200)


def test_repository_marker_suppresses_migration_and_metadata_query(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    write_release(config.install_root, 100)
    config.repository_deployment_marker.touch()
    steam = FakeSteam(200)
    state = updater(config, steam).prepare_start()
    assert state["last_result"] == "repository-deploy-suppressed"
    assert not steam.latest_calls
    assert (config.install_root / "start-server.sh").exists()
    assert not (config.install_root / ACTIVE_RELEASE_FILE).exists()
    assert not (config.install_root / "releases").exists()


def test_flat_install_migrates_only_after_structural_inspection(tmp_path: Path) -> None:
    config = configuration(tmp_path, policy=UpdatePolicy.MANUAL)
    write_release(config.install_root, 100)
    steam = FakeSteam(200)
    target = updater(config, steam)
    state = target.prepare_start()
    active = target.active_release()
    assert active.parent == config.install_root / "releases"
    assert not (config.install_root / "start-server.sh").exists()
    assert state["last_result"] == "manual-policy"
    assert not steam.latest_calls
    document = json.loads((active / "ProjectZomboid64.json").read_text(encoding="utf-8"))
    assert "-Xms2g" in document["vmArgs"]
    assert "-Xmx6g" in document["vmArgs"]


def test_invalid_flat_install_is_not_moved_or_mutated(tmp_path: Path) -> None:
    config = configuration(tmp_path, policy=UpdatePolicy.MANUAL)
    write_release(config.install_root, 100)
    original = (config.install_root / "ProjectZomboid64.json").read_bytes()
    with (config.install_root / "start-server.sh").open("a", encoding="utf-8") as stream:
        stream.write('./ProjectZomboid64 "$@"\n')
    with pytest.raises(UpdateError, match="ambiguous"):
        updater(config, FakeSteam(100)).prepare_start()
    assert (config.install_root / "start-server.sh").exists()
    assert (config.install_root / "ProjectZomboid64.json").read_bytes() == original
    assert not (config.install_root / ACTIVE_RELEASE_FILE).exists()


def test_no_update_starts_current_release_without_backup(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    active = select_release(config, 100)
    steam = FakeSteam(100)
    backup = FakeBackup(config.backup_root)
    target = updater(config, steam, backup)
    state = target.prepare_start()
    assert state["state"] == "current"
    assert state["last_result"] == "no-update"
    assert target.active_release() == active
    assert steam.latest_calls == ["public"]
    assert not steam.install_calls
    assert not backup.calls


def test_local_update_activates_backs_up_and_records_correct_builds(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    steam = FakeSteam(200)
    backup = FakeBackup(config.backup_root)
    target = updater(config, steam, backup)
    pending = target.prepare_start()
    active = target.active_release()
    assert pending["state"] == "pending-readiness"
    assert pending["previous_build"] == 100
    assert pending["candidate_build"] == 200
    assert active != old
    assert read_installed_metadata(active, 380870).build_id == 200
    assert (config.install_root / PREVIOUS_RELEASE_FILE).read_text().strip() == (
        "releases/steam-100"
    )
    assert backup.calls == [(100, 200)]
    assert target.mark_world_opened() is True
    target.mark_runtime_ready("42.20.3", "abc123")
    accepted = target.status()
    assert accepted["state"] == "current"
    assert accepted["last_result"] == "updated"
    assert accepted["last_update_from_build"] == 100
    assert accepted["last_update_to_build"] == 200
    assert accepted["pz_version"] == "42.20.3"


def test_initial_install_is_recorded_as_installed_without_empty_world_backup(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    backup = FakeBackup(config.backup_root)
    target = updater(config, FakeSteam(200), backup)
    assert target.prepare_start()["state"] == "pending-readiness"
    assert not backup.calls
    target.mark_world_opened()
    target.mark_runtime_ready("42.20.3", "abc123")
    state = target.status()
    assert state["last_result"] == "installed"
    assert state["last_update_from_build"] is None
    assert state["last_update_to_build"] == 200


def test_aws_candidate_requires_external_acceptance(tmp_path: Path) -> None:
    config = configuration(tmp_path, deployment_environment="aws")
    select_release(config, 100)
    target = updater(config, FakeSteam(200))
    target.prepare_start()
    target.mark_world_opened()
    target.mark_runtime_ready("42.20.3", "abc123")
    assert target.status()["state"] == "runtime-ready"
    accepted = target.accept()
    assert accepted["state"] == "current"
    assert accepted["last_update_from_build"] == 100


def test_acceptance_before_runtime_readiness_is_a_noop(tmp_path: Path) -> None:
    config = configuration(tmp_path, deployment_environment="aws")
    select_release(config, 100)
    target = updater(config, FakeSteam(200))
    target.prepare_start()
    assert target.accept()["state"] == "pending-readiness"


def test_candidate_failure_starts_known_good_and_explicit_retry_is_allowed(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    steam = FakeSteam(200, install_error=UpdateError("download failed"))
    target = updater(config, steam)
    failed = target.prepare_start()
    assert failed["state"] == "failed-before-world-open"
    assert failed["blocked_build"] == 200
    assert target.active_release() == old
    assert not list((config.install_root / "releases").glob(".candidate-*"))
    steam.install_error = None
    retried = target.explicit_update()
    assert retried["state"] == "pending-readiness"
    assert read_installed_metadata(target.active_release(), 380870).build_id == 200


def test_candidate_space_failure_starts_known_good_and_is_recorded(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    target = updater(config, FakeSteam(200))

    def no_space(_: object) -> None:
        raise UpdateError("insufficient test space")

    target._require_candidate_space = no_space  # type: ignore[method-assign]
    state = target.prepare_start()
    assert state["last_result"] == "update-precondition-failed"
    assert "insufficient" in str(state["detail"])
    assert target.active_release() == old


def test_manual_policy_preserves_explicit_update_failure_state(tmp_path: Path) -> None:
    config = configuration(tmp_path, policy=UpdatePolicy.MANUAL)
    old = select_release(config, 100)
    StateStore(config.state_path).write(
        {
            "state": "failed-before-world-open",
            "current_build": 100,
            "candidate_build": 200,
            "blocked_build": 200,
            "last_result": "candidate-rejected",
            "detail": "review required",
        }
    )
    target = updater(config, FakeSteam(200))
    state = target.prepare_start()
    assert state["state"] == "failed-before-world-open"
    assert state["last_result"] == "candidate-rejected"
    assert target.active_release() == old


def test_backup_failure_never_changes_active_release(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    backup = FakeBackup(config.backup_root, error=UpdateError("disk full"))
    target = updater(config, FakeSteam(200), backup)
    failed = target.prepare_start()
    assert failed["last_result"] == "backup-failed"
    assert target.active_release() == old
    assert read_installed_metadata(old, 380870).build_id == 100


def test_downgrade_is_blocked_without_preventing_known_good_start(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 200)
    steam = FakeSteam(100)
    state = updater(config, steam).prepare_start()
    assert state["last_result"] == "downgrade-blocked"
    assert (config.install_root / ACTIVE_RELEASE_FILE).read_text().strip() == (
        f"releases/{old.name}"
    )
    assert not steam.install_calls


def test_mods_block_automatic_update_but_not_current_start(tmp_path: Path) -> None:
    config = configuration(tmp_path, mods="example-mod")
    old = select_release(config, 100)
    steam = FakeSteam(200)
    state = updater(config, steam).prepare_start()
    assert state["last_result"] == "blocked-mods"
    assert not steam.latest_calls
    assert (config.install_root / old.relative_to(config.install_root)).exists()


def test_interrupted_download_is_cleaned_and_blocked(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    candidate = write_release(config.install_root / "releases" / ".candidate-200-test", 200)
    StateStore(config.state_path).write(
        {
            "state": "downloading-candidate",
            "current_build": 100,
            "candidate_build": 200,
            "candidate_release": "releases/.candidate-200-test",
        }
    )
    state = updater(config, FakeSteam(200)).prepare_start()
    assert state["last_result"] == "candidate-interrupted"
    assert not candidate.exists()
    assert (config.install_root / ACTIVE_RELEASE_FILE).read_text().strip() == (
        f"releases/{old.name}"
    )


def test_interrupted_initial_install_is_retried_because_no_release_exists(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    candidate = write_release(config.install_root / "releases" / ".candidate-200-test", 200)
    StateStore(config.state_path).write(
        {
            "state": "downloading-candidate",
            "current_build": None,
            "candidate_build": 200,
            "candidate_release": "releases/.candidate-200-test",
        }
    )
    target = updater(config, FakeSteam(200))
    state = target.prepare_start()
    assert not candidate.exists()
    assert state["state"] == "pending-readiness"
    assert read_installed_metadata(target.active_release(), 380870).build_id == 200


def test_interrupted_activation_finishes_only_when_candidate_pointer_won(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    candidate = write_release(config.install_root / "releases" / "steam-200-candidate", 200)
    (config.install_root / PREVIOUS_RELEASE_FILE).write_text(
        f"releases/{old.name}\n", encoding="utf-8"
    )
    (config.install_root / ACTIVE_RELEASE_FILE).write_text(
        f"releases/{candidate.name}\n", encoding="utf-8"
    )
    StateStore(config.state_path).write(
        {
            "state": "activating",
            "current_build": 100,
            "previous_build": 100,
            "candidate_build": 200,
            "previous_release": f"releases/{old.name}",
            "candidate_release": f"releases/{candidate.name}",
            "world_opened": False,
        }
    )
    state = updater(config, FakeSteam(200)).prepare_start()
    assert state["state"] == "pending-readiness"
    assert candidate.exists()


def test_interrupted_activation_discards_unselected_candidate(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    candidate = write_release(config.install_root / "releases" / "steam-200-candidate", 200)
    StateStore(config.state_path).write(
        {
            "state": "activating",
            "current_build": 100,
            "previous_build": 100,
            "candidate_build": 200,
            "previous_release": f"releases/{old.name}",
            "candidate_release": f"releases/{candidate.name}",
            "world_opened": False,
        }
    )
    state = updater(config, FakeSteam(200)).prepare_start()
    assert state["last_result"] == "activation-failed"
    assert not candidate.exists()
    assert (config.install_root / ACTIVE_RELEASE_FILE).read_text().strip() == (
        f"releases/{old.name}"
    )


def test_readiness_failure_after_world_open_never_rolls_back(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    target = updater(config, FakeSteam(200))
    target.prepare_start()
    candidate = target.active_release()
    target.mark_world_opened()
    target.fail_readiness("RCON did not become ready")
    state = target.prepare_start()
    assert state["state"] == "failed-after-world-open"
    assert target.active_release() == candidate
    assert target.active_release() != old


def test_world_open_marker_rejects_pointer_mismatch(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    old = select_release(config, 100)
    StateStore(config.state_path).write(
        {
            "state": "pending-readiness",
            "current_build": 200,
            "previous_build": 100,
            "candidate_build": 200,
            "candidate_release": "releases/missing-candidate",
            "previous_release": f"releases/{old.name}",
            "world_opened": False,
        }
    )
    with pytest.raises(UpdateError, match="active release pointer"):
        updater(config, FakeSteam(200)).mark_world_opened()


def test_transaction_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "transaction.lock"
    with TransactionLock(path):
        with pytest.raises(UpdateError, match="transaction"):
            with TransactionLock(path):
                pytest.fail("competing lock unexpectedly succeeded")
    with TransactionLock(path):
        assert path.exists()


def test_state_store_rejects_oversized_state(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(UpdateError, match="safe regular file"):
        StateStore(path).load()


def test_state_store_rejects_directory_and_unsupported_schema(tmp_path: Path) -> None:
    directory = tmp_path / "directory-state"
    directory.mkdir()
    with pytest.raises(UpdateError, match="safe regular file"):
        StateStore(directory).load()
    path = tmp_path / STATE_FILE
    path.write_text('{"schema_version":99,"state":"current"}', encoding="utf-8")
    with pytest.raises(UpdateError, match="unsupported schema"):
        StateStore(path).load()


def test_version_reader_uses_the_latest_version_line(tmp_path: Path) -> None:
    log = tmp_path / "console.txt"
    log.write_text(
        "version=42.19.0 deadbeef demo=false\nnoise\nversion=42.20.3 70207f62e0 demo=false\n",
        encoding="utf-8",
    )
    assert read_pz_version(log) == ("42.20.3", "70207f62e0")


def test_state_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    StateStore(path).write({"state": "current"})
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
