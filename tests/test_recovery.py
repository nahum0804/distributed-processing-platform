from __future__ import annotations

import fakeredis
import pytest

from src.workers.config import Settings
from src.workers.recovery import recover_subtask


class FakeReporter:
    def __init__(self, report_result: bool = True):
        self.reported: list[dict] = []
        self.report_result = report_result

    def report(self, payload: dict) -> bool:
        self.reported.append(payload)
        return self.report_result


def make_settings(**overrides) -> Settings:
    defaults = dict(max_attempts=3)
    defaults.update(overrides)
    return Settings(**defaults)


def make_subtask(
    redis_client,
    sid: str,
    *,
    case_id: str = "case-1",
    operation: str | None = "transcode_video",
    status: str = "assigned",
    worker_id: str = "worker-a",
    attempts: int = 0,
    started_at: str = "",
) -> None:
    mapping = {
        "subtask_id": sid,
        "case_id": case_id,
        "status": status,
        "worker_id": worker_id,
        "attempts": attempts,
        "assigned_at": "2026-09-27T00:00:00+00:00",
        "progress": 0,
    }
    if operation is not None:
        mapping["operation"] = operation
    if started_at:
        mapping["started_at"] = started_at
    redis_client.hset(f"subtask:{sid}", mapping=mapping)


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


def test_requeues_to_front_of_queue(redis_client):
    redis_client.rpush("queue:transcode_video", "existing-1")
    make_subtask(redis_client, "sid-1", operation="transcode_video", status="running", attempts=1)

    reporter = FakeReporter()
    result = recover_subtask(redis_client, make_settings(), reporter, "sid-1", "worker_restart")

    assert result == "requeued"
    queue = redis_client.lrange("queue:transcode_video", 0, -1)
    assert queue == ["sid-1", "existing-1"]

    data = redis_client.hgetall("subtask:sid-1")
    assert data["status"] == "pending"
    assert data["worker_id"] == ""
    assert data["progress"] == "0"
    assert data["requeue_reason"] == "worker_restart"
    assert "requeued_at" in data
    assert reporter.reported == []


def test_max_attempts_reports_failure_with_given_reporter_host(redis_client):
    make_subtask(
        redis_client, "sid-doomed", operation="extract_audio", status="running",
        attempts=3, worker_id="worker-a", started_at="2026-09-27T01:00:00+00:00",
    )

    reporter = FakeReporter()
    result = recover_subtask(
        redis_client, make_settings(max_attempts=3), reporter, "sid-doomed", "worker_restart",
        reporter_host="node-7",
    )

    assert result == "failed"
    assert redis_client.lrange("queue:extract_audio", 0, -1) == []
    assert len(reporter.reported) == 1

    payload = reporter.reported[0]
    assert payload["subtask_id"] == "sid-doomed"
    assert payload["worker_id"] == "worker-a"
    assert payload["host"] == "node-7"
    assert payload["status"] == "failed"
    assert payload["error_type"] == "WorkerLostError"
    assert payload["error"] == "Sub-tarea abandonada tras 3 intentos (worker_restart)"
    assert payload["started_at"] == "2026-09-27T01:00:00+00:00"
    assert payload["attempts"] == 3
    assert payload["output_bytes"] == 0
    assert payload["media_duration_s"] is None

    assert redis_client.hget("subtask:sid-doomed", "requeue_reason") == "worker_restart"


def test_default_reporter_host_is_reaper(redis_client):
    make_subtask(redis_client, "sid-doomed", status="running", attempts=5)

    reporter = FakeReporter()
    recover_subtask(redis_client, make_settings(max_attempts=3), reporter, "sid-doomed", "max_age")

    assert reporter.reported[0]["host"] == "reaper"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_terminal_subtask_is_cleaned(redis_client, status):
    make_subtask(redis_client, "sid-1", status=status)

    reporter = FakeReporter()
    result = recover_subtask(redis_client, make_settings(), reporter, "sid-1", "worker_restart")

    assert result == "cleaned"
    assert reporter.reported == []


def test_missing_subtask_hash_is_cleaned(redis_client):
    reporter = FakeReporter()
    result = recover_subtask(redis_client, make_settings(), reporter, "sid-ghost", "worker_restart")

    assert result == "cleaned"
    assert reporter.reported == []


def test_missing_operation_is_skipped_and_untouched(redis_client):
    make_subtask(redis_client, "sid-bad", operation=None, status="assigned", attempts=0)

    reporter = FakeReporter()
    result = recover_subtask(redis_client, make_settings(), reporter, "sid-bad", "worker_restart")

    assert result == "skipped"
    data = redis_client.hgetall("subtask:sid-bad")
    assert data["status"] == "assigned"
    assert reporter.reported == []


def test_requeue_sets_case_retrying_and_increments_retries(redis_client):
    redis_client.hset("case:case-1", mapping={"status": "processing", "retries": "0"})
    make_subtask(redis_client, "sid-1", status="running", attempts=1)

    result = recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")

    assert result == "requeued"
    assert redis_client.hget("case:case-1", "status") == "retrying"
    assert redis_client.hget("case:case-1", "retries") == "1"


def test_requeue_increments_retries_on_each_requeue(redis_client):
    redis_client.hset("case:case-1", mapping={"status": "retrying", "retries": "1"})
    make_subtask(redis_client, "sid-1", status="running", attempts=1)
    make_subtask(redis_client, "sid-2", status="running", attempts=1)

    recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")
    recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-2", "max_age")

    assert redis_client.hget("case:case-1", "status") == "retrying"
    assert redis_client.hget("case:case-1", "retries") == "3"


@pytest.mark.parametrize("case_status", ["completed", "partially_completed", "failed"])
def test_requeue_leaves_terminal_case_untouched(redis_client, case_status):
    redis_client.hset("case:case-1", mapping={"status": case_status, "retries": "2"})
    make_subtask(redis_client, "sid-1", status="running", attempts=1)

    result = recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")

    assert result == "requeued"
    assert redis_client.hget("case:case-1", "status") == case_status
    assert redis_client.hget("case:case-1", "retries") == "2"


def test_requeue_with_missing_case_hash_creates_nothing(redis_client):
    make_subtask(redis_client, "sid-1", status="running", attempts=1)

    result = recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")

    assert result == "requeued"
    assert redis_client.exists("case:case-1") == 0


def test_failure_report_does_not_touch_case(redis_client):
    redis_client.hset("case:case-1", mapping={"status": "processing", "retries": "0"})
    make_subtask(redis_client, "sid-1", status="running", attempts=3)

    result = recover_subtask(redis_client, make_settings(max_attempts=3), FakeReporter(), "sid-1", "max_age")

    assert result == "failed"
    assert redis_client.hget("case:case-1", "status") == "processing"
    assert redis_client.hget("case:case-1", "retries") == "0"


@pytest.mark.parametrize("attempts", [1, 3])
def test_cancelled_case_marks_subtask_cancelled_and_does_not_requeue(redis_client, attempts):
    redis_client.hset("case:case-1", mapping={"status": "cancelled", "retries": "0"})
    make_subtask(redis_client, "sid-1", status="running", attempts=attempts)

    reporter = FakeReporter()
    result = recover_subtask(redis_client, make_settings(max_attempts=3), reporter, "sid-1", "worker_lost")

    assert result == "cleaned"
    assert redis_client.hget("subtask:sid-1", "status") == "cancelled"
    assert redis_client.lrange("queue:transcode_video", 0, -1) == []
    assert reporter.reported == []
    assert redis_client.hget("case:case-1", "status") == "cancelled"
    assert redis_client.hget("case:case-1", "retries") == "0"


def test_high_priority_subtask_requeued_to_high_queue(redis_client):
    redis_client.rpush("queue:transcode_video:high", "existing-high")
    make_subtask(redis_client, "sid-1", status="running", attempts=1)
    redis_client.hset("subtask:sid-1", "priority", "high")

    result = recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")

    assert result == "requeued"
    assert redis_client.lrange("queue:transcode_video:high", 0, -1) == ["sid-1", "existing-high"]
    assert redis_client.lrange("queue:transcode_video", 0, -1) == []


def test_normal_priority_subtask_requeued_to_normal_queue(redis_client):
    make_subtask(redis_client, "sid-1", status="running", attempts=1)
    redis_client.hset("subtask:sid-1", "priority", "normal")

    recover_subtask(redis_client, make_settings(), FakeReporter(), "sid-1", "max_age")

    assert redis_client.lrange("queue:transcode_video", 0, -1) == ["sid-1"]
    assert redis_client.lrange("queue:transcode_video:high", 0, -1) == []
