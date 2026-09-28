from __future__ import annotations

import types
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from src.workers.config import OPERATIONS
from src.workers.storage import StorageError


class FakeStorage:
    def __init__(self, dataset_bucket: str = "dataset", results_bucket: str = "results", fail_upload: bool = False):
        self.dataset_bucket = dataset_bucket
        self.results_bucket = results_bucket
        self.fail_upload = fail_upload
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets_ensured = False

    def ensure_buckets(self) -> None:
        self.buckets_ensured = True

    def ping(self) -> bool:
        return True

    def download(self, key: str, dest_dir: Path) -> Path:
        data = self.objects.get((self.dataset_bucket, key))
        if data is None:
            raise StorageError(f"no such object: {self.dataset_bucket}/{key}")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = dest_dir / PurePosixPath(key).name
        local_path.write_bytes(data)
        return local_path

    def upload_outputs(self, paths: Iterable[str | Path], case_id: str, subtask_id: str) -> list[str]:
        if self.fail_upload:
            raise StorageError("fake upload failure")
        results: list[str] = []
        for path in paths:
            path = Path(path)
            key = f"{case_id}/{subtask_id}/{path.name}"
            self.objects[(self.results_bucket, key)] = path.read_bytes()
            results.append(f"{self.results_bucket}/{key}")
        return results

    def upload_file(self, bucket: str, key: str, path: str | Path) -> None:
        if self.fail_upload:
            raise StorageError("fake upload failure")
        self.objects[(bucket, key)] = Path(path).read_bytes()


class ProcessingError(Exception):
    pass


class UnsupportedFormatError(ProcessingError):
    pass


class CorruptInputError(ProcessingError):
    pass


class ProcessingTimeoutError(ProcessingError):
    pass


class InputNotFoundError(ProcessingError):
    pass


class FFmpegNotAvailableError(ProcessingError):
    pass


_EXCEPTION_CLASSES = {
    "ProcessingError": ProcessingError,
    "UnsupportedFormatError": UnsupportedFormatError,
    "CorruptInputError": CorruptInputError,
    "ProcessingTimeoutError": ProcessingTimeoutError,
    "InputNotFoundError": InputNotFoundError,
    "FFmpegNotAvailableError": FFmpegNotAvailableError,
}


@dataclass
class ProcessResult:
    operation: str
    input: str
    outputs: list[str]
    duration_s: float
    media_duration_s: float | None
    output_bytes: int


def make_fake_processor(outcome=None) -> types.SimpleNamespace:
    calls: list[dict] = []

    def process(operation, src, out_dir, params=None, on_progress=None, threads=None, timeout=None) -> ProcessResult:
        calls.append({
            "operation": operation,
            "src": src,
            "out_dir": out_dir,
            "params": params,
            "on_progress": on_progress,
            "threads": threads,
            "timeout": timeout,
        })

        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, str):
            raise _EXCEPTION_CLASSES[outcome]("fake failure")
        if callable(outcome):
            outcome(calls[-1])

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        out_file = (out_path / (Path(src).stem + ".out")).resolve()
        out_file.write_bytes(b"fake-output")

        if on_progress is not None:
            on_progress(0)
            on_progress(100)

        return ProcessResult(
            operation=operation,
            input=str(src),
            outputs=[str(out_file)],
            duration_s=0.01,
            media_duration_s=None,
            output_bytes=out_file.stat().st_size,
        )

    return types.SimpleNamespace(
        ProcessingError=ProcessingError,
        UnsupportedFormatError=UnsupportedFormatError,
        CorruptInputError=CorruptInputError,
        ProcessingTimeoutError=ProcessingTimeoutError,
        InputNotFoundError=InputNotFoundError,
        FFmpegNotAvailableError=FFmpegNotAvailableError,
        SUPPORTED_OPERATIONS=OPERATIONS,
        ProcessResult=ProcessResult,
        process=process,
        calls=calls,
    )
