import json
from collections import Counter
from pathlib import Path

from scripts import build_dataset as bd


def _fake_manifest(n=480, seed=42):
    return [
        {"file": s.name, "key": s.key, "type": s.type, "format": s.format, "size_class": s.size_class,
         "duration_s": s.duration_s, "resolution": s.resolution, "bytes": 100, "event": s.event,
         "session": s.session, "user": s.user, "batch": s.batch, "problematic": s.problematic}
        for s in bd.plan_files(n, seed)
    ]


def test_plan_deterministic_and_count():
    a = bd.plan_files(480, 42)
    assert a == bd.plan_files(480, 42)
    assert a != bd.plan_files(480, 7)
    assert len(a) == 480
    assert len({s.key for s in a}) == 480


def test_plan_proportions_and_coverage():
    specs = bd.plan_files(480, 42)
    videos = [s for s in specs if s.type == "video"]
    assert 0.55 <= len(videos) / 480 <= 0.65
    assert {s.format for s in specs if s.type == "video"} == set(bd.VIDEO_FORMATS)
    assert {s.format for s in specs if s.type == "audio"} == set(bd.AUDIO_FORMATS)
    assert {s.size_class for s in specs} == set(bd.SIZE_CLASSES)
    problems = [s for s in specs if s.problematic]
    assert 10 <= len(problems) <= 18
    assert all(s.format == "mp4" and s.problem for s in problems)
    assert {s.source for s in problems} == set(bd.PROBLEMS)
    assert 8 <= len({s.event for s in specs}) <= 12


def test_batches_single_type_and_events_mixed():
    specs = bd.plan_files(480, 42)
    batch_types = {}
    for s in specs:
        batch_types.setdefault(s.batch, set()).add(s.type)
    assert all(len(t) == 1 for t in batch_types.values())
    event_types = {}
    for s in specs:
        event_types.setdefault(s.event, set()).add(s.type)
    assert all(len(t) == 2 for t in event_types.values())


def _spec(**kw):
    base = dict(name="x.mp4", type="video", format="mp4", size_class="medium", duration_s=12,
                resolution="640x360", source="testsrc", freq=440, event="e", session="s", user="u", batch="b")
    base.update(kw)
    return bd.FileSpec(**base)


def test_ffmpeg_args_video_formats():
    a = bd.ffmpeg_args(_spec(), "/tmp/o.mp4")
    assert isinstance(a, list) and a[0] == "ffmpeg" and a[-1] == "/tmp/o.mp4"
    assert "libx264" in a and "ultrafast" in a and "aac" in a
    assert a[a.index("-t") + 1] == "12"
    assert "testsrc=size=640x360:rate=24" in a
    w = bd.ffmpeg_args(_spec(format="webm"), "o.webm")
    assert "libvpx-vp9" in w and "libopus" in w
    v = bd.ffmpeg_args(_spec(format="avi"), "o.avi")
    assert "mpeg4" in v and "libmp3lame" in v
    h = bd.ffmpeg_args(_spec(size_class="heavy", resolution="1280x720", duration_s=40), "o.mkv")
    assert "testsrc=size=1280x720:rate=24" in h and h[h.index("-t") + 1] == "40"


def test_ffmpeg_args_audio_formats():
    expected = {"mp3": "libmp3lame", "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis", "m4a": "aac"}
    for fmt, codec in expected.items():
        s = _spec(type="audio", format=fmt, resolution=None, duration_s=20, name=f"a.{fmt}")
        a = bd.ffmpeg_args(s, f"a.{fmt}")
        assert codec in a and "-c:v" not in a and a[a.index("-t") + 1] == "20"
    noise = bd.ffmpeg_args(_spec(type="audio", format="mp3", source="anoisesrc", resolution=None), "a.mp3")
    assert any(x.startswith("anoisesrc") for x in noise)


def test_ffmpeg_args_problematic():
    only_audio = bd.ffmpeg_args(_spec(problem="x", source="solo_audio", resolution=None), "o.mp4")
    assert "-vn" in only_audio
    no_audio = bd.ffmpeg_args(_spec(problem="x", source="sin_audio"), "o.mp4")
    assert "-an" in no_audio and "aac" not in no_audio


def test_group_cases_rules():
    files = _fake_manifest()
    by_key = {f["key"]: f for f in files}
    result = bd.group_cases(files, 42)
    cases = result["cases"]
    assert result["criteria"]
    covered = set()
    for c in cases:
        assert bd.CASE_MIN <= len(c["subtasks"]) <= bd.CASE_MAX, c["name"]
        assert all(s["task_type"] in bd.VALID_TASK_TYPES for s in c["subtasks"])
        types = {by_key[s["file_path"]]["type"] for s in c["subtasks"]}
        ops = {s["task_type"] for s in c["subtasks"]}
        covered.update(s["file_path"] for s in c["subtasks"])
        if c["criterion"] == "batch":
            assert c["kind"] == "homogeneous" and len(types) == 1 and len(ops) == 1
        elif c["criterion"] == "event+session":
            assert c["kind"] == "heterogeneous" and len(types) == 2 and len(ops) >= 2
            for s in c["subtasks"]:
                valid = bd.VIDEO_OPS if by_key[s["file_path"]]["type"] == "video" else bd.AUDIO_OPS
                assert s["task_type"] in valid
        else:
            assert c["criterion"] == "user" and ops == {"auto"}
    assert covered == set(by_key)
    assert Counter(c["criterion"] for c in cases).keys() == {"batch", "event+session", "user"}
    assert {"homogeneous", "heterogeneous"} == {c["kind"] for c in cases}
    assert any(s["task_type"] == "generate_thumbnail" for c in cases for s in c["subtasks"]
               if c["criterion"] == "batch")
    assert any(by_key[s["file_path"]]["problematic"] for c in cases for s in c["subtasks"])
    assert result == bd.group_cases(files, 42)


def test_summarize():
    t = bd.summarize(_fake_manifest(100))
    assert t["files"] == 100 and t["videos"] + t["audios"] == 100
    assert t["bytes"] == 10000 and t["problematic"] == 3
    assert sum(t["by_format"].values()) == 100


def test_build_with_fake_runner_drops_failures(tmp_path):
    def runner(args):
        if args[-1].endswith("_light.mp3") and "fail" not in runner.__dict__:
            runner.fail = args[-1]
            raise RuntimeError("ffmpeg boom")
        Path(args[-1]).write_bytes(b"x" * 64)

    manifest = bd.build(tmp_path / "ds", 60, 1, jobs=4, runner=runner)
    assert hasattr(runner, "fail")
    assert manifest["totals"]["files"] == len(manifest["files"]) == 59
    assert all(Path(runner.fail).name != f["file"] for f in manifest["files"])
    on_disk = json.loads((tmp_path / "ds" / "manifest.json").read_text())
    assert on_disk["totals"] == manifest["totals"] and on_disk["seed"] == 1
    corrupt = [f for f in manifest["files"] if f.get("problem") and f["bytes"] == 4096]
    assert corrupt
    cases = json.loads((tmp_path / "ds" / "cases.json").read_text())
    keys = {f["key"] for f in manifest["files"]}
    assert {s["file_path"] for c in cases["cases"] for s in c["subtasks"]} == keys
    readme = (tmp_path / "ds" / "README.md").read_text()
    assert "Composicion" in readme and "59" in readme
    assert (tmp_path / "ds" / "media").is_dir()
