from __future__ import annotations

import logging
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Iterable

import minio

from src.workers.config import Settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, settings: Settings, client: minio.Minio | None = None):
        self.settings = settings
        self.client = client if client is not None else minio.Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def ensure_buckets(self) -> None:
        for bucket in (self.settings.dataset_bucket, self.settings.results_bucket):
            try:
                if not self.client.bucket_exists(bucket):
                    self.client.make_bucket(bucket)
            except Exception as e:
                raise StorageError(f"failed to ensure bucket {bucket!r} exists") from e

    def ping(self) -> bool:
        try:
            return bool(self.client.bucket_exists(self.settings.dataset_bucket))
        except Exception:
            return False

    def download(self, key: str, dest_dir: Path) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = dest_dir / PurePosixPath(key).name
        try:
            self.client.fget_object(self.settings.dataset_bucket, key, str(local_path))
        except Exception as e:
            raise StorageError(f"failed to download {key!r} from bucket {self.settings.dataset_bucket!r}") from e
        return local_path

    def download_result(self, ref: str, dest_dir: Path) -> Path:
        bucket = self.settings.results_bucket
        key = ref[len(bucket) + 1:] if ref.startswith(f"{bucket}/") else ref
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            local_path = dest_dir / PurePosixPath(key).name
            self.client.fget_object(bucket, key, str(local_path))
        except Exception as e:
            raise StorageError(f"failed to download {key!r} from bucket {bucket!r}") from e
        return local_path

    def upload_outputs(self, paths: Iterable[str | Path], case_id: str, subtask_id: str) -> list[str]:
        results: list[str] = []
        for path in paths:
            path = Path(path)
            key = f"{case_id}/{subtask_id}/{path.name}"
            self.upload_file(self.settings.results_bucket, key, path)
            results.append(f"{self.settings.results_bucket}/{key}")
        return results

    def upload_file(self, bucket: str, key: str, path: str | Path) -> None:
        content_type, _ = mimetypes.guess_type(str(path))
        content_type = content_type or "application/octet-stream"
        try:
            self.client.fput_object(bucket, key, str(path), content_type=content_type)
        except Exception as e:
            raise StorageError(f"failed to upload {path!r} to {bucket}/{key}") from e
