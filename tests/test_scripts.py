from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
import responses

from scripts import check_connectivity, seed_minio, submit_case
from src.workers.config import OPERATIONS, Settings
from tests.fakes import FakeStorage

# NOTE: pyyaml was already installed in .venv (verified with `python -c "import yaml"`
# before writing these tests), so the compose files below are parsed with it directly.
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def make_settings(**overrides) -> Settings:
    return Settings.from_env({**overrides})


# --- submit_case: classify ---

@pytest.mark.parametrize("name,expected", [
    ("movie.mp4", "video"),
    ("movie.MKV", "video"),
    ("clip.avi", "video"),
    ("clip.mov", "video"),
    ("clip.webm", "video"),
    ("clip.flv", "video"),
    ("clip.wmv", "video"),
    ("clip.m4v", "video"),
    ("song.mp3", "audio"),
    ("song.WAV", "audio"),
    ("song.flac", "audio"),
    ("song.ogg", "audio"),
    ("song.m4a", "audio"),
    ("song.aac", "audio"),
    ("song.opus", "audio"),
    ("song.wma", "audio"),
    ("readme.txt", "other"),
    ("metadata.json", "other"),
    ("noext", "other"),
])
def test_classify(name, expected):
    assert submit_case.classify(Path(name)) == expected


# --- submit_case: plan_operations ---

def test_plan_operations_auto_skips_other_files():
    files = [Path("a.mp4"), Path("b.mp3"), Path("c.txt")]
    plan = submit_case.plan_operations(files, "auto")
    assert plan == [(Path("a.mp4"), "transcode_video"), (Path("b.mp3"), "convert_audio")]


def test_plan_operations_mixed_cycles_per_kind():
    files = [Path(f"v{i}.mp4") for i in range(5)] + [Path(f"a{i}.mp3") for i in range(3)] + [Path("x.txt")]
    plan = submit_case.plan_operations(files, "mixed")
    ops_by_path = dict(plan)

    assert ops_by_path[Path("v0.mp4")] == "transcode_video"
    assert ops_by_path[Path("v1.mp4")] == "extract_audio"
    assert ops_by_path[Path("v2.mp4")] == "generate_thumbnail"
    assert ops_by_path[Path("v3.mp4")] == "extract_metadata"
    assert ops_by_path[Path("v4.mp4")] == "transcode_video"

    assert ops_by_path[Path("a0.mp3")] == "convert_audio"
    assert ops_by_path[Path("a1.mp3")] == "extract_metadata"
    assert ops_by_path[Path("a2.mp3")] == "convert_audio"

    assert ops_by_path[Path("x.txt")] == "extract_metadata"


def test_plan_operations_explicit_operation_applies_to_all():
    files = [Path("a.mp4"), Path("b.mp3"), Path("c.txt")]
    plan = submit_case.plan_operations(files, "extract_metadata")
    assert plan == [(f, "extract_metadata") for f in files]
    assert all(op in OPERATIONS for _, op in plan)


def test_plan_operations_invalid_mode_raises():
    with pytest.raises(ValueError):
        submit_case.plan_operations([Path("a.mp4")], "not_a_real_operation")


# --- submit_case: build_case_payload ---

def test_build_case_payload():
    keys_ops = [("dataset-x/a.mp4", "transcode_video"), ("dataset-x/b.mp3", "convert_audio")]
    payload = submit_case.build_case_payload(keys_ops)
    assert payload == {
        "files": [
            {"path": "dataset-x/a.mp4", "operation": "transcode_video"},
            {"path": "dataset-x/b.mp3", "operation": "convert_audio"},
        ]
    }


def test_build_case_payload_empty():
    assert submit_case.build_case_payload([]) == {"files": []}


# --- submit_case: upload_plan uses injected storage ---

def test_upload_plan_uses_injected_storage(tmp_path):
    f1 = tmp_path / "a.mp4"
    f1.write_bytes(b"video-data")
    fake = FakeStorage()

    keys_ops = submit_case.upload_plan(fake, [(f1, "transcode_video")], "myprefix", "dataset")

    assert fake.buckets_ensured is True
    assert keys_ops == [("myprefix/a.mp4", "transcode_video")]
    assert fake.objects[("dataset", "myprefix/a.mp4")] == b"video-data"


# --- submit_case: poll_case ---

@responses.activate
def test_poll_case_reaches_completed():
    coordinator_url = "http://coord.local"
    case_id = "case-123"

    responses.add(
        responses.GET, f"{coordinator_url}/cases/{case_id}",
        json={"case_id": case_id, "status": "processing", "completed_subtasks": 0, "failed_subtasks": 0, "total_subtasks": 2},
        status=200,
    )
    responses.add(
        responses.GET, f"{coordinator_url}/cases/{case_id}",
        json={"case_id": case_id, "status": "completed", "completed_subtasks": 2, "failed_subtasks": 0, "total_subtasks": 2},
        status=200,
    )

    sleeps = []
    fake_clock = [0.0]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock[0] += s

    def fake_now():
        return fake_clock[0]

    result = submit_case.poll_case(
        coordinator_url, case_id, poll_interval=1.0, timeout=30.0, sleep=fake_sleep, now=fake_now
    )

    assert result["status"] == "completed"
    assert result["_timed_out"] is False
    assert sleeps == [1.0]


@responses.activate
def test_poll_case_times_out():
    coordinator_url = "http://coord.local"
    case_id = "case-456"

    responses.add(
        responses.GET, f"{coordinator_url}/cases/{case_id}",
        json={"case_id": case_id, "status": "processing", "completed_subtasks": 0, "failed_subtasks": 0, "total_subtasks": 2},
        status=200,
    )

    fake_clock = [0.0]

    def fake_sleep(s):
        fake_clock[0] += s

    def fake_now():
        return fake_clock[0]

    result = submit_case.poll_case(
        coordinator_url, case_id, poll_interval=5.0, timeout=9.0, sleep=fake_sleep, now=fake_now
    )

    assert result["_timed_out"] is True
    assert result["status"] == "processing"


@responses.activate
def test_submit_case_posts_payload_and_returns_json():
    coordinator_url = "http://coord.local"
    responses.add(
        responses.POST, f"{coordinator_url}/cases/",
        json={"case_id": "abc", "subtasks_created": 1, "status": "processing"},
        status=200,
    )

    result = submit_case.submit_case(coordinator_url, [("k", "transcode_video")])

    assert result["case_id"] == "abc"
    sent = responses.calls[0].request
    assert sent.url == f"{coordinator_url}/cases/"


# --- seed_minio ---

def test_build_key():
    directory = Path("/data/myset")
    path = Path("/data/myset/sub/clip.mp4")
    assert seed_minio.build_key(directory, path, "myset") == "myset/sub/clip.mp4"


def test_seed_uploads_all_files_with_injected_storage(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.mp4").write_bytes(b"1234")
    (tmp_path / "sub" / "b.json").write_bytes(b"1234567")

    fake = FakeStorage()
    count, total_bytes = seed_minio.seed(tmp_path, fake, prefix="myset")

    assert fake.buckets_ensured is True
    assert count == 2
    assert total_bytes == 11
    assert fake.objects[("dataset", "myset/a.mp4")] == b"1234"
    assert fake.objects[("dataset", "myset/sub/b.json")] == b"1234567"


def test_seed_respects_bucket_argument(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    fake = FakeStorage()

    seed_minio.seed(tmp_path, fake, prefix="p", bucket="custom-bucket")

    assert ("custom-bucket", "p/a.mp4") in fake.objects


# --- check_connectivity ---

def test_check_redis_ok():
    settings = make_settings()

    class OkClient:
        def ping(self):
            return True

    ok, detail = check_connectivity.check_redis(settings, client=OkClient())
    assert ok is True
    assert "localhost:6379" in detail


def test_check_redis_auth_failure_explained():
    import redis as redis_module

    settings = make_settings()

    class FailingClient:
        def ping(self):
            raise redis_module.AuthenticationError("invalid password")

    ok, detail = check_connectivity.check_redis(settings, client=FailingClient())
    assert ok is False
    assert "REDIS_PASSWORD" in detail


def test_check_redis_generic_failure():
    settings = make_settings()

    class FailingClient:
        def ping(self):
            raise ConnectionError("refused")

    ok, detail = check_connectivity.check_redis(settings, client=FailingClient())
    assert ok is False
    assert "no responde" in detail


def test_check_coordinator_ok():
    settings = make_settings(COORDINATOR_URL="http://x:8000")

    class FakeResp:
        status_code = 200

    ok, detail = check_connectivity.check_coordinator(settings, get=lambda url, timeout: FakeResp())
    assert ok is True


def test_check_coordinator_request_exception():
    import requests

    settings = make_settings(COORDINATOR_URL="http://x:8000")

    def raiser(url, timeout):
        raise requests.ConnectionError("down")

    ok, detail = check_connectivity.check_coordinator(settings, get=raiser)
    assert ok is False
    assert "coordinador" in detail.lower()


def test_check_minio_ok_with_fake_storage():
    settings = make_settings()
    fake = FakeStorage()
    ok, detail = check_connectivity.check_minio(settings, storage=fake)
    assert ok is True


def test_check_minio_fail_when_ping_false():
    settings = make_settings()

    class DownStorage:
        def ping(self):
            return False

    ok, detail = check_connectivity.check_minio(settings, storage=DownStorage())
    assert ok is False
    assert "MINIO_ENDPOINT" in detail


def test_check_minio_create_buckets_calls_ensure_buckets():
    settings = make_settings()
    fake = FakeStorage()
    ok, detail = check_connectivity.check_minio(settings, storage=fake, create_buckets=True)
    assert ok is True
    assert fake.buckets_ensured is True


def test_check_ffmpeg_missing_is_warn_not_fail():
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    ok, detail = check_connectivity.check_ffmpeg(run=missing)
    assert ok is False
    assert "ffmpeg" in detail.lower()


def test_check_ffmpeg_present():
    class Result:
        returncode = 0
        stdout = "ffmpeg version 6.0\nmore text\n"

    ok, detail = check_connectivity.check_ffmpeg(run=lambda *a, **k: Result())
    assert ok is True
    assert "6.0" in detail


def test_check_work_dir_writable(tmp_path):
    target = tmp_path / "workdir"
    ok, detail = check_connectivity.check_work_dir(target)
    assert ok is True
    assert target.exists()


def test_config_summary_never_prints_secrets():
    settings = make_settings(REDIS_PASSWORD="supersecret", MINIO_SECRET_KEY="topsecretkey")
    summary = check_connectivity.config_summary(settings)
    assert "supersecret" not in summary
    assert "topsecretkey" not in summary


def test_main_exit_code_1_when_redis_fails(monkeypatch, capsys):
    monkeypatch.setattr(check_connectivity, "check_redis", lambda settings: (False, "redis down"))
    monkeypatch.setattr(check_connectivity, "check_coordinator", lambda settings: (True, "ok"))
    monkeypatch.setattr(check_connectivity, "check_minio", lambda settings, create_buckets=False: (True, "ok"))
    monkeypatch.setattr(check_connectivity, "check_ffmpeg", lambda: (False, "missing"))
    monkeypatch.setattr(check_connectivity, "check_work_dir", lambda work_dir: (True, "ok"))

    exit_code = check_connectivity.main([])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "[FAIL] redis" in out
    assert "[WARN] ffmpeg" in out


def test_main_exit_code_0_when_only_ffmpeg_warns(monkeypatch, capsys):
    monkeypatch.setattr(check_connectivity, "check_redis", lambda settings: (True, "ok"))
    monkeypatch.setattr(check_connectivity, "check_coordinator", lambda settings: (True, "ok"))
    monkeypatch.setattr(check_connectivity, "check_minio", lambda settings, create_buckets=False: (True, "ok"))
    monkeypatch.setattr(check_connectivity, "check_ffmpeg", lambda: (False, "missing"))
    monkeypatch.setattr(check_connectivity, "check_work_dir", lambda work_dir: (True, "ok"))

    exit_code = check_connectivity.main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "[WARN] ffmpeg" in out


# --- compose files structural checks ---

def _load_compose(name: str) -> dict:
    path = REPO_ROOT / name
    with path.open() as f:
        return yaml.safe_load(f)


def test_root_compose_has_redis_minio_reaper():
    compose = _load_compose("docker-compose.yml")
    services = compose["services"]
    assert "redis" in services
    assert "minio" in services
    assert "reaper" in services
    assert "version" not in compose


def test_local_compose_worker_has_replicas_3():
    compose = _load_compose("deploy/docker-compose.local.yml")
    worker = compose["services"]["worker"]
    assert worker["deploy"]["replicas"] == 3


def test_worker_compose_has_stop_grace_period():
    compose = _load_compose("deploy/docker-compose.worker.yml")
    worker = compose["services"]["worker"]
    assert "stop_grace_period" in worker
