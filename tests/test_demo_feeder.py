import random
from pathlib import Path

import requests
import yaml

from scripts import demo_feeder as df
from src.workers.config import OPERATIONS, Settings
from src.workers.storage import StorageError

ROOT = Path(__file__).resolve().parent.parent
LIB = {
    "video": ["a.mp4", "b.mkv", "c.mov", "d.avi", "e.mp4", "f.mp4"],
    "audio": ["a.mp3", "b.wav", "c.ogg", "d.flac", "e.m4a"],
    "problem": ["corrupto.mp4", "solo_audio.mp4", "sin_audio.mp4"],
}


def test_pick_case_valid_and_mix():
    labels = set()
    for seed in range(300):
        payload, label = df.pick_case(random.Random(seed), LIB)
        labels.add(label)
        assert payload["subtasks"]
        for s in payload["subtasks"]:
            assert s["task_type"] in OPERATIONS or s["task_type"] == "auto"
            assert s["file_path"].startswith("demo/")
    assert {"video", "audio", "mixed"} <= labels
    assert any(l.endswith("+problema") for l in labels)


def test_pick_case_priority_and_metadata():
    priorities, labels_ok = [], True
    for seed in range(400):
        payload, label = df.pick_case(random.Random(seed), LIB)
        priorities.append(payload["priority"])
        labels_ok &= payload["metadata"] == {"source": "demo", "label": label}
    assert set(priorities) == {"normal", "high"}
    assert labels_ok
    assert 0.08 < priorities.count("high") / len(priorities) < 0.25


def test_build_library_skips_failures(tmp_path):
    def runner(args):
        out = Path(args[-1])
        if out.suffix == ".webm":
            raise RuntimeError("codec")
        out.write_bytes(b"x")

    lib = df.build_library(tmp_path, runner, random.Random(1))
    assert "clip_d.webm" not in lib["video"]
    assert len(lib["video"]) == 5 and len(lib["audio"]) == 5
    assert "corrupto.mp4" in lib["problem"]


class Resp:
    def __init__(self, fail=False, data=None):
        self.fail = fail
        self.data = data or {"case_id": "c1"}

    def raise_for_status(self):
        if self.fail:
            raise requests.HTTPError("500")

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, fail_first=True, fail_cancel=False):
        self.posts = 0
        self.bodies = []
        self.cancels = []
        self.fail_next = fail_first
        self.fail_cancel = fail_cancel

    def get(self, url, timeout=None):
        return Resp()

    def post(self, url, json=None, timeout=None):
        if url.endswith("/cancel"):
            self.cancels.append(url)
            return Resp(fail=self.fail_cancel)
        self.posts += 1
        self.bodies.append(json)
        if self.fail_next:
            self.fail_next = False
            return Resp(fail=True)
        return Resp()


class ImmediateTimer:
    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args = delay, fn, args
        self.daemon = False

    def start(self):
        self.fn(*self.args)


class FakeStorage:
    def __init__(self):
        self.uploads = []
        self.fail_first = True

    def ensure_buckets(self):
        pass

    def upload_file(self, bucket, key, path):
        if self.fail_first:
            self.fail_first = False
            raise StorageError("boom")
        self.uploads.append(key)


def test_run_submits_max_cases_and_survives_errors(tmp_path):
    def runner(args):
        Path(args[-1]).write_bytes(b"x")

    session, storage = FakeSession(), FakeStorage()
    sent = df.run(Settings(), session, storage, random.Random(3), sleep=lambda s: None,
                  max_cases=5, out_dir=tmp_path, runner=runner, cancel_fraction=0.0)
    assert sent == 5
    assert session.posts == 6
    assert len(storage.uploads) == len(set(storage.uploads)) == 13


def _touch_runner(args):
    Path(args[-1]).write_bytes(b"x")


def test_run_cancels_some_cases_shortly_after_submit(tmp_path):
    session = FakeSession(fail_first=False)
    sent = df.run(Settings(), session, FakeStorage(), random.Random(5), sleep=lambda s: None, max_cases=6,
                  out_dir=tmp_path, runner=_touch_runner, cancel_fraction=1.0, timer_factory=ImmediateTimer)
    assert sent == 6
    assert session.cancels == [f"{Settings().coordinator_url}/cases/c1/cancel"] * 6


def test_run_does_not_cancel_when_fraction_zero(tmp_path):
    session = FakeSession(fail_first=False)
    df.run(Settings(), session, FakeStorage(), random.Random(5), sleep=lambda s: None, max_cases=4,
           out_dir=tmp_path, runner=_touch_runner, cancel_fraction=0.0, timer_factory=ImmediateTimer)
    assert session.cancels == []


def test_run_cancel_delay_is_a_few_seconds(tmp_path):
    delays = []

    class Recording(ImmediateTimer):
        def __init__(self, delay, fn, args=()):
            super().__init__(delay, fn, args)
            delays.append(delay)

    df.run(Settings(), FakeSession(fail_first=False), FakeStorage(), random.Random(5), sleep=lambda s: None,
           max_cases=3, out_dir=tmp_path, runner=_touch_runner, cancel_fraction=1.0, timer_factory=Recording)
    assert delays and all(df.CANCEL_DELAY_RANGE[0] <= d <= df.CANCEL_DELAY_RANGE[1] for d in delays)


def test_run_survives_cancel_errors(tmp_path):
    session = FakeSession(fail_first=False, fail_cancel=True)
    sent = df.run(Settings(), session, FakeStorage(), random.Random(5), sleep=lambda s: None, max_cases=3,
                  out_dir=tmp_path, runner=_touch_runner, cancel_fraction=1.0, timer_factory=ImmediateTimer)
    assert sent == 3 and len(session.cancels) == 3


def test_run_posts_priority_and_metadata(tmp_path):
    session = FakeSession(fail_first=False)
    df.run(Settings(), session, FakeStorage(), random.Random(9), sleep=lambda s: None, max_cases=8,
           out_dir=tmp_path, runner=_touch_runner, cancel_fraction=0.0)
    assert all(b["priority"] in ("normal", "high") and b["metadata"]["source"] == "demo" for b in session.bodies)


def test_submit_returns_none_on_http_error():
    assert df.submit(Settings(), FakeSession(), {"subtasks": []}, "x") is None
    assert df.cancel(Settings(), FakeSession(fail_first=False, fail_cancel=True), "c") is False


def test_compose_files():
    demo = yaml.safe_load((ROOT / "deploy/docker-compose.demo.yml").read_text())
    assert "docker-compose.local.yml" in demo["include"]
    assert demo["services"]["feeder"]["command"] == ["python", "-m", "scripts.demo_feeder"]
    local = yaml.safe_load((ROOT / "deploy/docker-compose.local.yml").read_text())
    assert local["services"]["redis"]["ports"] == ["127.0.0.1:6379:6379"]
