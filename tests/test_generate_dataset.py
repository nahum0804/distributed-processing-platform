import json
import random
from pathlib import Path

import pytest
import requests
import responses

from scripts import generate_dataset as gd
from src.workers.config import OPERATIONS

URL = "http://coord.test"
ROOT = Path(__file__).resolve().parent.parent


def make_manifest(tmp_path, n=30, problematic_every=10):
    files = []
    for i in range(n):
        video = i % 3 != 0
        files.append({
            "file": f"{'video' if video else 'audio'}_{i:04d}.{'mp4' if video else 'mp3'}",
            "key": f"evento{i % 2}/{'video' if video else 'audio'}_{i:04d}.{'mp4' if video else 'mp3'}",
            "type": "video" if video else "audio",
            "format": "mp4" if video else "mp3",
            "size_class": "light",
            "duration_s": 6 + i,
            "resolution": "320x240" if video else None,
            "bytes": 1000 + i,
            "event": f"evento{i % 2}",
            "session": "sesion1",
            "user": f"usuario{i % 4:02d}",
            "batch": f"lote_{i % 3}",
            "problematic": i % problematic_every == 0,
        })
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"files": files}))
    return path, files


def all_cases(tmp_path, seed=1, n=60, **kw):
    path, files = make_manifest(tmp_path)
    loaded = gd.load_manifest_files(path)
    cases = gd.generate_cases(n, kw.pop("min_sub", 2), kw.pop("max_sub", 8), random.Random(seed), loaded, **kw)
    return cases, files


def params_valid(op, params):
    assert set(params) <= gd.ALLOWED_PARAMS[op]
    if "crf" in params:
        assert isinstance(params["crf"], int) and 0 <= params["crf"] <= 51
    if "preset" in params:
        assert params["preset"] in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow",
                                    "slower", "veryslow", "placebo"}
    if "height" in params:
        assert params["height"] > 0 and params["height"] % 2 == 0
    if "hwaccel" in params:
        assert params["hwaccel"] == "nvenc"
    if "bitrate" in params:
        value = params["bitrate"]
        number = int(str(value).rstrip("k"))
        assert 8 <= number <= 320
    if "timestamp" in params:
        assert isinstance(params["timestamp"], (int, float)) and params["timestamp"] >= 0
    if "width" in params:
        assert 16 <= params["width"] <= 7680


def test_allowed_params_cover_all_operations():
    assert set(gd.ALLOWED_PARAMS) == set(OPERATIONS) == set(gd.VALID_TASK_TYPES)


def test_payload_keys_exist_in_manifest_and_ops_match_type(tmp_path):
    cases, files = all_cases(tmp_path)
    by_key = {f["key"]: f for f in files}
    for case in cases:
        for st in case.to_payload()["subtasks"]:
            entry = by_key[st["file_path"]]
            assert st["task_type"] in gd.OPERATIONS_BY_TYPE[entry["type"]]
            if entry["type"] == "audio":
                assert st["task_type"] in ("convert_audio", "extract_metadata")
            else:
                assert st["task_type"] != "convert_audio"


def test_params_valid_per_operation(tmp_path):
    cases, _ = all_cases(tmp_path, n=200)
    seen_ops = set()
    for case in cases:
        for st in case.subtasks:
            seen_ops.add(st.task_type)
            params_valid(st.task_type, st.params)
    assert seen_ops == set(OPERATIONS)


def test_thumbnail_timestamp_below_duration(tmp_path):
    cases, files = all_cases(tmp_path, n=300)
    duration = {f["key"]: f["duration_s"] for f in files}
    stamps = [(st.params["timestamp"], duration[st.file_path]) for c in cases for st in c.subtasks
              if st.task_type == "generate_thumbnail" and "timestamp" in st.params]
    assert stamps and all(0 <= t < d for t, d in stamps)


def test_priorities_valid_and_about_ten_percent(tmp_path):
    cases, _ = all_cases(tmp_path, n=1000)
    priorities = [c.to_payload()["priority"] for c in cases]
    assert set(priorities) == set(gd.PRIORITIES)
    assert 0.06 < priorities.count("high") / len(priorities) < 0.14


def test_high_fraction_extremes(tmp_path):
    cases, _ = all_cases(tmp_path, n=20, high_fraction=0.0)
    assert {c.priority for c in cases} == {"normal"}
    cases, _ = all_cases(tmp_path, n=20, high_fraction=1.0)
    assert {c.priority for c in cases} == {"high"}


def test_subtask_metadata_from_manifest_and_case_metadata(tmp_path):
    cases, files = all_cases(tmp_path, n=10)
    by_key = {f["key"]: f for f in files}
    for case in cases:
        payload = case.to_payload()
        assert payload["metadata"]["source"] == "generate_dataset"
        for st in payload["subtasks"]:
            entry = by_key[st["file_path"]]
            assert st["metadata"] == {k: entry[k] for k in gd.FILE_METADATA_FIELDS}


def test_subtask_count_within_range_and_no_duplicate_files(tmp_path):
    cases, _ = all_cases(tmp_path, n=50, min_sub=3, max_sub=6)
    for case in cases:
        keys = [st.file_path for st in case.subtasks]
        assert 3 <= len(keys) <= 6
        assert len(keys) == len(set(keys))


def test_more_subtasks_than_files_still_works(tmp_path):
    path, _ = make_manifest(tmp_path, n=4)
    files = gd.load_manifest_files(path)
    cases = gd.generate_cases(5, 6, 6, random.Random(0), files)
    assert all(len(c.subtasks) == 6 for c in cases)


def test_reproducible_with_seed(tmp_path):
    a, _ = all_cases(tmp_path, seed=7, n=15)
    b, _ = all_cases(tmp_path, seed=7, n=15)
    assert [c.to_payload() for c in a] == [c.to_payload() for c in b]


def test_exclude_problematic(tmp_path):
    path, files = make_manifest(tmp_path)
    bad = {f["key"] for f in files if f["problematic"]}
    assert bad
    loaded = gd.load_manifest_files(path, include_problematic=False)
    assert not bad & {f["key"] for f in loaded}
    assert len(gd.load_manifest_files(path)) == len(files)


def test_real_manifest_generates_valid_cases_when_present():
    manifest = ROOT / "dataset" / "manifest.json"
    if not manifest.is_file():
        pytest.skip("dataset/manifest.json no existe")
    files = gd.load_manifest_files(manifest)
    keys = {f["key"] for f in files}
    for case in gd.generate_cases(50, 2, 8, random.Random(3), files):
        for st in case.subtasks:
            assert st.file_path in keys
            params_valid(st.task_type, st.params)


def test_missing_manifest_error_in_spanish(tmp_path, capsys):
    code = gd.main(["--manifest", str(tmp_path / "no_existe.json"), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1
    assert "ERROR" in out and "scripts.build_dataset" in out and "no se encontro" in out


def test_invalid_manifest_reports_error(tmp_path, capsys):
    bad = tmp_path / "manifest.json"
    bad.write_text("{no es json")
    assert gd.main(["--manifest", str(bad)]) == 1
    assert "ERROR" in capsys.readouterr().out
    bad.write_text(json.dumps({"files": []}))
    assert gd.main(["--manifest", str(bad)]) == 1


def test_dry_run_prints_payloads_without_http(tmp_path, capsys):
    path, _ = make_manifest(tmp_path)
    with responses.RequestsMock() as rsps:
        code = gd.main(["--manifest", str(path), "--dry-run", "--seed", "1", "--min-cases", "5", "--max-cases", "5"])
    assert code == 0 and not rsps.calls
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out and '"priority"' in out and '"subtasks"' in out


@pytest.mark.parametrize("args", [
    ["--min-cases", "0"], ["--min-cases", "5", "--max-cases", "2"], ["--min-sub", "0"],
    ["--min-sub", "5", "--max-sub", "2"], ["--workers", "0"], ["--high-fraction", "1.5"],
])
def test_invalid_args_exit_1(tmp_path, args, capsys):
    path, _ = make_manifest(tmp_path)
    assert gd.main(["--manifest", str(path), "--dry-run", *args]) == 1
    assert "ERROR" in capsys.readouterr().out


@responses.activate
def test_main_submits_all_cases_with_new_body(tmp_path, capsys):
    path, files = make_manifest(tmp_path)
    keys = {f["key"] for f in files}
    responses.add(responses.POST, f"{URL}/cases", json={"case_id": "abc"}, status=201)

    code = gd.main(["--manifest", str(path), "--url", URL + "/", "--seed", "3", "--min-cases", "6",
                    "--max-cases", "6", "--workers", "3"])

    assert code == 0
    assert len(responses.calls) == 6
    for call in responses.calls:
        body = json.loads(call.request.body)
        assert body["priority"] in gd.PRIORITIES
        assert all(st["file_path"] in keys for st in body["subtasks"])
    assert "Exitosos             : 6" in capsys.readouterr().out


@responses.activate
def test_main_reports_http_failures(tmp_path, capsys):
    path, _ = make_manifest(tmp_path)
    responses.add(responses.POST, f"{URL}/cases", json={"detail": "x"}, status=422)

    code = gd.main(["--manifest", str(path), "--url", URL, "--min-cases", "2", "--max-cases", "2"])

    assert code == 1
    assert "fallaron" in capsys.readouterr().out


@responses.activate
def test_submit_case_connection_error_is_captured(tmp_path):
    spec = gd.CaseSpec(1, [gd.SubtaskSpec("extract_metadata", "a/b.mp4")], priority="high")
    responses.add(responses.POST, f"{URL}/cases", body=requests.ConnectionError("down"))
    with requests.Session() as session:
        result = gd.submit_case(session, URL, spec)
    assert not result.success and "ConnectionError" in result.error
