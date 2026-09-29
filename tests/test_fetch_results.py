import json

import requests

import responses

from scripts import fetch_results
from tests.fakes import FakeStorage

BASE = "http://coordinator:8000"


def entry(sid, file_path, status, outputs=(), **extra):
    return {
        "subtask_id": sid, "file_path": file_path, "status": status, "host": "h1",
        "processing_s": 1.5, "outputs": list(outputs), "error_type": None, **extra,
    }


def make_report(case_id="c1"):
    return {
        "case_id": case_id, "status": "partially_completed", "summary": "2 ok; 1 fallida(s) (CorruptInputError)",
        "totals": {"total": 3, "completed": 2, "failed": 1, "pending": 0},
        "subtasks_by_operation": {
            "transcode_video": [
                entry("s1", "d/a.mp4", "completed", [f"results/{case_id}/s1/a.mp4"]),
                entry("s2", "d/b.mp4", "failed", error_type="CorruptInputError"),
            ],
            "extract_audio": [entry("s3", "d/c.mp4", "completed", [f"results/{case_id}/s3/c.mp3"])],
        },
    }


def make_storage(case_id="c1"):
    st = FakeStorage()
    st.objects[("results", f"{case_id}/s1/a.mp4")] = b"A"
    st.objects[("results", f"{case_id}/s3/c.mp3")] = b"C"
    return st



@responses.activate
def test_fetch_case_downloads_grouped_and_writes_reports(tmp_path):
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=make_report())
    counts = fetch_results.fetch_case("c1", requests, make_storage(), tmp_path, BASE, False)

    assert counts == {"downloaded": 2, "failed": 1, "no_output": 0, "warnings": 0}
    assert (tmp_path / "c1/transcode_video/s1/a.mp4").read_bytes() == b"A"
    assert (tmp_path / "c1/extract_audio/s3/c.mp3").read_bytes() == b"C"
    data = json.loads((tmp_path / "c1/reporte.json").read_text(encoding="utf-8"))
    assert data["case_id"] == "c1"
    md = (tmp_path / "c1/reporte.md").read_text(encoding="utf-8")
    assert "partially_completed" in md
    assert "CorruptInputError" in md
    assert "transcode_video/s1/a.mp4" in md
    assert "| d/a.mp4 | transcode_video | completed | h1 | 1.50 |" in md


@responses.activate
def test_missing_object_is_warning_and_continues(tmp_path, capsys):
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=make_report())
    storage = make_storage()
    del storage.objects[("results", "c1/s1/a.mp4")]

    counts = fetch_results.fetch_case("c1", requests, storage, tmp_path, BASE, False)

    assert counts["downloaded"] == 1
    assert counts["warnings"] == 1
    assert "Aviso" in capsys.readouterr().out
    assert (tmp_path / "c1/reporte.json").exists()


@responses.activate
def test_only_completed_skips_failed(tmp_path):
    report = make_report()
    report["subtasks_by_operation"]["transcode_video"][1]["outputs"] = ["results/c1/s2/partial.mp4"]
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=report)
    storage = make_storage()
    storage.objects[("results", "c1/s2/partial.mp4")] = b"P"

    counts = fetch_results.fetch_case("c1", requests, storage, tmp_path, BASE, True)

    assert counts["downloaded"] == 2
    assert counts["failed"] == 1
    assert not (tmp_path / "c1/transcode_video/s2").exists()


@responses.activate
def test_completed_without_outputs_counted(tmp_path):
    report = make_report()
    report["subtasks_by_operation"]["extract_audio"][0]["outputs"] = []
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=report)

    counts = fetch_results.fetch_case("c1", requests, make_storage(), tmp_path, BASE, False)

    assert counts["no_output"] == 1
    assert counts["downloaded"] == 1


@responses.activate
def test_main_404_exits_1_but_processes_other_cases(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COORDINATOR_URL", BASE)
    responses.add(responses.GET, f"{BASE}/cases/nope/report", status=404, json={"detail": "no"})
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=make_report())

    rc = fetch_results.main(["nope", "c1", "--out", str(tmp_path)], storage=make_storage())

    out = capsys.readouterr().out
    assert rc == 1
    assert "nope: error" in out
    assert "c1: 2 archivos descargados, 1 sub-tareas fallidas, 0 sin salida" in out
    assert (tmp_path / "c1/reporte.md").exists()


@responses.activate
def test_main_success_exit_0(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_URL", BASE)
    responses.add(responses.GET, f"{BASE}/cases/c1/report", json=make_report())

    assert fetch_results.main(["c1", "--out", str(tmp_path)], storage=make_storage()) == 0
