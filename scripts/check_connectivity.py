from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from pathlib import Path

import redis as redis_module
import requests

from src.workers.config import Settings, make_redis
from src.workers.storage import Storage, StorageError


def check_redis(settings: Settings, client=None) -> tuple[bool, str]:
    try:
        client = client if client is not None else make_redis(settings)
        client.ping()
        return True, f"Redis OK en {settings.redis_host}:{settings.redis_port}"
    except redis_module.AuthenticationError as e:
        return False, (
            f"Redis en {settings.redis_host}:{settings.redis_port} rechazo la autenticacion "
            f"(revisa REDIS_PASSWORD): {e}"
        )
    except Exception as e:
        return False, f"Redis en {settings.redis_host}:{settings.redis_port} no responde: {e}"


def check_coordinator(settings: Settings, get=requests.get) -> tuple[bool, str]:
    url = f"{settings.coordinator_url}/openapi.json"
    try:
        resp = get(url, timeout=5)
        if resp.status_code == 200:
            return True, f"Coordinador OK en {settings.coordinator_url}"
        return False, f"Coordinador respondio HTTP {resp.status_code} en {url}"
    except requests.RequestException as e:
        return False, f"No se pudo conectar al coordinador en {url}: {e}"


def check_minio(settings: Settings, storage=None, create_buckets: bool = False) -> tuple[bool, str]:
    storage = storage if storage is not None else Storage(settings)
    if create_buckets:
        try:
            storage.ensure_buckets()
        except StorageError as e:
            return False, f"No se pudieron crear los buckets en MinIO: {e}"
    if storage.ping():
        return True, f"MinIO OK en {settings.minio_endpoint}"
    return False, (
        f"MinIO no responde en {settings.minio_endpoint} "
        f"(revisa MINIO_ENDPOINT, MINIO_ACCESS_KEY/MINIO_SECRET_KEY y el bucket '{settings.dataset_bucket}')"
    )


def check_ffmpeg(run=subprocess.run) -> tuple[bool, str]:
    try:
        result = run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return False, "ffmpeg no encontrado en PATH (normal si esto corre fuera de un worker/Docker)"
    except Exception as e:
        return False, f"No se pudo ejecutar ffmpeg: {e}"
    if result.returncode != 0:
        return False, "ffmpeg -version devolvio un codigo de salida distinto de 0"
    first_line = result.stdout.splitlines()[0] if result.stdout else "ffmpeg"
    return True, f"ffmpeg OK: {first_line}"


def check_work_dir(work_dir: Path) -> tuple[bool, str]:
    work_dir = Path(work_dir)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        probe = work_dir / f".connectivity-check-{uuid.uuid4().hex}"
        probe.write_text("ok")
        probe.unlink()
        return True, f"WORK_DIR escribible: {work_dir}"
    except Exception as e:
        return False, f"WORK_DIR no escribible ({work_dir}): {e}"


def config_summary(settings: Settings) -> str:
    return (
        f"worker_id={settings.worker_id} "
        f"queues={','.join(settings.worker_queues)} "
        f"concurrency={settings.worker_concurrency} "
        f"threads_per_job={settings.threads_per_job()} "
        f"redis={settings.redis_host}:{settings.redis_port} "
        f"coordinator_url={settings.coordinator_url} "
        f"minio_endpoint={settings.minio_endpoint}"
    )


def _line(status: str, name: str, detail: str) -> str:
    return f"[{status}] {name}: {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica conectividad del worker con Redis, el coordinador, MinIO y ffmpeg."
    )
    parser.add_argument("--create-buckets", action="store_true", help="Crea los buckets de MinIO si no existen")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    print(f"Config: {config_summary(settings)}")

    any_fail = False

    ok, detail = check_redis(settings)
    print(_line("OK" if ok else "FAIL", "redis", detail))
    any_fail = any_fail or not ok

    ok, detail = check_coordinator(settings)
    print(_line("OK" if ok else "FAIL", "coordinator", detail))
    any_fail = any_fail or not ok

    ok, detail = check_minio(settings, create_buckets=args.create_buckets)
    print(_line("OK" if ok else "FAIL", "minio", detail))
    any_fail = any_fail or not ok

    ok, detail = check_ffmpeg()
    print(_line("OK" if ok else "WARN", "ffmpeg", detail))

    ok, detail = check_work_dir(settings.work_dir)
    print(_line("OK" if ok else "FAIL", "work_dir", detail))
    any_fail = any_fail or not ok

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
