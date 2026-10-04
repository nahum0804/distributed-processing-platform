import re
from pathlib import Path

import pytest
import requests
import responses
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dashboard" / "app.py"
CASOS = ROOT / "dashboard" / "pages" / "1_Casos.py"
BASE = "http://localhost:8000"


def _app_source():
    src = APP.read_text(encoding="utf-8")
    # Replace (also indented) sleep/rerun statements with `pass` to avoid the refresh loop.
    return re.sub(r"^(\s*)(time\.sleep\(REFRESH\)|st\.rerun\(\))\s*$", r"\1pass", src, flags=re.M)


WORKERS = [
    {"worker_id": "w1", "host": "h1", "ip": "10.0.0.1", "alive": True,
     "cpu_percent": "40.0", "mem_percent": "20.0", "gpu": "RTX 3060", "nvenc_ok": "1",
     "active_subtasks": "2", "completed_count": "7", "failed_count": "1",
     "concurrency": "4", "queues": "q", "last_seen": "2026-01-01T10:00:00"},
    {"worker_id": "w2", "host": "h2", "alive": False},
]
STATS = {"queues": {"queue:extract_audio": 3, "queue:transcode_video": 0},
         "cases_by_status": {"completed": 5}, "workers_alive": 1,
         "workers_total": 2, "subtasks_active": 2}
HW = [{"worker_id": "w1", "host": "h1", "ip": "10.0.0.1", "cpu_percent": 40.0,
       "mem_percent": 20.0, "gpu": "RTX 3060", "nvenc_ok": "1", "gpu_percent": 55.0,
       "active_subtasks": 2, "last_seen": "x"}]
CASES = [{"case_id": "c1", "status": "completed", "priority": "normal",
          "total_subtasks": "2", "pending_subtasks": "0",
          "created_at": "2026-01-01T00:00:00", "finished_at": "2026-01-01T00:01:00"}]
SUB = {"subtask_id": "s1", "file_path": "videos/a.mp4", "status": "completed",
       "worker_id": "w1", "host": "h1", "processing_s": 2.0, "outputs": ["o.mp4"],
       "metadata": {}}
REPORT = {"case_id": "c1", "status": "completed", "priority": "normal", "retries": 0,
          "summary": "2 ok", "totals": {"total": 2, "completed": 2, "failed": 0, "pending": 0},
          "failure_breakdown": {}, "avg_processing_s_by_operation": {"transcode_video": 2.0},
          "avg_processing_s_by_host": {"h1": 2.0},
          "subtasks_by_operation": {"transcode_video": [SUB]}}


def _mock_ok():
    responses.add(responses.GET, f"{BASE}/workers", json=WORKERS)
    responses.add(responses.GET, f"{BASE}/stats", json=STATS)
    responses.add(responses.GET, f"{BASE}/hardware", json=HW)


def _metrics(at):
    return {m.label: m.value for m in at.metric}


@responses.activate
def test_main_page_metrics():
    _mock_ok()
    at = AppTest.from_string(_app_source(), default_timeout=30).run()
    assert not at.exception
    m = _metrics(at)
    assert m["🖥️ Nodos activos"] == "1"
    assert m["📊 CPU promedio"] == "40.0%"
    assert m["💾 RAM promedio"] == "20.0%"
    assert m["✅ Completadas"] == "7"
    assert m["queue:extract_audio"] == "3"
    assert m["✅ completed"] == "5"


@responses.activate
def test_main_page_coordinator_down():
    for path in ("workers", "stats", "hardware"):
        responses.add(responses.GET, f"{BASE}/{path}",
                      body=requests.exceptions.ConnectionError("down"))
    at = AppTest.from_string(_app_source(), default_timeout=30).run()
    assert not at.exception
    assert any("No se puede conectar" in e.value for e in at.error)


@responses.activate
def test_main_page_redis_down_503():
    responses.add(responses.GET, f"{BASE}/workers", status=503, json={"detail": "Redis caído"})
    responses.add(responses.GET, f"{BASE}/stats", status=503, json={"detail": "Redis caído"})
    responses.add(responses.GET, f"{BASE}/hardware", json=[])
    at = AppTest.from_string(_app_source(), default_timeout=30).run()
    assert not at.exception
    assert any("Redis no disponible" in e.value for e in at.error)


@responses.activate
def test_casos_page():
    responses.add(responses.GET, f"{BASE}/cases", json=CASES)
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=REPORT)
    at = AppTest.from_file(str(CASOS), default_timeout=30).run()
    assert not at.exception
    m = _metrics(at)
    assert m["📋 Total"] == "2"
    assert m["✅ Completadas"] == "2"
    assert m["❌ Fallidas"] == "0"


@responses.activate
def test_casos_page_coordinator_down():
    responses.add(responses.GET, f"{BASE}/cases",
                  body=requests.exceptions.ConnectionError("down"))
    at = AppTest.from_file(str(CASOS), default_timeout=30).run()
    assert not at.exception
    assert any("No se pudo conectar" in e.value for e in at.error)


@responses.activate
def test_casos_page_empty():
    responses.add(responses.GET, f"{BASE}/cases", json=[])
    at = AppTest.from_file(str(CASOS), default_timeout=30).run()
    assert not at.exception
    assert any("No hay casos" in i.value for i in at.info)


@pytest.mark.parametrize("path", [APP, CASOS])
def test_no_deprecated_use_container_width(path):
    assert "use_container_width" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- Cola SQLite (server.py)

COLA = ROOT / "dashboard" / "pages" / "2_Cola_SQLite.py"
Q_STATUS = {"pending": 10, "processing": 2, "completed": 5, "failed": 1, "total": 18}
Q_WORKERS = [
    {"worker_id": "laptop-a", "processing": 1, "completed": 3, "failed": 0, "total_time_sec": 9.0,
     "avg_time_sec": 3.0, "last_activity": "2026-10-03 18:32:00"},
    {"worker_id": "laptop-b", "processing": 1, "completed": 2, "failed": 1, "total_time_sec": 4.0,
     "avg_time_sec": 2.0, "last_activity": "2026-10-03 18:32:05"},
]
Q_TASK = {"id": 7, "filename": "mp4/x.mp4", "file_type": "mp4", "status": "processing", "worker_id": "laptop-a",
          "retry_count": 0, "created_at": "t", "updated_at": "t", "error_log": None, "execution_time_sec": None}


def _strip_loop(src: str) -> str:
    return re.sub(r"^(\s*)(time\.sleep\(REFRESH\)|st\.rerun\(\))\s*$", r"\1pass", src, flags=re.M)


def _mock_queue():
    responses.add(responses.GET, f"{BASE}/tasks/status", json=Q_STATUS)
    responses.add(responses.GET, f"{BASE}/tasks/workers", json=Q_WORKERS)
    responses.add(responses.GET, f"{BASE}/tasks", json=[
        Q_TASK, {**Q_TASK, "id": 8, "status": "completed", "execution_time_sec": 2.5},
        {**Q_TASK, "id": 9, "status": "failed", "error_log": "CorruptInputError: x"}])


@responses.activate
def test_cola_sqlite_page_shows_queue_and_workers():
    _mock_queue()
    at = AppTest.from_string(_strip_loop(COLA.read_text(encoding="utf-8")), default_timeout=30).run()

    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["⏳ Pendientes"] == "10" and metrics["✅ Completadas"] == "5"
    assert metrics["🖥️ Workers con actividad"] == "2"
    assert len(at.dataframe) >= 3


@responses.activate
def test_cola_sqlite_page_server_down():
    responses.add(responses.GET, f"{BASE}/tasks/status", body=requests.ConnectionError("down"))
    at = AppTest.from_string(_strip_loop(COLA.read_text(encoding="utf-8")), default_timeout=30).run()

    assert not at.exception
    assert any("No se puede leer la cola" in e.value for e in at.error)


@responses.activate
def test_main_page_switches_to_sqlite_queue_when_pointed_at_server_py(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "app.py").write_text(_app_source(), encoding="utf-8")
    (tmp_path / "pages" / "2_Cola_SQLite.py").write_text(_strip_loop(COLA.read_text(encoding="utf-8")),
                                                          encoding="utf-8")
    responses.add(responses.GET, f"{BASE}/workers", status=404, json={"detail": "Not Found"})
    responses.add(responses.GET, f"{BASE}/stats", status=404, json={"detail": "Not Found"})
    responses.add(responses.GET, f"{BASE}/hardware", status=404, json={"detail": "Not Found"})
    _mock_queue()

    at = AppTest.from_file(str(tmp_path / "app.py"), default_timeout=30).run()

    assert not at.exception
    assert any("Cola del Servidor Central" in m.value for m in at.markdown)
