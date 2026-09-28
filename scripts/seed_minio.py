from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.workers.config import Settings
from src.workers.storage import Storage, StorageError


def iter_files(directory: Path):
    directory = Path(directory)
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            yield path


def build_key(directory: Path, path: Path, prefix: str) -> str:
    rel = Path(path).relative_to(directory).as_posix()
    return f"{prefix}/{rel}"


def seed(directory: Path, storage, prefix: str, bucket: str = "dataset") -> tuple[int, int]:
    directory = Path(directory)
    storage.ensure_buckets()
    count = 0
    total_bytes = 0
    for path in iter_files(directory):
        key = build_key(directory, path, prefix)
        storage.upload_file(bucket, key, path)
        count += 1
        total_bytes += path.stat().st_size
    return count, total_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube un directorio local a MinIO como dataset de prueba.")
    parser.add_argument("local_dir", type=Path)
    parser.add_argument("--prefix", default=None, help="Prefijo de las keys (default: nombre del directorio)")
    parser.add_argument("--bucket", default="dataset")
    args = parser.parse_args(argv)

    directory = args.local_dir
    if not directory.is_dir():
        print(f"Error: {directory} no es un directorio valido")
        return 1

    prefix = args.prefix or directory.name

    settings = Settings.from_env()
    storage = Storage(settings)

    try:
        count, total_bytes = seed(directory, storage, prefix, bucket=args.bucket)
    except StorageError as e:
        print(f"Error subiendo a MinIO: {e}")
        return 1

    print(f"Subidos {count} archivo(s), {total_bytes} bytes, a bucket '{args.bucket}' con prefijo '{prefix}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
