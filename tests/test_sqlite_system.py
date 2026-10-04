"""Tests for the SQLite task-queue system (server.py, seed_tasks.py, worker.py)."""
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import seed_tasks
import server
import worker
from tests.fakes import make_fake_processor

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "tasks.db"))
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "dataset"
    for rel in ("mp3s/a.mp3", "mp3s/b.mp3", "mp4/mp4s/v1.mp4", "wav/sub/w1.wav", "otros/notas.txt"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
    return root


class ClientSession:
    """requests-like adapter so seed_tasks/worker talk to the in-process TestClient."""

    def __init__(self, client):
        self.client = client

    def post(self, url, json=None, timeout=None):
        return self.client.post("/" + url.split("://", 1)[1].split("/", 1)[1], json=json)

    def get(self, url, params=None, timeout=None):
        return self.client.get("/" + url.split("://", 1)[1].split("/", 1)[1], params=params)


def status(client):
    return client.get("/tasks/status").json()


# ---------------------------------------------------------------- server.py

def test_db_path_is_absolute_next_to_server_by_default():
    if os.getenv("TASKS_DB"):
        pytest.skip("TASKS_DB overrides the default")
    assert os.path.isabs(server.DB_PATH)
    assert Path(server.DB_PATH).parent == REPO_ROOT


def test_register_reports_created_and_normalizes_windows_paths(client):
    r1 = client.post("/tasks/register", json={"filename": "mp3s\\a.mp3"})
    r2 = client.post("/tasks/register", json={"filename": "mp3s/a.mp3"})

    assert r1.status_code == 200 and r1.json()["created"] is True
    assert r1.json()["filename"] == "mp3s/a.mp3"
    assert r2.json()["created"] is False
    assert status(client)["total"] == 1


def test_register_rejects_unsupported_format(client):
    assert client.post("/tasks/register", json={"filename": "doc.txt"}).status_code == 400


def test_next_assigns_pending_then_returns_null(client):
    client.post("/tasks/register", json={"filename": "a.mp3"})

    task = client.get("/tasks/next", params={"worker_id": "w1"}).json()
    assert task["status"] == "processing" and task["worker_id"] == "w1"
    assert client.get("/tasks/next", params={"worker_id": "w2"}).json() is None


def test_concurrent_workers_never_get_the_same_task(client):
    for i in range(30):
        client.post("/tasks/register", json={"filename": f"f{i}.mp3"})
    taken: list[int] = []
    lock = threading.Lock()

    def consume(wid):
        while True:
            task = server.get_next_task(worker_id=wid)
            if task is None:
                return
            with lock:
                taken.append(task["id"])

    threads = [threading.Thread(target=consume, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(taken) == list(range(1, 31))
    assert status(client)["processing"] == 30


def test_report_completed_and_failed_with_retries(client):
    client.post("/tasks/register", json={"filename": "ok.mp3"})
    client.post("/tasks/register", json={"filename": "bad.mp3"})
    ok = client.get("/tasks/next", params={"worker_id": "w"}).json()
    client.post(f"/tasks/{ok['id']}/report", json={"worker_id": "w", "status": "completed"})

    for attempt in range(server.MAX_RETRIES + 1):
        bad = client.get("/tasks/next", params={"worker_id": "w"}).json()
        resp = client.post(f"/tasks/{bad['id']}/report",
                           json={"worker_id": "w", "status": "failed", "error_message": "boom"}).json()
    assert resp["status"] == "failed"
    assert status(client) == {"pending": 0, "processing": 0, "completed": 1, "failed": 1, "total": 2}


def test_report_unknown_task_and_bad_status(client):
    assert client.post("/tasks/999/report", json={"worker_id": "w", "status": "completed"}).status_code == 404
    client.post("/tasks/register", json={"filename": "a.mp3"})
    assert client.post("/tasks/1/report", json={"worker_id": "w", "status": "raro"}).status_code == 400


def test_stale_processing_task_is_requeued(client, monkeypatch):
    client.post("/tasks/register", json={"filename": "a.mp3"})
    client.get("/tasks/next", params={"worker_id": "dead"})
    monkeypatch.setattr(server, "TASK_TIMEOUT_SECONDS", -1)

    task = client.get("/tasks/next", params={"worker_id": "alive"}).json()

    assert task["worker_id"] == "alive" and task["retry_count"] == 1


def test_reset_empties_queue_and_restarts_ids(client):
    for name in ("a.mp3", "b.mp3"):
        client.post("/tasks/register", json={"filename": name})

    resp = client.post("/tasks/reset", json={}).json()

    assert resp == {"status": "ok", "deleted": 2, "files_registered": 0}
    assert status(client)["total"] == 0
    assert client.post("/tasks/register", json={"filename": "c.mp3"}).json()["id"] == 1


def test_reset_with_dataset_rescans_and_scan_reports_existing(client, dataset):
    first = client.post("/tasks/scan_dataset", json={"dataset_path": str(dataset)}).json()
    again = client.post("/tasks/scan_dataset", json={"dataset_path": str(dataset)}).json()
    reset = client.post("/tasks/reset", json={"dataset_path": str(dataset)}).json()

    assert first["files_registered"] == 4 and again["already_registered"] == 4
    assert reset["deleted"] == 4 and reset["files_registered"] == 4
    assert status(client)["pending"] == 4
    names = {client.get("/tasks/next", params={"worker_id": "w"}).json()["filename"] for _ in range(4)}
    assert names == {"mp3s/a.mp3", "mp3s/b.mp3", "mp4/mp4s/v1.mp4", "wav/sub/w1.wav"}


def test_reset_unknown_dataset_path_is_404(client):
    assert client.post("/tasks/reset", json={"dataset_path": "/no/existe"}).status_code == 404


# ---------------------------------------------------------------- seed_tasks.py

def test_reseeding_without_reset_leaves_nothing_pending_and_warns(client, dataset, caplog):
    session = ClientSession(client)
    first = seed_tasks.scan_and_seed_dataset("http://srv", str(dataset), session=session)
    for _ in range(4):
        t = client.get("/tasks/next", params={"worker_id": "w"}).json()
        client.post(f"/tasks/{t['id']}/report", json={"worker_id": "w", "status": "completed"})

    second = seed_tasks.scan_and_seed_dataset("http://srv", str(dataset), session=session)

    assert first == {"found": 4, "new": 4, "existing": 0, "errors": 0}
    assert second == {"found": 4, "new": 0, "existing": 4, "errors": 0}
    assert status(client)["pending"] == 0
    assert "--reset" in caplog.text


def test_seed_with_reset_puts_everything_back_to_pending(client, dataset):
    session = ClientSession(client)
    seed_tasks.scan_and_seed_dataset("http://srv", str(dataset), session=session)
    for _ in range(4):
        t = client.get("/tasks/next", params={"worker_id": "w"}).json()
        client.post(f"/tasks/{t['id']}/report", json={"worker_id": "w", "status": "completed"})

    result = seed_tasks.scan_and_seed_dataset("http://srv", str(dataset), reset=True, session=session)

    assert result == {"found": 4, "new": 4, "existing": 0, "errors": 0}
    assert status(client) == {"pending": 4, "processing": 0, "completed": 0, "failed": 0, "total": 4}


def test_find_media_uses_forward_slashes(dataset):
    names = [f["filename"] for f in seed_tasks.find_media(str(dataset))]
    assert names == ["mp3s/a.mp3", "mp3s/b.mp3", "mp4/mp4s/v1.mp4", "wav/sub/w1.wav"]


def test_seed_missing_dataset_dir_reports_error(client):
    assert seed_tasks.scan_and_seed_dataset("http://srv", "/no/existe", session=ClientSession(client))["errors"] == 1


# ---------------------------------------------------------------- worker.py

def test_resolve_path_accepts_backslashes_and_falls_back_to_basename(dataset):
    index = worker.build_index(str(dataset))

    assert worker.resolve_path("mp3s\\a.mp3", str(dataset)) == str(dataset / "mp3s" / "a.mp3")
    assert worker.resolve_path("otra/estructura/v1.mp4", str(dataset), index) == str(dataset / "mp4" / "mp4s" / "v1.mp4")
    assert worker.resolve_path("no/existe.mp3", str(dataset), index) is None


@pytest.mark.parametrize("file_type,filename,expected_op", [
    ("mp4", "mp4/mp4s/v1.mp4", "transcode_video"),
    ("mp3", "mp3s/a.mp3", "convert_audio"),
    ("wav", "wav/sub/w1.wav", "convert_audio"),
])
def test_process_uses_real_processor_with_operation_by_type(dataset, tmp_path, file_type, filename, expected_op):
    proc = make_fake_processor()

    ok, err, _, outputs = worker.process_media_file(
        7, filename, file_type, str(dataset), output_dir=str(tmp_path / "out"), processor=proc)

    assert ok and err == "" and outputs
    assert proc.calls[0]["operation"] == expected_op
    assert proc.calls[0]["out_dir"] == os.path.join(str(tmp_path / "out"), "tarea_7")


def test_process_missing_file_fails_instead_of_faking_success(dataset, tmp_path):
    proc = make_fake_processor()

    ok, err, _, _ = worker.process_media_file(1, "mp3s/no.mp3", "mp3", str(dataset),
                                              output_dir=str(tmp_path), processor=proc)

    assert not ok and "no encontrado" in err.lower()
    assert proc.calls == []


def test_process_reports_processing_error_type(dataset, tmp_path):
    proc = make_fake_processor(outcome="CorruptInputError")

    ok, err, _, _ = worker.process_media_file(1, "mp3s/a.mp3", "mp3", str(dataset),
                                              output_dir=str(tmp_path), processor=proc)

    assert not ok and err.startswith("CorruptInputError")


def test_process_explicit_operation_overrides_type(dataset, tmp_path):
    proc = make_fake_processor()
    worker.process_media_file(1, "mp4/mp4s/v1.mp4", "mp4", str(dataset), output_dir=str(tmp_path),
                              operation="generate_thumbnail", processor=proc)
    assert proc.calls[0]["operation"] == "generate_thumbnail"


def test_simulate_mode_does_not_call_processor(dataset, monkeypatch):
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    proc = make_fake_processor()

    ok, _, _, outputs = worker.process_media_file(1, "mp3s/a.mp3", "mp3", str(dataset), simulate=True, processor=proc)

    assert ok and outputs == [] and proc.calls == []


def test_check_requirements(dataset, monkeypatch):
    args = worker.parse_args(["--dataset-dir", str(dataset)])
    monkeypatch.setattr(worker.shutil, "which", lambda b: None)
    assert "FFmpeg" in worker.check_requirements(args)
    monkeypatch.setattr(worker.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert worker.check_requirements(args) is None
    assert "no existe" in worker.check_requirements(worker.parse_args(["--dataset-dir", "/no/existe"]))
    assert worker.check_requirements(worker.parse_args(["--dataset-dir", "/no/existe", "--simulate"])) is None


def test_worker_loop_end_to_end_with_server_and_seeder(client, dataset, tmp_path, monkeypatch):
    """Seed with --reset, run the real worker loop against the real server until the queue is empty."""
    import src.workers.multimedia_processor as mp
    fake = make_fake_processor()
    monkeypatch.setattr(mp, "process", fake.process)
    monkeypatch.setattr(worker.shutil, "which", lambda b: f"/usr/bin/{b}")
    session = ClientSession(client)
    monkeypatch.setattr(worker.requests, "get", session.get)
    monkeypatch.setattr(worker.requests, "post", session.post)

    def stop_when_idle(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop_when_idle)
    seed_tasks.scan_and_seed_dataset("http://srv", str(dataset), reset=True, session=session)

    with pytest.raises(SystemExit) as exit_info:
        worker.run_worker(["--server", "http://srv", "--worker-id", "w-e2e", "--dataset-dir", str(dataset),
                           "--output-dir", str(tmp_path / "out")])

    assert exit_info.value.code == 0
    assert status(client) == {"pending": 0, "processing": 0, "completed": 4, "failed": 0, "total": 4}
    assert sorted(c["operation"] for c in fake.calls) == [
        "convert_audio", "convert_audio", "convert_audio", "transcode_video"]


# ---------------------------------------------------------------- dashboard endpoints

def _complete(client, worker_id, seconds):
    task = client.get("/tasks/next", params={"worker_id": worker_id}).json()
    client.post(f"/tasks/{task['id']}/report",
                json={"worker_id": worker_id, "status": "completed", "execution_time_sec": seconds})
    return task


def test_list_tasks_filters_and_keeps_execution_time(client):
    for name in ("a.mp3", "b.mp3", "c.mp4"):
        client.post("/tasks/register", json={"filename": name})
    _complete(client, "w1", 1.5)
    client.get("/tasks/next", params={"worker_id": "w2"})

    everything = client.get("/tasks").json()
    done = client.get("/tasks", params={"status": "completed"}).json()
    running = client.get("/tasks", params={"status": "processing"}).json()

    assert len(everything) == 3
    assert [(t["filename"], t["worker_id"], t["execution_time_sec"]) for t in done] == [("a.mp3", "w1", 1.5)]
    assert [(t["filename"], t["worker_id"]) for t in running] == [("b.mp3", "w2")]
    assert len(client.get("/tasks", params={"limit": 1}).json()) == 1


def test_workers_summary(client):
    for i in range(4):
        client.post("/tasks/register", json={"filename": f"f{i}.mp3"})
    _complete(client, "laptop-a", 2.0)
    _complete(client, "laptop-a", 4.0)
    _complete(client, "laptop-b", 3.0)
    client.get("/tasks/next", params={"worker_id": "laptop-b"})

    summary = {w["worker_id"]: w for w in client.get("/tasks/workers").json()}

    assert summary["laptop-a"]["completed"] == 2 and summary["laptop-a"]["processing"] == 0
    assert summary["laptop-a"]["total_time_sec"] == 6.0 and summary["laptop-a"]["avg_time_sec"] == 3.0
    assert summary["laptop-b"]["completed"] == 1 and summary["laptop-b"]["processing"] == 1
    assert summary["laptop-b"]["last_activity"]


def test_old_database_without_execution_time_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL UNIQUE,
                        file_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', worker_id TEXT,
                        retry_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, error_log TEXT)""")
        conn.execute("INSERT INTO tasks (filename, file_type) VALUES ('viejo.mp3', 'mp3')")
    monkeypatch.setattr(server, "DB_PATH", str(db))

    with TestClient(server.app) as c:
        _complete(c, "w", 0.5)
        assert c.get("/tasks", params={"status": "completed"}).json()[0]["execution_time_sec"] == 0.5
