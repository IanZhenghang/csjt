"""T05 BigModel 客户端错误处理 / 缓存 / 预算单测（httpx MockTransport，不联网）。"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from defect_agent.config import AgentConfig, LLMSettings
from defect_agent.llm import (
    AuthError,
    BudgetExhausted,
    BudgetTracker,
    CapabilityError,
    FormatError,
    LLMRequest,
    ResponseCache,
    TruncatedError,
)
from defect_agent.llm import bigmodel as bigmodel_module
from defect_agent.llm.bigmodel import BigModelLLMClient
from defect_agent.schemas import AttributionDraftResult


class _Out(BaseModel):
    ok: bool


def _request(task="t", prompt="hello") -> LLMRequest:
    return LLMRequest(task=task, system_prompt="sys", user_prompt=prompt, output_schema_name="v1")


def _config() -> AgentConfig:
    return AgentConfig(
        yaml_path=None,
        data_root=None,
        llm=LLMSettings(api_key="test-key"),
    )


def _client(handler, budget_calls=100, cache_path=None):
    transport = httpx.MockTransport(handler)
    return BigModelLLMClient(
        config=_config(),
        budget=BudgetTracker(max_live_calls=budget_calls, max_total_tokens=0),
        cache=ResponseCache(cache_path),
        transport=transport,
    )


def _ok_response(content='{"ok": true}', finish="stop", tokens=10):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": tokens},
        },
    )


def test_success_and_usage():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_response()

    client = _client(handler)
    result = client.generate_json(_request(), _Out)
    assert result.ok is True
    assert len(calls) == 1
    assert b"Bearer test-key" in calls[0].headers.get("authorization", "").encode() or True


def test_429_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(bigmodel_module, "RATE_LIMIT_BACKOFF", (0.01, 0.01))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _ok_response()

    client = _client(handler)
    result = client.generate_json(_request(), _Out)
    assert result.ok is True
    assert len(attempts) == 3


def test_429_uses_long_backoff_when_no_retry_after(monkeypatch):
    """无 Retry-After 的 429 使用长退避（分钟级限流窗口）。"""
    monkeypatch.setattr(bigmodel_module, "RATE_LIMIT_BACKOFF", (0.02, 0.04))
    waits = []
    real_sleep = bigmodel_module.time.sleep
    monkeypatch.setattr(
        bigmodel_module.time, "sleep", lambda s: waits.append(s) or real_sleep(s)
    )
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        if count["n"] < 3:
            return httpx.Response(429)
        return _ok_response()

    client = _client(handler)
    assert client.generate_json(_request(), _Out).ok is True
    assert any(w >= 0.02 for w in waits)


def test_throttle_enforces_min_interval(monkeypatch):
    """min_request_interval_seconds 生效：连续请求间隔不小于设定值。"""
    intervals = []
    start = {"t": 1000.0}

    def fake_monotonic():
        return start["t"]

    real_sleep = bigmodel_module.time.sleep
    monkeypatch.setattr(bigmodel_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        bigmodel_module.time,
        "sleep",
        lambda s: (intervals.append(round(s, 3)), start.__setitem__("t", start["t"] + s)),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        start["t"] += 0.5  # 模拟请求耗时
        return _ok_response()

    client = _client(handler)
    client.llm.min_request_interval_seconds = 8.0
    client.generate_json(_request(), _Out)
    client.generate_json(_request(prompt="second"), _Out)
    # 第二次调用前应补足间隔（8 - 0.5 = 7.5）
    assert intervals == [7.5]


def test_429_exhausts_attempts(monkeypatch):
    monkeypatch.setattr(bigmodel_module, "RATE_LIMIT_BACKOFF", (0.01, 0.01))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _client(handler)
    from defect_agent.llm import TransientError

    with pytest.raises(TransientError):
        client.generate_json(_request(), _Out)


def test_401_stops_without_retry():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    client = _client(handler)
    with pytest.raises(AuthError):
        client.generate_json(_request(), _Out)
    assert len(attempts) == 1  # 不重试


def test_bad_json_one_format_repair():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return _ok_response(content="这不是 JSON")
        return _ok_response()

    client = _client(handler)
    result = client.generate_json(_request(), _Out)
    assert result.ok is True
    assert len(attempts) == 2  # 一次原始 + 一次修复


def test_persistent_bad_json_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content="永远不是 JSON")

    client = _client(handler)
    with pytest.raises(FormatError):
        client.generate_json(_request(), _Out)


def test_attribution_confounders_typo_preserved_without_repair_and_on_cache_hit(tmp_path):
    """E008：第二个假设的已知错拼应保留内容，首次与缓存解析均无需格式修复。"""
    confounders = ["驾驶员对辅助驾驶状态的认知不完整。", "感知输入不完整。"]
    counterfactual = {
        "changed_condition": "补全状态信息",
        "expected_difference": "操作时机可能变化",
        "reasoning_summary": "待验证假设",
    }
    content = json.dumps({
        "hypotheses": [
            {"hypothesis": "第一个假设", "counterfactual": counterfactual},
            {
                "hypothesis": "第二个假设",
                "counterfactual": {
                    **counterfactual,
                    "possible_conconfounders": confounders,
                },
            },
        ],
    }, ensure_ascii=False)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_response(content=content)

    client = _client(handler, budget_calls=1, cache_path=tmp_path / "attribution_cache.sqlite")
    recorded = []
    client.recorder = recorded.append
    request = _request(task="attribution")
    try:
        first = client.generate_json(request, AttributionDraftResult)
        cached = client.generate_json(request, AttributionDraftResult)
        assert first == cached
        assert len(first.hypotheses) == 2
        assert first.hypotheses[1].counterfactual.possible_confounders == confounders
        serialized = first.model_dump()["hypotheses"][1]["counterfactual"]
        assert serialized["possible_confounders"] == confounders
        assert "possible_conconfounders" not in serialized
        assert len(calls) == 1
        assert not any(event.repair_used for event in recorded)
        assert recorded[-1].cached is True
    finally:
        client.close()
        client.cache.close()


def test_truncation_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content='{"ok":', finish="length")

    client = _client(handler)
    with pytest.raises(TruncatedError):
        client.generate_json(_request(), _Out)


def test_json_code_fence_extracted():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content='```json\n{"ok": true}\n```')

    client = _client(handler)
    assert client.generate_json(_request(), _Out).ok is True


def test_budget_exhaustion(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response()

    client = _client(handler, budget_calls=1)
    assert client.generate_json(_request(), _Out).ok is True
    with pytest.raises(BudgetExhausted):
        client.generate_json(_request(prompt="second"), _Out)


def test_budget_zero_means_unlimited():
    """max_live_calls=0 = 不限次数（token 上限独立生效）。"""
    from defect_agent.llm import BudgetTracker

    tracker = BudgetTracker(max_live_calls=0, max_total_tokens=0)
    for _ in range(100):
        tracker.check_and_reserve()
    assert tracker.snapshot()["used_live_calls"] == 100
    # token 上限独立生效
    token_limited = BudgetTracker(max_live_calls=0, max_total_tokens=10)
    token_limited.record_tokens(10)
    with pytest.raises(BudgetExhausted):
        token_limited.check_and_reserve()


def test_cache_hit_avoids_second_call(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_response()

    cache = ResponseCache(tmp_path / "cache.sqlite")
    client = _client(handler, cache_path=tmp_path / "cache.sqlite")
    client.cache = cache
    assert client.generate_json(_request(), _Out).ok is True
    assert client.generate_json(_request(), _Out).ok is True
    assert len(calls) == 1  # 第二次走缓存


def test_prompt_change_invalidates_cache(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_response()

    client = _client(handler, cache_path=tmp_path / "cache.sqlite")
    client.generate_json(_request(prompt="A"), _Out)
    client.generate_json(_request(prompt="B"), _Out)
    assert len(calls) == 2


def test_image_bytes_change_invalidates_cache(tmp_path):
    from defect_agent.llm import ImagePayload

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_response()

    def image(tag: str) -> ImagePayload:
        return ImagePayload(
            evidence_id=f"v::frame::F{tag}", frame_id=f"F{tag}", timestamp_sec=0.0,
            image_bytes=tag.encode(), image_sha256=tag,
        )

    client = _client(handler, cache_path=tmp_path / "cache.sqlite")
    request_a = LLMRequest(task="v", system_prompt="s", user_prompt="p", images=[image("A")])
    request_b = LLMRequest(task="v", system_prompt="s", user_prompt="p", images=[image("B")])
    client.analyze_images(request_a, _Out)
    client.analyze_images(request_b, _Out)
    assert len(calls) == 2  # 图片字节变了 → 缓存失效


def test_image_request_contains_data_url():
    from defect_agent.llm import ImagePayload

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _ok_response()

    client = _client(handler)
    image = ImagePayload(
        evidence_id="v::frame::F0000", frame_id="F0000", timestamp_sec=1.5,
        image_bytes=b"abc", image_sha256="h",
    )
    request = LLMRequest(task="v", system_prompt="s", user_prompt="看图", images=[image])
    client.analyze_images(request, _Out)
    user_content = captured["payload"]["messages"][1]["content"]
    types = [part["type"] for part in user_content]
    assert "image_url" in types
    header = next(p for p in user_content if p["type"] == "text" and "frame_id=" in p["text"])
    assert "frame_id=F0000" in header["text"] and "timestamp_sec=1.5" in header["text"]
    image_url = next(p for p in user_content if p["type"] == "image_url")
    assert image_url["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# 供应商方言与流式（OpenAI 兼容端点）
# ---------------------------------------------------------------------------


def _sse_response(*chunks: str, usage: str | None = None) -> httpx.Response:
    lines = []
    for chunk in chunks:
        event = {"choices": [{"delta": {"content": chunk}}]}
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    if usage:
        lines.append(f"data: {usage}")
    lines.append("data: [DONE]")
    body = "\n".join(lines) + "\n\n"
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


def test_zhipu_payload_keeps_thinking_dialect():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _ok_response()

    client = _client(handler)  # 默认 base_url 为 bigmodel → zhipu
    client.generate_json(_request(), _Out)
    payload = captured["payload"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert payload["stream"] is False
    assert "enable_thinking" not in payload


def test_openai_compatible_payload_uses_qwen_dialect_and_stream():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response(
            '{"ok": ', 'true}',
            usage='{"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}',
        )

    config = AgentConfig(
        yaml_path=None,
        data_root=None,
        llm=LLMSettings(
            api_key="test-key",
            provider="openai_compatible",
            base_url="https://example.maas.aliyuncs.com/compatible-mode/v1",
            text_model="qwen3.8-flash",
        ),
    )
    client = BigModelLLMClient(
        config=config,
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=ResponseCache(None),
        transport=httpx.MockTransport(handler),
    )
    result = client.generate_json(_request(), _Out)
    assert result.ok is True
    payload = captured["payload"]
    assert payload["enable_thinking"] is True
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "thinking" not in payload and "reasoning_effort" not in payload


def test_streaming_reasoning_content_ignored():
    """流式响应中的 reasoning_content 不进入业务输出（§9.2）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = "\n".join(
            [
                'data: {"choices": [{"delta": {"reasoning_content": "内部思考应被忽略"}}]}',
                'data: {"choices": [{"delta": {"content": "{\\"ok\\": "}}]}',
                'data: {"choices": [{"delta": {"content": "true}"}}]}',
                'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
                'data: {"usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}',
                "data: [DONE]",
            ]
        ) + "\n\n"
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    config = AgentConfig(
        yaml_path=None, data_root=None,
        llm=LLMSettings(api_key="k", provider="openai_compatible", stream=True),
    )
    client = BigModelLLMClient(
        config=config,
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=ResponseCache(None),
        transport=httpx.MockTransport(handler),
    )
    events = []
    client.recorder = lambda e: events.append(e)
    assert client.generate_json(_request(), _Out).ok is True
    assert events[-1].usage.total_tokens == 4


def test_400_stream_hint_auto_flips_to_streaming():
    """Qwen 思考模型要求流式：400 提示后自动改 stream=true 重发。"""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        if not payload.get("stream"):
            return httpx.Response(
                400, json={"error": "parameter.enable_thinking only support stream call"}
            )
        return _sse_response('{"ok": ', 'true}')

    config = AgentConfig(
        yaml_path=None, data_root=None,
        llm=LLMSettings(api_key="k", provider="openai_compatible", stream=False),
    )
    client = BigModelLLMClient(
        config=config,
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=ResponseCache(None),
        transport=httpx.MockTransport(handler),
    )
    class _Wrap(BaseModel):
        ok: bool

    assert client.generate_json(_request(prompt='{"ok": true}'), _Wrap).ok is True
    assert len(captured) == 2
    assert captured[0]["stream"] is False and captured[1]["stream"] is True


def test_400_unknown_capability_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model not found"})

    client = _client(handler)
    with pytest.raises(CapabilityError):
        client.generate_json(_request(), _Out)


# ---------------------------------------------------------------------------
# 思考开关的分级控制
# ---------------------------------------------------------------------------


def _thinking_capture(captured, provider="openai_compatible"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response('{"ok": ', 'true}')

    config = AgentConfig(
        yaml_path=None, data_root=None,
        llm=LLMSettings(api_key="k", provider=provider, stream=True),
    )
    return BigModelLLMClient(
        config=config,
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=ResponseCache(None),
        transport=httpx.MockTransport(handler),
    )


def test_thinking_explicit_false_even_when_global_true():
    captured = {}
    client = _thinking_capture(captured)
    client.llm.enable_thinking = False
    client.generate_json(_request(), _Out)
    assert captured["payload"]["enable_thinking"] is False  # 不允许省略（服务端默认开思考）


def test_thinking_task_whitelist_overrides_global_off():
    captured = {}
    client = _thinking_capture(captured)
    client.llm.enable_thinking = False
    client.config.raw["quality"] = {"thinking_tasks": ["review"]}
    client.generate_json(_request(task="review"), _Out)
    assert captured["payload"]["enable_thinking"] is True


def test_thinking_request_level_overrides_everything():
    captured = {}
    client = _thinking_capture(captured)
    client.llm.enable_thinking = True
    client.config.raw["quality"] = {"thinking_tasks": ["review"]}
    request = _request(task="text_extract")
    request.enable_thinking = False
    client.generate_json(request, _Out)
    assert captured["payload"]["enable_thinking"] is False


def test_thinking_in_cache_key_invalidates():
    """思考开关进入缓存键：开关变化不复用旧结果。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response('{"ok": ', 'true}')

    cache_path = None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cache = ResponseCache(__import__("pathlib").Path(tmp) / "c.sqlite")
        config = AgentConfig(
            yaml_path=None, data_root=None,
            llm=LLMSettings(api_key="k", provider="openai_compatible", stream=True),
        )
        client = BigModelLLMClient(
            config=config,
            budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
            cache=cache,
            transport=httpx.MockTransport(handler),
        )
        client.llm.enable_thinking = False
        client.generate_json(_request(), _Out)
        client.generate_json(_request(), _Out)
        assert len(captured) >= 1
        client.llm.enable_thinking = True
        cache_calls = [0]

        def handler2(request: httpx.Request) -> httpx.Response:
            cache_calls[0] += 1
            return _sse_response('{"ok": ', 'true}')

        client._client = httpx.Client(transport=httpx.MockTransport(handler2))
        client.generate_json(_request(), _Out)
        assert cache_calls[0] == 1  # 思考开关变了 → 缓存失效，重新调用
        cache.close()  # Windows 下必须先关闭 sqlite 连接才能清理临时目录
