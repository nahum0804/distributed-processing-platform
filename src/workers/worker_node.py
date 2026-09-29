from __future__ import annotations

import importlib
import json
import logging
import shutil
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import redis.exceptions

from src.workers.config import Settings, make_redis
from src.workers.heartbeat import Heartbeat, WorkerStats
from src.workers.recovery import ACTIVE_STATES, recover_subtask
from src.workers.reporter import Reporter
from src.workers.storage import Storage, StorageError

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 15.0
MAX_BACKOFF = 30.0


class Worker:
    """Consumes subtasks from Redis queues, runs them through the processor and reports results."""

    def __init__(
        self,
        settings: Settings,
        redis_client: redis.Redis | None = None,
        storage: Storage | None = None,
        processor=None,
        reporter: Reporter | None = None,
        heartbeat_enabled: bool = True,
        ffmpeg_info: tuple[str, str] | None = None,
    ):
        self.settings = settings
        self.redis = redis_client if redis_client is not None else make_redis(settings)
        self.storage = storage if storage is not None else Storage(settings)
        if processor is None:
            processor = importlib.import_module("src.workers.multimedia_processor")
        self.processor = processor
        self.reporter = reporter if reporter is not None else Reporter(settings, self.redis)
        self.heartbeat_enabled = heartbeat_enabled
        self.ffmpeg_info = ffmpeg_info
        self.stats = WorkerStats()
        self.stop_event = threading.Event()
        self.host = settings.node_name
        self.heartbeat: Heartbeat | None = None
        self._consumers: list[threading.Thread] = []
        self._flusher: threading.Thread | None = None
        self._inflight_key = f"worker:{settings.worker_id}:inflight"

    def start(self) -> None:
        self.recover_own_inflight()

        try:
            self.storage.ensure_buckets()
        except StorageError as e:
            logger.warning("no se pudieron asegurar los buckets: %s", e)

        logger.info(
            "Worker %s iniciado en %s: colas=%s concurrencia=%d hilos_ffmpeg=%d redis=%s:%s coordinador=%s",
            self.settings.worker_id, self.settings.node_name, ",".join(self.settings.worker_queues),
            self.settings.worker_concurrency, self.settings.threads_per_job(),
            self.settings.redis_host, self.settings.redis_port, self.settings.coordinator_url,
        )

        if self.heartbeat_enabled:
            self.heartbeat = Heartbeat(
                self.settings, self.redis, self.stats, self.stop_event,
                ffmpeg_info=self.ffmpeg_info, processor=self.processor,
            )
            self.heartbeat.start()

        self._consumers = [
            threading.Thread(target=self._consumer_loop, name=f"consumer-{i}", daemon=True)
            for i in range(self.settings.worker_concurrency)
        ]
        for thread in self._consumers:
            thread.start()

        self._flusher = threading.Thread(target=self._flush_loop, name="pending-flusher", daemon=True)
        self._flusher.start()

    def stop(self, timeout: float | None = None) -> None:
        self.stop_event.set()
        for thread in self._consumers:
            thread.join(timeout)
        if self._flusher is not None:
            self._flusher.join(timeout)
        if self.heartbeat is not None:
            self.heartbeat.join(timeout)

    def recover_own_inflight(self) -> dict:
        """Recovers this worker's own leftover inflight subtasks (e.g. a restart with the same worker_id)."""
        counts = {"requeued": 0, "failed": 0, "cleaned": 0}
        try:
            sids = list(self.redis.smembers(self._inflight_key))
        except Exception as e:
            logger.warning("no se pudo leer %s al iniciar: %s", self._inflight_key, e)
            return counts

        for sid in sids:
            try:
                data = self.redis.hgetall(f"subtask:{sid}")
                status = data.get("status") if data else None
                if data and status in ACTIVE_STATES and data.get("worker_id") == self.settings.worker_id:
                    result = recover_subtask(
                        self.redis, self.settings, self.reporter, sid,
                        reason="worker_restart", reporter_host=self.host,
                    )
                    if result in counts:
                        counts[result] += 1
            except Exception as e:
                logger.warning("error recuperando subtarea propia %s al iniciar: %s", sid, e)
            try:
                self.redis.srem(self._inflight_key, sid)
            except Exception as e:
                logger.warning("no se pudo limpiar %s de %s: %s", sid, self._inflight_key, e)

        if counts["requeued"] or counts["failed"]:
            logger.warning("worker %s recuperó tareas propias al iniciar: %s", self.settings.worker_id, counts)
        return counts

    def run_forever(self) -> None:
        self.start()

        def _handle_signal(signum, frame):
            self.stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handle_signal)

        # A bare wait() can't be interrupted by Ctrl+C on Windows.
        while not self.stop_event.wait(1.0):
            pass
        self.stop()

    def _consumer_loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                if self.poll_once(timeout=1):
                    backoff = 1.0
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("error de conexion a redis: %s", e)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception:
                logger.exception("error inesperado en el loop del consumidor")

    def _flush_loop(self) -> None:
        while not self.stop_event.wait(FLUSH_INTERVAL):
            try:
                self.reporter.flush_pending()
            except Exception:
                logger.exception("error al vaciar reportes pendientes")

    def poll_once(self, timeout: int = 1) -> bool:
        item = self.redis.blpop(self.settings.queue_keys(), timeout=timeout)
        if item is None:
            return False
        queue_name, raw = item
        subtask_id = self._parse_queue_item(raw)
        if subtask_id is None:
            logger.error("item invalido en %s, se descarta: %.200s", queue_name, raw)
            return True
        self.handle_subtask(subtask_id)
        return True

    @staticmethod
    def _parse_queue_item(raw: str) -> str | None:
        """Queue items are a plain subtask id (reaper) or the coordinator's JSON payload."""
        raw = raw.strip()
        if not raw.startswith("{"):
            return raw or None
        try:
            subtask_id = json.loads(raw).get("subtask_id")
        except (json.JSONDecodeError, AttributeError):
            return None
        return subtask_id if isinstance(subtask_id, str) and subtask_id else None

    def handle_subtask(self, subtask_id: str) -> dict | None:
        subtask_key = f"subtask:{subtask_id}"
        data = self.redis.hgetall(subtask_key)
        if not data:
            logger.warning("subtarea %s no encontrada", subtask_id)
            return None
        if data.get("status") in ("completed", "failed"):
            logger.info("subtarea %s ya terminal, se omite", subtask_id)
            return None

        assigned_at = datetime.now(timezone.utc).isoformat()
        pipe = self.redis.pipeline()
        pipe.hset(
            subtask_key,
            mapping={
                "status": "assigned",
                "worker_id": self.settings.worker_id,
                "assigned_at": assigned_at,
                "assigned_ts": time.time(),
            },
        )
        pipe.hincrby(subtask_key, "attempts", 1)
        pipe.sadd(self._inflight_key, subtask_id)
        results = pipe.execute()
        attempts = results[1]

        self.stats.task_started()
        started_monotonic = time.monotonic()

        work_dir = self.settings.work_dir / subtask_id
        in_dir = work_dir / "in"
        out_dir = work_dir / "out"

        started_at = assigned_at
        status = "failed"
        error: str | None = None
        error_type: str | None = None
        outputs: list[str] = []
        media_duration_s: float | None = None
        output_bytes = 0
        encoder: str | None = None

        try:
            src = self.storage.download(data["file_path"], in_dir)
            started_at = datetime.now(timezone.utc).isoformat()
            self.redis.hset(subtask_key, mapping={"status": "running", "started_at": started_at, "progress": 0})

            params = self._parse_params(data.get("params"), subtask_id)
            on_progress = self._make_progress_callback(subtask_key)

            result = self.processor.process(
                data["operation"],
                str(src),
                str(out_dir),
                params=params,
                on_progress=on_progress,
                threads=self.settings.threads_per_job(),
                timeout=self.settings.ffmpeg_timeout,
            )
            outputs = self.storage.upload_outputs(result.outputs, data["case_id"], subtask_id)
            status = "completed"
            media_duration_s = result.media_duration_s
            output_bytes = result.output_bytes
            encoder = getattr(result, "encoder", None)
        except StorageError as e:
            status = "failed"
            error = str(e)
            error_type = "StorageError"
        except Exception as e:
            status = "failed"
            error = str(e)
            processing_error_cls = getattr(self.processor, "ProcessingError", None)
            if processing_error_cls is not None and isinstance(e, processing_error_cls):
                error_type = type(e).__name__
            else:
                error_type = "InternalError"
                logger.exception("error interno procesando %s", subtask_id)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        finished_at = datetime.now(timezone.utc).isoformat()
        processing_s = round(time.monotonic() - started_monotonic, 3)

        payload = {
            "subtask_id": subtask_id,
            "case_id": data.get("case_id"),
            "worker_id": self.settings.worker_id,
            "host": self.host,
            "status": status,
            "result_path": outputs[0] if outputs else "",
            "outputs": outputs,
            "error": error,
            "error_type": error_type,
            "started_at": started_at,
            "finished_at": finished_at,
            "processing_s": processing_s,
            "media_duration_s": media_duration_s,
            "output_bytes": output_bytes if status == "completed" else 0,
            "encoder": encoder,
            "attempts": attempts,
        }

        try:
            self.reporter.report(payload)
            self.redis.srem(self._inflight_key, subtask_id)
            self.stats.task_finished(status == "completed")
        except Exception:
            logger.exception("error al finalizar subtarea %s", subtask_id)

        return payload

    def _parse_params(self, raw: str | None, subtask_id: str) -> dict:
        try:
            params = json.loads(raw) if raw else {}
            if not isinstance(params, dict):
                raise ValueError("params no es un objeto JSON")
            return params
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("params invalidos para %s, se usa {}: %s", subtask_id, e)
            return {}

    def _make_progress_callback(self, subtask_key: str):
        state = {"last": 0.0}

        def on_progress(percent) -> None:
            try:
                now = time.monotonic()
                if percent == 0 or percent == 100 or (now - state["last"]) >= 1.0:
                    self.redis.hset(subtask_key, "progress", int(percent))
                    state["last"] = now
            except Exception:
                pass

        return on_progress


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
    )
    Worker(Settings.from_env()).run_forever()


if __name__ == "__main__":
    main()
