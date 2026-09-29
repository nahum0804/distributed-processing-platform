"""
Coordinator service — Plataforma Distribuida de Procesamiento Multimedia
========================================================================
Persona 1 · Coordinador y Orquestador  (v3.0)

Cambios v3 (revisión Kenny Rodríguez 27/09/2026)
-------------------------------------------------
BLOQUEANTES resueltos
  1. Idempotencia atómica con SADD en case:{id}:done  — evita doble conteo
     cuando el worker reintenta por pérdida de respuesta HTTP.
  2. Validación 422 de operation en POST /cases via router.resolve_task_type().
     Tipos inválidos → error inmediato; "auto"/None → detección por extensión.
  3. Lista case:{id}:subtasks guardada con RPUSH al crear cada sub-tarea.
  4. Creación atómica con pipeline(transaction=True): si falla a mitad no
     quedan casos huérfanos en estado "processing" con 0 sub-tareas.

RÚBRICA cumplida
  5. Routing por tipo en router.py (classify + resolve_task_type).
  6. GET /cases/{id}/report — reporte consolidado agrupado por operación.
  7. Todos los campos del worker almacenados: error_type, outputs (json.dumps),
     host, started_at, finished_at, processing_s, media_duration_s,
     output_bytes, attempts. None → "".
  8. params opcional por sub-tarea, guardado como JSON string.

Contratos respetados
--------------------
* Persona 2 (Infra/Workers): cola alimentada con JSON payload completo.
* Dev 3 (FFmpeg): hash subtask:{id} expone 'operation', 'file_path', 'params'.
* Reporter (worker_node.py): SubtaskReport acepta todos los campos del worker.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, List, Optional

import redis as redis_lib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.coordinator.router import queue_key, resolve_task_type

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
    version="3.0.0",
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


class CaseRequest(BaseModel):
    """Payload for POST /cases."""

    case_id: Optional[str] = None       # auto-generated if not provided
    subtasks: List[SubtaskRequest]


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
    resolved: list[tuple[SubtaskRequest, str, str]] = []  # (st, resolved_type, queue)
    for st in case_req.subtasks:
        op = resolve_task_type(st.task_type, st.file_path)  # raises 422 on invalid
        resolved.append((st, op, queue_key(op)))

    case_id: str = case_req.case_id or str(uuid.uuid4())
    total: int = len(case_req.subtasks)
    created_at: str = _now_iso()
    case_key = f"case:{case_id}"
    case_subtasks_key = f"case:{case_id}:subtasks"
    done_set_key = f"case:{case_id}:done"

    subtask_ids: list[str] = []
    subtask_id_list: list[str] = []

    # Pre-generate IDs and payloads outside the pipeline
    entries: list[tuple[str, str, str, dict, str]] = []
    for st, op, q in resolved:
        sid = str(uuid.uuid4())
        params_json = json.dumps(st.params or {})
        queue_payload = json.dumps({
            "subtask_id": sid,
            "case_id": case_id,
            "task_type": op,
            "file_path": st.file_path,
            "params": st.params or {},
        })
        entries.append((sid, op, q, st, params_json, queue_payload))  # type: ignore[arg-type]
        subtask_id_list.append(sid)

    # --- atomic transaction ---
    pipe = redis_client.pipeline(transaction=True)

    # Case metadata hash
    pipe.hset(case_key, mapping={
        "case_id": case_id,
        "status": "processing",
        "total_subtasks": total,
        "pending_subtasks": total,
        "created_at": created_at,
    })
    # Register in global set
    pipe.sadd("cases:registry", case_id)
    # Pre-create done set as empty (ensures key exists for SADD checks)
    # We don't need to create it explicitly; SADD will create it on first use.

    for sid, op, q, st, params_json, queue_payload in entries:  # type: ignore[assignment]
        # Subtask hash (fields read by the worker)
        pipe.hset(f"subtask:{sid}", mapping={
            "subtask_id": sid,
            "case_id": case_id,
            "operation": op,       # field the worker reads
            "task_type": op,       # coordinator-side query field
            "file_path": st.file_path,
            "status": "pending",
            "params": params_json,
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
        "status": "processing",
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

    # --- Barrier / Join ---
    if remaining <= 0:
        had_failure = _case_had_failure_fast(case_id)
        final_status = "partially_completed" if had_failure else "completed"
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


def _case_had_failure_fast(case_id: str) -> bool:
    """
    Check for failed subtasks using the per-case list (O(N subtasks), no SCAN).
    Reads case:{id}:subtasks to get IDs, then checks each hash for status='failed'.
    """
    subtask_ids = redis_client.lrange(f"case:{case_id}:subtasks", 0, -1)
    pipe = redis_client.pipeline()
    for sid in subtask_ids:
        pipe.hget(f"subtask:{sid}", "status")
    statuses = pipe.execute()
    return any(s == "failed" for s in statuses)


# ---------------------------------------------------------------------------
# GET /cases — list all registered cases
# ---------------------------------------------------------------------------
@app.get("/cases", response_model=list, tags=["Dashboard"])
@app.get("/cases/", response_model=list, tags=["Dashboard"], include_in_schema=False)
def list_cases() -> list:
    """Return metadata for every registered case, newest first."""
    case_ids: set[str] = redis_client.smembers("cases:registry")
    result = []
    for cid in case_ids:
        data = redis_client.hgetall(f"case:{cid}")
        if data:
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
        }
        by_operation[op].append(entry)

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
    }


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
