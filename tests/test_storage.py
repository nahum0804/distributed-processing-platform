from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.workers.config import Settings
from src.workers.storage import Storage, StorageError
from tests.fakes import FakeStorage, make_fake_processor


def make_settings(**overrides) -> Settings:
    return Settings.from_env({**overrides})


def test_storage_uses_injected_client_and_settings():
    settings = make_settings()
    client = MagicMock()
    storage = Storage(settings, client=client)
    assert storage.client is client


def test_download_calls_fget_object_and_returns_path(tmp_path):
    settings = make_settings(DATASET_BUCKET="dataset")
    client = MagicMock()
    storage = Storage(settings, client=client)

    result = storage.download("case_hetero_01/video1.mp4", tmp_path)

    expected_path = tmp_path / "video1.mp4"
    client.fget_object.assert_called_once_with("dataset", "case_hetero_01/video1.mp4", str(expected_path))
    assert result == expected_path


def test_download_wraps_client_exception(tmp_path):
    settings = make_settings()
    client = MagicMock()
    client.fget_object.side_effect = OSError("boom")
    storage = Storage(settings, client=client)

    with pytest.raises(StorageError):
        storage.download("some/key.mp4", tmp_path)


def test_upload_outputs_calls_fput_object_and_returns_result_paths(tmp_path):
    settings = make_settings(RESULTS_BUCKET="results")
    client = MagicMock()
    storage = Storage(settings, client=client)

    f1 = tmp_path / "out1.mp4"
    f1.write_bytes(b"data1")
    f2 = tmp_path / "out2.jpg"
    f2.write_bytes(b"data2")

    result = storage.upload_outputs([f1, f2], case_id="case1", subtask_id="sub1")

    assert result == ["results/case1/sub1/out1.mp4", "results/case1/sub1/out2.jpg"]
    assert client.fput_object.call_count == 2
    call_args_list = client.fput_object.call_args_list
    assert call_args_list[0].args[:3] == ("results", "case1/sub1/out1.mp4", str(f1))
    assert call_args_list[1].args[:3] == ("results", "case1/sub1/out2.jpg", str(f2))


def test_upload_outputs_wraps_client_exception(tmp_path):
    settings = make_settings()
    client = MagicMock()
    client.fput_object.side_effect = Exception("upload failed")
    storage = Storage(settings, client=client)

    f1 = tmp_path / "out.mp4"
    f1.write_bytes(b"data")

    with pytest.raises(StorageError):
        storage.upload_outputs([f1], case_id="case1", subtask_id="sub1")


def test_upload_file_generic_helper(tmp_path):
    settings = make_settings()
    client = MagicMock()
    storage = Storage(settings, client=client)

    f1 = tmp_path / "thumb.png"
    f1.write_bytes(b"data")

    storage.upload_file("some-bucket", "some/key.png", f1)

    client.fput_object.assert_called_once()
    args, kwargs = client.fput_object.call_args
    assert args[:3] == ("some-bucket", "some/key.png", str(f1))
    assert kwargs.get("content_type") == "image/png"


def test_ensure_buckets_creates_only_missing():
    settings = make_settings(DATASET_BUCKET="dataset", RESULTS_BUCKET="results")
    client = MagicMock()
    client.bucket_exists.side_effect = lambda b: b == "dataset"
    storage = Storage(settings, client=client)

    storage.ensure_buckets()

    client.make_bucket.assert_called_once_with("results")


def test_ensure_buckets_wraps_exception():
    settings = make_settings()
    client = MagicMock()
    client.bucket_exists.side_effect = Exception("connection refused")
    storage = Storage(settings, client=client)

    with pytest.raises(StorageError):
        storage.ensure_buckets()


def test_ping_true_when_bucket_reachable():
    settings = make_settings()
    client = MagicMock()
    client.bucket_exists.return_value = True
    storage = Storage(settings, client=client)
    assert storage.ping() is True


def test_ping_false_on_error():
    settings = make_settings()
    client = MagicMock()
    client.bucket_exists.side_effect = Exception("down")
    storage = Storage(settings, client=client)
    assert storage.ping() is False


# --- FakeStorage ---

def test_fake_storage_download_roundtrip(tmp_path):
    fake = FakeStorage()
    fake.objects[("dataset", "case1/video.mp4")] = b"content"

    result = fake.download("case1/video.mp4", tmp_path)

    assert result == tmp_path / "video.mp4"
    assert result.read_bytes() == b"content"


def test_fake_storage_download_missing_key_raises(tmp_path):
    fake = FakeStorage()
    with pytest.raises(StorageError):
        fake.download("missing/key.mp4", tmp_path)


def test_fake_storage_upload_outputs(tmp_path):
    fake = FakeStorage()
    f1 = tmp_path / "out.mp4"
    f1.write_bytes(b"result-data")

    result = fake.upload_outputs([f1], case_id="case1", subtask_id="sub1")

    assert result == ["results/case1/sub1/out.mp4"]
    assert fake.objects[("results", "case1/sub1/out.mp4")] == b"result-data"


def test_fake_storage_fail_upload_flag(tmp_path):
    fake = FakeStorage(fail_upload=True)
    f1 = tmp_path / "out.mp4"
    f1.write_bytes(b"data")

    with pytest.raises(StorageError):
        fake.upload_outputs([f1], case_id="case1", subtask_id="sub1")

    with pytest.raises(StorageError):
        fake.upload_file("results", "some/key", f1)


# --- make_fake_processor ---

def test_fake_processor_success_path(tmp_path):
    fake = make_fake_processor()
    src = tmp_path / "input.mp4"
    src.write_bytes(b"in")
    out_dir = tmp_path / "out"

    progress_events = []
    result = fake.process(
        "transcode_video", str(src), str(out_dir),
        params={"resolution": "720p"},
        on_progress=progress_events.append,
        threads=2,
        timeout=30,
    )

    assert result.operation == "transcode_video"
    assert result.outputs
    for output in result.outputs:
        assert Path(output).is_absolute()
        assert Path(output).exists()
    assert progress_events == [0, 100]
    assert len(fake.calls) == 1
    assert fake.calls[0]["operation"] == "transcode_video"
    assert fake.calls[0]["params"] == {"resolution": "720p"}
    assert fake.calls[0]["threads"] == 2


def test_fake_processor_named_failure(tmp_path):
    fake = make_fake_processor(outcome="CorruptInputError")
    src = tmp_path / "input.mp4"
    src.write_bytes(b"in")

    with pytest.raises(fake.CorruptInputError):
        fake.process("transcode_video", str(src), str(tmp_path / "out"))
    assert len(fake.calls) == 1


def test_fake_processor_instance_outcome(tmp_path):
    fake2 = make_fake_processor(outcome=RuntimeError("boom"))
    src = tmp_path / "input.mp4"
    src.write_bytes(b"in")
    with pytest.raises(RuntimeError):
        fake2.process("extract_audio", str(src), str(tmp_path / "out"))


def test_fake_processor_callable_outcome(tmp_path):
    seen = []

    def side_effect(call_kwargs):
        seen.append(call_kwargs["operation"])

    fake = make_fake_processor(outcome=side_effect)
    src = tmp_path / "input.mp4"
    src.write_bytes(b"in")

    result = fake.process("generate_thumbnail", str(src), str(tmp_path / "out"))

    assert seen == ["generate_thumbnail"]
    assert result.operation == "generate_thumbnail"


def test_fake_processor_callable_outcome_can_raise(tmp_path):
    def side_effect(call_kwargs):
        raise ValueError("bad params")

    fake = make_fake_processor(outcome=side_effect)
    src = tmp_path / "input.mp4"
    src.write_bytes(b"in")

    with pytest.raises(ValueError):
        fake.process("convert_audio", str(src), str(tmp_path / "out"))


def test_fake_processor_supported_operations():
    fake = make_fake_processor()
    assert "transcode_video" in fake.SUPPORTED_OPERATIONS
    assert len(fake.SUPPORTED_OPERATIONS) == 5


def test_download_result_strips_bucket_prefix(tmp_path):
    client = MagicMock()
    storage = Storage(make_settings(RESULTS_BUCKET="results"), client=client)

    result = storage.download_result("results/case1/sub1/out.mp4", tmp_path / "a" / "b")

    expected = tmp_path / "a" / "b" / "out.mp4"
    client.fget_object.assert_called_once_with("results", "case1/sub1/out.mp4", str(expected))
    assert result == expected
    assert expected.parent.is_dir()


def test_download_result_accepts_bare_key(tmp_path):
    client = MagicMock()
    storage = Storage(make_settings(RESULTS_BUCKET="results"), client=client)

    storage.download_result("case1/sub1/out.mp4", tmp_path)

    client.fget_object.assert_called_once_with("results", "case1/sub1/out.mp4", str(tmp_path / "out.mp4"))


def test_download_result_wraps_client_exception(tmp_path):
    client = MagicMock()
    client.fget_object.side_effect = OSError("boom")
    storage = Storage(make_settings(), client=client)

    with pytest.raises(StorageError):
        storage.download_result("results/c/s/x.mp4", tmp_path)


def test_probe_reports_ok_missing_bucket_and_down():
    from unittest.mock import MagicMock

    settings = Settings.from_env({})
    client = MagicMock()
    client.bucket_exists.return_value = True
    assert Storage(settings, client=client).probe() == "ok"

    client.bucket_exists.return_value = False
    assert Storage(settings, client=client).probe() == "missing_bucket"

    client.list_buckets.side_effect = OSError("boom")
    assert Storage(settings, client=client).probe() == "down"
