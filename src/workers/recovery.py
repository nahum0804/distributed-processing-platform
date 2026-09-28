"""Recovers a single orphaned subtask by requeuing it or reporting it as failed.

Shared by scripts/reaper.py (recovering subtasks of dead or stuck workers) and
src/workers/worker_node.py (a worker recovering its own leftover inflight subtasks
after restarting with the same worker_id).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("assigned", "running")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def recover_subtask(redis_client, settings, reporter, sid: str, reason: str, reporter_host: str = "reaper") -> str:
    """Requeues or fails an orphaned subtask, returning "requeued" | "failed" | "cleaned" | "skipped"."""
    data = redis_client.hgetall(f"subtask:{sid}")
    status = data.get("status") if data else None
    if not data or status not in ACTIVE_STATES:
        return "cleaned"

    attempts = int(data.get("attempts") or 0)
    operation = data.get("operation")

    if attempts < settings.max_attempts:
        if not operation:
            logger.error("subtarea %s sin operation, no se puede reencolar", sid)
            return "skipped"
        try:
            pipe = redis_client.pipeline()
            pipe.hset(f"subtask:{sid}", mapping={
                "status": "queued",
                "worker_id": "",
                "progress": 0,
                "requeued_at": _now_iso(),
                "requeue_reason": reason,
            })
            pipe.lpush(f"queue:{operation}", sid)
            pipe.execute()
            logger.warning(
                "subtarea %s (operation=%s) reencolada tras %d intentos (%s)",
                sid, operation, attempts, reason,
            )
            return "requeued"
        except Exception:
            logger.exception("no se pudo reencolar subtarea %s", sid)
            return "skipped"

    payload = {
        "subtask_id": sid,
        "case_id": data.get("case_id"),
        "worker_id": data.get("worker_id") or "",
        "host": reporter_host,
        "status": "failed",
        "result_path": "",
        "outputs": [],
        "error": f"Sub-tarea abandonada tras {attempts} intentos ({reason})",
        "error_type": "WorkerLostError",
        "started_at": data.get("started_at") or data.get("assigned_at") or "",
        "finished_at": _now_iso(),
        "processing_s": 0.0,
        "media_duration_s": None,
        "output_bytes": 0,
        "attempts": attempts,
    }
    try:
        redis_client.hset(f"subtask:{sid}", "requeue_reason", reason)
    except Exception:
        logger.exception("no se pudo anotar requeue_reason en subtarea %s", sid)

    try:
        reporter.report(payload)
    except Exception:
        logger.exception("no se pudo reportar fallo de subtarea %s", sid)
    finally:
        logger.warning("subtarea %s marcada como fallida tras %d intentos (%s)", sid, attempts, reason)

    return "failed"
