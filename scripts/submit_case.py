from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from src.workers.config import OPERATIONS, Settings
from src.workers.storage import Storage, StorageError

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}

MIXED_VIDEO_CYCLE = ("transcode_video", "extract_audio", "generate_thumbnail", "extract_metadata")
MIXED_AUDIO_CYCLE = ("convert_audio", "extract_metadata")

FINISHED_STATUSES = {"completed", "partially_completed", "failed", "cancelled"}


def classify(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def list_media_files(directory: Path) -> list[Path]:
    directory = Path(directory)
    return sorted(p for p in directory.rglob("*") if p.is_file())


def plan_operations(files: list[Path], mode: str) -> list[tuple[Path, str]]:
    if mode == "auto":
        plan = []
        for f in files:
            kind = classify(f)
            if kind == "video":
                plan.append((f, "transcode_video"))
            elif kind == "audio":
                plan.append((f, "convert_audio"))
        return plan

    if mode == "mixed":
        plan = []
        video_i = 0
        audio_i = 0
        for f in files:
            kind = classify(f)
            if kind == "video":
                op = MIXED_VIDEO_CYCLE[video_i % len(MIXED_VIDEO_CYCLE)]
                video_i += 1
            elif kind == "audio":
                op = MIXED_AUDIO_CYCLE[audio_i % len(MIXED_AUDIO_CYCLE)]
                audio_i += 1
            else:
                op = "extract_metadata"
            plan.append((f, op))
        return plan

    if mode not in OPERATIONS:
        raise ValueError(f"operacion desconocida: {mode!r} (validas: {', '.join(OPERATIONS)}, auto, mixed)")
    return [(f, mode) for f in files]


def build_case_payload(keys_ops: list[tuple[str, str]]) -> dict:
    return {"subtasks": [{"task_type": op, "file_path": key, "params": None} for key, op in keys_ops]}


def upload_plan(storage, plan: list[tuple[Path, str]], prefix: str, bucket: str) -> list[tuple[str, str]]:
    storage.ensure_buckets()
    keys_ops = []
    for path, op in plan:
        key = f"{prefix}/{path.name}"
        storage.upload_file(bucket, key, path)
        keys_ops.append((key, op))
    return keys_ops


def submit_case(coordinator_url: str, keys_ops: list[tuple[str, str]], session=requests) -> dict:
    payload = build_case_payload(keys_ops)
    resp = session.post(f"{coordinator_url}/cases", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def count_subtasks(subtasks: list[dict]) -> tuple[int, int]:
    ok = sum(1 for s in subtasks if s.get("status") == "completed")
    failed = sum(1 for s in subtasks if s.get("status") == "failed")
    return ok, failed


def fetch_report(coordinator_url: str, case_id: str, session=requests) -> dict | None:
    try:
        resp = session.get(f"{coordinator_url}/cases/{case_id}/report", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Aviso: no se pudo obtener el reporte de {case_id}: {e}")
        return None


def print_report(case_id: str, report: dict) -> None:
    print(f"Reporte {case_id}: {report.get('summary', '')}")
    breakdown = report.get("failure_breakdown") or {}
    if breakdown:
        print("  fallos: " + ", ".join(f"{k}={v}" for k, v in breakdown.items()))
    by_host = report.get("avg_processing_s_by_host") or {}
    if by_host:
        print("  prom(s) por host: " + ", ".join(f"{h}={v:.2f}" for h, v in by_host.items()))


def http_error_detail(e: requests.RequestException) -> str:
    resp = getattr(e, "response", None)
    if resp is None:
        return str(e)
    try:
        detail = resp.json().get("detail", resp.text)
    except (ValueError, AttributeError):
        detail = resp.text
    return f"{resp.status_code} {detail}"


def poll_case(
    coordinator_url: str,
    case_id: str,
    poll_interval: float,
    timeout: float,
    session=requests,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    start = now()
    deadline = start + timeout
    last_status = None
    while True:
        resp = session.get(f"{coordinator_url}/cases/{case_id}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        case = data.get("case", {})
        subtasks = data.get("subtasks", [])
        status = case.get("status")
        ok, failed = count_subtasks(subtasks)
        total = case.get("total_subtasks", len(subtasks))
        data.update(case_id=case_id, status=status, ok=ok, failed=failed, total=total)
        if status != last_status:
            print(f"{case_id} {status} {ok}/{failed}/{total}")
            last_status = status
        if status in FINISHED_STATUSES:
            data["_wall_time"] = now() - start
            data["_timed_out"] = False
            return data
        if now() >= deadline:
            data["_wall_time"] = now() - start
            data["_timed_out"] = True
            return data
        sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube archivos y crea un caso de prueba en el coordinador.")
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--prefix", default=None)
    parser.add_argument("--mode", default="auto", help="auto | mixed | <operacion explicita>")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args(argv)

    directory = args.dir
    if not directory.is_dir():
        print(f"Error: {directory} no es un directorio valido")
        return 1

    prefix = args.prefix or directory.name
    all_files = list_media_files(directory)

    try:
        plan = plan_operations(all_files, args.mode)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if args.mode == "auto" and len(plan) < len(all_files):
        skipped = len(all_files) - len(plan)
        print(f"Nota: se omitieron {skipped} archivo(s) sin operacion automatica (modo auto)")

    if not plan:
        print("No hay archivos para enviar")
        return 1

    if args.mode == "auto":
        plan = [(path, "auto") for path, _ in plan]

    settings = Settings.from_env()

    if args.no_upload:
        keys_ops = [(f"{prefix}/{path.name}", op) for path, op in plan]
    else:
        storage = Storage(settings)
        try:
            keys_ops = upload_plan(storage, plan, prefix, settings.dataset_bucket)
        except StorageError as e:
            print(f"Error subiendo a MinIO: {e}")
            return 1

    coordinator_url = settings.coordinator_url

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.repeat)) as pool:
            futures = [pool.submit(submit_case, coordinator_url, keys_ops) for _ in range(args.repeat)]
            cases = [fut.result() for fut in futures]
    except requests.RequestException as e:
        print(f"Error HTTP al crear el caso: {http_error_detail(e)}")
        return 1

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(cases))) as pool:
        futures = {
            pool.submit(poll_case, coordinator_url, c["case_id"], args.poll, args.timeout): c for c in cases
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    for r in results:
        if not r.get("_timed_out"):
            report = fetch_report(coordinator_url, r["case_id"])
            if report:
                print_report(r["case_id"], report)

    print("\nResumen:")
    print(f"{'case_id':38} {'status':20} {'ok/fail/total':15} tiempo(s)")
    timed_out_any = False
    for r in results:
        if r.get("_timed_out"):
            timed_out_any = True
        summary = f"{r.get('ok', '?')}/{r.get('failed', '?')}/{r.get('total', '?')}"
        print(f"{r.get('case_id', '?'):38} {r.get('status', '?'):20} {summary:15} {r.get('_wall_time', 0.0):.1f}")

    return 2 if timed_out_any else 0


if __name__ == "__main__":
    sys.exit(main())
