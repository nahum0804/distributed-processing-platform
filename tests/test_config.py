from pathlib import Path

import pytest
import redis

from src.workers.config import OPERATIONS, Settings, make_redis


def test_defaults():
    s = Settings.from_env({})
    assert s.redis_host == "localhost"
    assert s.redis_port == 6379
    assert s.redis_password is None
    assert s.coordinator_url == "http://localhost:8000"
    assert s.minio_endpoint == "localhost:9000"
    assert s.minio_access_key == "minioadmin"
    assert s.minio_secret_key == "minioadmin"
    assert s.minio_secure is False
    assert s.dataset_bucket == "dataset"
    assert s.results_bucket == "results"
    assert s.worker_id.startswith("worker-")
    assert s.worker_queues == OPERATIONS
    assert s.worker_concurrency == 1
    assert s.heartbeat_interval == 5.0
    assert s.heartbeat_ttl == 15
    assert s.ffmpeg_timeout is None
    assert s.reaper_interval == 10.0
    assert s.reaper_max_age == 2100.0
    assert s.max_attempts == 3


def test_node_name_defaults_to_hostname_and_reads_env(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "container-abc")
    assert Settings.from_env({}).node_name == "container-abc"
    assert Settings.from_env({"NODE_NAME": "kenni-laptop"}).node_name == "kenni-laptop"


def test_env_parsing_basic_types():
    env = {
        "REDIS_HOST": "10.0.0.1",
        "REDIS_PORT": "6380",
        "REDIS_PASSWORD": "secret",
        "COORDINATOR_URL": "http://10.0.0.2:8000",
        "MINIO_ENDPOINT": "10.0.0.3:9000",
        "MINIO_ACCESS_KEY": "ak",
        "MINIO_SECRET_KEY": "sk",
        "DATASET_BUCKET": "ds",
        "RESULTS_BUCKET": "res",
        "WORKER_ID": "worker-custom",
        "WORKER_CONCURRENCY": "3",
        "WORK_DIR": "/tmp/custom-work",
        "HEARTBEAT_INTERVAL": "2.5",
        "HEARTBEAT_TTL": "20",
        "FFMPEG_TIMEOUT": "120",
        "REAPER_INTERVAL": "7.5",
        "REAPER_MAX_AGE": "3000",
        "MAX_ATTEMPTS": "5",
    }
    s = Settings.from_env(env)
    assert s.redis_host == "10.0.0.1"
    assert s.redis_port == 6380
    assert s.redis_password == "secret"
    assert s.coordinator_url == "http://10.0.0.2:8000"
    assert s.minio_endpoint == "10.0.0.3:9000"
    assert s.minio_access_key == "ak"
    assert s.minio_secret_key == "sk"
    assert s.dataset_bucket == "ds"
    assert s.results_bucket == "res"
    assert s.worker_id == "worker-custom"
    assert s.worker_concurrency == 3
    assert s.work_dir == Path("/tmp/custom-work")
    assert s.heartbeat_interval == 2.5
    assert s.heartbeat_ttl == 20
    assert s.ffmpeg_timeout == 120.0
    assert s.reaper_interval == 7.5
    assert s.reaper_max_age == 3000.0
    assert s.max_attempts == 5


def test_empty_env_values_fall_back_to_defaults():
    env = {
        "REDIS_HOST": "",
        "REDIS_PASSWORD": "",
        "FFMPEG_TIMEOUT": "",
        "WORKER_ID": "",
        "WORK_DIR": "",
    }
    s = Settings.from_env(env)
    assert s.redis_host == "localhost"
    assert s.redis_password is None
    assert s.ffmpeg_timeout is None
    assert s.worker_id.startswith("worker-")
    assert s.work_dir == Settings.work_dir


@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("True", True),
    ("yes", True),
    ("YES", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("no", False),
    ("off", False),
])
def test_minio_secure_parsing(value, expected):
    s = Settings.from_env({"MINIO_SECURE": value})
    assert s.minio_secure is expected


def test_minio_secure_default_when_missing():
    s = Settings.from_env({})
    assert s.minio_secure is False


def test_worker_queues_parsing_preserves_order():
    s = Settings.from_env({"WORKER_QUEUES": "extract_audio,transcode_video"})
    assert s.worker_queues == ("extract_audio", "transcode_video")


def test_worker_queues_tolerates_whitespace_and_queue_prefix():
    s = Settings.from_env({"WORKER_QUEUES": " queue:transcode_video , extract_audio "})
    assert s.worker_queues == ("transcode_video", "extract_audio")


def test_worker_queues_invalid_value_raises():
    with pytest.raises(ValueError, match="bogus_operation"):
        Settings.from_env({"WORKER_QUEUES": "transcode_video,bogus_operation"})


def test_worker_queues_empty_raises():
    with pytest.raises(ValueError):
        Settings.from_env({"WORKER_QUEUES": "   ,  ,"})


def test_worker_concurrency_zero_raises():
    with pytest.raises(ValueError):
        Settings.from_env({"WORKER_CONCURRENCY": "0"})


def test_worker_concurrency_negative_raises():
    with pytest.raises(ValueError):
        Settings.from_env({"WORKER_CONCURRENCY": "-1"})


def test_coordinator_url_trailing_slash_stripped():
    s = Settings.from_env({"COORDINATOR_URL": "http://host:8000/"})
    assert s.coordinator_url == "http://host:8000"

    s2 = Settings.from_env({"COORDINATOR_URL": "http://host:8000///"})
    assert s2.coordinator_url == "http://host:8000"


def test_queue_keys_order():
    s = Settings.from_env({"WORKER_QUEUES": "generate_thumbnail,transcode_video"})
    assert s.queue_keys() == [
        "queue:generate_thumbnail:high", "queue:transcode_video:high",
        "queue:generate_thumbnail", "queue:transcode_video",
    ]


def test_hwaccel_default_none_and_empty_is_none():
    assert Settings.from_env({}).hwaccel is None
    assert Settings.from_env({"HWACCEL": ""}).hwaccel is None
    assert Settings.from_env({"HWACCEL": "  "}).hwaccel is None


@pytest.mark.parametrize("raw", ["nvenc", "NVENC", " Nvenc "])
def test_hwaccel_nvenc_case_insensitive(raw):
    assert Settings.from_env({"HWACCEL": raw}).hwaccel == "nvenc"


def test_hwaccel_invalid_raises_naming_value():
    with pytest.raises(ValueError, match="cuda"):
        Settings.from_env({"HWACCEL": "cuda"})


def test_threads_per_job(monkeypatch):
    monkeypatch.setattr("os.process_cpu_count", lambda: 8)
    s = Settings.from_env({"WORKER_CONCURRENCY": "2"})
    assert s.threads_per_job() == 4

    s16 = Settings.from_env({"WORKER_CONCURRENCY": "16"})
    assert s16.threads_per_job() == 1


def test_threads_per_job_none_cpu_count(monkeypatch):
    monkeypatch.setattr("os.process_cpu_count", lambda: None)
    s = Settings.from_env({"WORKER_CONCURRENCY": "1"})
    assert s.threads_per_job() == 1


def test_make_redis_builds_client():
    s = Settings.from_env({"REDIS_HOST": "127.0.0.1", "REDIS_PORT": "6379", "REDIS_PASSWORD": "pw"})
    client = make_redis(s)
    assert isinstance(client, redis.Redis)
    kwargs = client.get_connection_kwargs()
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 6379
    assert kwargs["password"] == "pw"
    assert kwargs["decode_responses"] is True
