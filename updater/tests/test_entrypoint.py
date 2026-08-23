from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="requires Linux process groups")


def _entrypoint_without_main(tmp_path: Path) -> Path:
    source = Path("docker/entrypoint.sh").read_text(encoding="utf-8")
    target = tmp_path / "entrypoint-functions.sh"
    target.write_text(source.replace('\nmain "$@"\n', "\n"), encoding="utf-8")
    return target


def _fake_runtime(tmp_path: Path, readiness_exit: int) -> tuple[Path, dict[str, str]]:
    commands = tmp_path / "commands"
    commands.mkdir()
    updater = commands / "pz-updater"
    updater.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  log-fingerprint) printf missing\\n ;;\n"
        "  mark-world-opened) exit 0 ;;\n"
        f"  wait-runtime-ready) exit {readiness_exit} ;;\n"
        "  fail-readiness) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    updater.chmod(0o755)
    release = tmp_path / "release"
    release.mkdir()
    wrapper = release / ".pz-start-server.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "while IFS= read -r command; do\n"
        "  [[ $command == quit ]] && exit 0\n"
        "done\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    zombied = tmp_path / "zomboid"
    zombied.mkdir()
    environment = os.environ | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "PZ_SERVER_NAME": "test",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "test-password",
        "PZ_XMS": "2g",
        "PZ_XMX": "6g",
        "ZOMBOID_DIR": str(zombied),
        "SHUTDOWN_SAVE_SECONDS": "0",
        "SHUTDOWN_GRACE_SECONDS": "2",
    }
    return release, environment


def test_prepare_release_ignores_noisy_prepare_stdout(tmp_path: Path) -> None:
    entrypoint = _entrypoint_without_main(tmp_path)
    server_root = tmp_path / "server"
    release = server_root / "releases" / "steam-24775771"
    release.mkdir(parents=True)
    wrapper = release / ".pz-start-server.sh"
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    wrapper.chmod(0o755)

    commands = tmp_path / "commands"
    commands.mkdir()
    updater = commands / "pz-updater"
    updater.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  prepare-start) printf 'Steam Console Client...\\nUpdate state (0x61) downloading\\n' ;;\n"
        f"  active-release) printf '%s\\n' '{release}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    updater.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "PZ_SERVER_DIR": str(server_root),
    }

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; prepare_release; printf "%s\\n" "$selected_release"',
            "bash",
            str(entrypoint),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(release)
    assert "Steam Console Client" in result.stderr
    assert "Update state (0x61) downloading" in result.stderr


def test_readiness_failure_stops_current_candidate_nonzero(tmp_path: Path) -> None:
    entrypoint = _entrypoint_without_main(tmp_path)
    release, environment = _fake_runtime(tmp_path, readiness_exit=1)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; run_server "$2"', "bash", str(entrypoint), str(release)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert result.returncode != 0
    assert (
        "Runtime readiness failed; stopping the current candidate without rollback."
        in result.stdout
    )
    assert "Project Zomboid was stopped because runtime readiness failed." in result.stdout


def test_sigterm_during_readiness_is_a_graceful_intentional_stop(tmp_path: Path) -> None:
    entrypoint = _entrypoint_without_main(tmp_path)
    release, environment = _fake_runtime(tmp_path, readiness_exit=0)
    process = subprocess.Popen(
        ["bash", "-c", 'source "$1"; run_server "$2"', "bash", str(entrypoint), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    time.sleep(0.5)
    process.terminate()
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    assert "Project Zomboid stopped after a graceful shutdown request." in stdout
