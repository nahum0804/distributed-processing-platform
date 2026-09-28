from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from scripts.reaper import ACTIVE_STATES, TERMINAL_STATES, Reaper
from src.workers.config import Settings


class FakeReporter:
    def __init__(self, report_result: bool = True):
        self.reported: list[dict] = []
        self.report_result = report_result
        self.flush_calls = 0

    def report(self, payload: dict) -> bool:
        self.reported.append(payload)
        return self.report_result

    def flush_pending(self, max_items: int = 100) -> int:
        self.flush_calls += 1
        return 0


def make_settings(**overrides) -> Settings:
    defaults = dict(reaper_interval=0.01, reaper_max_age=100.0, max_attempts=3)
    defaults.update(overrides)
    return Settings(**defaults)


def make_subtask(
    redis_client,
    sid: str,
    *,
    case_id: str = "case-1",
    file_path: str = "/data/in.mp4",
    operation: str | None = "transcode_video",
    status: str = "assigned",
    worker_id: str = "worker-a",
    attempts: int = 0,
    assigned_ts: float | None = 1000.0,
    started_at: str = "",
) -> None:
    mapping = {
        "subtask_id": sid,
        "case_id": case_id,
        "file_path": file_path,
        "status": status,
        "worker_id": worker_id,
        "attempts": attempts,
        "assigned_at": "2026-09-27T00:00:00+00:00",
        "progress": 0,
    }
    if operation is not None:
        mapping["operation"] = operation
    if assigned_ts is not None:
        mapping["assigned_ts"] = assigned_ts
    if started_at:
        mapping["started_at"] = started_at
    redis_client.hset(f"subtask:{sid}", mapping=mapping)


def register_inflight(redis_client, wid: str, sids: list[str], alive: bool = True) -> None:
    redis_client.sadd("workers:registry", wid)
    if sids:
        redis_client.sadd(f"worker:{wid}:inflight", *sids)
    if alive:
        redis_client.hset(f"worker:{wid}", mapping={"worker_id": wid, "status": "alive"})


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


def test_dead_worker_requeues_inflight_to_front_of_queue(redis_client):
    redis_client.rpush("queue:transcode_video", "existing-1")
    make_subtask(redis_client, "sid-1", operation="transcode_video", status="assigned")
    make_subtask(redis_client, "sid-2", operation="transcode_video", status="running")
    register_inflight(redis_client, "worker-a", ["sid-1", "sid-2"], alive=False)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 2, "failed": 0, "workers_removed": 1, "cleaned": 0}

    queue = redis_client.lrange("queue:transcode_video", 0, -1)
    assert queue[2] == "existing-1"
    assert set(queue[:2]) == {"sid-1", "sid-2"}

    for sid in ("sid-1", "sid-2"):
        data = redis_client.hgetall(f"subtask:{sid}")
        assert data["status"] == "queued"
        assert data["worker_id"] == ""
        assert data["requeue_reason"] == "worker_lost"
        assert "requeued_at" in data

    assert redis_client.exists("worker:worker-a:inflight") == 0
    assert redis_client.sismember("workers:registry", "worker-a") == 0
    assert reporter.reported == []


def test_alive_worker_fresh_task_untouched(redis_client):
    make_subtask(redis_client, "sid-fresh", status="running", assigned_ts=995.0)
    register_inflight(redis_client, "worker-b", ["sid-fresh"], alive=True)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(reaper_max_age=100.0), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 0, "failed": 0, "workers_removed": 0, "cleaned": 0}
    data = redis_client.hgetall("subtask:sid-fresh")
    assert data["status"] == "running"
    assert redis_client.sismember("worker:worker-b:inflight", "sid-fresh") == 1


def test_alive_worker_expired_task_requeued_and_removed_from_inflight(redis_client):
    make_subtask(redis_client, "sid-old", status="assigned", assigned_ts=100.0)
    register_inflight(redis_client, "worker-b", ["sid-old"], alive=True)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(reaper_max_age=100.0), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 1, "failed": 0, "workers_removed": 0, "cleaned": 0}
    data = redis_client.hgetall("subtask:sid-old")
    assert data["status"] == "queued"
    assert data["requeue_reason"] == "max_age"
    assert redis_client.sismember("worker:worker-b:inflight", "sid-old") == 0
    assert redis_client.lrange("queue:transcode_video", 0, -1) == ["sid-old"]


def test_alive_worker_missing_assigned_ts_untouched(redis_client, caplog):
    make_subtask(redis_client, "sid-noage", status="assigned", assigned_ts=None)
    register_inflight(redis_client, "worker-b", ["sid-noage"], alive=True)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(reaper_max_age=1.0), redis_client, reporter, clock=lambda: 1000.0)
    with caplog.at_level("WARNING"):
        counts = reaper.run_once()

    assert counts == {"requeued": 0, "failed": 0, "workers_removed": 0, "cleaned": 0}
    data = redis_client.hgetall("subtask:sid-noage")
    assert data["status"] == "assigned"
    assert redis_client.sismember("worker:worker-b:inflight", "sid-noage") == 1
    assert any("assigned_ts" in message for message in caplog.messages)


def test_attempts_at_max_reports_failure_and_does_not_requeue(redis_client):
    make_subtask(
        redis_client,
        "sid-doomed",
        operation="extract_audio",
        status="running",
        attempts=3,
        worker_id="worker-a",
        started_at="2026-09-27T01:00:00+00:00",
    )
    register_inflight(redis_client, "worker-a", ["sid-doomed"], alive=False)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(max_attempts=3), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 0, "failed": 1, "workers_removed": 1, "cleaned": 0}
    assert redis_client.lrange("queue:extract_audio", 0, -1) == []
    assert len(reporter.reported) == 1

    payload = reporter.reported[0]
    assert payload["subtask_id"] == "sid-doomed"
    assert payload["case_id"] == "case-1"
    assert payload["worker_id"] == "worker-a"
    assert payload["host"] == "reaper"
    assert payload["status"] == "failed"
    assert payload["result_path"] == ""
    assert payload["outputs"] == []
    assert payload["error"] == "Sub-tarea abandonada tras 3 intentos (worker_lost)"
    assert payload["error_type"] == "WorkerLostError"
    assert payload["started_at"] == "2026-09-27T01:00:00+00:00"
    assert "finished_at" in payload
    assert payload["processing_s"] == 0.0
    assert payload["media_duration_s"] is None
    assert payload["output_bytes"] == 0
    assert payload["attempts"] == 3

    assert redis_client.hget("subtask:sid-doomed", "requeue_reason") == "worker_lost"


def test_attempts_at_max_started_at_falls_back_to_assigned_at(redis_client):
    make_subtask(redis_client, "sid-doomed2", status="running", attempts=5, worker_id="worker-a")
    register_inflight(redis_client, "worker-a", ["sid-doomed2"], alive=False)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(max_attempts=3), redis_client, reporter, clock=lambda: 1000.0)
    reaper.run_once()

    payload = reporter.reported[0]
    assert payload["started_at"] == "2026-09-27T00:00:00+00:00"


def test_completed_or_missing_hash_inflight_entries_are_cleaned(redis_client):
    make_subtask(redis_client, "sid-done", status="completed")
    register_inflight(redis_client, "worker-b", ["sid-done", "sid-ghost"], alive=True)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 0, "failed": 0, "workers_removed": 0, "cleaned": 2}
    assert redis_client.smembers("worker:worker-b:inflight") == set()
    assert reporter.reported == []
    assert redis_client.lrange("queue:transcode_video", 0, -1) == []


def test_dead_worker_with_terminal_subtask_is_cleaned_not_recovered(redis_client):
    make_subtask(redis_client, "sid-failed-already", status="failed")
    register_inflight(redis_client, "worker-a", ["sid-failed-already"], alive=False)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts == {"requeued": 0, "failed": 0, "workers_removed": 1, "cleaned": 1}
    assert reporter.reported == []


def test_malformed_subtask_missing_operation_does_not_stop_other_sids(redis_client):
    make_subtask(redis_client, "sid-bad", operation=None, status="assigned")
    make_subtask(redis_client, "sid-good", operation="convert_audio", status="running")
    register_inflight(redis_client, "worker-a", ["sid-bad", "sid-good"], alive=False)

    reporter = FakeReporter()
    reaper = Reaper(make_settings(), redis_client, reporter, clock=lambda: 1000.0)
    counts = reaper.run_once()

    assert counts["requeued"] == 1
    assert counts["workers_removed"] == 1
    good_data = redis_client.hgetall("subtask:sid-good")
    assert good_data["status"] == "queued"
    assert redis_client.lrange("queue:convert_audio", 0, -1) == ["sid-good"]
    bad_data = redis_client.hgetall("subtask:sid-bad")
    assert bad_data["status"] == "assigned"


def test_run_forever_stops_on_event(redis_client):
    reporter = FakeReporter()
    settings = make_settings(reaper_interval=0.01)
    reaper = Reaper(settings, redis_client, reporter)
    stop_event = threading.Event()

    thread = threading.Thread(target=reaper.run_forever, args=(stop_event,))
    thread.start()
    time.sleep(0.05)
    stop_event.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert reporter.flush_calls >= 1


def test_run_forever_logs_exception_and_continues(redis_client, caplog, monkeypatch):
    reporter = FakeReporter()
    settings = make_settings(reaper_interval=0.01)
    reaper = Reaper(settings, redis_client, reporter)
    stop_event = threading.Event()

    calls = {"n": 0}
    original_run_once = reaper.run_once

    def flaky_run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        if calls["n"] >= 2:
            stop_event.set()
        return original_run_once()

    monkeypatch.setattr(reaper, "run_once", flaky_run_once)

    with caplog.at_level("ERROR"):
        thread = threading.Thread(target=reaper.run_forever, args=(stop_event,))
        thread.start()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert calls["n"] >= 2
    assert any("ciclo del reaper" in message for message in caplog.messages)


def test_import_scripts_reaper_does_not_require_reporter_module(monkeypatch):
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "src.workers.reporter" or name.startswith("src.workers.reporter"):
            raise ImportError("reporter module unavailable")
        return real_import(name, *args, **kwargs)

    sys.modules.pop("scripts.reaper", None)
    monkeypatch.setattr(builtins, "__import__", blocking_import)
    module = importlib.import_module("scripts.reaper")
    assert hasattr(module, "Reaper")
    assert hasattr(module, "main")
    assert module.ACTIVE_STATES == ACTIVE_STATES
    assert module.TERMINAL_STATES == TERMINAL_STATES
