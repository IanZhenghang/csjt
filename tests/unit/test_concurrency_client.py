"""Offline concurrency, accounting, and embedding cache isolation regressions."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import BaseModel

from defect_agent.config import AgentConfig, LLMSettings
from defect_agent.llm.base import BudgetExhausted, LLMRequest, TransientError
from defect_agent.llm.bigmodel import BigModelLLMClient, retry_after_seconds
from defect_agent.llm.budget import BudgetTracker
from defect_agent.llm.cache import ResponseCache
from defect_agent.llm.embeddings import EmbeddingClient, EmbeddingError, create_embedding_client
from defect_agent.llm.scheduling import RequestScheduler


class Output(BaseModel):
    value: str


def config(**kwargs):
    return AgentConfig(yaml_path=None, data_root=None,
                       llm=LLMSettings(mode="live", api_key="offline-test-key", **kwargs))


def response(value, tokens=3):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"value": value})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    })


def request(value):
    return LLMRequest(task="align", system_prompt="system", user_prompt=value)


def test_parallel_requests_keep_payload_and_usage_independent():
    barrier = threading.Barrier(2)
    events = []

    def handle(req):
        prompt = json.loads(req.content)["messages"][1]["content"]
        barrier.wait(timeout=3)
        return response(prompt)

    client = BigModelLLMClient(config(), BudgetTracker(20, 0), ResponseCache(None),
                              transport=httpx.MockTransport(handle))
    client.recorder = events.append
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: client.generate_json(request(text), Output).value, ["A", "B"]))
    assert results == ["A", "B"]
    assert client.budget.snapshot()["used_live_calls"] == 2
    assert client.budget.snapshot()["used_total_tokens"] == 6
    assert len(events) == 2 and all(event.task == "align" for event in events)
    client.close()


def test_budget_reservation_is_atomic_for_parallel_workers():
    calls = []

    def handle(req):
        calls.append(1)
        time.sleep(0.01)
        return response("ok")

    client = BigModelLLMClient(config(), BudgetTracker(1, 0), ResponseCache(None),
                              transport=httpx.MockTransport(handle))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.generate_json, request(value), Output) for value in ["A", "B"]]
        outcomes = []
        for future in pending:
            try:
                outcomes.append(future.result())
            except BudgetExhausted:
                outcomes.append("budget")
    assert len(calls) == 1
    assert outcomes.count("budget") == 1
    client.close()


def test_every_retry_and_repair_reserves_budget_and_records_usage():
    responses = [httpx.Response(429, headers={"Retry-After": "0"}),
                 httpx.Response(200, json={"choices": [{"message": {"content": "bad JSON"}}],
                                          "usage": {"total_tokens": 7}}), response("fixed", tokens=11)]
    events = []
    client = BigModelLLMClient(config(), BudgetTracker(10, 0), ResponseCache(None),
                              transport=httpx.MockTransport(lambda req: responses.pop(0)))
    client.recorder = events.append
    assert client.generate_json(request("repair"), Output).value == "fixed"
    assert client.budget.snapshot()["used_live_calls"] == 3
    assert client.budget.snapshot()["used_total_tokens"] == 18
    assert len(events) == 3
    assert events[0].error == "HTTP 429"
    assert events[2].repair_used is True
    client.close()


def test_budget_can_stop_format_repair_without_losing_first_usage():
    events = []
    client = BigModelLLMClient(config(), BudgetTracker(1, 0), ResponseCache(None),
                              transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
                                  "choices": [{"message": {"content": "bad"}}],
                                  "usage": {"total_tokens": 8},
                              })))
    client.recorder = events.append
    with pytest.raises(BudgetExhausted):
        client.generate_json(request("repair"), Output)
    assert len(events) == 1 and events[0].usage.total_tokens == 8
    assert client.budget.snapshot()["used_total_tokens"] == 8
    client.close()


def test_429_cooldown_applies_to_other_worker_too():
    scheduler = RequestScheduler(2)
    deferred = threading.Event()
    starts = []
    original_defer = scheduler.defer

    def defer(seconds):
        original_defer(seconds)
        deferred.set()

    scheduler.defer = defer

    def handle(req):
        starts.append(time.monotonic())
        if len(starts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.06"})
        return response(json.loads(req.content)["messages"][1]["content"])

    client = BigModelLLMClient(config(), BudgetTracker(10, 0), ResponseCache(None),
                              transport=httpx.MockTransport(handle), scheduler=scheduler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.generate_json, request("A"), Output)
        assert deferred.wait(timeout=3)
        second = pool.submit(client.generate_json, request("B"), Output)
        assert first.result().value == "A"
        assert second.result().value == "B"
    assert len(starts) == 3
    assert all(start - starts[0] >= 0.05 for start in starts[1:])
    client.close()


def test_scheduler_interval_and_slots_apply_across_workers():
    scheduler = RequestScheduler(2, min_interval_seconds=0.02)
    starts = []
    active = 0
    maximum = 0
    lock = threading.Lock()

    def work():
        nonlocal active, maximum
        with scheduler.slot():
            with lock:
                active += 1
                maximum = max(active, maximum)
                starts.append(time.monotonic())
            time.sleep(0.05)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: work(), range(4)))
    assert maximum == 2
    assert all(later - earlier >= 0.018 for earlier, later in zip(starts, starts[1:]))


def test_scheduler_rejects_cooldown_beyond_deadline_without_request():
    scheduler = RequestScheduler()
    scheduler.defer(10)
    with pytest.raises(TransientError):
        with scheduler.slot(time.monotonic() + 0.1):
            pytest.fail("must not admit request")


def test_concurrent_provider_dialect_fallback_handles_stale_payloads():
    barrier = threading.Barrier(2)

    def handle(req):
        payload = json.loads(req.content)
        if not payload.get("stream"):
            barrier.wait(timeout=3)
            return httpx.Response(400, text="enable_thinking requires stream")
        if "stream_options" in payload:
            return httpx.Response(400, text="stream_options unsupported")
        value = payload["messages"][1]["content"]
        chunk = {"choices": [{"delta": {"content": json.dumps({"value": value})}}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\ndata: [DONE]\n")

    client = BigModelLLMClient(config(stream=False), BudgetTracker(10, 0), ResponseCache(None),
                              transport=httpx.MockTransport(handle))
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda text: client.generate_json(request(text), Output).value, ["A", "B"]))
    assert result == ["A", "B"]
    assert client.use_stream is True
    assert client._stream_options_dropped is True
    client.close()


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 1], [float("inf"), 1], ["1", 2], [True, 1], [10 ** 1000, 1]])
def test_invalid_embedding_vectors_are_rejected(vector):
    with pytest.raises(EmbeddingError):
        EmbeddingClient._parse({"data": [{"index": 0, "embedding": vector}]}, 1)


@pytest.mark.parametrize("entries", [
    [{"index": -1, "embedding": [1, 0]}],
    [{"index": 1, "embedding": [1, 0]}],
    [{"index": True, "embedding": [1, 0]}],
    [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}],
    [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1, 0, 0]}],
])
def test_invalid_embedding_indices_and_mixed_dimensions(entries):
    with pytest.raises(EmbeddingError):
        EmbeddingClient._parse({"data": entries}, len(entries))


def test_embedding_cache_isolates_endpoint_model_preprocess_and_dimensions(tmp_path):
    calls = []
    cache = ResponseCache(tmp_path / "embeddings.sqlite")

    def handle(req):
        calls.append(str(req.url))
        size = json.loads(req.content).get("dimensions", 2)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1] * size}],
                                        "usage": {"prompt_tokens": 4, "total_tokens": 4}})

    clients = [EmbeddingClient(config(), cache=cache, transport=httpx.MockTransport(handle), **opts)
               for opts in [{"base_url": "https://a.test/v1", "model": "m"},
                            {"base_url": "https://b.test/v1", "model": "m"},
                            {"base_url": "https://a.test/v1", "model": "m", "preprocess_version": "v2"},
                            {"base_url": "https://a.test/v1", "model": "m", "dimensions": 3},
                            {"base_url": "https://a.test/v1", "model": "other"}]]
    events = []
    try:
        for client in clients:
            client.recorder = events.append
            client.embed(["text"])
            client.embed(["text"])
        assert len(calls) == 5
        assert len(events) == 10
        assert sum(event.cached for event in events) == 5
        assert sum(event.usage.total_tokens for event in events if event.usage) == 20
    finally:
        for client in clients:
            client.close()
        cache.close()


def test_embedding_usage_budget_and_retry_count_include_all_posts():
    calls = []
    events = []

    def handle(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}],
                                        "usage": {"prompt_tokens": 9, "total_tokens": 9}})

    budget = BudgetTracker(5, 0)
    client = EmbeddingClient(config(), "m", budget=budget, transport=httpx.MockTransport(handle),
                             recorder=events.append)
    assert client.embed(["hello"]) == [[1, 2]]
    assert budget.used_live_calls == 2 and budget.used_total_tokens == 9
    assert len(events) == 2 and events[0].error == "embedding HTTP 429"
    client.close()


def test_corrupt_embedding_cache_is_refetched_and_invalid_response_never_cached(tmp_path):
    cache = ResponseCache(tmp_path / "embeddings.sqlite")
    client = EmbeddingClient(config(), "m", cache=cache, dimensions=2,
                             transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
                                 "data": [{"index": 0, "embedding": [1, 2, 3]}],
                             })))
    cache.set(client._key("text"), '{"dimensions": 2, "vector": [0, 0]}', "m")
    try:
        with pytest.raises(EmbeddingError):
            client.embed(["text"])
        assert json.loads(cache.get(client._key("text")))["vector"] == [0, 0]
    finally:
        client.close()
        cache.close()


def test_factory_shares_rate_limit_domain_only_for_same_base_url():
    scheduler = RequestScheduler()
    cfg = config()
    same = create_embedding_client(cfg, None, None, scheduler=scheduler)
    cfg.env["EMBEDDING_BASE_URL"] = "https://embedding.test/v1"
    separate = create_embedding_client(cfg, None, None, scheduler=scheduler)
    try:
        assert same.scheduler is scheduler
        assert separate.scheduler is not scheduler
    finally:
        same.close()
        separate.close()


@pytest.mark.parametrize("value", ["broken", "nan", "inf"])
def test_invalid_retry_after_uses_default(value):
    assert retry_after_seconds(value, 7) == 7
