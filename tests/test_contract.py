import time
from pathlib import PurePosixPath

import fakeredis
import pytest
from fastapi.testclient import TestClient

import src.coordinator.main as main
from fakes import FakeStorage, make_fake_processor

from scripts.reaper import Reaper
from src.workers.config import OPERATIONS, Settings
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

    def make_worker(self, processor=None):
        return Worker(
            self.settings,
            redis_client=self.redis,
            storage=self.storage,
            processor=processor if processor is not None else make_fake_processor(),
            reporter=self.reporter,
            heartbeat_enabled=False,
            ffmpeg_info=("test", "none"),
        )

    def create_case(self, subtasks):
        for st in subtasks:
            self.storage.objects[(self.storage.dataset_bucket, st["file_path"])] = b"input"
        return self.client.post("/cases", json={"subtasks": subtasks})

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
