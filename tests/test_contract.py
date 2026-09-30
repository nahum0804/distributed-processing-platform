import dataclasses
import threading
import time
from pathlib import PurePosixPath

import fakeredis
import pytest
from fastapi.testclient import TestClient

import src.coordinator.main as main
from fakes import FakeStorage, make_fake_processor

from scripts.reaper import Reaper
from src.workers.config import OPERATIONS, Settings
from src.workers.heartbeat import Heartbeat, WorkerStats
from src.workers.reporter import Reporter
from src.workers.worker_node import Worker

COORD = "http://coord"


class CoordinatorSession:
    def __init__(self, client):
        self.client = client
        self.payloads = []

    def post(self, url, json=None, timeout=None):
        assert url.startswith(COORD)
        self.payloads.append(json)
        return self.client.post(url[len(COORD):], json=json)


class Env:
    def __init__(self, tmp_path, fake, client, storage):
        self.redis = fake
        self.client = client
        self.storage = storage
        self.settings = Settings.from_env({
            "WORKER_ID": "w1",
            "WORK_DIR": str(tmp_path),
            "WORKER_CONCURRENCY": "1",
            "COORDINATOR_URL": COORD,
        })
        self.session = CoordinatorSession(client)
        self.reporter = Reporter(self.settings, fake, session=self.session, delays=())

    def make_worker(self, processor=None, settings=None):
        return Worker(
            settings if settings is not None else self.settings,
            redis_client=self.redis,
            storage=self.storage,
            processor=processor if processor is not None else make_fake_processor(),
            reporter=self.reporter,
            heartbeat_enabled=False,
            ffmpeg_info=("test", "none"),
        )

    def create_case(self, subtasks, **extra):
        for st in subtasks:
            self.storage.objects[(self.storage.dataset_bucket, st["file_path"])] = b"input"
        return self.client.post("/cases", json={"subtasks": subtasks, **extra})

    def case(self, case_id):
        return self.client.get(f"/cases/{case_id}").json()["case"]

    def report(self, case_id):
        return self.client.get(f"/cases/{case_id}/report").json()

    def drain(self, worker, limit=50):
        processed = 0
        while processed < limit and worker.poll_once(timeout=1):
            processed += 1
        return processed


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(main, "redis_client", fake)
    return Env(tmp_path, fake, TestClient(main.app), FakeStorage())


def subtasks_of(client, case_id):
    return client.get(f"/cases/{case_id}").json()["subtasks"]


def test_heterogeneous_case_end_to_end(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audios/b.wav"},
        {"task_type": "extract_metadata", "file_path": "videos/c.mp4"},
    ])
    assert resp.status_code == 201
    case_id = resp.json()["case_id"]

    worker = env.make_worker()
    assert env.drain(worker) == 3

    case = env.client.get(f"/cases/{case_id}").json()
    assert case["case"]["status"] == "completed"
    subtasks = {PurePosixPath(s["file_path"]).name: s for s in case["subtasks"]}
    assert len(subtasks) == 3
    for st in subtasks.values():
        assert st["status"] == "completed"
        assert st["worker_id"] == "w1"
    assert subtasks["a.mp4"]["task_type"] == "transcode_video"
    assert subtasks["b.wav"]["task_type"] == "convert_audio"
    assert subtasks["c.mp4"]["task_type"] == "extract_metadata"

    report = env.client.get(f"/cases/{case_id}/report").json()
    assert report["status"] == "completed"
    assert report["totals"] == {"total": 3, "completed": 3, "failed": 0, "pending": 0}
    assert set(report["subtasks_by_operation"]) == {"transcode_video", "convert_audio", "extract_metadata"}
    for entries in report["subtasks_by_operation"].values():
        for entry in entries:
            assert entry["outputs"]
            assert all(o.startswith("results/") for o in entry["outputs"])


def test_failed_subtask_yields_partially_completed(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/good.mp4"},
        {"task_type": "auto", "file_path": "videos/bad.mp4"},
    ])
    case_id = resp.json()["case_id"]

    def fail_on_bad(call):
        if PurePosixPath(call["src"]).name == "bad.mp4":
            raise make_fake_processor().CorruptInputError("corrupt")

    worker = env.make_worker(make_fake_processor(fail_on_bad))
    env.drain(worker)

    assert env.client.get(f"/cases/{case_id}").json()["case"]["status"] == "partially_completed"
    report = env.client.get(f"/cases/{case_id}/report").json()
    assert report["failure_breakdown"] == {"CorruptInputError": 1}
    assert report["totals"]["completed"] == 1
    assert report["totals"]["failed"] == 1


def test_duplicate_report_is_ignored(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audios/b.wav"},
    ])
    case_id = resp.json()["case_id"]

    worker = env.make_worker()
    assert worker.poll_once(timeout=1) is True
    assert len(env.session.payloads) == 1
    payload = env.session.payloads[0]

    before = env.client.get(f"/cases/{case_id}").json()["case"]
    assert before["status"] == "processing"
    assert int(before["pending_subtasks"]) == 1

    dup = env.client.post("/subtasks/report", json=payload)
    assert dup.status_code == 200
    body = dup.json()
    assert "ignorado" in body["message"].lower()
    assert "pending_subtasks" not in body
    assert body["case_status"] == before["status"]

    after = env.client.get(f"/cases/{case_id}").json()["case"]
    assert int(after["pending_subtasks"]) == int(before["pending_subtasks"])
    assert after["status"] == before["status"]


def test_reaper_recovers_subtask_of_dead_worker(env):
    resp = env.create_case([{"task_type": "auto", "file_path": "videos/a.mp4"}])
    case_id = resp.json()["case_id"]
    sid = resp.json()["subtask_ids"][0]

    taken = env.redis.blpop(["queue:transcode_video"], timeout=1)
    assert taken is not None
    env.redis.hset(f"subtask:{sid}", mapping={
        "status": "running",
        "worker_id": "dead-w",
        "attempts": 1,
        "assigned_ts": time.time(),
    })
    env.redis.sadd("worker:dead-w:inflight", sid)
    env.redis.sadd("workers:registry", "dead-w")

    counts = Reaper(env.settings, env.redis, env.reporter).run_once()
    assert counts["requeued"] == 1

    st = subtasks_of(env.client, case_id)[0]
    assert st["status"] == "pending"

    worker = env.make_worker()
    assert env.drain(worker) == 1

    case = env.client.get(f"/cases/{case_id}").json()
    assert case["case"]["status"] == "completed"
    st = case["subtasks"][0]
    assert st["status"] == "completed"
    assert st["worker_id"] == "w1"
    assert int(st["attempts"]) == 2


def test_invalid_task_type_rejected_without_side_effects(env):
    resp = env.client.post("/cases", json={"subtasks": [
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "reticulate_splines", "file_path": "videos/b.mp4"},
    ]})
    assert resp.status_code == 422
    assert env.redis.keys("*") == []


def test_queue_items_are_parseable_by_worker(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audios/b.wav"},
        {"task_type": "generate_thumbnail", "file_path": "videos/c.mp4", "params": {"time": 3}},
    ])
    sids = set(resp.json()["subtask_ids"])

    parsed = set()
    for op in OPERATIONS:
        for raw in env.redis.lrange(f"queue:{op}", 0, -1):
            sid = Worker._parse_queue_item(raw)
            assert sid is not None
            parsed.add(sid)
    assert parsed == sids


# ---------------------------------------------------------------------------
# v4 flows: queued state, priority, cancel, retrying, dashboard endpoints
# ---------------------------------------------------------------------------
def _names(calls):
    return [PurePosixPath(c["src"]).name for c in calls]


def test_lifecycle_queued_processing_completed(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "videos/b.mp4"},
        {"task_type": "auto", "file_path": "audios/c.wav"},
    ])
    assert resp.status_code == 201
    assert resp.json()["status"] == "queued"
    case_id = resp.json()["case_id"]

    case = env.case(case_id)
    assert case["status"] == "queued"
    assert "started_at" not in case
    assert {s["status"] for s in subtasks_of(env.client, case_id)} == {"pending"}

    worker = env.make_worker()
    assert worker.poll_once(timeout=1) is True
    case = env.case(case_id)
    assert case["status"] == "processing"
    assert case["started_at"]
    assert int(case["pending_subtasks"]) == 2

    assert env.drain(worker) == 2
    case = env.case(case_id)
    assert case["status"] == "completed"
    assert int(case["pending_subtasks"]) == 0
    assert case["finished_at"]
    assert case["started_at"] <= case["finished_at"]
    assert env.report(case_id)["totals"] == {"total": 3, "completed": 3, "failed": 0, "pending": 0}


def test_all_subtasks_failing_yields_failed_case(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audios/b.wav"},
        {"task_type": "extract_metadata", "file_path": "videos/c.mp4"},
    ])
    case_id = resp.json()["case_id"]

    processor = make_fake_processor("CorruptInputError")
    worker = env.make_worker(processor)
    assert env.drain(worker) == 3
    assert len(processor.calls) == 3

    assert env.case(case_id)["status"] == "failed"
    report = env.report(case_id)
    assert report["status"] == "failed"
    assert report["failure_breakdown"] == {"CorruptInputError": 3}
    assert report["totals"] == {"total": 3, "completed": 0, "failed": 3, "pending": 0}
    for st in subtasks_of(env.client, case_id):
        assert st["status"] == "failed"
        assert st["error_type"] == "CorruptInputError"
    for entries in report["subtasks_by_operation"].values():
        for entry in entries:
            assert entry["status"] == "failed"
            assert entry["error_type"] == "CorruptInputError"
            assert entry["outputs"] == []


def test_high_priority_case_is_processed_first(env):
    normal = env.create_case([
        {"task_type": "transcode_video", "file_path": "videos/n1.mp4"},
        {"task_type": "transcode_video", "file_path": "videos/n2.mp4"},
    ])
    high = env.create_case(
        [{"task_type": "transcode_video", "file_path": "videos/h1.mp4"}],
        priority="high",
    )
    assert normal.json()["priority"] == "normal"
    assert high.json()["priority"] == "high"
    normal_id, high_id = normal.json()["case_id"], high.json()["case_id"]
    high_sid = high.json()["subtask_ids"][0]

    stats = env.client.get("/stats").json()
    assert stats["queues"]["queue:transcode_video:high"] == 1
    assert stats["queues"]["queue:transcode_video"] == 2
    assert stats["cases_by_status"] == {"queued": 2}

    processor = make_fake_processor()
    settings = dataclasses.replace(env.settings, worker_queues=("transcode_video",))
    worker = env.make_worker(processor, settings=settings)
    assert env.drain(worker) == 3

    assert _names(processor.calls) == ["h1.mp4", "n1.mp4", "n2.mp4"]
    assert [p["subtask_id"] for p in env.session.payloads][0] == high_sid

    stats = env.client.get("/stats").json()
    assert stats["queues"]["queue:transcode_video:high"] == 0
    assert stats["queues"]["queue:transcode_video"] == 0
    assert stats["cases_by_status"] == {"completed": 2}

    assert env.report(high_id)["priority"] == "high"
    assert env.report(normal_id)["priority"] == "normal"
    high_entry = env.report(high_id)["subtasks_by_operation"]["transcode_video"][0]
    assert high_entry["priority"] == "high"


def test_cancel_before_any_work_leaves_nothing_to_process(env):
    resp = env.create_case([
        {"task_type": "auto", "file_path": "videos/a.mp4"},
        {"task_type": "auto", "file_path": "audios/b.wav"},
        {"task_type": "extract_metadata", "file_path": "videos/c.mp4"},
    ])
    case_id = resp.json()["case_id"]

    cancel = env.client.post(f"/cases/{case_id}/cancel")
    assert cancel.status_code == 200
    body = cancel.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_subtasks"] == 3
    assert body["running_subtasks"] == 0

    processor = make_fake_processor()
    worker = env.make_worker(processor)
    env.drain(worker)  # queue items are still there; the worker skips them
    assert processor.calls == []
    assert env.session.payloads == []

    case = env.case(case_id)
    assert case["status"] == "cancelled"
    assert int(case["pending_subtasks"]) == 0
    assert {s["status"] for s in subtasks_of(env.client, case_id)} == {"cancelled"}
    # cancelling again is a conflict, not a state change
    assert env.client.post(f"/cases/{case_id}/cancel").status_code == 409
    assert env.case(case_id)["status"] == "cancelled"


def test_cancel_after_partial_progress_skips_remaining_queue_items(env):
    resp = env.create_case([
        {"task_type": "transcode_video", "file_path": "videos/a.mp4"},
        {"task_type": "transcode_video", "file_path": "videos/b.mp4"},
        {"task_type": "transcode_video", "file_path": "videos/c.mp4"},
    ])
    case_id = resp.json()["case_id"]

    processor = make_fake_processor()
    worker = env.make_worker(processor)
    assert worker.poll_once(timeout=1) is True
    assert _names(processor.calls) == ["a.mp4"]
    assert env.case(case_id)["status"] == "processing"

    cancel = env.client.post(f"/cases/{case_id}/cancel").json()
    assert cancel["cancelled_subtasks"] == 2
    assert cancel["running_subtasks"] == 0
    assert env.redis.llen("queue:transcode_video") == 2

    env.drain(worker)
    assert _names(processor.calls) == ["a.mp4"]  # remaining two never processed
    assert len(env.session.payloads) == 1
    assert env.redis.llen("queue:transcode_video") == 0

    case = env.case(case_id)
    assert case["status"] == "cancelled"
    assert int(case["pending_subtasks"]) == 0
    statuses = sorted(s["status"] for s in subtasks_of(env.client, case_id))
    assert statuses == ["cancelled", "cancelled", "completed"]

    report = env.report(case_id)
    assert report["status"] == "cancelled"
    assert report["totals"] == {"total": 3, "completed": 1, "failed": 0, "pending": 0}


@pytest.mark.parametrize("priority, queue", [
    ("normal", "queue:transcode_video"),
    ("high", "queue:transcode_video:high"),
])
def test_reaper_retry_flow_marks_case_retrying_then_completes(env, priority, queue):
    resp = env.create_case(
        [{"task_type": "transcode_video", "file_path": "videos/a.mp4"}],
        priority=priority,
    )
    case_id = resp.json()["case_id"]
    sid = resp.json()["subtask_ids"][0]
    assert env.redis.llen(queue) == 1

    # a worker that will "die" really claims the subtask through the worker code path
    dead_settings = dataclasses.replace(env.settings, worker_id="dead-w")
    dead = env.make_worker(settings=dead_settings)
    popped = env.redis.blpop(dead_settings.queue_keys(), timeout=1)
    assert popped is not None and popped[0] == queue
    assert Worker._parse_queue_item(popped[1]) == sid
    outcome, _data, attempts = dead._claim_subtask(sid)
    assert (outcome, attempts) == ("claimed", 1)
    env.redis.sadd("workers:registry", "dead-w")  # registered, but no heartbeat key

    case = env.case(case_id)
    assert case["status"] == "processing"
    assert subtasks_of(env.client, case_id)[0]["status"] == "assigned"
    assert env.client.get("/stats").json()["workers_alive"] == 0

    counts = Reaper(env.settings, env.redis, env.reporter).run_once()
    assert counts["requeued"] == 1
    assert counts["workers_removed"] == 1

    case = env.case(case_id)
    assert case["status"] == "retrying"
    assert int(case["retries"]) == 1
    st = subtasks_of(env.client, case_id)[0]
    assert st["status"] == "pending"
    assert st["requeue_reason"] == "worker_lost"
    assert env.redis.lrange(queue, 0, -1) == [sid]
    other = "queue:transcode_video" if priority == "high" else "queue:transcode_video:high"
    assert env.redis.llen(other) == 0
    assert env.client.get("/stats").json()["cases_by_status"] == {"retrying": 1}

    worker = env.make_worker()
    assert env.drain(worker) == 1

    case = env.case(case_id)
    assert case["status"] == "completed"
    assert int(case["retries"]) == 1
    st = subtasks_of(env.client, case_id)[0]
    assert st["status"] == "completed"
    assert st["worker_id"] == "w1"
    assert int(st["attempts"]) == 2

    report = env.report(case_id)
    assert report["status"] == "completed"
    assert report["retries"] == 1
    assert report["subtasks_by_operation"]["transcode_video"][0]["attempts"] == 2


def test_workers_and_stats_reflect_real_heartbeat(env):
    worker = env.make_worker()
    env.create_case([{"task_type": "auto", "file_path": "videos/a.mp4"}])
    assert env.drain(worker) == 1  # worker.stats now has completed=1

    assert env.client.get("/workers").json() == []
    assert env.client.get("/stats").json()["workers_alive"] == 0

    hb = Heartbeat(
        env.settings, env.redis, worker.stats, threading.Event(),
        ffmpeg_info=("ffmpeg test", "none"), gpu_info={},
    )
    hb.beat()

    workers = env.client.get("/workers").json()
    assert len(workers) == 1
    w = workers[0]
    assert w["worker_id"] == "w1"
    assert w["alive"] is True
    assert w["host"] == env.settings.node_name
    assert w["queues"] == ",".join(env.settings.worker_queues)
    assert w["concurrency"] == "1"
    assert 0.0 <= float(w["cpu_percent"]) <= 100.0
    assert 0.0 <= float(w["mem_percent"]) <= 100.0
    assert w["completed_count"] == "1"
    assert w["failed_count"] == "0"
    assert w["active_subtasks"] == "0"
    assert w["ffmpeg_version"] == "ffmpeg test"
    assert w["last_seen"]

    stats = env.client.get("/stats").json()
    assert stats["workers_alive"] == 1
    assert stats["workers_total"] == 1
    assert stats["subtasks_active"] == 0
    assert stats["cases_by_status"] == {"completed": 1}
    assert set(stats["queues"]) == (
        {f"queue:{op}" for op in OPERATIONS} | {f"queue:{op}:high" for op in OPERATIONS}
    )
    assert all(v == 0 for v in stats["queues"].values())

    # heartbeat expiry: registered but no longer alive
    env.redis.delete("worker:w1")
    workers = env.client.get("/workers").json()
    assert [(x["worker_id"], x["alive"]) for x in workers] == [("w1", False)]
    stats = env.client.get("/stats").json()
    assert stats["workers_alive"] == 0
    assert stats["workers_total"] == 1


def test_metadata_round_trip_and_totals_by_type_and_operation(env):
    meta_a = {"source": "camera-1", "tags": ["x", "y"], "nested": {"k": 1}}
    meta_b = {"lang": "es"}
    resp = env.create_case(
        [
            {"task_type": "auto", "file_path": "videos/a.mp4", "metadata": meta_a},
            {"task_type": "auto", "file_path": "videos/b.mp4"},
            {"task_type": "auto", "file_path": "audios/c.wav", "metadata": meta_b},
            {"task_type": "extract_metadata", "file_path": "videos/d.mp4"},
        ],
        metadata={"owner": "kenny", "batch": 7},
    )
    case_id = resp.json()["case_id"]
    sids = resp.json()["subtask_ids"]

    st = env.client.get(f"/subtasks/{sids[0]}").json()
    assert st["metadata"] == meta_a
    assert st["case_id"] == case_id
    assert st["operation"] == "transcode_video"
    assert st["params"] == {}
    assert env.client.get(f"/subtasks/{sids[1]}").json()["metadata"] == {}
    assert env.client.get(f"/subtasks/{sids[2]}").json()["metadata"] == meta_b
    assert env.client.get("/subtasks/nope").status_code == 404

    worker = env.make_worker()
    assert env.drain(worker) == 4

    # metadata survives the whole lifecycle (worker/report updates don't clobber it)
    st = env.client.get(f"/subtasks/{sids[0]}").json()
    assert st["status"] == "completed"
    assert st["metadata"] == meta_a
    assert st["outputs"] and st["outputs"][0].startswith("results/")

    report = env.report(case_id)
    assert report["metadata"] == {"owner": "kenny", "batch": 7}
    by_name = {
        PurePosixPath(e["file_path"]).name: e
        for entries in report["subtasks_by_operation"].values()
        for e in entries
    }
    assert by_name["a.mp4"]["metadata"] == meta_a
    assert by_name["b.mp4"]["metadata"] == {}
    assert by_name["c.wav"]["metadata"] == meta_b
    assert by_name["d.mp4"]["metadata"] == {}

    assert report["totals_by_type_and_operation"] == {
        "video": {
            "transcode_video": {"completed": 2, "failed": 0, "other": 0},
            "extract_metadata": {"completed": 1, "failed": 0, "other": 0},
        },
        "audio": {"convert_audio": {"completed": 1, "failed": 0, "other": 0}},
    }
    assert {k: len(v) for k, v in report["subtasks_by_type"].items()} == {"video": 3, "audio": 1}
    assert {e["file_type"] for e in report["subtasks_by_type"]["audio"]} == {"audio"}
