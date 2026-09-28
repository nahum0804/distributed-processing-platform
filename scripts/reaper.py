"""Recovers subtasks orphaned by dead or stuck workers and requeues or fails them."""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("assigned", "running")
TERMINAL_STATES = ("completed", "failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Reaper:
    def __init__(self, settings, redis_client, reporter, clock=time.time):
        self.settings = settings
        self.redis = redis_client
        self.reporter = reporter
        self.clock = clock

    def run_once(self) -> dict:
        counts = {"requeued": 0, "failed": 0, "workers_removed": 0, "cleaned": 0}
        try:
            worker_ids = list(self.redis.smembers("workers:registry"))
        except Exception:
            logger.exception("no se pudo leer workers:registry")
            return counts

        for wid in worker_ids:
            try:
                self._process_worker(wid, counts)
            except Exception:
                logger.exception("error procesando worker %s", wid)
        return counts

    def _process_worker(self, wid: str, counts: dict) -> None:
        inflight_key = f"worker:{wid}:inflight"
        alive = bool(self.redis.exists(f"worker:{wid}"))
        try:
            sids = list(self.redis.smembers(inflight_key))
        except Exception:
            logger.exception("no se pudo leer %s", inflight_key)
            sids = []

        if not alive:
            for sid in sids:
                try:
                    self._recover(sid, "worker_lost", counts)
                except Exception:
                    logger.exception("error recuperando subtarea %s del worker muerto %s", sid, wid)
            try:
                self.redis.delete(inflight_key)
                self.redis.srem("workers:registry", wid)
                counts["workers_removed"] += 1
            except Exception:
                logger.exception("no se pudo limpiar registro del worker %s", wid)
            return

        for sid in sids:
            try:
                self._process_inflight_sid(sid, inflight_key, counts)
            except Exception:
                logger.exception("error procesando subtarea %s del worker %s", sid, wid)

    def _process_inflight_sid(self, sid: str, inflight_key: str, counts: dict) -> None:
        data = self.redis.hgetall(f"subtask:{sid}")
        status = data.get("status") if data else None
        if not data or status not in ACTIVE_STATES:
            self.redis.srem(inflight_key, sid)
            counts["cleaned"] += 1
            return

        assigned_ts = data.get("assigned_ts")
        expired = False
        if assigned_ts:
            try:
                expired = (self.clock() - float(assigned_ts)) > self.settings.reaper_max_age
            except (TypeError, ValueError):
                logger.warning("assigned_ts inválido para subtarea %s: %r", sid, assigned_ts)
        else:
            logger.warning("assigned_ts ausente para subtarea %s", sid)

        if expired:
            self._recover(sid, "max_age", counts)
            self.redis.srem(inflight_key, sid)

    def _recover(self, sid: str, reason: str, counts: dict) -> None:
        data = self.redis.hgetall(f"subtask:{sid}")
        status = data.get("status") if data else None
        if not data or status not in ACTIVE_STATES:
            counts["cleaned"] += 1
            return

        attempts = int(data.get("attempts") or 0)
        operation = data.get("operation")

        if attempts < self.settings.max_attempts:
            if not operation:
                logger.error("subtarea %s sin operation, no se puede reencolar", sid)
                return
            try:
                pipe = self.redis.pipeline()
                pipe.hset(f"subtask:{sid}", mapping={
                    "status": "queued",
                    "worker_id": "",
                    "progress": 0,
                    "requeued_at": _now_iso(),
                    "requeue_reason": reason,
                })
                pipe.lpush(f"queue:{operation}", sid)
                pipe.execute()
                counts["requeued"] += 1
                logger.warning(
                    "subtarea %s (operation=%s) reencolada tras %d intentos (%s)",
                    sid, operation, attempts, reason,
                )
            except Exception:
                logger.exception("no se pudo reencolar subtarea %s", sid)
            return

        payload = {
            "subtask_id": sid,
            "case_id": data.get("case_id"),
            "worker_id": data.get("worker_id") or "",
            "host": "reaper",
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
            self.redis.hset(f"subtask:{sid}", "requeue_reason", reason)
        except Exception:
            logger.exception("no se pudo anotar requeue_reason en subtarea %s", sid)

        try:
            self.reporter.report(payload)
        except Exception:
            logger.exception("no se pudo reportar fallo de subtarea %s", sid)
        finally:
            counts["failed"] += 1
            logger.warning("subtarea %s marcada como fallida tras %d intentos (%s)", sid, attempts, reason)

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("error en ciclo del reaper")
            try:
                self.reporter.flush_pending()
            except Exception:
                logger.exception("error al vaciar reportes pendientes")
            stop_event.wait(self.settings.reaper_interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from src.workers.config import Settings, make_redis
    from src.workers.reporter import Reporter

    settings = Settings.from_env()
    redis_client = make_redis(settings)
    reporter = Reporter(settings, redis_client)
    reaper = Reaper(settings, redis_client, reporter)

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    reaper.run_forever(stop_event)


if __name__ == "__main__":
    main()
