"""
Coordinator service — Plataforma Distribuida de Procesamiento Multimedia
========================================================================
Persona 1 · Coordinador y Orquestador  (v4.0)

Responsabilidades
-----------------
* Recibir casos (POST /cases), validar/resolver la operación de cada sub-tarea
  (router.py; "auto" → por extensión) y encolarlas en Redis de forma atómica,
  en queue:{op} o queue:{op}:high según la prioridad del caso.
* Barrier/Join: POST /subtasks/report es idempotente (SADD en case:{id}:done)
  y cierra el caso cuando pending_subtasks llega a 0.
* Consultas para dashboard/clientes: GET /cases[?status=], /cases/{id},
  /cases/{id}/report, /subtasks/{id}, /workers, /stats.

Novedades v4 (sobre v3)
-----------------------
  1. priority ("normal" | "high") por caso → colas queue:{op}:high, que los
     workers consumen antes que las normales.
  2. metadata libre por caso y por sub-tarea (se guarda como JSON string).
  3. Estados de caso: queued → processing → retrying → completed |
     partially_completed | failed | cancelled. El coordinador crea "queued" y
     fija los terminales; el worker pasa a "processing" al tomar la primera
     sub-tarea; el reaper marca "retrying" y suma case.retries al reencolar.
  4. POST /cases/{id}/cancel (409 si el caso ya es terminal; un caso cancelado
     nunca cambia de estado).
  5. GET /subtasks/{id}, GET /workers (heartbeats, campo alive), GET /stats
     (colas, casos por estado, workers vivos) y GET /cases?status=.
  6. El reporte añade priority, metadata, retries, subtasks_by_type y
     totals_by_type_and_operation.

Contratos respetados
--------------------
* Workers (src/workers): payload JSON completo en la cola; hash subtask:{id}
  con 'operation', 'file_path', 'params', 'priority'.
* Reporter (worker_node.py): SubtaskReport acepta todos los campos del worker.
* Detalle de claves y estados: docs/WORKER_CONTRACT.md.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

import redis as redis_lib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.coordinator.router import VALID_TASK_TYPES, classify, queue_key, resolve_task_type

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection — exclusively from environment variables
# ---------------------------------------------------------------------------
_REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
_REDIS_PASSWORD: Optional[str] = os.getenv("REDIS_PASSWORD") or None  # "" → None

redis_client: redis_lib.Redis = redis_lib.Redis(
    host=_REDIS_HOST,
    port=_REDIS_PORT,
    password=_REDIS_PASSWORD,
    decode_responses=True,
    socket_keepalive=True,
    health_check_interval=30,
)

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Nodo Coordinador — Plataforma Distribuida de Procesamiento Multimedia",
    version="4.0.0",
    description=(
        "Orquesta casos de procesamiento multimedia, encola sub-tareas en Redis "
        "y consolida resultados mediante el patrón Barrier/Join."
    ),
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class SubtaskRequest(BaseModel):
    """
    Description of a single sub-task inside a case creation request.

    ``task_type`` puede ser cualquiera de las 5 operaciones válidas,
    ``"auto"`` (detección por extensión) o ``None`` (equivalente a auto).
    """

    task_type: Optional[str] = "auto"   # "auto" → router elige por extensión
    file_path: str                       # MinIO object key
    params: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class CaseRequest(BaseModel):
    """Payload for POST /cases."""

    case_id: Optional[str] = None       # auto-generated if not provided
    subtasks: List[SubtaskRequest]
    priority: Literal["normal", "high"] = "normal"
    metadata: Optional[dict[str, Any]] = None


class SubtaskReport(BaseModel):
    """
    Extended report model sent by workers via POST /subtasks/report.
    Exactly matches the payload built in worker_node.py → Worker.handle_subtask().
    """

    subtask_id: str
    case_id: str
    status: str                          # "completed" | "failed"
    worker_id: Optional[str] = None
    host: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    processing_s: Optional[float] = None
    media_duration_s: Optional[float] = None
    output_bytes: Optional[int] = None
    attempts: Optional[int] = None
    outputs: Optional[List[str]] = None  # MinIO result keys
    error: Optional[str] = None
    error_type: Optional[str] = None
    encoder: Optional[str] = None        # FFmpeg encoder used (libx264, h264_nvenc...)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str(value: Any) -> str:
    """Safely coerce any value to string; None → empty string (Redis rejects None)."""
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# POST /cases — atomic case creation with transaction pipeline
# ---------------------------------------------------------------------------
@app.post("/cases", status_code=201, response_model=dict, tags=["Cases"])
@app.post("/cases/", status_code=201, response_model=dict, tags=["Cases"], include_in_schema=False)
def create_case(case_req: CaseRequest) -> dict:
    """
    Create a processing case and enqueue each sub-task atomically.

    Fix 1  — task_type validated / auto-resolved BEFORE any Redis write (fail-fast).
    Fix 2  — uses pipeline(transaction=True): the entire case + subtask creation
             is one atomic Redis transaction. No orphan cases on partial failure.
    Fix 3  — ``RPUSH case:{id}:subtasks <sid>`` persists the subtask list so
             the dashboard and report endpoints don't need SCAN.
    Fix 4  — auto-routing via router.resolve_task_type() (rubric requirement).
    """
    if not case_req.subtasks:
        raise HTTPException(status_code=422, detail="El caso debe tener al menos una sub-tarea.")

    # --- resolve + validate all task types before touching Redis ---
    priority = case_req.priority
    resolved: list[tuple[SubtaskRequest, str, str]] = []  # (st, resolved_type, queue)
    for st in case_req.subtasks:
        op = resolve_task_type(st.task_type, st.file_path)  # raises 422 on invalid
        resolved.append((st, op, queue_key(op, priority)))

    case_id: str = case_req.case_id or str(uuid.uuid4())
    total: int = len(case_req.subtasks)
    created_at: str = _now_iso()
    case_metadata_json = json.dumps(case_req.metadata or {})
    case_key = f"case:{case_id}"
    case_subtasks_key = f"case:{case_id}:subtasks"
    done_set_key = f"case:{case_id}:done"

    subtask_ids: list[str] = []
    subtask_id_list: list[str] = []

    # Pre-generate IDs and payloads outside the pipeline
    entries: list[tuple[str, str, str, SubtaskRequest, str, str, str]] = []
    for st, op, q in resolved:
        sid = str(uuid.uuid4())
        params_json = json.dumps(st.params or {})
        metadata_json = json.dumps(st.metadata or {})
        queue_payload = json.dumps({
            "subtask_id": sid,
            "case_id": case_id,
            "task_type": op,
            "file_path": st.file_path,
            "params": st.params or {},
            "priority": priority,
        })
        entries.append((sid, op, q, st, params_json, metadata_json, queue_payload))  # type: ignore[arg-type]
        subtask_id_list.append(sid)

    # --- atomic transaction ---
    pipe = redis_client.pipeline(transaction=True)

    # Case metadata hash
    pipe.hset(case_key, mapping={
        "case_id": case_id,
        "status": "queued",
        "total_subtasks": total,
        "pending_subtasks": total,
        "created_at": created_at,
        "priority": priority,
        "metadata": case_metadata_json,
        "retries": "0",
    })
    # Register in global set
    pipe.sadd("cases:registry", case_id)
    # Pre-create done set as empty (ensures key exists for SADD checks)
    # We don't need to create it explicitly; SADD will create it on first use.

    for sid, op, q, st, params_json, metadata_json, queue_payload in entries:
        # Subtask hash (fields read by the worker)
        pipe.hset(f"subtask:{sid}", mapping={
            "subtask_id": sid,
            "case_id": case_id,
            "operation": op,       # field the worker reads
            "task_type": op,       # coordinator-side query field
            "file_path": st.file_path,
            "status": "pending",
            "params": params_json,
            "metadata": metadata_json,
            "priority": priority,
        })
        # [FIX 3] Append subtask ID to per-case list (O(1) lookup, no SCAN needed)
        pipe.rpush(case_subtasks_key, sid)
        # Enqueue full JSON payload
        pipe.rpush(q, queue_payload)
        subtask_ids.append(sid)

    pipe.execute()  # atomic: all or nothing

    logger.info(
        "Caso creado: %s | subtareas: %d | ops: %s",
        case_id, total, list({op for _, op, _ in resolved})
    )

    return {
        "case_id": case_id,
        "status": "queued",
        "priority": priority,
        "total_subtasks": total,
        "subtask_ids": subtask_ids,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# POST /subtasks/report — atomic-idempotent reporting + Barrier/Join
# ---------------------------------------------------------------------------
@app.post("/subtasks/report", status_code=200, response_model=dict, tags=["Subtasks"])
def report_subtask(report: SubtaskReport) -> dict:
    """
    Receive a worker result report for a sub-task.

    Idempotency (FIX 1 — atomic via SADD)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``SADD case:{id}:done <subtask_id>`` returns 1 only the FIRST time.
    On retry (worker didn't get our response) it returns 0 → early return,
    no counter decrement. This prevents double-counting and premature close.

    Barrier / Join
    ~~~~~~~~~~~~~~
    When ``pending_subtasks`` reaches ≤ 0 all subtasks have reported.
    Failure detection uses the per-case subtask list (no SCAN).
    """
    subtask_id = report.subtask_id
    case_id = report.case_id
    subtask_key = f"subtask:{subtask_id}"
    case_key = f"case:{case_id}"
    done_set_key = f"case:{case_id}:done"

    # --- guard: subtask must exist ---
    if not redis_client.exists(subtask_key):
        raise HTTPException(status_code=404, detail=f"Sub-tarea '{subtask_id}' no encontrada.")

    # --- [FIX 1] atomic idempotency via SADD ---
    added = redis_client.sadd(done_set_key, subtask_id)
    if added == 0:
        # Already processed — return current case status without touching counters
        current_case_status = redis_client.hget(case_key, "status") or "unknown"
        logger.info(
            "Reporte duplicado ignorado (SADD=0): subtask=%s case=%s",
            subtask_id, case_id,
        )
        return {
            "message": "Reporte ignorado por idempotencia",
            "subtask_id": subtask_id,
            "case_id": case_id,
            "case_status": current_case_status,
        }

    # --- validate reported status ---
    if report.status not in ("completed", "failed"):
        # Undo the sadd so the report can be retried with a correct status
        redis_client.srem(done_set_key, subtask_id)
        raise HTTPException(
            status_code=422,
            detail=(
                f"El campo 'status' debe ser 'completed' o 'failed'. "
                f"Recibido: '{report.status}'"
            ),
        )

    # --- update subtask hash with ALL worker metrics (None → "") ---
    subtask_updates: dict[str, str] = {
        "status": report.status,
        "worker_id": _str(report.worker_id),
        "host": _str(report.host),
        "started_at": _str(report.started_at),
        "finished_at": _str(report.finished_at),
        "processing_s": _str(report.processing_s),
        "media_duration_s": _str(report.media_duration_s),
        "output_bytes": _str(report.output_bytes) if report.output_bytes is not None else "0",
        "attempts": _str(report.attempts),
        "outputs": json.dumps(report.outputs or []),   # list → JSON string
        "error": _str(report.error),
        "error_type": _str(report.error_type),
        "encoder": _str(report.encoder),
    }
    redis_client.hset(subtask_key, mapping=subtask_updates)

    # --- decrement pending counter ---
    remaining = redis_client.hincrby(case_key, "pending_subtasks", -1)
    case_status = redis_client.hget(case_key, "status")

    # --- Barrier / Join (un caso cancelado nunca cambia de estado) ---
    if remaining <= 0 and case_status != "cancelled":
        final_status = _final_case_status(case_id)
        redis_client.hset(case_key, mapping={
            "status": final_status,
            "finished_at": _now_iso(),
        })
        case_status = final_status
        logger.info(
            "Caso %s finalizado → %s (pending=%d)", case_id, final_status, remaining
        )

    return {
        "message": "Reporte procesado correctamente",
        "subtask_id": subtask_id,
        "case_id": case_id,
        "subtask_status": report.status,
        "case_status": case_status,
        "pending_subtasks": remaining,
    }


def _subtask_statuses(case_id: str) -> list[str | None]:
    """Statuses of every subtask of a case using the per-case list (no SCAN)."""
    subtask_ids = redis_client.lrange(f"case:{case_id}:subtasks", 0, -1)
    pipe = redis_client.pipeline()
    for sid in subtask_ids:
        pipe.hget(f"subtask:{sid}", "status")
    return pipe.execute()


def _final_case_status(case_id: str) -> str:
    """
    completed → sin fallidas; failed → todas fallidas;
    partially_completed → al menos una fallida y al menos una completada.
    """
    statuses = _subtask_statuses(case_id)
    failed = sum(1 for s in statuses if s == "failed")
    completed = sum(1 for s in statuses if s == "completed")
    if failed == 0:
        return "completed"
    if completed == 0:
        return "failed"
    return "partially_completed"


_TERMINAL_CASE_STATUSES = frozenset(
    {"completed", "partially_completed", "failed", "cancelled"}
)


# ---------------------------------------------------------------------------
# POST /cases/{case_id}/cancel
# ---------------------------------------------------------------------------
@app.post("/cases/{case_id}/cancel", status_code=200, response_model=dict, tags=["Cases"])
def cancel_case(case_id: str) -> dict:
    """
    Cancel a case. Pending subtasks are marked ``cancelled`` and counted as done
    (SADD + decrement of ``pending_subtasks``); assigned/running subtasks keep
    running and workers skip cancelled subtasks still sitting in the queues.
    """
    case_key = f"case:{case_id}"
    status = redis_client.hget(case_key, "status")
    if status is None:
        raise HTTPException(status_code=404, detail=f"Caso '{case_id}' no encontrado.")
    if status in _TERMINAL_CASE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"El caso '{case_id}' ya está en estado terminal: '{status}'.",
        )

    now = _now_iso()
    redis_client.hset(case_key, mapping={
        "status": "cancelled",
        "cancelled_at": now,
        "finished_at": now,
    })

    done_set_key = f"case:{case_id}:done"
    subtask_ids = redis_client.lrange(f"case:{case_id}:subtasks", 0, -1)
    pipe = redis_client.pipeline()
    for sid in subtask_ids:
        pipe.hget(f"subtask:{sid}", "status")
    statuses = pipe.execute()

    cancelled = 0
    running = 0
    for sid, st_status in zip(subtask_ids, statuses):
        if st_status == "pending":
            if redis_client.sadd(done_set_key, sid):
                redis_client.hset(f"subtask:{sid}", "status", "cancelled")
                cancelled += 1
        elif st_status not in ("completed", "failed", "cancelled", None):
            running += 1
    if cancelled:
        redis_client.hincrby(case_key, "pending_subtasks", -cancelled)

    logger.info(
        "Caso %s cancelado | canceladas: %d | en ejecución: %d",
        case_id, cancelled, running,
    )
    return {
        "case_id": case_id,
        "status": "cancelled",
        "cancelled_subtasks": cancelled,
        "running_subtasks": running,
    }


# ---------------------------------------------------------------------------
# GET /cases — list all registered cases
# ---------------------------------------------------------------------------
@app.get("/cases", response_model=list, tags=["Dashboard"])
@app.get("/cases/", response_model=list, tags=["Dashboard"], include_in_schema=False)
def list_cases(status: Optional[str] = None) -> list:
    """Return metadata for every registered case, newest first (optional ?status= filter)."""
    case_ids: set[str] = redis_client.smembers("cases:registry")
    result = []
    for cid in case_ids:
        data = redis_client.hgetall(f"case:{cid}")
        if data and (status is None or data.get("status") == status):
            result.append(data)
    result.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return result


# ---------------------------------------------------------------------------
# GET /cases/{case_id} — detailed case view
# ---------------------------------------------------------------------------
@app.get("/cases/{case_id}", response_model=dict, tags=["Dashboard"])
def get_case(case_id: str) -> dict:
    """
    Return full case metadata together with the detailed state and metrics
    for every associated sub-task, using the per-case subtask list (no SCAN).
    """
    case_data = redis_client.hgetall(f"case:{case_id}")
    if not case_data:
        raise HTTPException(status_code=404, detail=f"Caso '{case_id}' no encontrado.")

    subtasks = _fetch_subtasks_fast(case_id)
    return {"case": case_data, "subtasks": subtasks}


# ---------------------------------------------------------------------------
# GET /cases/{case_id}/report — consolidated report (rubric item 6)
# ---------------------------------------------------------------------------
@app.get("/cases/{case_id}/report", response_model=dict, tags=["Dashboard"])
def get_case_report(case_id: str) -> dict:
    """
    Consolidated report for a case, grouped by operation type.

    Returns
    -------
    For each operation type present in the case:
      - list of sub-tasks with file_path, status, worker, host,
        started_at, finished_at, processing_s, media_duration_s,
        output_bytes, outputs, error, error_type
    Plus a top-level summary:
      - total, completed, failed counts
      - breakdown of failures by error_type
      - average processing_s per operation + per host
    """
    case_data = redis_client.hgetall(f"case:{case_id}")
    if not case_data:
        raise HTTPException(status_code=404, detail=f"Caso '{case_id}' no encontrado.")

    subtasks = _fetch_subtasks_fast(case_id)

    # --- group subtasks by operation ---
    by_operation: dict[str, list[dict]] = defaultdict(list)
    total_ok = 0
    total_fail = 0
    failure_types: dict[str, int] = defaultdict(int)
    processing_by_op: dict[str, list[float]] = defaultdict(list)
    processing_by_host: dict[str, list[float]] = defaultdict(list)
    by_type: dict[str, list[dict]] = defaultdict(list)
    totals_by_type_op: dict[str, dict[str, dict[str, int]]] = {}

    for st in subtasks:
        op = st.get("task_type") or st.get("operation", "unknown")
        status = st.get("status", "")
        entry = {
            "subtask_id": st.get("subtask_id"),
            "file_path": st.get("file_path"),
            "status": status,
            "worker_id": st.get("worker_id") or None,
            "host": st.get("host") or None,
            "started_at": st.get("started_at") or None,
            "finished_at": st.get("finished_at") or None,
            "processing_s": _float_or_none(st.get("processing_s")),
            "media_duration_s": _float_or_none(st.get("media_duration_s")),
            "output_bytes": _int_or_none(st.get("output_bytes")),
            "outputs": _json_list(st.get("outputs")),
            "error": st.get("error") or None,
            "error_type": st.get("error_type") or None,
            "attempts": _int_or_none(st.get("attempts")),
            "encoder": st.get("encoder") or None,
            "metadata": _json_dict(st.get("metadata")),
            "priority": st.get("priority") or None,
        }
        file_type = classify(st.get("file_path") or "")
        entry["file_type"] = file_type
        by_operation[op].append(entry)
        by_type[file_type].append(entry)
        bucket = totals_by_type_op.setdefault(file_type, {}).setdefault(
            op, {"completed": 0, "failed": 0, "other": 0}
        )
        bucket[status if status in ("completed", "failed") else "other"] += 1

        if status == "completed":
            total_ok += 1
        elif status == "failed":
            total_fail += 1
            etype = st.get("error_type") or "UnknownError"
            failure_types[etype] += 1

        ps = _float_or_none(st.get("processing_s"))
        if ps is not None:
            processing_by_op[op].append(ps)
            host = st.get("host") or "unknown"
            processing_by_host[host].append(ps)

    # --- build summary ---
    total = int(case_data.get("total_subtasks", 0))
    pending = int(case_data.get("pending_subtasks", 0))

    avg_by_op = {op: round(sum(v) / len(v), 3) for op, v in processing_by_op.items() if v}
    avg_by_host = {h: round(sum(v) / len(v), 3) for h, v in processing_by_host.items() if v}

    summary_text_parts = []
    if total_ok:
        summary_text_parts.append(f"{total_ok} ok")
    if total_fail:
        ft_str = ", ".join(f"{v} {k}" for k, v in failure_types.items())
        summary_text_parts.append(f"{total_fail} fallida(s) ({ft_str})")
    if pending > 0:
        summary_text_parts.append(f"{pending} pendiente(s)")
    summary_text = "; ".join(summary_text_parts) or "sin sub-tareas"

    return {
        "case_id": case_id,
        "status": case_data.get("status"),
        "created_at": case_data.get("created_at"),
        "finished_at": case_data.get("finished_at"),
        "priority": case_data.get("priority") or "normal",
        "metadata": _json_dict(case_data.get("metadata")),
        "retries": _int_or_none(case_data.get("retries")) or 0,
        "summary": summary_text,
        "totals": {
            "total": total,
            "completed": total_ok,
            "failed": total_fail,
            "pending": pending,
        },
        "failure_breakdown": dict(failure_types),
        "avg_processing_s_by_operation": avg_by_op,
        "avg_processing_s_by_host": avg_by_host,
        "subtasks_by_operation": dict(by_operation),
        "subtasks_by_type": dict(by_type),
        "totals_by_type_and_operation": totals_by_type_op,
    }


# ---------------------------------------------------------------------------
# GET /subtasks/{subtask_id}, /workers, /stats — dashboard / test client
# ---------------------------------------------------------------------------
@app.get("/subtasks/{subtask_id}", response_model=dict, tags=["Dashboard"])
def get_subtask(subtask_id: str) -> dict:
    """Return the subtask hash with outputs/params/metadata parsed to JSON."""
    data = redis_client.hgetall(f"subtask:{subtask_id}")
    if not data:
        raise HTTPException(status_code=404, detail=f"Sub-tarea '{subtask_id}' no encontrada.")
    data["outputs"] = _json_list(data.get("outputs"))
    data["params"] = _json_dict(data.get("params"))
    data["metadata"] = _json_dict(data.get("metadata"))
    return data


# ---------------------------------------------------------------------------
# Redis error helpers
# ---------------------------------------------------------------------------
def _redis_unavailable(e: Exception) -> HTTPException:
    """Convert a Redis connection error into a clean HTTP 503."""
    logger.error("Redis no disponible: %s", e)
    return HTTPException(
        status_code=503,
        detail=(
            f"Redis no está disponible ({_REDIS_HOST}:{_REDIS_PORT}). "
            "Inicia Redis antes de usar el coordinador. "
            f"Detalle: {e}"
        ),
    )


def _worker_entries() -> list[dict]:
    worker_ids = sorted(redis_client.smembers("workers:registry"))
    entries = []
    for wid in worker_ids:
        heartbeat = redis_client.hgetall(f"worker:{wid}")
        entries.append({**heartbeat, "worker_id": wid, "alive": bool(heartbeat)})
    return entries


# ---------------------------------------------------------------------------
# GET /health — diagnóstico rápido
# ---------------------------------------------------------------------------
@app.get("/health", response_model=dict, tags=["Dashboard"])
def health_check() -> dict:
    """Comprueba que el coordinador puede alcanzar Redis."""
    try:
        redis_client.ping()
        return {"status": "ok", "redis": f"{_REDIS_HOST}:{_REDIS_PORT}"}
    except (redis_lib.exceptions.ConnectionError, redis_lib.exceptions.TimeoutError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"Redis no disponible en {_REDIS_HOST}:{_REDIS_PORT} — {e}",
        )


@app.get("/workers", response_model=list, tags=["Dashboard"])
def list_workers() -> list:
    """Workers from ``workers:registry``; ``alive`` = heartbeat key still exists."""
    try:
        return _worker_entries()
    except redis_lib.exceptions.ConnectionError as e:
        raise _redis_unavailable(e)


@app.get("/stats", response_model=dict, tags=["Dashboard"])
def get_stats() -> dict:
    """Queue lengths, cases by status and worker liveness."""
    try:
        queues: dict[str, int] = {}
        for op in sorted(VALID_TASK_TYPES):
            for key in (queue_key(op), queue_key(op, "high")):
                queues[key] = redis_client.llen(key)

        cases_by_status: dict[str, int] = defaultdict(int)
        for cid in redis_client.smembers("cases:registry"):
            st = redis_client.hget(f"case:{cid}", "status")
            if st:
                cases_by_status[st] += 1

        workers = _worker_entries()
        alive = [w for w in workers if w["alive"]]
        return {
            "queues": queues,
            "cases_by_status": dict(cases_by_status),
            "workers_alive": len(alive),
            "workers_total": len(workers),
            "subtasks_active": sum(_int_or_none(w.get("active_subtasks")) or 0 for w in alive),
        }
    except redis_lib.exceptions.ConnectionError as e:
        raise _redis_unavailable(e)


# ---------------------------------------------------------------------------
# GET /hardware — per-worker hardware snapshot for real-time monitoring
# ---------------------------------------------------------------------------
@app.get("/hardware", response_model=list, tags=["Dashboard"])
def get_hardware() -> list:
    """
    Return a lightweight hardware snapshot for every *alive* worker.

    Fields
    ------
    worker_id, host, ip          — identity
    cpu_percent                  — CPU utilization reported by last heartbeat
    mem_percent                  — RAM utilization reported by last heartbeat
    mem_total_gb                 — total physical RAM in GiB (if published)
    cpu_count                    — logical CPU count (if published)
    gpu                          — GPU model name (\"none\" if CPU-only)
    nvenc_ok                     — \"1\" if h264_nvenc encoder is available
    gpu_percent                  — GPU utilization % (null if unavailable)
    active_subtasks              — in-flight sub-tasks right now
    last_seen                    — ISO timestamp of the last heartbeat
    """
    try:
        workers = _worker_entries()
    except redis_lib.exceptions.ConnectionError:
        return []   # dashboard recibe lista vacía; no crashea
    result = []
    for w in workers:
        if not w.get("alive"):
            continue
        entry: dict[str, Any] = {
            "worker_id":        w.get("worker_id"),
            "host":             w.get("host"),
            "ip":               w.get("ip"),
            "cpu_percent":      _float_or_none(w.get("cpu_percent")),
            "mem_percent":      _float_or_none(w.get("mem_percent")),
            "mem_total_gb":     _float_or_none(w.get("mem_total_gb")),
            "cpu_count":        _int_or_none(w.get("cpu_count")),
            "gpu":              w.get("gpu") or "none",
            "nvenc_ok":         w.get("nvenc_ok", "0"),
            "gpu_percent":      _float_or_none(w.get("gpu_percent")),
            "active_subtasks":  _int_or_none(w.get("active_subtasks")) or 0,
            "last_seen":        w.get("last_seen"),
        }
        result.append(entry)
    return result



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _fetch_subtasks_fast(case_id: str) -> list[dict]:
    """
    Fetch all subtask hashes for a case using the pre-stored list.
    O(N) with one pipeline — no SCAN required.
    """
    subtask_ids = redis_client.lrange(f"case:{case_id}:subtasks", 0, -1)
    if not subtask_ids:
        return []
    pipe = redis_client.pipeline()
    for sid in subtask_ids:
        pipe.hgetall(f"subtask:{sid}")
    results = pipe.execute()
    return [r for r in results if r]


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
