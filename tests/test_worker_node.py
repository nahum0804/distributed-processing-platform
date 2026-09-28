import json
import threading
import time

import fakeredis
import requests
import responses

from fakes import FakeStorage, make_fake_processor

from src.workers.config import Settings
from src.workers.reporter import PENDING_KEY, Reporter
from src.workers.storage import StorageError
from src.workers.worker_node import Worker


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings.from_env({
        "WORKER_ID": "w1",
        "WORK_DIR": str(tmp_path),
        "WORKER_CONCURRENCY": "1",
        "COORDINATOR_URL": "http://coord",
        **overrides,
    })


def seed_subtask(redis_client, storage, sid, case_id="c1", operation="transcode_video",
                  file_bytes=b"input-bytes", params=None, raw_params=None):
    key = f"{case_id}/{sid}/input.bin"
    storage.objects[(storage.dataset_bucket, key)] = file_bytes
    mapping = {
        "subtask_id": sid,
        "case_id": case_id,
        "file_path": key,
        "operation": operation,
        "status": "queued",
    }
    if params is not None:
        mapping["params"] = json.dumps(params)
    elif raw_params is not None:
        mapping["params"] = raw_params
    redis_client.hset(f"subtask:{sid}", mapping=mapping)
    redis_client.rpush(f"queue:{operation}", sid)
    return key


class StubReporter:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls = []
        self.lock = threading.Lock()

    def report(self, payload):
        with self.lock:
            self.calls.append((time.monotonic(), payload))
        return self.ok

    def flush_pending(self, max_items: int = 100):
        return 0


def make_worker(tmp_path, redis_client=None, storage=None, processor=None, reporter=None, **settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    redis_client = redis_client if redis_client is not None else fakeredis.FakeRedis(decode_responses=True)
    storage = storage if storage is not None else FakeStorage()
    processor = processor if processor is not None else make_fake_processor()
    reporter = reporter if reporter is not None else StubReporter()
    worker = Worker(
        settings,
        redis_client=redis_client,
        storage=storage,
        processor=processor,
        reporter=reporter,
        heartbeat_enabled=False,
        ffmpeg_info=("test", "none"),
    )
    return worker, redis_client, storage, processor, reporter


def test_multi_queue_priority(tmp_path):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    storage = FakeStorage()
    processor = make_fake_processor()
    worker, redis_client, storage, processor, reporter = make_worker(
        tmp_path, redis_client=redis_client, storage=storage, processor=processor,
        WORKER_QUEUES="generate_thumbnail,transcode_video",
    )

    seed_subtask(redis_client, storage, "sid-a", operation="transcode_video")
    seed_subtask(redis_client, storage, "sid-b", operation="generate_thumbnail")

    assert worker.poll_once(timeout=1) is True
    assert worker.poll_once(timeout=1) is True

    assert processor.calls[0]["operation"] == "generate_thumbnail"
    assert processor.calls[1]["operation"] == "transcode_video"


def test_assigned_fields_and_inflight_during_processing(tmp_path):
    observed = {}

    def check(call_kwargs):
        observed["status"] = redis_client.hget("subtask:sid1", "status")
        observed["worker_id"] = redis_client.hget("subtask:sid1", "worker_id")
        observed["assigned_ts"] = redis_client.hget("subtask:sid1", "assigned_ts")
        observed["attempts"] = redis_client.hget("subtask:sid1", "attempts")
        observed["inflight"] = redis_client.sismember("worker:w1:inflight", "sid1")

    processor = make_fake_processor(outcome=check)
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path, processor=processor)
    seed_subtask(redis_client, storage, "sid1")

    payload = worker.handle_subtask("sid1")

    assert observed["status"] == "running"
    assert observed["worker_id"] == "w1"
    assert float(observed["assigned_ts"]) > 0
    assert observed["attempts"] == "1"
    assert bool(observed["inflight"]) is True

    assert payload["status"] == "completed"
    assert bool(redis_client.sismember("worker:w1:inflight", "sid1")) is False


def test_success_payload_fields(tmp_path):
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path)
    seed_subtask(redis_client, storage, "sid1", params={"key": "value"})

    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "completed"
    assert payload["outputs"] == [f"results/c1/sid1/input.out"]
    assert payload["result_path"] == payload["outputs"][0]
    assert payload["output_bytes"] == len(b"fake-output")
    assert payload["error"] is None
    assert payload["error_type"] is None
    assert payload["attempts"] == 1
    assert isinstance(payload["processing_s"], float)

    call = processor.calls[-1]
    assert call["params"] == {"key": "value"}
    assert call["threads"] == worker.settings.threads_per_job()
    assert call["timeout"] == worker.settings.ffmpeg_timeout

    assert redis_client.hget("subtask:sid1", "progress") == "100"
    assert not (worker.settings.work_dir / "sid1").exists()

    assert len(reporter.calls) == 1
    assert reporter.calls[0][1] == payload


def test_each_processing_error_subclass_marks_failed(tmp_path):
    for exc_name in (
        "UnsupportedFormatError", "CorruptInputError", "ProcessingTimeoutError",
        "InputNotFoundError", "FFmpegNotAvailableError",
    ):
        processor = make_fake_processor(outcome=exc_name)
        worker, redis_client, storage, processor, reporter = make_worker(tmp_path, processor=processor)
        seed_subtask(redis_client, storage, "sid1")

        payload = worker.handle_subtask("sid1")

        assert payload["status"] == "failed"
        assert payload["error_type"] == exc_name
        assert payload["error"]
        assert payload["output_bytes"] == 0
        assert not (worker.settings.work_dir / "sid1").exists()


def test_missing_dataset_object_is_storage_error(tmp_path):
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path)
    redis_client.hset("subtask:sid1", mapping={
        "subtask_id": "sid1", "case_id": "c1", "file_path": "c1/sid1/missing.bin",
        "operation": "transcode_video", "status": "queued",
    })

    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "failed"
    assert payload["error_type"] == "StorageError"
    assert payload["output_bytes"] == 0
    assert processor.calls == []


def test_upload_failure_is_storage_error(tmp_path):
    storage = FakeStorage(fail_upload=True)
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path, storage=storage)
    seed_subtask(redis_client, storage, "sid1")

    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "failed"
    assert payload["error_type"] == "StorageError"
    assert payload["output_bytes"] == 0


def test_runtime_error_is_internal_error_and_next_task_processes(tmp_path):
    broken_processor = make_fake_processor(outcome=RuntimeError("boom"))
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path, processor=broken_processor)
    seed_subtask(redis_client, storage, "sid1")

    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "failed"
    assert payload["error_type"] == "InternalError"

    ok_processor = make_fake_processor()
    worker.processor = ok_processor
    seed_subtask(redis_client, storage, "sid2")

    payload2 = worker.handle_subtask("sid2")
    assert payload2["status"] == "completed"


def test_invalid_params_json_falls_back_to_empty_dict(tmp_path):
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path)
    seed_subtask(redis_client, storage, "sid1", raw_params="not-json")

    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "completed"
    assert processor.calls[-1]["params"] == {}


def test_terminal_status_is_skipped(tmp_path):
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path)
    seed_subtask(redis_client, storage, "sid1")
    redis_client.hset("subtask:sid1", "status", "completed")

    result = worker.handle_subtask("sid1")

    assert result is None
    assert processor.calls == []
    assert reporter.calls == []


def test_reporter_failure_parks_payload_and_still_srems_inflight(tmp_path):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings(tmp_path, COORDINATOR_URL="http://coord")
    reporter = Reporter(settings, redis_client, delays=(0, 0), sleep=lambda s: None)
    storage = FakeStorage()
    processor = make_fake_processor()
    worker = Worker(
        settings, redis_client=redis_client, storage=storage, processor=processor,
        reporter=reporter, heartbeat_enabled=False, ffmpeg_info=("test", "none"),
    )
    seed_subtask(redis_client, storage, "sid1")

    with responses.RequestsMock() as rsps:
        for _ in range(3):
            rsps.add(responses.POST, "http://coord/subtasks/report", body=requests.ConnectionError("boom"))
        payload = worker.handle_subtask("sid1")

    assert payload["status"] == "completed"
    assert redis_client.llen(PENDING_KEY) == 1
    assert bool(redis_client.sismember("worker:w1:inflight", "sid1")) is False


def test_lazy_processor_import_does_not_crash_construction(tmp_path):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    settings = make_settings(tmp_path)
    storage = FakeStorage()
    worker = Worker(settings, redis_client=redis_client, storage=storage, reporter=StubReporter(),
                     heartbeat_enabled=False)

    seed_subtask(redis_client, storage, "sid1")
    payload = worker.handle_subtask("sid1")

    assert payload["status"] == "failed"
    assert payload["error_type"] == "InternalError"


def test_start_with_concurrency_two_processes_in_parallel(tmp_path):
    processor = make_fake_processor(outcome=lambda kwargs: time.sleep(0.3))
    worker, redis_client, storage, processor, reporter = make_worker(
        tmp_path, processor=processor, WORKER_CONCURRENCY="2",
    )
    seed_subtask(redis_client, storage, "sid1")
    seed_subtask(redis_client, storage, "sid2")

    start = time.monotonic()
    worker.start()
    deadline = start + 2.0
    while len(reporter.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(timeout=2)

    assert len(reporter.calls) == 2
    elapsed = max(t for t, _ in reporter.calls) - start
    assert elapsed < 0.55


def test_stop_lets_current_task_finish_and_report(tmp_path):
    processor = make_fake_processor(outcome=lambda kwargs: time.sleep(0.3))
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path, processor=processor)
    seed_subtask(redis_client, storage, "sid1")

    worker.start()
    time.sleep(0.05)
    worker.stop(timeout=2)

    assert len(reporter.calls) == 1
    assert reporter.calls[0][1]["status"] == "completed"


def test_heartbeat_thread_starts_with_start(tmp_path):
    worker, redis_client, storage, processor, reporter = make_worker(tmp_path)
    worker.heartbeat_enabled = True
    worker.start()
    time.sleep(0.05)
    assert worker.heartbeat is not None
    assert worker.heartbeat.is_alive()
    worker.stop(timeout=2)
    assert not worker.heartbeat.is_alive()


def test_start_continues_when_ensure_buckets_raises_storage_error(tmp_path):
    class FailingStorage(FakeStorage):
        def ensure_buckets(self):
            raise StorageError("boom")

    worker, redis_client, storage, processor, reporter = make_worker(tmp_path, storage=FailingStorage())
    worker.start()
    worker.stop(timeout=2)
