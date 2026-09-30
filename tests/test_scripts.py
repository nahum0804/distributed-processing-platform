from __future__ import annotations

import io
import json
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
    keys_ops = [("dataset-x/a.mp4", "transcode_video"), ("dataset-x/b.mp3", "auto")]
    payload = submit_case.build_case_payload(keys_ops)
    assert payload == {
        "subtasks": [
            {"task_type": "transcode_video", "file_path": "dataset-x/a.mp4", "params": None},
            {"task_type": "auto", "file_path": "dataset-x/b.mp3", "params": None},
        ]
    }


def test_build_case_payload_empty():
    assert submit_case.build_case_payload([]) == {"subtasks": []}


def test_build_case_payload_priority_metadata_and_params():
    payload = submit_case.build_case_payload(
        [("a.mp4", "transcode_video"), ("b.mp3", "convert_audio")],
        priority="high",
        metadata={"name": "caso"},
        subtask_metadata=[{"event": "boda"}, None],
        subtask_params=[{"crf": 28}, None],
    )
    assert payload == {
        "priority": "high",
        "metadata": {"name": "caso"},
        "subtasks": [
            {"task_type": "transcode_video", "file_path": "a.mp4", "params": {"crf": 28}, "metadata": {"event": "boda"}},
            {"task_type": "convert_audio", "file_path": "b.mp3", "params": None},
        ],
    }


@pytest.mark.parametrize("status", ["completed", "partially_completed", "failed", "cancelled"])
def test_finished_statuses_are_terminal(status):
    assert status in submit_case.FINISHED_STATUSES
    assert not {"queued", "processing", "retrying"} & submit_case.FINISHED_STATUSES


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

COORD = "http://coord.local"


def case_body(case_id, status, subtask_statuses, total=None):
    return {
        "case": {"case_id": case_id, "status": status, "total_subtasks": total or len(subtask_statuses)},
        "subtasks": [{"status": st, "operation": "transcode_video"} for st in subtask_statuses],
    }


def make_clock():
    clock = [0.0]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    return sleeps, fake_sleep, lambda: clock[0]


@responses.activate
def test_poll_case_reaches_completed(capsys):
    case_id = "case-123"
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "processing", ["completed", "pending"]))
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "completed", ["completed", "completed"]))
    sleeps, fake_sleep, fake_now = make_clock()

    result = submit_case.poll_case(COORD, case_id, poll_interval=1.0, timeout=30.0, sleep=fake_sleep, now=fake_now)

    assert result["status"] == "completed"
    assert (result["ok"], result["failed"], result["total"]) == (2, 0, 2)
    assert result["_timed_out"] is False
    assert sleeps == [1.0]
    out = capsys.readouterr().out
    assert f"{case_id} processing 1/0/2" in out
    assert f"{case_id} completed 2/0/2" in out


@responses.activate
def test_poll_case_counts_failed():
    case_id = "case-f"
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "partially_completed", ["completed", "failed"]))
    _, fake_sleep, fake_now = make_clock()

    result = submit_case.poll_case(COORD, case_id, 1.0, 30.0, sleep=fake_sleep, now=fake_now)

    assert (result["ok"], result["failed"], result["total"]) == (1, 1, 2)
    assert result["_timed_out"] is False


@responses.activate
def test_poll_case_times_out():
    case_id = "case-456"
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "processing", ["pending", "pending"]))
    _, fake_sleep, fake_now = make_clock()

    result = submit_case.poll_case(COORD, case_id, poll_interval=5.0, timeout=9.0, sleep=fake_sleep, now=fake_now)

    assert result["_timed_out"] is True
    assert result["status"] == "processing"


@responses.activate
def test_submit_case_posts_payload_and_returns_json():
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "abc", "status": "processing", "total_subtasks": 1, "subtask_ids": ["s1"], "created_at": "x"},
        status=201,
    )

    result = submit_case.submit_case(COORD, [("k", "transcode_video")])

    assert result["case_id"] == "abc"
    sent = responses.calls[0].request
    assert sent.url == f"{COORD}/cases"
    assert json.loads(sent.body) == {
        "priority": "normal",
        "subtasks": [{"task_type": "transcode_video", "file_path": "k", "params": None}],
    }


@responses.activate
def test_submit_case_sends_priority():
    responses.add(responses.POST, f"{COORD}/cases", json={"case_id": "abc"}, status=201)

    submit_case.submit_case(COORD, [("k", "extract_metadata")], priority="high")

    assert json.loads(responses.calls[0].request.body)["priority"] == "high"


@pytest.mark.parametrize("status", ["cancelled", "failed"])
@responses.activate
def test_poll_case_stops_on_new_terminal_statuses(status):
    responses.add(responses.GET, f"{COORD}/cases/c", json=case_body("c", status, ["failed", "pending"]))
    _, fake_sleep, fake_now = make_clock()

    result = submit_case.poll_case(COORD, "c", 1.0, 30.0, sleep=fake_sleep, now=fake_now)

    assert result["status"] == status
    assert result["_timed_out"] is False


@responses.activate
def test_poll_case_retrying_is_not_terminal():
    responses.add(responses.GET, f"{COORD}/cases/c", json=case_body("c", "retrying", ["pending"]))
    _, fake_sleep, fake_now = make_clock()

    result = submit_case.poll_case(COORD, "c", 5.0, 9.0, sleep=fake_sleep, now=fake_now)

    assert result["_timed_out"] is True


@responses.activate
def test_cancel_case_ok():
    body = {"case_id": "c1", "status": "cancelled", "cancelled_subtasks": 3, "running_subtasks": 1}
    responses.add(responses.POST, f"{COORD}/cases/c1/cancel", json=body)

    assert submit_case.cancel_case(COORD, "c1") == body


@pytest.mark.parametrize("code", [404, 409])
@responses.activate
def test_cancel_case_http_errors_return_none(code, capsys):
    responses.add(responses.POST, f"{COORD}/cases/c1/cancel", json={"detail": "nope"}, status=code)

    assert submit_case.cancel_case(COORD, "c1") is None
    out = capsys.readouterr().out
    assert str(code) in out and "nope" in out


# --- submit_case: report ---

@responses.activate
def test_fetch_report_and_print(capsys):
    report = {
        "status": "partially_completed",
        "summary": "1 de 2 completadas",
        "totals": {"total": 2, "completed": 1, "failed": 1, "pending": 0},
        "failure_breakdown": {"timeout": 1},
        "avg_processing_s_by_host": {"h1": 1.5},
    }
    responses.add(responses.GET, f"{COORD}/cases/c1/report", json=report)

    got = submit_case.fetch_report(COORD, "c1")
    submit_case.print_report("c1", got)

    out = capsys.readouterr().out
    assert "1 de 2 completadas" in out
    assert "timeout=1" in out
    assert "h1=1.50" in out


def test_print_report_shows_priority_and_totals_by_type_and_operation(capsys):
    submit_case.print_report("c1", {
        "summary": "ok",
        "priority": "high",
        "totals_by_type_and_operation": {
            "video": {"transcode_video": {"completed": 3, "failed": 1}, "extract_metadata": 2},
            "audio": {"convert_audio": {"completed": 4}},
        },
    })

    out = capsys.readouterr().out
    assert "[prioridad high]" in out
    assert "video/transcode_video=completed:3,failed:1" in out
    assert "video/extract_metadata=2" in out
    assert "audio/convert_audio=completed:4" in out


def test_print_report_without_new_fields_is_quiet(capsys):
    submit_case.print_report("c1", {"summary": "ok"})

    out = capsys.readouterr().out
    assert "por tipo/operacion" not in out and "prioridad" not in out


@responses.activate
def test_fetch_report_failure_is_warning(capsys):
    responses.add(responses.GET, f"{COORD}/cases/c1/report", json={"detail": "x"}, status=500)

    assert submit_case.fetch_report(COORD, "c1") is None
    assert "Aviso" in capsys.readouterr().out


# --- submit_case: main ---

@pytest.fixture
def media_dir(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"v")
    (tmp_path / "b.mp3").write_bytes(b"a")
    (tmp_path / "c.txt").write_bytes(b"t")
    return tmp_path


def run_main(monkeypatch, media_dir, *extra):
    fixed = Settings.from_env({"COORDINATOR_URL": COORD})
    monkeypatch.setattr(submit_case.Settings, "from_env", classmethod(lambda cls, env=None: fixed))
    return submit_case.main(["--dir", str(media_dir), "--no-upload", "--poll", "0", *extra])


def add_happy_path(case_id="cid"):
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": case_id, "status": "processing", "total_subtasks": 2, "subtask_ids": ["1", "2"], "created_at": "x"},
        status=201,
    )
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "processing", ["pending", "pending"]))
    responses.add(responses.GET, f"{COORD}/cases/{case_id}", json=case_body(case_id, "completed", ["completed", "completed"]))
    responses.add(
        responses.GET, f"{COORD}/cases/{case_id}/report",
        json={"status": "completed", "summary": "todo bien", "failure_breakdown": {}, "avg_processing_s_by_host": {"h1": 2.0}},
    )


def posted_tasks():
    return json.loads(responses.calls[0].request.body)["subtasks"]


@responses.activate
def test_main_auto_sends_task_type_auto(monkeypatch, media_dir, capsys):
    add_happy_path()

    code = run_main(monkeypatch, media_dir, "--mode", "auto")

    assert code == 0
    tasks = posted_tasks()
    assert [t["task_type"] for t in tasks] == ["auto", "auto"]
    assert [t["file_path"] for t in tasks] == [f"{media_dir.name}/a.mp4", f"{media_dir.name}/b.mp3"]
    out = capsys.readouterr().out
    assert "omitieron 1" in out
    assert "cid completed 2/0/2" in out
    assert "todo bien" in out
    assert "h1=2.00" in out


@responses.activate
def test_main_mixed_sends_concrete_ops(monkeypatch, media_dir):
    add_happy_path()

    assert run_main(monkeypatch, media_dir, "--mode", "mixed") == 0

    assert [t["task_type"] for t in posted_tasks()] == ["transcode_video", "convert_audio", "extract_metadata"]


@responses.activate
def test_main_explicit_operation(monkeypatch, media_dir):
    add_happy_path()

    assert run_main(monkeypatch, media_dir, "--mode", "extract_metadata") == 0

    assert {t["task_type"] for t in posted_tasks()} == {"extract_metadata"}


@responses.activate
def test_main_422_prints_detail_and_exits_1(monkeypatch, media_dir, capsys):
    responses.add(responses.POST, f"{COORD}/cases", json={"detail": "task_type invalido: foo"}, status=422)

    assert run_main(monkeypatch, media_dir, "--mode", "auto") == 1

    out = capsys.readouterr().out
    assert "422" in out
    assert "task_type invalido: foo" in out


@responses.activate
def test_main_report_failure_does_not_fail(monkeypatch, media_dir, capsys):
    add_happy_path()
    responses.replace(responses.GET, f"{COORD}/cases/cid/report", json={"detail": "boom"}, status=500)

    assert run_main(monkeypatch, media_dir, "--mode", "auto") == 0
    assert "Aviso" in capsys.readouterr().out


@responses.activate
def test_main_timeout_exit_2(monkeypatch, media_dir):
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "cid", "status": "processing", "total_subtasks": 2, "subtask_ids": [], "created_at": "x"},
        status=201,
    )
    responses.add(responses.GET, f"{COORD}/cases/cid", json=case_body("cid", "processing", ["pending", "pending"]))

    assert run_main(monkeypatch, media_dir, "--mode", "auto", "--timeout", "0") == 2


@responses.activate
def test_main_sends_priority_high(monkeypatch, media_dir):
    add_happy_path()

    assert run_main(monkeypatch, media_dir, "--mode", "auto", "--priority", "high") == 0

    assert json.loads(responses.calls[0].request.body)["priority"] == "high"


@responses.activate
def test_main_default_priority_is_normal(monkeypatch, media_dir):
    add_happy_path()

    assert run_main(monkeypatch, media_dir, "--mode", "auto") == 0

    assert json.loads(responses.calls[0].request.body)["priority"] == "normal"


def test_main_rejects_invalid_priority(monkeypatch, media_dir):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, media_dir, "--priority", "urgente")


@responses.activate
def test_main_cancel_after_cancels_and_exits_0(monkeypatch, media_dir, capsys):
    slept = []
    monkeypatch.setattr(submit_case.time, "sleep", slept.append)
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "cid", "status": "queued", "total_subtasks": 2, "subtask_ids": [], "created_at": "x"},
        status=201,
    )
    responses.add(
        responses.POST, f"{COORD}/cases/cid/cancel",
        json={"case_id": "cid", "status": "cancelled", "cancelled_subtasks": 2, "running_subtasks": 0},
    )
    responses.add(responses.GET, f"{COORD}/cases/cid", json=case_body("cid", "cancelled", ["failed", "failed"]))
    responses.add(responses.GET, f"{COORD}/cases/cid/report", json={"summary": "cancelado", "priority": "normal"})

    assert run_main(monkeypatch, media_dir, "--mode", "auto", "--cancel-after", "3") == 0

    assert 3.0 in slept
    out = capsys.readouterr().out
    assert "Cancelado cid: estado=cancelled canceladas=2 en_ejecucion=0" in out
    assert "cid cancelled 0/2/2" in out


@responses.activate
def test_main_cancel_conflict_does_not_fail(monkeypatch, media_dir, capsys):
    monkeypatch.setattr(submit_case.time, "sleep", lambda s: None)
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "cid", "status": "queued", "total_subtasks": 2, "subtask_ids": [], "created_at": "x"},
        status=201,
    )
    responses.add(responses.POST, f"{COORD}/cases/cid/cancel", json={"detail": "ya terminado"}, status=409)
    responses.add(responses.GET, f"{COORD}/cases/cid", json=case_body("cid", "completed", ["completed", "completed"]))
    responses.add(responses.GET, f"{COORD}/cases/cid/report", json={"summary": "ok"})

    assert run_main(monkeypatch, media_dir, "--mode", "auto", "--cancel-after", "0") == 0
    assert "409" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["failed", "cancelled"])
@responses.activate
def test_main_failed_or_cancelled_case_exits_0(monkeypatch, media_dir, status):
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "cid", "status": "queued", "total_subtasks": 2, "subtask_ids": [], "created_at": "x"},
        status=201,
    )
    responses.add(responses.GET, f"{COORD}/cases/cid", json=case_body("cid", status, ["failed", "failed"]))
    responses.add(responses.GET, f"{COORD}/cases/cid/report", json={"summary": "x"})

    assert run_main(monkeypatch, media_dir, "--mode", "auto") == 0


@responses.activate
def test_main_http_error_while_polling_exits_1(monkeypatch, media_dir, capsys):
    responses.add(
        responses.POST, f"{COORD}/cases",
        json={"case_id": "cid", "status": "queued", "total_subtasks": 2, "subtask_ids": [], "created_at": "x"},
        status=201,
    )
    responses.add(responses.GET, f"{COORD}/cases/cid", json={"detail": "boom"}, status=500)

    assert run_main(monkeypatch, media_dir, "--mode", "auto") == 1
    assert "500" in capsys.readouterr().out


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


def test_worker_compose_grace_period_covers_max_ffmpeg_timeout():
    compose = _load_compose("deploy/docker-compose.worker.yml")
    grace = compose["services"]["worker"]["stop_grace_period"]
    match = re.fullmatch(r"(\d+)([smh])", str(grace))
    assert match, grace
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    assert seconds >= 30 * 60


def test_worker_gpu_override_requests_nvenc_and_gpu_device():
    compose = _load_compose("deploy/docker-compose.worker-gpu.yml")
    worker = compose["services"]["worker"]
    env = worker["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    assert env["HWACCEL"] == "nvenc"
    assert "video" in env["NVIDIA_DRIVER_CAPABILITIES"].split(",")
    devices = worker["deploy"]["resources"]["reservations"]["devices"]
    assert any(
        d["driver"] == "nvidia" and "video" in d["capabilities"] and "gpu" in d["capabilities"]
        for d in devices
    )


def test_check_env_file_warns_crlf_empty_password_and_localhost_in_container(tmp_path):
    from scripts.check_connectivity import check_env_file

    env = tmp_path / ".env"
    env.write_bytes(b"REDIS_HOST=localhost\r\nREDIS_PASSWORD=\r\n")
    settings = Settings.from_env({"REDIS_HOST": "localhost", "COORDINATOR_URL": "http://localhost:8000"})

    warnings = check_env_file(settings, env_path=env, in_container=True)

    assert any("CRLF" in w for w in warnings)
    assert any("REDIS_PASSWORD" in w for w in warnings)
    assert any("REDIS_HOST" in w and "COORDINATOR_URL" in w for w in warnings)


def test_check_env_file_clean_setup_has_no_warnings(tmp_path):
    from scripts.check_connectivity import check_env_file

    env = tmp_path / ".env"
    env.write_bytes(b"REDIS_HOST=192.168.1.10\nREDIS_PASSWORD=secreto\n")
    settings = Settings.from_env({"REDIS_HOST": "192.168.1.10", "REDIS_PASSWORD": "secreto",
                                  "COORDINATOR_URL": "http://192.168.1.10:8000", "MINIO_ENDPOINT": "192.168.1.10:9000"})

    assert check_env_file(settings, env_path=env, in_container=True) == []


class _ProbeStorage:
    def __init__(self, state):
        self.state = state

    def probe(self):
        return self.state

    def ensure_buckets(self):
        pass


@pytest.mark.parametrize("state,ok,fragment", [
    ("ok", True, "MinIO OK"),
    ("missing_bucket", True, "aun no existe"),
    ("down", False, "no responde"),
])
def test_check_minio_distinguishes_missing_bucket_from_unreachable(state, ok, fragment):
    result_ok, detail = check_connectivity.check_minio(Settings.from_env({}), storage=_ProbeStorage(state))

    assert result_ok is ok
    assert fragment in detail
