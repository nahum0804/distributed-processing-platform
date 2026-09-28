import json

import fakeredis
import requests
import responses

from src.workers.config import Settings
from src.workers.reporter import PENDING_KEY, Reporter


def make_settings(**overrides) -> Settings:
    return Settings.from_env({"COORDINATOR_URL": "http://coord", **overrides})


def make_reporter(redis_client, **kwargs) -> Reporter:
    kwargs.setdefault("delays", (0, 0))
    kwargs.setdefault("sleep", lambda s: None)
    return Reporter(make_settings(), redis_client, **kwargs)


def make_payload(subtask_id="s1") -> dict:
    return {"subtask_id": subtask_id, "case_id": "c1", "status": "completed"}


@responses.activate
def test_report_success_first_try():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    responses.add(responses.POST, "http://coord/subtasks/report", json={"ok": True}, status=200)

    assert reporter.report(make_payload()) is True
    assert len(responses.calls) == 1


@responses.activate
def test_report_retries_then_succeeds():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    url = "http://coord/subtasks/report"
    responses.add(responses.POST, url, status=500)
    responses.add(responses.POST, url, status=500)
    responses.add(responses.POST, url, status=200)

    assert reporter.report(make_payload()) is True
    assert len(responses.calls) == 3


@responses.activate
def test_report_connection_error_all_attempts_parks_payload():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    url = "http://coord/subtasks/report"
    for _ in range(3):
        responses.add(responses.POST, url, body=requests.ConnectionError("boom"))

    payload = make_payload("s2")
    assert reporter.report(payload) is False
    assert len(responses.calls) == 3
    pending = redis_client.lrange(PENDING_KEY, 0, -1)
    assert len(pending) == 1
    assert json.loads(pending[0]) == payload


@responses.activate
def test_report_4xx_not_retried_and_not_parked():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    responses.add(responses.POST, "http://coord/subtasks/report", status=404)

    assert reporter.report(make_payload("s3")) is False
    assert len(responses.calls) == 1
    assert redis_client.llen(PENDING_KEY) == 0


@responses.activate
def test_flush_pending_delivers_oldest_first_in_order():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    p1, p2 = make_payload("first"), make_payload("second")
    redis_client.lpush(PENDING_KEY, json.dumps(p1))
    redis_client.lpush(PENDING_KEY, json.dumps(p2))

    url = "http://coord/subtasks/report"
    responses.add(responses.POST, url, status=200)
    responses.add(responses.POST, url, status=200)

    delivered = reporter.flush_pending()

    assert delivered == 2
    assert redis_client.llen(PENDING_KEY) == 0
    bodies = [json.loads(call.request.body) for call in responses.calls]
    assert bodies == [p1, p2]


@responses.activate
def test_flush_pending_stops_and_keeps_order_on_retryable_failure():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    p1, p2 = make_payload("oldest"), make_payload("newest")
    redis_client.lpush(PENDING_KEY, json.dumps(p1))
    redis_client.lpush(PENDING_KEY, json.dumps(p2))

    responses.add(responses.POST, "http://coord/subtasks/report", status=500)

    delivered = reporter.flush_pending()

    assert delivered == 0
    assert len(responses.calls) == 1
    remaining = redis_client.lrange(PENDING_KEY, 0, -1)
    assert [json.loads(item) for item in remaining] == [p2, p1]


@responses.activate
def test_flush_pending_drops_4xx_item():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    redis_client.lpush(PENDING_KEY, json.dumps(make_payload("bad")))
    responses.add(responses.POST, "http://coord/subtasks/report", status=422)

    delivered = reporter.flush_pending()

    assert delivered == 0
    assert redis_client.llen(PENDING_KEY) == 0


def test_flush_pending_drops_invalid_json():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)
    redis_client.lpush(PENDING_KEY, "not-json")

    delivered = reporter.flush_pending()

    assert delivered == 0
    assert redis_client.llen(PENDING_KEY) == 0


def test_flush_pending_empty_queue_returns_zero():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    reporter = make_reporter(redis_client)

    assert reporter.flush_pending() == 0
