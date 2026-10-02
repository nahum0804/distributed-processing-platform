from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
from datetime import datetime, timezone

import psutil
import redis

from src.workers.config import Settings

logger = logging.getLogger(__name__)

_GPU_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi")

# Optional pynvml — available only on NVIDIA nodes
try:
    import pynvml  # type: ignore
    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False


def _gpu_utilization_percent() -> float | None:
    """Return GPU utilization % for the first NVIDIA device, or None."""
    if not _NVML_OK:
        return None
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        return float(util.gpu)
    except Exception:
        return None


class WorkerStats:
    """Thread-safe counters for a worker's in-flight and finished subtasks."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.completed = 0
        self.failed = 0

    def task_started(self) -> None:
        with self._lock:
            self.active += 1

    def task_finished(self, ok: bool) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)
            if ok:
                self.completed += 1
            else:
                self.failed += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"active": self.active, "completed": self.completed, "failed": self.failed}


def detect_ffmpeg() -> tuple[str, str]:
    try:
        version_result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        version = version_result.stdout.splitlines()[0].strip() if version_result.stdout else "unavailable"
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "unavailable", "none"

    try:
        encoders_result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10
        )
        output = encoders_result.stdout or ""
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        output = ""

    found = [name for name in _GPU_ENCODERS if name in output]
    gpu_encoders = ",".join(found) if found else "none"
    return version, gpu_encoders


def local_ip(target_host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target_host, 80))
            return sock.getsockname()[0]
    except Exception:
        return "unknown"


class Heartbeat(threading.Thread):
    """Periodically publishes this worker's liveness and stats to Redis."""

    def __init__(
        self,
        settings: Settings,
        redis_client: redis.Redis,
        stats: WorkerStats,
        stop_event: threading.Event,
        ffmpeg_info: tuple[str, str] | None = None,
        processor=None,
        gpu_info: dict | None = None,
    ):
        super().__init__(daemon=True, name="heartbeat")
        self.settings = settings
        self.redis = redis_client
        self.stats = stats
        self.stop_event = stop_event
        self.ffmpeg_version, self.gpu_encoders = ffmpeg_info if ffmpeg_info is not None else detect_ffmpeg()
        self.gpu, self.nvenc_ok = self._resolve_gpu(processor, gpu_info)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.key = f"worker:{settings.worker_id}"
        # Static fields collected once at startup
        self._cpu_count: int = os.cpu_count() or 1
        mem = psutil.virtual_memory()
        self._mem_total_gb: float = round(mem.total / (1024 ** 3), 2)

    @staticmethod
    def _resolve_gpu(processor, gpu_info: dict | None) -> tuple[str, str]:
        if gpu_info is None:
            detect = getattr(processor, "detect_hw_encoders", None)
            if not callable(detect):
                return "unknown", "0"
            try:
                gpu_info = detect()
            except Exception as e:
                logger.warning("fallo la deteccion de GPU: %s", e)
                return "unknown", "0"
        if not isinstance(gpu_info, dict):
            return "unknown", "0"
        if not gpu_info.get("nvenc"):
            return "none", "0"
        return str(gpu_info.get("gpu_name") or "unknown"), "1"

    def beat(self) -> None:
        stats = self.stats.snapshot()
        now = datetime.now(timezone.utc).isoformat()
        gpu_pct = _gpu_utilization_percent()
        mapping: dict = {
            "worker_id": self.settings.worker_id,
            "host": self.settings.node_name,
            "hostname": socket.gethostname(),
            "ip": local_ip(self.settings.redis_host),
            "queues": ",".join(self.settings.worker_queues),
            "concurrency": self.settings.worker_concurrency,
            "threads_per_job": self.settings.threads_per_job(),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_percent": psutil.virtual_memory().percent,
            "cpu_count": self._cpu_count,
            "mem_total_gb": self._mem_total_gb,
            "active_subtasks": stats["active"],
            "completed_count": stats["completed"],
            "failed_count": stats["failed"],
            "ffmpeg_version": self.ffmpeg_version,
            "gpu_encoders": self.gpu_encoders,
            "gpu": self.gpu,
            "nvenc_ok": self.nvenc_ok,
            "hwaccel": self.settings.hwaccel or "none",
            "started_at": self.started_at,
            "last_seen": now,
        }
        # Only publish gpu_percent when NVML is available (avoids storing "" in Redis)
        if gpu_pct is not None:
            mapping["gpu_percent"] = gpu_pct

        pipe = self.redis.pipeline()
        pipe.hset(self.key, mapping=mapping)
        pipe.expire(self.key, self.settings.heartbeat_ttl)
        pipe.sadd("workers:registry", self.settings.worker_id)
        pipe.execute()

    def run(self) -> None:
        try:
            self.beat()
        except Exception as e:
            logger.warning("fallo de heartbeat: %s", e)
        while not self.stop_event.wait(self.settings.heartbeat_interval):
            try:
                self.beat()
            except Exception as e:
                logger.warning("fallo de heartbeat: %s", e)

