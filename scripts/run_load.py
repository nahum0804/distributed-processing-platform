"""
run_load.py - Sube el dataset, envia sus casos al coordinador y mide la carga.

    python -m scripts.run_load --dataset dataset --upload --concurrency 8

Escribe docs/evidencia/carga_<fecha>.json y .md con tiempos por caso, distribucion por host,
promedios por operacion, fallos y notas de saturacion.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.submit_case import FINISHED_STATUSES, build_case_payload
from src.workers.config import Settings, make_redis
from src.workers.storage import Storage, StorageError

logger = logging.getLogger("run_load")

WORKER_NUMERIC = ("cpu_percent", "mem_percent", "active_subtasks", "completed_count", "failed_count")


def load_dataset(dataset: Path) -> tuple[dict, dict]:
    dataset = Path(dataset)
    manifest = json.loads((dataset / "manifest.json").read_text())
    cases = json.loads((dataset / "cases.json").read_text())
    return manifest, cases


def select_cases(cases: dict, kinds: list[str] | None, limit: int | None) -> list[dict]:
    selected = [c for c in cases["cases"] if not kinds or c["kind"] in kinds]
    return selected[:limit] if limit else selected


def _already_uploaded(storage, bucket: str, key: str, size: int) -> bool:
    try:
        return storage.client.stat_object(bucket, key).size == size
    except Exception:
        return False


def upload_dataset(storage, manifest: dict, media_dir: Path, bucket: str, workers: int = 8) -> dict:
    storage.ensure_buckets()
    files = manifest["files"]
    stats = {"uploaded": 0, "skipped": 0, "failed": 0}

    def one(f: dict) -> str:
        if _already_uploaded(storage, bucket, f["key"], f["bytes"]):
            return "skipped"
        try:
            storage.upload_file(bucket, f["key"], Path(media_dir) / f["key"])
            return "uploaded"
        except (StorageError, OSError) as e:
            logger.warning("no se pudo subir %s: %s", f["key"], e)
            return "failed"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, result in enumerate(pool.map(one, files), 1):
            stats[result] += 1
            if i % 25 == 0 or i == len(files):
                logger.info("subida: %d/%d (%s)", i, len(files), stats)
    return stats


def submit_one(session, url: str, case: dict, clock=time.time) -> dict:
    payload = build_case_payload([(s["file_path"], s["task_type"]) for s in case["subtasks"]])
    info = {"name": case["name"], "kind": case["kind"], "criterion": case.get("criterion"),
            "subtasks": len(case["subtasks"]), "submitted_at": clock(), "case_id": None, "error": None}
    try:
        resp = session.post(f"{url}/cases", json=payload, timeout=30)
        resp.raise_for_status()
        info["case_id"] = resp.json()["case_id"]
    except (requests.RequestException, ValueError, KeyError) as e:
        info["error"] = str(e)
        logger.warning("fallo el envio de %s: %s", case["name"], e)
    return info


def submit_all(session, url: str, cases: list[dict], concurrency: int, clock=time.time) -> list[dict]:
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return list(pool.map(lambda c: submit_one(session, url, c, clock), cases))


def connect_redis(settings: Settings, redis_client=None):
    try:
        client = redis_client if redis_client is not None else make_redis(settings)
        client.ping()
        return client
    except Exception as e:
        logger.warning("Redis no disponible (%s); se continua sin heartbeats", e)
        return None


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sample_workers(redis_client) -> dict[str, dict]:
    if redis_client is None:
        return {}
    workers: dict[str, dict] = {}
    try:
        for wid in sorted(redis_client.smembers("workers:registry")):
            hb = redis_client.hgetall(f"worker:{wid}")
            if not hb:
                continue
            entry = {"host": hb.get("host") or hb.get("hostname") or wid}
            entry.update({k: _num(hb.get(k)) for k in WORKER_NUMERIC})
            workers[wid] = entry
    except Exception as e:
        logger.warning("no se pudieron leer heartbeats: %s", e)
    return workers


def _fetch_status(session, url: str, case_id: str) -> str | None:
    try:
        resp = session.get(f"{url}/cases/{case_id}", timeout=15)
        resp.raise_for_status()
        return resp.json().get("case", {}).get("status")
    except (requests.RequestException, ValueError):
        return None


def monitor(session, url: str, submitted: list[dict], redis_client, poll: float, timeout: float,
            concurrency: int = 8, sleep=time.sleep, clock=time.monotonic) -> dict:
    start = clock()
    statuses = {c["case_id"]: "submitted" for c in submitted if c["case_id"]}
    finished_at: dict[str, float] = {}
    series: list[dict] = []
    timed_out = False
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while True:
            open_ids = [cid for cid, st in statuses.items() if st not in FINISHED_STATUSES]
            elapsed = clock() - start
            for cid, st in zip(open_ids, pool.map(lambda c: _fetch_status(session, url, c), open_ids)):
                if st:
                    statuses[cid] = st
                    if st in FINISHED_STATUSES:
                        finished_at[cid] = clock() - start
            workers = sample_workers(redis_client)
            series.append({
                "t": round(elapsed, 3),
                "cases": dict(sorted({s: list(statuses.values()).count(s) for s in set(statuses.values())}.items())),
                "active_total": int(sum(w["active_subtasks"] for w in workers.values())),
                "workers": workers,
            })
            if all(st in FINISHED_STATUSES for st in statuses.values()):
                break
            if clock() - start >= timeout:
                timed_out = True
                break
            sleep(poll)
    return {"statuses": statuses, "finished_after_s": finished_at, "series": series,
            "timed_out": timed_out, "elapsed_s": clock() - start}


def fetch_reports(session, url: str, case_ids: list[str], concurrency: int = 8) -> dict[str, dict]:
    def one(cid: str):
        try:
            resp = session.get(f"{url}/cases/{cid}/report", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("sin reporte para %s: %s", cid, e)
            return None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return {cid: r for cid, r in zip(case_ids, pool.map(one, case_ids)) if r}


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def compute_metrics(submitted: list[dict], reports: dict[str, dict], mon: dict, wall_s: float) -> dict:
    per_case = []
    status_counts: dict[str, int] = {}
    failures: dict[str, int] = {}
    host_count: dict[str, int] = {}
    host_times: dict[str, list[float]] = {}
    op_times: dict[str, list[float]] = {}
    op_count: dict[str, int] = {}
    done_subtasks = 0
    total_subtasks = 0

    for info in submitted:
        cid = info["case_id"]
        report = reports.get(cid) if cid else None
        status = "submit_error" if not cid else (report or {}).get("status") or mon["statuses"].get(cid, "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        total_subtasks += info["subtasks"]
        duration = None
        if report:
            created, finished = _parse_iso(report.get("created_at")), _parse_iso(report.get("finished_at"))
            if created is not None and finished is not None:
                duration = round(finished - created, 3)
            for etype, n in (report.get("failure_breakdown") or {}).items():
                failures[etype] = failures.get(etype, 0) + n
            for op, entries in (report.get("subtasks_by_operation") or {}).items():
                for st in entries:
                    if st.get("status") in ("completed", "failed"):
                        done_subtasks += 1
                    host = st.get("host")
                    if host:
                        host_count[host] = host_count.get(host, 0) + 1
                    ps = st.get("processing_s")
                    if ps is not None:
                        host_times.setdefault(host or "unknown", []).append(ps)
                        op_times.setdefault(op, []).append(ps)
                    op_count[op] = op_count.get(op, 0) + 1
        if duration is None and cid in mon["finished_after_s"]:
            duration = round(mon["finished_after_s"][cid], 3)
        per_case.append({"name": info["name"], "kind": info["kind"], "case_id": cid, "status": status,
                         "subtasks": info["subtasks"], "duration_s": duration, "error": info["error"]})

    peak_cpu: dict[str, float] = {}
    peak_active: dict[str, int] = {}
    for sample in mon["series"]:
        for w in sample["workers"].values():
            host = w["host"]
            peak_cpu[host] = max(peak_cpu.get(host, 0.0), w["cpu_percent"])
            peak_active[host] = max(peak_active.get(host, 0), int(w["active_subtasks"]))

    durations = [c["duration_s"] for c in per_case if c["duration_s"] is not None]
    return {
        "wall_s": round(wall_s, 3),
        "cases": len(submitted),
        "subtasks_total": total_subtasks,
        "subtasks_done": done_subtasks,
        "throughput_subtasks_per_min": round(done_subtasks / (wall_s / 60), 2) if wall_s > 0 else 0.0,
        "case_status_counts": dict(sorted(status_counts.items())),
        "case_duration_avg_s": _avg(durations),
        "case_duration_max_s": max(durations) if durations else 0.0,
        "failure_breakdown": dict(sorted(failures.items())),
        "subtasks_per_host": dict(sorted(host_count.items())),
        "avg_processing_s_by_host": {h: _avg(v) for h, v in sorted(host_times.items())},
        "avg_processing_s_by_operation": {o: _avg(v) for o, v in sorted(op_times.items())},
        "subtasks_per_operation": dict(sorted(op_count.items())),
        "max_concurrent_active": max((s["active_total"] for s in mon["series"]), default=0),
        "peak_cpu_by_host": {h: round(v, 1) for h, v in sorted(peak_cpu.items())},
        "peak_active_by_host": dict(sorted(peak_active.items())),
        "per_case": per_case,
    }


def _table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def render_md(config: dict, m: dict, timed_out: bool, workers_seen: bool) -> str:
    lines = ["# Prueba de carga del dataset", "", "## Configuracion", ""]
    lines += _table(["Parametro", "Valor"], [[k, v] for k, v in config.items()])
    lines += ["## Totales", ""]
    lines += _table(["Metrica", "Valor"], [
        ["Resultado", "TIMEOUT (casos sin terminar)" if timed_out else "todos los casos terminaron"],
        ["Tiempo total (s)", m["wall_s"]], ["Casos", m["cases"]],
        ["Sub-tareas enviadas", m["subtasks_total"]], ["Sub-tareas finalizadas", m["subtasks_done"]],
        ["Throughput (sub-tareas/min)", m["throughput_subtasks_per_min"]],
        ["Duracion promedio por caso (s)", m["case_duration_avg_s"]],
        ["Duracion maxima por caso (s)", m["case_duration_max_s"]],
    ])
    lines += ["### Estado de los casos", ""]
    lines += _table(["Estado", "Casos"], [[k, v] for k, v in m["case_status_counts"].items()])
    lines += ["## Distribucion por host", ""]
    lines += _table(["Host", "Sub-tareas", "Prom. procesamiento (s)"], [
        [h, n, m["avg_processing_s_by_host"].get(h, "-")] for h, n in m["subtasks_per_host"].items()])
    lines += ["## Promedios por operacion", ""]
    lines += _table(["Operacion", "Sub-tareas", "Prom. procesamiento (s)"], [
        [o, m["subtasks_per_operation"].get(o, 0), v] for o, v in m["avg_processing_s_by_operation"].items()])
    lines += ["## Fallos por tipo de error", ""]
    lines += _table(["error_type", "Cantidad"], [[k, v] for k, v in m["failure_breakdown"].items()]) \
        if m["failure_breakdown"] else ["Sin fallos.", ""]
    lines += ["## Saturacion", ""]
    if workers_seen:
        lines += [f"- Maximo de sub-tareas activas simultaneas: {m['max_concurrent_active']}", ""]
        lines += _table(["Host", "Pico CPU (%)", "Pico sub-tareas activas"], [
            [h, v, m["peak_active_by_host"].get(h, 0)] for h, v in m["peak_cpu_by_host"].items()])
    else:
        lines += ["No se obtuvieron heartbeats de Redis; sin datos de saturacion.", ""]
    return "\n".join(lines)


def run_load(args, settings: Settings, session=requests, storage=None, redis_client=None,
             sleep=time.sleep, clock=time.monotonic, stamp: str | None = None) -> tuple[int, Path]:
    dataset = Path(args.dataset)
    manifest, cases_doc = load_dataset(dataset)

    if args.upload:
        storage = storage or Storage(settings)
        stats = upload_dataset(storage, manifest, dataset / "media", settings.dataset_bucket, args.concurrency)
        logger.info("subida terminada: %s", stats)

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None
    cases = select_cases(cases_doc, kinds, args.limit)
    logger.info("enviando %d casos (concurrencia %d)", len(cases), args.concurrency)
    redis_conn = connect_redis(settings, redis_client)

    start = clock()
    submitted = submit_all(session, settings.coordinator_url, cases, args.concurrency)
    mon = monitor(session, settings.coordinator_url, submitted, redis_conn, args.poll, args.timeout,
                  args.concurrency, sleep, clock)
    wall = clock() - start
    ids = [s["case_id"] for s in submitted if s["case_id"]]
    reports = fetch_reports(session, settings.coordinator_url, ids, args.concurrency)
    metrics = compute_metrics(submitted, reports, mon, wall)

    config = {"dataset": str(dataset), "coordinador": settings.coordinator_url, "casos": len(cases),
              "concurrencia": args.concurrency, "tipos": args.kinds or "todos", "limite": args.limit or "sin limite",
              "poll (s)": args.poll, "timeout (s)": args.timeout, "subida": "si" if args.upload else "no"}
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"carga_{stamp}.md"
    workers_seen = any(s["workers"] for s in mon["series"])
    (out / f"carga_{stamp}.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(), "config": config, "timed_out": mon["timed_out"],
        "metrics": metrics, "series": mon["series"], "reports": reports}, indent=2, ensure_ascii=False))
    md_path.write_text(render_md(config, metrics, mon["timed_out"], workers_seen))
    return (2 if mon["timed_out"] else 0), md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube el dataset, envia casos y mide la carga.")
    parser.add_argument("--dataset", type=Path, default=Path("dataset"))
    parser.add_argument("--upload", action="store_true", help="sube los archivos a MinIO antes de enviar")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--kinds", default="homogeneous,heterogeneous")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--out", type=Path, default=Path("docs/evidencia"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    code, md_path = run_load(args, Settings.from_env())
    print(md_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
