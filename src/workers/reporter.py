from __future__ import annotations

import json
import logging
import time
from typing import Callable

import redis
import requests

from src.workers.config import Settings

logger = logging.getLogger(__name__)

PENDING_KEY = "reports:pending"


class Reporter:
    """Delivers subtask reports to the coordinator, with retries and a Redis-backed fallback queue."""

    def __init__(
        self,
        settings: Settings,
        redis_client: redis.Redis,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        delays: tuple[float, ...] = (1, 2, 4, 8, 16),
        timeout: float = 10.0,
    ):
        self.settings = settings
        self.redis = redis_client
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep
        self.delays = delays
        self.timeout = timeout
        self.url = f"{settings.coordinator_url}/subtasks/report"

    def _post(self, payload: dict) -> tuple[bool, bool]:
        """Attempt one POST. Returns (delivered, retryable_failure)."""
        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning("fallo de red al reportar %s: %s", payload.get("subtask_id"), e)
            return False, True
        if 200 <= response.status_code < 300:
            return True, False
        if response.status_code >= 500:
            logger.warning("coordinador respondio %s para %s", response.status_code, payload.get("subtask_id"))
            return False, True
        logger.error(
            "reporte rechazado (%s) para %s: %s",
            response.status_code,
            payload.get("subtask_id"),
            response.text[:500],
        )
        return False, False

    def report(self, payload: dict) -> bool:
        delivered, retryable = self._post(payload)
        if delivered:
            return True
        if not retryable:
            return False
        for delay in self.delays:
            self.sleep(delay)
            delivered, retryable = self._post(payload)
            if delivered:
                return True
            if not retryable:
                return False
        self._park(payload)
        return False

    def _park(self, payload: dict) -> None:
        try:
            self.redis.lpush(PENDING_KEY, json.dumps(payload))
        except Exception as e:
            logger.error("no se pudo encolar reporte pendiente %s: %s", payload.get("subtask_id"), e)

    def flush_pending(self, max_items: int = 100) -> int:
        delivered_count = 0
        for _ in range(max_items):
            item = self.redis.rpop(PENDING_KEY)
            if item is None:
                break
            try:
                payload = json.loads(item)
            except (TypeError, ValueError) as e:
                logger.error("reporte pendiente invalido descartado: %s", e)
                continue
            delivered, retryable = self._post(payload)
            if delivered:
                delivered_count += 1
                continue
            if retryable:
                self.redis.rpush(PENDING_KEY, item)
                break
        return delivered_count
