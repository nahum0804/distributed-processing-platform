import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import responses
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
BASE = "http://localhost:8000"
FMT = "%Y-%m-%d %H:%M:%S"


def _ts(secs_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).strftime(FMT)


STATUS = {"pending": 3, "processing": 1, "completed": 306, "failed": 2, "total": 312}
WORKERS = [
    {"worker_id": "lap-a", "processing": 1, "completed": 200, "failed": 0, "total_time_sec": 400.0,
     "avg_time_sec": 2.0, "last_activity": _ts(500)},
    {"worker_id": "lap-b", "processing": 0, "completed": 106, "failed": 2, "total_time_sec": 300.0,
     "avg_time_sec": 2.8, "last_activity": _ts(5)},
    {"worker_id": "lap-c", "processing": 0, "completed": 10, "failed": 0, "total_time_sec": 20.0,
     "avg_time_sec": 2.0, "last_activity": _ts(900)},
]


def _task(i, status, worker="lap-a", ftype="video", name=None, err=None):
    return {"id": i, "filename": name or f"f{i}.mp4", "file_type": ftype, "status": status,
            "worker_id": worker, "retry_count": 0, "created_at": _ts(100), "updated_at": _ts(50),
            "error_log": err, "execution_time_sec": 1.5}


TASKS = [_task(1, "completed"), _task(2, "failed", "lap-b", err="boom"),
         _task(3, "processing"), _task(4, "pending", None, "audio", "voz.wav")]


def _mock_sqlite(workers_status=200, tasks_status=200):
    responses.add(responses.GET, f"{BASE}/tasks/status", json=STATUS)
    responses.add(responses.GET, f"{BASE}/tasks/workers", json=WORKERS if workers_status == 200 else {}, status=workers_status)
    responses.add(responses.GET, f"{BASE}/tasks", json=TASKS if tasks_status == 200 else {}, status=tasks_status)


def _run():
    return AppTest.from_file(str(DASH / "app.py"), default_timeout=30).run()


def _kpis(at):
    out = {}
    for m in at.markdown:
        for lab, val in re.findall(r'kpi-label">([^<]+)</div><div class="kpi-value"[^>]*>([^<]+)<', m.value):
            out[lab] = val
    return out


def _text(at):
    return " ".join(m.value for m in at.markdown)


@responses.activate
def test_sqlite_navigation_and_resumen():
    _mock_sqlite()
    at = _run()
    assert not at.exception
    k = _kpis(at)
    assert k["Total"] == "312" and k["Completadas"] == "306" and k["Fallidas"] == "2"
    assert k["Workers activos"] == "2"  # lap-a (en proceso) + lap-b (reciente)


def _known_pages(at):
    try:
        at.switch_page("app.py")
    except ValueError as e:
        m = re.search(r"Known pages: \[(.*?)\]", str(e))
        return [Path(x.strip().strip("'")).stem for x in m.group(1).split(",")]
    raise AssertionError("switch_page no fallo")


@responses.activate
def test_sqlite_sidebar_pages_exact():
    _mock_sqlite()
    pages = [p.lower() for p in _known_pages(_run())]
    assert pages == ["resumen", "workers", "tareas"]


@responses.activate
def test_workers_page_states():
    _mock_sqlite()
    at = _run().switch_page("views/workers.py").run()
    assert not at.exception
    k = _kpis(at)
    assert k["Workers activos"] == "2"
    assert len(at.dataframe) >= 1


@responses.activate
def test_tareas_page_and_filters():
    _mock_sqlite()
    at = _run().switch_page("views/tareas.py").run()
    assert not at.exception
    at.multiselect(key="t_status").set_value(["failed"]).run()
    assert not at.exception
    at.text_input(key="t_text").set_value("voz").run()
    assert not at.exception


def test_filter_tasks_helper():
    import sys
    sys.path.insert(0, str(DASH))
    import pandas as pd
    from views.tareas import filter_tasks
    df = pd.DataFrame(TASKS)
    assert len(filter_tasks(df, ["failed"], [], [], "")) == 1
    assert len(filter_tasks(df, [], ["lap-b"], [], "")) == 1
    assert len(filter_tasks(df, [], [], ["audio"], "")) == 1
    assert list(filter_tasks(df, [], [], [], "VOZ")["id"]) == [4]


def test_time_helpers():
    import sys
    sys.path.insert(0, str(DASH))
    import ui
    now = datetime.now(timezone.utc)
    local = ui.to_local(now.strftime(FMT))
    assert abs((local - now.astimezone().replace(tzinfo=None)).total_seconds()) < 2
    assert ui.to_local(None) is None and ui.to_local("basura") is None
    assert ui.is_worker_active({"processing": 1, "last_activity": _ts(9999)})
    assert ui.is_worker_active({"processing": 0, "last_activity": _ts(10)})
    assert not ui.is_worker_active({"processing": 0, "last_activity": _ts(120)})
    assert not ui.is_worker_active({"processing": 0, "last_activity": None})


@responses.activate
def test_endpoint_error_is_visible_not_false_empty():
    _mock_sqlite(workers_status=404)
    at = _run().switch_page("views/workers.py").run()
    assert not at.exception
    assert any("/tasks/workers" in w.value for w in at.warning)
    assert "ningun worker" not in _text(at).lower() and not any("ningun worker" in i.value.lower() for i in at.info)
    at = _run().switch_page("views/resumen.py").run()
    assert any("/tasks/workers" in w.value for w in at.warning)


@responses.activate
def test_tareas_error_visible():
    _mock_sqlite(tasks_status=500)
    at = _run().switch_page("views/tareas.py").run()
    assert any("/tasks" in w.value for w in at.warning)


WORKERS_R = [{"worker_id": "w1", "host": "h1", "ip": "10.0.0.1", "alive": True, "cpu_percent": "40.0",
              "mem_percent": "20.0", "gpu": "none", "active_subtasks": "2", "completed_count": "7",
              "failed_count": "1", "concurrency": "4", "last_seen": "x"},
             {"worker_id": "w2", "host": "h2", "alive": False}]
STATS_R = {"queues": {"queue:a": 3}, "cases_by_status": {"completed": 5}}
CASES = [{"case_id": "c1", "status": "completed", "priority": "normal", "total_subtasks": "2",
          "pending_subtasks": "0", "created_at": "x", "finished_at": "y"}]
REPORT = {"case_id": "c1", "status": "completed", "priority": "normal", "retries": 0, "summary": "ok",
          "totals": {"total": 2, "completed": 2, "failed": 0, "pending": 0}, "failure_breakdown": {},
          "avg_processing_s_by_operation": {"op": 2.0}, "avg_processing_s_by_host": {"h1": 2.0},
          "subtasks_by_operation": {"op": [{"subtask_id": "s1", "status": "completed"}]}}


def _mock_redis():
    responses.add(responses.GET, f"{BASE}/tasks/status", status=404, json={})
    responses.add(responses.GET, f"{BASE}/stats", json=STATS_R)
    responses.add(responses.GET, f"{BASE}/workers", json=WORKERS_R)
    responses.add(responses.GET, f"{BASE}/hardware", json=[])
    responses.add(responses.GET, f"{BASE}/cases", json=CASES)
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=REPORT)


@responses.activate
def test_redis_backend_pages():
    _mock_redis()
    at = _run()
    assert not at.exception
    assert _kpis(at)["Nodos activos"] == "1"
    at = at.switch_page("views/casos.py").run()
    assert not at.exception
    assert _kpis(at)["Total"] == "2"
    assert [p.lower() for p in _known_pages(at)] == ["monitor", "casos"]


@responses.activate
def test_unreachable_shows_conexion():
    responses.add(responses.GET, f"{BASE}/tasks/status", body=requests.exceptions.ConnectionError("x"))
    responses.add(responses.GET, f"{BASE}/stats", status=404, json={})
    at = _run()
    assert not at.exception
    assert any("No se puede conectar" in e.value for e in at.error)
    assert any("uvicorn server:app" in c.value for c in at.code)


def test_static_no_emoji_and_no_use_container_width():
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕⏩-⏿️✅❌]")
    files = list(DASH.rglob("*.py"))
    assert files and not (DASH / "pages").exists()
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert not emoji.search(src), f
        assert "use_container_width" not in src, f
