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
    def __init__(self, fail=False):
        self.fail = fail

    def raise_for_status(self):
        if self.fail:
            raise requests.HTTPError("500")

    def json(self):
        return {"case_id": "c1"}


class FakeSession:
    def __init__(self):
        self.posts = 0
        self.fail_next = True

    def get(self, url, timeout=None):
        return Resp()

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        if self.fail_next:
            self.fail_next = False
            return Resp(fail=True)
        return Resp()


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
                  max_cases=5, out_dir=tmp_path, runner=runner)
    assert sent == 5
    assert session.posts == 6
    assert len(storage.uploads) == len(set(storage.uploads)) == 13


def test_compose_files():
    demo = yaml.safe_load((ROOT / "deploy/docker-compose.demo.yml").read_text())
    assert "docker-compose.local.yml" in demo["include"]
    assert demo["services"]["feeder"]["command"] == ["python", "-m", "scripts.demo_feeder"]
    local = yaml.safe_load((ROOT / "deploy/docker-compose.local.yml").read_text())
    assert local["services"]["redis"]["ports"] == ["127.0.0.1:6379:6379"]
