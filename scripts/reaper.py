"""Recovers subtasks orphaned by dead or stuck workers and requeues or fails them."""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timezone

from src.workers.recovery import ACTIVE_STATES, recover_subtask

logger = logging.getLogger(__name__)

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
        result = recover_subtask(self.redis, self.settings, self.reporter, sid, reason)
        if result in ("requeued", "failed", "cleaned"):
            counts[result] += 1

    def run_forever(self, stop_event: threading.Event) -> None:
        logger.info(
            "Reaper iniciado: intervalo=%ss max_age=%ss max_attempts=%s",
            self.settings.reaper_interval, self.settings.reaper_max_age, self.settings.max_attempts,
        )
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
