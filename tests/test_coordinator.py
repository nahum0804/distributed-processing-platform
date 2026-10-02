import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

import src.coordinator.main as main

OPS = (
    "transcode_video",
    "extract_audio",
    "generate_thumbnail",
    "convert_audio",
    "extract_metadata",
)


@pytest.fixture
def fake(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(main, "redis_client", r)
    return r


@pytest.fixture
def client(fake):
    return TestClient(main.app)


def create(client, subtasks=None, **extra):
    body = {"subtasks": subtasks or [{"task_type": "auto", "file_path": "videos/a.mp4"}]}
    body.update(extra)
    return client.post("/cases", json=body)


def report(client, case_id, subtask_id, status="completed", **extra):
    body = {"subtask_id": subtask_id, "case_id": case_id, "status": status,
            "worker_id": "w1", "host": "h1", "processing_s": 2.0,
            "outputs": [f"out/{subtask_id}.mp4"] if status == "completed" else None}
    body.update(extra)
    return client.post("/subtasks/report", json=body)


def case_status(fake, case_id):
    return fake.hget(f"case:{case_id}", "status")


# --- creación -------------------------------------------------------------
def test_create_case_starts_queued_with_defaults(client, fake):
    data = create(client).json()
    assert data["status"] == "queued"
    assert data["priority"] == "normal"
    case = fake.hgetall(f"case:{data['case_id']}")
    assert case["status"] == "queued"
    assert case["priority"] == "normal"
    assert case["metadata"] == "{}"
    assert case["retries"] == "0"
    st = fake.hgetall(f"subtask:{data['subtask_ids'][0]}")
    assert st["status"] == "pending"
    assert st["metadata"] == "{}"
    assert st["priority"] == "normal"


def test_normal_priority_goes_to_normal_queue(client, fake):
    data = create(client).json()
    assert fake.llen("queue:transcode_video") == 1
    assert fake.llen("queue:transcode_video:high") == 0
    payload = json.loads(fake.lpop("queue:transcode_video"))
    assert payload["priority"] == "normal"
    assert payload["subtask_id"] == data["subtask_ids"][0]


def test_high_priority_goes_to_high_queue(client, fake):
    data = create(client, [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "convert_audio", "file_path": "audio/b.wav"},
    ], priority="high").json()
    assert data["priority"] == "high"
    assert fake.llen("queue:transcode_video:high") == 1
    assert fake.llen("queue:convert_audio:high") == 1
    assert fake.llen("queue:transcode_video") == 0
    assert fake.llen("queue:convert_audio") == 0
    payload = json.loads(fake.lpop("queue:convert_audio:high"))
    assert payload["priority"] == "high"
    assert fake.hget("case:" + data["case_id"], "priority") == "high"
    assert fake.hget("subtask:" + data["subtask_ids"][0], "priority") == "high"


def test_invalid_priority_is_422_and_writes_nothing(client, fake):
    resp = create(client, priority="urgent")
    assert resp.status_code == 422
    assert fake.keys("*") == []


def test_invalid_operation_is_422(client, fake):
    resp = create(client, [{"task_type": "hackear", "file_path": "videos/a.mp4"}])
    assert resp.status_code == 422
    assert fake.keys("*") == []


def test_metadata_is_stored(client, fake):
    data = create(
        client,
        [{"task_type": "auto", "file_path": "videos/a.mp4", "metadata": {"cliente": "x"}}],
        metadata={"lote": 7},
    ).json()
    assert json.loads(fake.hget(f"case:{data['case_id']}", "metadata")) == {"lote": 7}
    assert json.loads(fake.hget(f"subtask:{data['subtask_ids'][0]}", "metadata")) == {"cliente": "x"}


# --- barrera --------------------------------------------------------------
def make_two(client):
    data = create(client, [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audio/b.mp3"},
    ]).json()
    return data["case_id"], data["subtask_ids"]


def test_barrier_completed(client, fake):
    cid, (s1, s2) = make_two(client)
    assert report(client, cid, s1).json()["case_status"] == "queued"
    assert report(client, cid, s2).json()["case_status"] == "completed"
    assert fake.hget(f"case:{cid}", "finished_at")


def test_barrier_partially_completed(client, fake):
    cid, (s1, s2) = make_two(client)
    report(client, cid, s1)
    report(client, cid, s2, "failed", error="boom", error_type="FFmpegError")
    assert case_status(fake, cid) == "partially_completed"


def test_barrier_failed_when_all_failed(client, fake):
    cid, (s1, s2) = make_two(client)
    report(client, cid, s1, "failed", error="a")
    report(client, cid, s2, "failed", error="b")
    assert case_status(fake, cid) == "failed"


def test_invalid_report_status_is_422_and_retryable(client, fake):
    cid, (s1, _) = make_two(client)
    assert report(client, cid, s1, "weird").status_code == 422
    assert report(client, cid, s1).status_code == 200
    assert fake.hget(f"case:{cid}", "pending_subtasks") == "1"


# --- cancelación ----------------------------------------------------------
def test_cancel_marks_pending_and_counts_running(client, fake):
    data = create(client, [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "videos/b.mp4"},
        {"task_type": "auto", "file_path": "audio/c.mp3"},
    ]).json()
    cid, (s1, s2, s3) = data["case_id"], data["subtask_ids"]
    fake.hset(f"subtask:{s1}", "status", "running")

    resp = client.post(f"/cases/{cid}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {
        "case_id": cid, "status": "cancelled",
        "cancelled_subtasks": 2, "running_subtasks": 1,
    }
    case = fake.hgetall(f"case:{cid}")
    assert case["status"] == "cancelled"
    assert case["cancelled_at"] and case["finished_at"]
    assert case["pending_subtasks"] == "1"
    assert fake.hget(f"subtask:{s1}", "status") == "running"
    assert fake.hget(f"subtask:{s2}", "status") == "cancelled"
    assert fake.hget(f"subtask:{s3}", "status") == "cancelled"
    assert fake.smembers(f"case:{cid}:done") == {s2, s3}
    assert fake.llen("queue:transcode_video") == 2


def test_cancel_missing_case_is_404(client):
    assert client.post("/cases/nope/cancel").status_code == 404


@pytest.mark.parametrize("terminal", ["completed", "partially_completed", "failed", "cancelled"])
def test_cancel_terminal_case_is_409(client, fake, terminal):
    cid = create(client).json()["case_id"]
    fake.hset(f"case:{cid}", "status", terminal)
    assert client.post(f"/cases/{cid}/cancel").status_code == 409


def test_cancel_twice_is_409(client):
    cid = create(client).json()["case_id"]
    assert client.post(f"/cases/{cid}/cancel").status_code == 200
    assert client.post(f"/cases/{cid}/cancel").status_code == 409


def test_report_after_cancel_keeps_case_cancelled_and_is_idempotent(client, fake):
    data = create(client, [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "videos/b.mp4"},
    ]).json()
    cid, (s1, s2) = data["case_id"], data["subtask_ids"]
    fake.hset(f"subtask:{s1}", "status", "running")
    client.post(f"/cases/{cid}/cancel")

    resp = report(client, cid, s1)
    assert resp.status_code == 200
    assert resp.json()["case_status"] == "cancelled"
    assert resp.json()["pending_subtasks"] == 0
    assert case_status(fake, cid) == "cancelled"
    assert fake.hget(f"subtask:{s1}", "status") == "completed"
    assert json.loads(fake.hget(f"subtask:{s1}", "outputs")) == [f"out/{s1}.mp4"]

    dup = report(client, cid, s1)
    assert "idempotencia" in dup.json()["message"]
    assert fake.hget(f"case:{cid}", "pending_subtasks") == "0"
    assert case_status(fake, cid) == "cancelled"


def test_report_for_cancelled_subtask_is_ignored(client, fake):
    data = create(client).json()
    cid, sid = data["case_id"], data["subtask_ids"][0]
    client.post(f"/cases/{cid}/cancel")
    resp = report(client, cid, sid)
    assert "idempotencia" in resp.json()["message"]
    assert fake.hget(f"subtask:{sid}", "status") == "cancelled"


# --- reporte consolidado --------------------------------------------------
def test_report_groups_by_type_and_operation(client, fake):
    data = create(
        client,
        [
            {"task_type": "transcode_video", "file_path": "videos/a.mp4", "metadata": {"k": 1}},
            {"task_type": "generate_thumbnail", "file_path": "videos/b.mkv"},
            {"task_type": "extract_metadata", "file_path": "videos/c.mp4"},
            {"task_type": "convert_audio", "file_path": "audio/d.wav"},
            {"task_type": "extract_metadata", "file_path": "docs/e.bin"},
        ],
        priority="high",
        metadata={"lote": "A"},
    ).json()
    cid, (s1, s2, s3, s4, s5) = data["case_id"], data["subtask_ids"]
    report(client, cid, s1)
    report(client, cid, s2)
    report(client, cid, s3, "failed", error="x", error_type="FFmpegError")
    report(client, cid, s4)
    assert case_status(fake, cid) == "queued"

    rep = client.get(f"/cases/{cid}/report").json()
    assert rep["priority"] == "high"
    assert rep["metadata"] == {"lote": "A"}
    assert set(rep["subtasks_by_type"]) == {"video", "audio", "other"}
    assert len(rep["subtasks_by_type"]["video"]) == 3
    assert all(e["file_type"] == "video" for e in rep["subtasks_by_type"]["video"])
    assert rep["totals_by_type_and_operation"] == {
        "video": {
            "transcode_video": {"completed": 1, "failed": 0, "other": 0},
            "generate_thumbnail": {"completed": 1, "failed": 0, "other": 0},
            "extract_metadata": {"completed": 0, "failed": 1, "other": 0},
        },
        "audio": {"convert_audio": {"completed": 1, "failed": 0, "other": 0}},
        "other": {"extract_metadata": {"completed": 0, "failed": 0, "other": 1}},
    }
    transcode = rep["subtasks_by_operation"]["transcode_video"][0]
    assert transcode["metadata"] == {"k": 1}
    assert transcode["file_type"] == "video"
    assert rep["totals"] == {"total": 5, "completed": 3, "failed": 1, "pending": 1}
    assert rep["failure_breakdown"] == {"FFmpegError": 1}


# --- endpoints de lectura -------------------------------------------------
def test_get_subtask_parses_json_fields(client):
    data = create(client, [{
        "task_type": "auto", "file_path": "videos/a.mp4",
        "params": {"crf": 23}, "metadata": {"a": "b"},
    }]).json()
    cid, sid = data["case_id"], data["subtask_ids"][0]
    report(client, cid, sid)
    st = client.get(f"/subtasks/{sid}").json()
    assert st["params"] == {"crf": 23}
    assert st["metadata"] == {"a": "b"}
    assert st["outputs"] == [f"out/{sid}.mp4"]
    assert st["status"] == "completed"


def test_get_subtask_missing_is_404(client):
    assert client.get("/subtasks/nope").status_code == 404


def test_workers_alive_and_dead(client, fake):
    fake.sadd("workers:registry", "w2", "w1", "w3")
    fake.hset("worker:w1", mapping={"worker_id": "w1", "host": "n1", "active_subtasks": 2})
    fake.hset("worker:w2", mapping={"worker_id": "w2", "host": "n2", "active_subtasks": 1})
    workers = client.get("/workers").json()
    assert [w["worker_id"] for w in workers] == ["w1", "w2", "w3"]
    assert [w["alive"] for w in workers] == [True, True, False]
    assert workers[0]["host"] == "n1"


def test_stats(client, fake):
    create(client, [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "videos/b.mp4"},
    ])
    create(client, [{"task_type": "convert_audio", "file_path": "audio/c.mp3"}], priority="high")
    fake.sadd("workers:registry", "w1", "w2", "w3")
    fake.hset("worker:w1", mapping={"worker_id": "w1", "active_subtasks": 2})
    fake.hset("worker:w2", mapping={"worker_id": "w2", "active_subtasks": 1})

    stats = client.get("/stats").json()
    expected_queues = {f"queue:{op}": 0 for op in OPS}
    expected_queues.update({f"queue:{op}:high": 0 for op in OPS})
    expected_queues["queue:transcode_video"] = 2
    expected_queues["queue:convert_audio:high"] = 1
    assert stats["queues"] == expected_queues
    assert stats["cases_by_status"] == {"queued": 2}
    assert stats["workers_alive"] == 2
    assert stats["workers_total"] == 3
    assert stats["subtasks_active"] == 3


def test_list_cases_status_filter(client, fake):
    c1 = create(client).json()["case_id"]
    c2 = create(client).json()["case_id"]
    client.post(f"/cases/{c2}/cancel")
    assert len(client.get("/cases").json()) == 2
    queued = client.get("/cases", params={"status": "queued"}).json()
    assert [c["case_id"] for c in queued] == [c1]
    cancelled = client.get("/cases", params={"status": "cancelled"}).json()
    assert [c["case_id"] for c in cancelled] == [c2]
    assert client.get("/cases", params={"status": "failed"}).json() == []


# --- health / hardware / Redis caído ----------------------------------------
def _redis_down(fake, monkeypatch):
    import redis as redis_lib

    def boom(*a, **k):
        raise redis_lib.exceptions.ConnectionError("down")

    for name in ("ping", "smembers", "hgetall", "llen"):
        monkeypatch.setattr(fake, name, boom)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "redis" in r.json()


def test_health_503_when_redis_down(client, fake, monkeypatch):
    _redis_down(fake, monkeypatch)
    r = client.get("/health")
    assert r.status_code == 503
    assert "Redis" in r.json()["detail"]


def test_health_503_when_redis_times_out(client, fake, monkeypatch):
    import redis as redis_lib

    def timeout(*args, **kwargs):
        raise redis_lib.exceptions.TimeoutError("timeout")

    monkeypatch.setattr(fake, "ping", timeout)
    r = client.get("/health")
    assert r.status_code == 503


def test_hardware_returns_alive_workers_only(client, fake):
    fake.sadd("workers:registry", "w1", "w2")
    fake.hset("worker:w1", mapping={
        "host": "h1", "ip": "10.0.0.1", "cpu_percent": "12.5", "mem_percent": "30",
        "mem_total_gb": "16", "cpu_count": "8", "gpu": "RTX", "nvenc_ok": "1",
        "gpu_percent": "44.0", "active_subtasks": "2", "last_seen": "t"})
    data = client.get("/hardware").json()
    assert len(data) == 1
    e = data[0]
    assert e["worker_id"] == "w1" and e["host"] == "h1"
    assert e["cpu_percent"] == 12.5 and e["mem_percent"] == 30.0
    assert e["mem_total_gb"] == 16.0 and e["cpu_count"] == 8
    assert e["gpu"] == "RTX" and e["gpu_percent"] == 44.0
    assert e["active_subtasks"] == 2


def test_hardware_defaults_without_gpu(client, fake):
    fake.sadd("workers:registry", "w1")
    fake.hset("worker:w1", mapping={"host": "h1"})
    e = client.get("/hardware").json()[0]
    assert e["gpu"] == "none" and e["gpu_percent"] is None and e["nvenc_ok"] == "0"


@pytest.mark.parametrize("path", ["/workers", "/stats"])
def test_dashboard_endpoints_503_when_redis_down(client, fake, monkeypatch, path):
    _redis_down(fake, monkeypatch)
    r = client.get(path)
    assert r.status_code == 503
    assert "Redis" in r.json()["detail"]


def test_hardware_empty_list_when_redis_down(client, fake, monkeypatch):
    # /hardware degrades to [] (documented in the endpoint) instead of 503
    _redis_down(fake, monkeypatch)
    r = client.get("/hardware")
    assert r.status_code == 200 and r.json() == []
