import json
import re
from argparse import Namespace
from pathlib import Path

import fakeredis
import pytest
import responses

from scripts import run_load as rl
from src.workers.config import Settings

URL = "http://coord.test"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class DownRedis:
    def ping(self):
        raise ConnectionError("down")


class Obj:
    def __init__(self, size):
        self.size = size


class StubClient:
    def __init__(self, existing):
        self.existing = existing

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.existing:
            raise RuntimeError("NoSuchKey")
        return Obj(self.existing[(bucket, key)])


class StubStorage:
    def __init__(self, existing=None):
        self.client = StubClient(existing or {})
        self.uploaded = []
        self.ensured = False

    def ensure_buckets(self):
        self.ensured = True

    def upload_file(self, bucket, key, path):
        self.uploaded.append((bucket, key, Path(path)))


def make_dataset(tmp_path, n_cases=3):
    files = [{"file": f"f{i}.mp4", "key": f"ev/f{i}.mp4", "bytes": 10 + i, "type": "video", "format": "mp4",
              "size_class": "light", "event": "ev", "session": "s1", "user": "u1", "batch": "lote_v01",
              "resolution": "320x240"} for i in range(4)]
    cases = [{"name": f"caso{i}", "kind": "homogeneous" if i % 2 == 0 else "heterogeneous", "criterion": "batch",
              "subtasks": [{"task_type": "transcode_video", "file_path": "ev/f0.mp4", "params": None},
                           {"task_type": "extract_metadata", "file_path": "ev/f1.mp4", "params": None}]}
             for i in range(n_cases)]
    (tmp_path / "manifest.json").write_text(json.dumps({"files": files}))
    (tmp_path / "cases.json").write_text(json.dumps({"cases": cases}))
    return tmp_path


def args_for(tmp_path, **kw):
    base = dict(dataset=tmp_path, upload=False, concurrency=2, limit=None, kinds="homogeneous,heterogeneous",
                timeout=100.0, poll=5.0, out=tmp_path / "out", high_fraction=0.1, seed=42)
    base.update(kw)
    return Namespace(**base)


def report_for(cid, seconds=30):
    return {
        "case_id": cid, "status": "partially_completed",
        "created_at": "2026-01-01T10:00:00+00:00", "finished_at": f"2026-01-01T10:00:{seconds:02d}+00:00",
        "failure_breakdown": {"FFmpegError": 1},
        "subtasks_by_operation": {
            "transcode_video": [{"status": "completed", "host": "nodo-a", "processing_s": 4.0}],
            "extract_metadata": [{"status": "failed", "host": "nodo-b", "processing_s": 2.0,
                                  "error_type": "FFmpegError"}],
        },
    }


def mock_api(rsps, finish_after=2, never=False, stats=None, bodies=None, final_status="partially_completed",
             durations=None):
    counts = {}

    def post(request):
        n = len(counts) + 1
        counts[f"id{n}"] = 0
        if bodies is not None:
            bodies.append(json.loads(request.body))
        return 200, {}, json.dumps({"case_id": f"id{n}"})

    def get_case(request):
        cid = request.url.rsplit("/", 1)[1]
        counts[cid] += 1
        done = not never and counts[cid] >= finish_after
        return 200, {}, json.dumps({"case": {"status": final_status if done else "processing"},
                                    "subtasks": []})

    rsps.add_callback(responses.POST, f"{URL}/cases", callback=post, content_type="application/json")
    rsps.add_callback(responses.GET, re.compile(rf"{URL}/cases/id\d+$"), callback=get_case,
                      content_type="application/json")
    rsps.add_callback(responses.GET, re.compile(rf"{URL}/cases/id\d+/report$"),
                      callback=lambda r: (200, {}, json.dumps(report_for(
                          r.url.split("/")[-2], (durations or {}).get(r.url.split("/")[-2], 30)))),
                      content_type="application/json")
    if stats is not None:
        rsps.add(responses.GET, f"{URL}/stats", json=stats)
    return counts


def settings():
    return Settings(coordinator_url=URL, redis_host="unreachable.invalid")


def test_upload_skips_existing(tmp_path):
    ds = make_dataset(tmp_path)
    manifest, _ = rl.load_dataset(ds)
    storage = StubStorage({("dataset", "ev/f0.mp4"): 10, ("dataset", "ev/f1.mp4"): 999})
    stats = rl.upload_dataset(storage, manifest, ds / "media", "dataset", workers=2)
    assert storage.ensured
    assert stats == {"uploaded": 3, "skipped": 1, "failed": 0}
    assert {k for _, k, _ in storage.uploaded} == {"ev/f1.mp4", "ev/f2.mp4", "ev/f3.mp4"}


def test_select_cases():
    doc = {"cases": [{"kind": "homogeneous", "name": "a"}, {"kind": "heterogeneous", "name": "b"},
                     {"kind": "homogeneous", "name": "c"}]}
    assert [c["name"] for c in rl.select_cases(doc, ["homogeneous"], None)] == ["a", "c"]
    assert len(rl.select_cases(doc, None, 2)) == 2


def test_sample_workers_reads_heartbeats():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.sadd("workers:registry", "w1", "w2")
    r.hset("worker:w1", mapping={"host": "nodo-a", "cpu_percent": "80.5", "mem_percent": "40",
                                 "active_subtasks": "2", "completed_count": "5", "failed_count": "1"})
    got = rl.sample_workers(r)
    assert list(got) == ["w1"]
    assert got["w1"]["cpu_percent"] == 80.5 and got["w1"]["active_subtasks"] == 2
    assert rl.sample_workers(None) == {}


def test_run_load_full(tmp_path):
    ds = make_dataset(tmp_path)
    r = fakeredis.FakeRedis(decode_responses=True)
    r.sadd("workers:registry", "w1", "w2")
    r.hset("worker:w1", mapping={"host": "nodo-a", "cpu_percent": "90", "active_subtasks": "3"})
    r.hset("worker:w2", mapping={"host": "nodo-b", "cpu_percent": "30", "active_subtasks": "1"})
    storage = StubStorage()
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        counts = mock_api(rsps)
        code, md = rl.run_load(args_for(ds, upload=True), settings(), storage=storage, redis_client=r,
                               sleep=clock.sleep, clock=clock, stamp="20260101_000000")
    assert code == 0 and len(counts) == 3
    assert len(storage.uploaded) == 4
    data = json.loads((ds / "out" / "carga_20260101_000000.json").read_text())
    m = data["metrics"]
    assert md.name == "carga_20260101_000000.md" and md.exists()
    assert m["cases"] == 3 and m["subtasks_total"] == 6 and m["subtasks_done"] == 6
    assert m["case_status_counts"] == {"partially_completed": 3}
    assert m["subtasks_per_host"] == {"nodo-a": 3, "nodo-b": 3}
    assert m["avg_processing_s_by_host"] == {"nodo-a": 4.0, "nodo-b": 2.0}
    assert m["avg_processing_s_by_operation"] == {"extract_metadata": 2.0, "transcode_video": 4.0}
    assert m["failure_breakdown"] == {"FFmpegError": 3}
    assert m["case_duration_avg_s"] == 30.0
    assert m["max_concurrent_active"] == 4
    assert m["peak_cpu_by_host"] == {"nodo-a": 90.0, "nodo-b": 30.0}
    assert m["throughput_subtasks_per_min"] == round(6 / (clock.now / 60), 2)
    assert len(data["series"]) >= 2
    text = md.read_text()
    assert "nodo-a" in text and "FFmpegError" in text and "Saturacion" in text


def test_assign_priorities_deterministic_and_exact_count():
    a = rl.assign_priorities(20, 0.25, 42)
    assert a == rl.assign_priorities(20, 0.25, 42)
    assert a.count("high") == 5 and a.count("normal") == 15
    assert a != rl.assign_priorities(20, 0.25, 7)


def test_assign_priorities_edges():
    assert rl.assign_priorities(10, 0.0, 1) == ["normal"] * 10
    assert rl.assign_priorities(4, 1.0, 1) == ["high"] * 4
    assert rl.assign_priorities(3, 0.1, 1).count("high") == 1
    assert rl.assign_priorities(0, 0.5, 1) == []


def test_build_payload_carries_metadata_and_priority():
    files = {"ev/f0.mp4": {"key": "ev/f0.mp4", "type": "video", "format": "mp4", "size_class": "light",
                           "event": "ev", "session": "s1", "user": "u1", "batch": "b1", "bytes": 5,
                           "resolution": "320x240", "problematic": False}}
    case = {"name": "caso0", "kind": "homogeneous", "criterion": "batch",
            "subtasks": [{"task_type": "transcode_video", "file_path": "ev/f0.mp4", "params": {"crf": 28}},
                         {"task_type": "extract_metadata", "file_path": "otro.mp4", "params": None}]}
    payload = rl.build_payload(case, "high", files)
    assert payload["priority"] == "high"
    assert payload["metadata"] == {"name": "caso0", "kind": "homogeneous", "criterion": "batch"}
    first, second = payload["subtasks"]
    assert first["metadata"] == {"event": "ev", "session": "s1", "user": "u1", "batch": "b1",
                                 "size_class": "light", "format": "mp4", "type": "video"}
    assert first["params"] == {"crf": 28}
    assert "metadata" not in second


def test_run_load_sends_priorities_and_metadata(tmp_path):
    ds = make_dataset(tmp_path, n_cases=4)
    clock = FakeClock()
    bodies = []
    with responses.RequestsMock() as rsps:
        mock_api(rsps, bodies=bodies, stats={"queues": {"transcode_video": 3}})
        # concurrency=1: the mock hands out case ids in POST order, which must match case order here.
        rl.run_load(args_for(ds, high_fraction=0.5, concurrency=1), settings(), redis_client=DownRedis(),
                    sleep=clock.sleep, clock=clock, stamp="p")
    assert sorted(b["priority"] for b in bodies) == ["high", "high", "normal", "normal"]
    assert all(b["metadata"]["kind"] in ("homogeneous", "heterogeneous") and b["metadata"]["name"] for b in bodies)
    assert bodies[0]["subtasks"][0]["metadata"]["event"] == "ev"
    m = json.loads((ds / "out" / "carga_p.json").read_text())["metrics"]
    assert m["duration_by_priority"]["high"]["cases"] == 2
    assert m["duration_by_priority"]["normal"]["cases"] == 2
    assert sorted(c["priority"] for c in m["per_case"]) == ["high", "high", "normal", "normal"]


def test_duration_by_priority_table(tmp_path):
    ds = make_dataset(tmp_path, n_cases=4)
    clock = FakeClock()
    priorities = rl.assign_priorities(4, 0.5, 42)
    durations = {f"id{i + 1}": (10 if p == "high" else 40) for i, p in enumerate(priorities)}
    with responses.RequestsMock() as rsps:
        mock_api(rsps, durations=durations, stats={"queues": {}})
        # concurrency=1: the mock hands out case ids in POST order, which must match case order here.
        rl.run_load(args_for(ds, high_fraction=0.5, concurrency=1), settings(), redis_client=DownRedis(),
                    sleep=clock.sleep, clock=clock, stamp="d")
    data = json.loads((ds / "out" / "carga_d.json").read_text())
    by_p = data["metrics"]["duration_by_priority"]
    assert by_p["high"]["avg_duration_s"] == 10.0 and by_p["normal"]["avg_duration_s"] == 40.0
    text = (ds / "out" / "carga_d.md").read_text()
    assert "Prioridad alta vs normal" in text
    assert "| high | 2 | 2 | 10.0 | 10.0 |" in text
    assert "| normal | 2 | 2 | 40.0 | 40.0 |" in text
    assert data["config"]["semilla"] == 42


def test_monitor_samples_stats_and_ignores_failures(tmp_path):
    ds = make_dataset(tmp_path, n_cases=1)
    clock = FakeClock()
    stats = {"queues": {"transcode_video": 7, "extract_metadata": 2}, "cases_by_status": {"processing": 1},
             "workers_alive": 2, "workers_total": 3, "subtasks_active": 4, "ignorado": 1}
    with responses.RequestsMock() as rsps:
        mock_api(rsps, stats=stats)
        rl.run_load(args_for(ds), settings(), redis_client=DownRedis(), sleep=clock.sleep, clock=clock, stamp="s")
    data = json.loads((ds / "out" / "carga_s.json").read_text())
    sample = data["series"][0]["stats"]
    assert sample["queues"] == {"transcode_video": 7, "extract_metadata": 2}
    assert "ignorado" not in sample and sample["workers_alive"] == 2
    assert data["metrics"]["max_queue_length"] == {"extract_metadata": 2, "transcode_video": 7}
    assert "Largo maximo de las colas" in (ds / "out" / "carga_s.md").read_text()


def test_stats_failure_is_ignored(tmp_path):
    ds = make_dataset(tmp_path, n_cases=1)
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        mock_api(rsps)
        rsps.add(responses.GET, f"{URL}/stats", status=500)
        code, md = rl.run_load(args_for(ds), settings(), redis_client=DownRedis(), sleep=clock.sleep,
                               clock=clock, stamp="f")
    assert code == 0
    data = json.loads((ds / "out" / "carga_f.json").read_text())
    assert all(s["stats"] is None for s in data["series"])
    assert data["metrics"]["max_queue_length"] == {}
    assert "Largo maximo" not in md.read_text()


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_terminal_statuses_end_monitoring(tmp_path, status):
    ds = make_dataset(tmp_path, n_cases=1)
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        mock_api(rsps, finish_after=1, final_status=status)
        code, _ = rl.run_load(args_for(ds), settings(), redis_client=DownRedis(), sleep=clock.sleep,
                              clock=clock, stamp="t")
    assert code == 0
    assert json.loads((ds / "out" / "carga_t.json").read_text())["timed_out"] is False


def test_run_load_without_redis(tmp_path):
    ds = make_dataset(tmp_path, n_cases=2)
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        mock_api(rsps, finish_after=1)
        code, md = rl.run_load(args_for(ds, limit=1, kinds="homogeneous"), settings(),
                               redis_client=DownRedis(), sleep=clock.sleep, clock=clock, stamp="x")
    assert code == 0
    data = json.loads((ds / "out" / "carga_x.json").read_text())
    assert data["metrics"]["cases"] == 1
    assert all(s["workers"] == {} for s in data["series"])
    assert "sin datos de saturacion" in md.read_text()


def test_run_load_timeout_exit_code(tmp_path):
    ds = make_dataset(tmp_path, n_cases=2)
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        mock_api(rsps, never=True)
        code, md = rl.run_load(args_for(ds, timeout=20.0, poll=5.0), settings(), redis_client=DownRedis(),
                               sleep=clock.sleep, clock=clock, stamp="t")
    assert code == 2
    assert json.loads((ds / "out" / "carga_t.json").read_text())["timed_out"] is True
    assert "TIMEOUT" in md.read_text()


def test_submit_error_counted(tmp_path):
    ds = make_dataset(tmp_path, n_cases=1)
    clock = FakeClock()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"{URL}/cases", status=500)
        code, _ = rl.run_load(args_for(ds), settings(), redis_client=DownRedis(), sleep=clock.sleep, clock=clock,
                              stamp="e")
    assert code == 0
    m = json.loads((ds / "out" / "carga_e.json").read_text())["metrics"]
    assert m["case_status_counts"] == {"submit_error": 1}
