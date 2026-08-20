from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pz_updater.core import StateStore
from pz_watchdog.metrics import (
    MetricsCollector,
    StatusSnapshot,
    format_status,
    human_bytes,
    human_duration,
    read_update_status,
    snapshot_json,
)
from pz_watchdog.models import PlayerObservation
from pz_watchdog.service import ServiceHealth


def write_manifest(release: Path, build: int) -> None:
    (release / "steamapps").mkdir(parents=True)
    (release / "steamapps" / "appmanifest_380870.acf").write_text(
        f'''"AppState"
{{
    "appid" "380870"
    "StateFlags" "4"
    "buildid" "{build}"
    "UpdateResult" "0"
}}
''',
        encoding="utf-8",
    )


def test_absent_update_state_is_explicitly_uninitialized(tmp_path: Path) -> None:
    status = read_update_status(tmp_path, "stable-on-start", "public")
    assert status == {
        "state": "uninitialized",
        "current_build": None,
        "candidate_build": None,
        "blocked_build": None,
        "last_result": None,
        "last_update_from_build": None,
        "last_update_to_build": None,
        "last_check_at": None,
        "last_successful_update_at": None,
        "update_policy": "stable-on-start",
        "steam_branch": "public",
        "pz_version": None,
        "pz_revision": None,
        "detail": None,
    }


def test_update_status_uses_active_manifest_and_whitelists_state(tmp_path: Path) -> None:
    release = tmp_path / "releases" / "steam-200"
    write_manifest(release, 200)
    (tmp_path / "active-release").write_text("releases/steam-200\n", encoding="utf-8")
    StateStore(tmp_path / "update-state.json").write(
        {
            "state": "current",
            "current_build": 100,
            "last_result": "updated",
            "pz_version": "42.20.3",
            "secret_internal_path": "/should/not/appear",
        }
    )
    status = read_update_status(tmp_path, "manual", "public")
    assert status["state"] == "current"
    assert status["current_build"] == 200
    assert status["last_result"] == "updated"
    assert status["pz_version"] == "42.20.3"
    assert "secret_internal_path" not in status


def test_legacy_flat_install_build_is_visible(tmp_path: Path) -> None:
    write_manifest(tmp_path, 100)
    (tmp_path / "start-server.sh").touch()
    status = read_update_status(tmp_path, "manual", "public")
    assert status["state"] == "uninitialized"
    assert status["current_build"] == 100


def test_malformed_or_inconsistent_update_state_is_unknown(tmp_path: Path) -> None:
    state_path = tmp_path / "update-state.json"
    state_path.write_text("not-json", encoding="utf-8")
    malformed = read_update_status(tmp_path, "stable-on-start", "public")
    assert malformed["state"] == "unknown"
    assert malformed["current_build"] is None
    assert "unreadable" in str(malformed["detail"])

    StateStore(state_path).write({"state": "current", "current_build": 100})
    missing_release = read_update_status(tmp_path, "stable-on-start", "public")
    assert missing_release["state"] == "unknown"
    assert "no selected" in str(missing_release["detail"])


def test_invalid_public_field_type_is_unknown(tmp_path: Path) -> None:
    StateStore(tmp_path / "update-state.json").write(
        {"state": "current", "candidate_build": "not-an-integer"}
    )
    status = read_update_status(tmp_path, "stable-on-start", "public")
    assert status["state"] == "unknown"
    assert "candidate_build" in str(status["detail"])


def test_human_and_json_status_include_update_contract() -> None:
    snapshot = StatusSnapshot(
        status="online",
        players=0,
        player_state="known",
        cpu_total_percent=10.0,
        cpu_per_core_percent=[10.0],
        hottest_core_percent=10.0,
        pz_process_memory_bytes=1024,
        system_memory_used_bytes=2048,
        system_memory_total_bytes=4096,
        system_memory_available_bytes=2048,
        server_uptime_seconds=60,
        container_uptime_seconds=30,
        disk_used_bytes=100,
        disk_total_bytes=1000,
        disk_free_bytes=900,
        update={
            "state": "current",
            "current_build": 24775771,
            "candidate_build": None,
            "update_policy": "stable-on-start",
            "steam_branch": "public",
        },
        collected_at="2026-08-20T00:00:00+00:00",
    )
    human = format_status(snapshot)
    assert "State: current" in human
    assert "Steam build: 24775771" in human
    document = json.loads(snapshot_json(snapshot))
    assert document["update"]["current_build"] == 24775771


class FakeService:
    def inspect_state(self) -> None:
        return None

    def health(self) -> ServiceHealth:
        return ServiceHealth.HEALTHY


def test_metrics_collector_keeps_host_and_update_observations_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pz_watchdog.metrics.psutil.cpu_percent", lambda **kwargs: [10.0, 30.0])
    monkeypatch.setattr(
        "pz_watchdog.metrics.psutil.virtual_memory",
        lambda: SimpleNamespace(total=1000, available=400),
    )
    monkeypatch.setattr(
        "pz_watchdog.metrics.psutil.disk_usage",
        lambda _: SimpleNamespace(used=500, total=2000, free=1500),
    )
    monkeypatch.setattr("pz_watchdog.metrics.psutil.boot_time", lambda: 0.0)
    collector = MetricsCollector(
        FakeService(),  # type: ignore[arg-type]
        tmp_path / "data",
        tmp_path / "server",
        "manual",
        "public",
    )
    snapshot = collector.collect(PlayerObservation.known(0))
    assert snapshot.status == "online"
    assert snapshot.players == 0
    assert snapshot.cpu_total_percent == 20.0
    assert snapshot.hottest_core_percent == 30.0
    assert snapshot.system_memory_used_bytes == 600
    assert snapshot.update["state"] == "uninitialized"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "unavailable"), (1024, "1.0 KiB"), (1024**3, "1.0 GiB")],
)
def test_human_bytes(value: int | None, expected: str) -> None:
    assert human_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "unavailable"), (60, "1m"), (90000, "1d 1h 0m")],
)
def test_human_duration(value: int | None, expected: str) -> None:
    assert human_duration(value) == expected
