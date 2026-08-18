from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .models import PlayerObservation
from .service import DockerComposeService, ServiceHealth, ServiceInspectionError


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    status: str
    players: int | None
    player_state: str
    cpu_total_percent: float
    cpu_per_core_percent: list[float]
    hottest_core_percent: float
    pz_process_memory_bytes: int | None
    system_memory_used_bytes: int
    system_memory_total_bytes: int
    system_memory_available_bytes: int
    server_uptime_seconds: int
    container_uptime_seconds: int | None
    disk_used_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    collected_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetricsCollector:
    def __init__(self, service: DockerComposeService, data_path: Path) -> None:
        self.service = service
        self.data_path = data_path

    def collect(self, players: PlayerObservation) -> StatusSnapshot:
        per_core = [round(value, 1) for value in psutil.cpu_percent(interval=1, percpu=True)]
        total_cpu = round(statistics.fmean(per_core), 1) if per_core else 0.0
        memory = psutil.virtual_memory()
        disk_target = self.data_path if self.data_path.exists() else self.data_path.parent
        disk = psutil.disk_usage(disk_target if disk_target.exists() else Path.cwd())
        try:
            state = self.service.inspect_state()
        except ServiceInspectionError:
            state = None
        health = self.service.health()
        process_memory = self._pz_process_memory(state)
        container_uptime = self._container_uptime(state)
        player_state = "unknown" if players.is_unknown else "known"

        return StatusSnapshot(
            status="online" if health is ServiceHealth.HEALTHY else health.value,
            players=players.count,
            player_state=player_state,
            cpu_total_percent=total_cpu,
            cpu_per_core_percent=per_core,
            hottest_core_percent=max(per_core, default=0.0),
            pz_process_memory_bytes=process_memory,
            system_memory_used_bytes=memory.total - memory.available,
            system_memory_total_bytes=memory.total,
            system_memory_available_bytes=memory.available,
            server_uptime_seconds=max(0, int(time.time() - psutil.boot_time())),
            container_uptime_seconds=container_uptime,
            disk_used_bytes=disk.used,
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
            collected_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _pz_process_memory(state: dict[str, object] | None) -> int | None:
        if state is None or not isinstance(state.get("Pid"), int):
            return None
        root_pid = int(state["Pid"])
        if root_pid <= 0:
            return None
        try:
            candidates = [
                psutil.Process(root_pid),
                *psutil.Process(root_pid).children(recursive=True),
            ]
            for process in candidates:
                command = " ".join(process.cmdline())
                if "zombie.network.GameServer" in command or "ProjectZomboid64" in command:
                    return process.memory_info().rss
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return None
        return None

    @staticmethod
    def _container_uptime(state: dict[str, object] | None) -> int | None:
        if state is None or not isinstance(state.get("StartedAt"), str):
            return None
        try:
            started = datetime.fromisoformat(str(state["StartedAt"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0, int((datetime.now(UTC) - started).total_seconds()))


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def human_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unavailable"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_status(snapshot: StatusSnapshot) -> str:
    players = "UNKNOWN" if snapshot.players is None else str(snapshot.players)
    return "\n".join(
        (
            "Project Zomboid",
            f"Status: {snapshot.status}",
            "",
            f"Players: {players}",
            "",
            "CPU:",
            f"  Total: {snapshot.cpu_total_percent:.1f}%",
            f"  Hottest core: {snapshot.hottest_core_percent:.1f}%",
            "",
            "Memory:",
            f"  PZ process: {human_bytes(snapshot.pz_process_memory_bytes)}",
            "  System: "
            f"{human_bytes(snapshot.system_memory_used_bytes)} / "
            f"{human_bytes(snapshot.system_memory_total_bytes)}",
            f"  Available: {human_bytes(snapshot.system_memory_available_bytes)}",
            "",
            "Uptime:",
            f"  Server: {human_duration(snapshot.server_uptime_seconds)}",
            f"  Container: {human_duration(snapshot.container_uptime_seconds)}",
            "",
            "Disk:",
            "  Used: "
            f"{human_bytes(snapshot.disk_used_bytes)} / "
            f"{human_bytes(snapshot.disk_total_bytes)}",
        )
    )


def snapshot_json(snapshot: StatusSnapshot) -> str:
    return json.dumps(snapshot.as_dict(), separators=(",", ":"), sort_keys=True)
