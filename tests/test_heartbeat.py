import subprocess
import threading
from unittest.mock import patch

import fakeredis

from src.workers.config import Settings
from src.workers.heartbeat import Heartbeat, WorkerStats, detect_ffmpeg, local_ip


def make_settings(**overrides) -> Settings:
    return Settings.from_env({
        "WORKER_ID": "w1",
        "HEARTBEAT_TTL": "15",
        "HEARTBEAT_INTERVAL": "5",
        **overrides,
    })


def test_worker_stats_counters_thread_safe():
    stats = WorkerStats()
    stats.task_started()
    stats.task_started()
    stats.task_finished(True)
    stats.task_finished(False)
    snap = stats.snapshot()
    assert snap == {"active": 0, "completed": 1, "failed": 1}


def test_worker_stats_active_never_negative():
    stats = WorkerStats()
    stats.task_finished(True)
    assert stats.snapshot()["active"] == 0


def test_detect_ffmpeg_found_with_nvenc():
    version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ffmpeg version 6.0\nmore\n")
    encoders_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="V..... h264_nvenc  NVIDIA encoder\nV..... libx264 encoder\n"
    )
    with patch("subprocess.run", side_effect=[version_result, encoders_result]):
        version, gpu = detect_ffmpeg()
    assert version == "ffmpeg version 6.0"
    assert gpu == "h264_nvenc"


def test_detect_ffmpeg_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        version, gpu = detect_ffmpeg()
    assert version == "unavailable"
    assert gpu == "none"


def test_detect_ffmpeg_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)):
        version, gpu = detect_ffmpeg()
    assert version == "unavailable"
    assert gpu == "none"


def test_local_ip_returns_unknown_on_error():
    with patch("socket.socket", side_effect=OSError("no network")):
        assert local_ip("localhost") == "unknown"


def test_beat_writes_fields_and_ttl_and_registry():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings()
    stats = WorkerStats()
    stop_event = threading.Event()
    hb = Heartbeat(settings, redis_client, stats, stop_event, ffmpeg_info=("ffmpeg version 6.0", "none"))

    hb.beat()

    key = f"worker:{settings.worker_id}"
    data = redis_client.hgetall(key)
    for field in (
        "worker_id", "host", "ip", "queues", "concurrency", "threads_per_job",
        "cpu_percent", "mem_percent", "active_subtasks", "completed_count",
        "failed_count", "ffmpeg_version", "gpu_encoders", "started_at", "last_seen",
    ):
        assert field in data, field

    assert data["worker_id"] == "w1"
    assert data["ffmpeg_version"] == "ffmpeg version 6.0"
    assert data["gpu_encoders"] == "none"

    ttl = redis_client.ttl(key)
    assert 0 < ttl <= settings.heartbeat_ttl
    assert redis_client.sismember("workers:registry", "w1")


def test_beat_reports_node_name_as_host():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings(NODE_NAME="kenni-laptop")
    hb = Heartbeat(settings, redis_client, WorkerStats(), threading.Event(), ffmpeg_info=("x", "none"))

    hb.beat()

    data = redis_client.hgetall(f"worker:{settings.worker_id}")
    assert data["host"] == "kenni-laptop"
    assert data["hostname"]


def test_run_survives_beat_exception_and_stops_on_event():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings(HEARTBEAT_INTERVAL="0.01")
    stats = WorkerStats()
    stop_event = threading.Event()
    hb = Heartbeat(settings, redis_client, stats, stop_event, ffmpeg_info=("test", "none"))

    call_count = {"n": 0}
    original_beat = hb.beat

    def flaky_beat():
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError("boom")
        original_beat()

    hb.beat = flaky_beat
    hb.start()

    for _ in range(200):
        if call_count["n"] >= 3:
            break
        threading.Event().wait(0.01)

    stop_event.set()
    hb.join(timeout=2)

    assert not hb.is_alive()
    assert call_count["n"] >= 3


class _Proc:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    def detect_hw_encoders(self):
        if self._exc:
            raise self._exc
        return self._result


def _gpu_beat(**kwargs):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings()
    hb = Heartbeat(settings, redis_client, WorkerStats(), threading.Event(),
                   ffmpeg_info=("x", "h264_nvenc"), **kwargs)
    hb.beat()
    return redis_client.hgetall(f"worker:{settings.worker_id}")


def test_gpu_detected_with_name():
    data = _gpu_beat(processor=_Proc({"nvenc": True, "gpu_name": "RTX 5060 Ti"}))
    assert data["gpu"] == "RTX 5060 Ti"
    assert data["nvenc_ok"] == "1"
    assert data["gpu_encoders"] == "h264_nvenc"


def test_gpu_none_when_nvenc_false():
    data = _gpu_beat(processor=_Proc({"nvenc": False, "gpu_name": None}))
    assert data["gpu"] == "none"
    assert data["nvenc_ok"] == "0"


def test_gpu_unknown_when_detection_raises():
    data = _gpu_beat(processor=_Proc(exc=RuntimeError("boom")))
    assert data["gpu"] == "unknown"
    assert data["nvenc_ok"] == "0"
    assert data["worker_id"] == "w1"


def test_gpu_unknown_without_detector():
    assert _gpu_beat(processor=object())["gpu"] == "unknown"
    data = _gpu_beat()
    assert data["gpu"] == "unknown"
    assert data["nvenc_ok"] == "0"


def test_explicit_gpu_info_bypasses_detection():
    proc = _Proc(exc=AssertionError("no debe llamarse"))
    data = _gpu_beat(processor=proc, gpu_info={"nvenc": True, "gpu_name": "GTX"})
    assert data["gpu"] == "GTX"
    assert data["nvenc_ok"] == "1"
