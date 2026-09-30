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
RETRYABLE_CASE_STATES = ("queued", "processing", "retrying")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _requeue(redis_client, sid: str, case_id: str | None, operation: str, priority: str | None, reason: str) -> str:
    """Requeues the subtask and flags its case as retrying in one WATCH/MULTI transaction."""
    subtask_key = f"subtask:{sid}"
    case_key = f"case:{case_id or ''}"
    queue_key = f"queue:{operation}:high" if priority == "high" else f"queue:{operation}"

    def txn(pipe):
        case_status = pipe.hget(case_key, "status") if case_id else None
        pipe.multi()
        if case_status == "cancelled":
            pipe.hset(subtask_key, "status", "cancelled")
            return "cleaned"
        pipe.hset(subtask_key, mapping={
            "status": "pending",
            "worker_id": "",
            "progress": 0,
            "requeued_at": _now_iso(),
            "requeue_reason": reason,
        })
        pipe.lpush(queue_key, sid)
        if case_status in RETRYABLE_CASE_STATES:
            pipe.hset(case_key, "status", "retrying")
            pipe.hincrby(case_key, "retries", 1)
        return "requeued"

    return redis_client.transaction(txn, case_key, value_from_callable=True)


def recover_subtask(redis_client, settings, reporter, sid: str, reason: str, reporter_host: str = "reaper") -> str:
    """Requeues or fails an orphaned subtask, returning "requeued" | "failed" | "cleaned" | "skipped"."""
    data = redis_client.hgetall(f"subtask:{sid}")
    status = data.get("status") if data else None
    if not data or status not in ACTIVE_STATES:
        return "cleaned"

    case_id = data.get("case_id")
    if case_id and redis_client.hget(f"case:{case_id}", "status") == "cancelled":
        redis_client.hset(f"subtask:{sid}", "status", "cancelled")
        logger.info("caso %s cancelado, subtarea %s marcada cancelada (%s)", case_id, sid, reason)
        return "cleaned"

    attempts = int(data.get("attempts") or 0)
    operation = data.get("operation")

    if attempts < settings.max_attempts:
        if not operation:
            logger.error("subtarea %s sin operation, no se puede reencolar", sid)
            return "skipped"
        try:
            outcome = _requeue(redis_client, sid, case_id, operation, data.get("priority"), reason)
        except Exception:
            logger.exception("no se pudo reencolar subtarea %s", sid)
            return "skipped"
        if outcome == "requeued":
            logger.warning(
                "subtarea %s (operation=%s) reencolada tras %d intentos (%s)",
                sid, operation, attempts, reason,
            )
        else:
            logger.info("caso %s cancelado, subtarea %s marcada cancelada (%s)", case_id, sid, reason)
        return outcome

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
