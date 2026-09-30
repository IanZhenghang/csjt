"""Offline regressions for bounded, validation-guided JSON repair requests."""

from __future__ import annotations

import copy
import json
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from defect_agent.config import AgentConfig, LLMSettings
from defect_agent.llm import (
    AuthError,
    BudgetExhausted,
    BudgetTracker,
    CapabilityError,
    FormatError,
    ImagePayload,
    LLMRequest,
    TruncatedError,
)
from defect_agent.llm import bigmodel as bigmodel_module
from defect_agent.llm.base import TransientError
from defect_agent.llm.bigmodel import BigModelLLMClient


class _Counterfactual(BaseModel):
    model_config = ConfigDict(extra="forbid")

    possible_confounders: list[str]


class _Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: Literal["low", "high"]
    counterfactual: _Counterfactual


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[_Hypothesis]


def _valid_data():
    return {
        "hypotheses": [{
            "confidence": "low",
            "counterfactual": {"possible_confounders": ["已有证据的限制"]},
        }],
    }


def _content(data=None):
    return json.dumps(_valid_data() if data is None else data, ensure_ascii=False)


def _response(content, *, tokens=5, finish="stop", stream=False):
    usage = {"prompt_tokens": 2, "completion_tokens": tokens - 2, "total_tokens": tokens}
    if stream:
        chunks = [
            {"choices": [{"delta": {"content": content[:len(content) // 2]}}]},
            {"choices": [{"delta": {"content": content[len(content) // 2:]}}]},
            {"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage},
        ]
        return httpx.Response(
            200,
            text="\n".join("data: " + json.dumps(chunk) for chunk in chunks)
            + "\ndata: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage,
    })


class _MemoryCache:
    """Observe cache writes without filesystem or SQLite fixtures."""

    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, output_json, model):
        self.values[key] = output_json
        self.writes.append((key, output_json, model))


@pytest.fixture
def client_factory():
    clients = []

    def make(handler, *, repairs=None, calls=100, tokens=0, **settings):
        raw = {} if repairs is None else {"budget": {"max_format_repairs_per_result": repairs}}
        config = AgentConfig(
            yaml_path=None,
            data_root=None,
            raw=raw,
            llm=LLMSettings(api_key="offline-test-key", **settings),
        )
        client = BigModelLLMClient(
            config,
            BudgetTracker(max_live_calls=calls, max_total_tokens=tokens),
            _MemoryCache(),
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def _request(**kwargs):
    return LLMRequest(
        task="attribution",
        system_prompt="按证据生成结构化归因。",
        user_prompt="原始证据：E008。不得新增证据。",
        context={"event_id": "E008", "evidence_ids": ["frame-1"]},
        output_schema_name="repair-test-v1",
        **kwargs,
    )


def _contains_schema(text):
    """Accept any feedback prose/formatting while checking the actual schema."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if value == _Draft.model_json_schema():
            return True
    return False


def test_default_repairs_follow_latest_validation_error_until_success(client_factory):
    extra = _valid_data()
    extra["hypotheses"].append({
        "confidence": "high",
        "counterfactual": {
            "possible_confounders": [],
            "unexpected_field": "SECOND_EXTRA_MARKER",
        },
    })
    enum = _valid_data()
    enum["hypotheses"].append({
        "confidence": "UNRECOGNIZED_CONFIDENCE_MARKER",
        "counterfactual": {"possible_confounders": []},
    })
    contents = ["FIRST_NON_JSON_RESPONSE", _content(extra), _content(enum), _content()]
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return _response(contents[len(payloads) - 1], tokens=len(payloads) + 3)

    client = client_factory(handler)
    events = []
    client.recorder = events.append
    request = _request()
    original = copy.deepcopy(request)

    assert client.generate_json(request, _Draft).model_dump() == _valid_data()
    assert len(payloads) == 4  # Default: initial output plus up to three repairs.
    assert request == original
    assert len(payloads[0]["messages"]) == 2
    for index, payload in enumerate(payloads[1:], start=1):
        messages = payload["messages"]
        assert len(messages) == 4
        assert messages[:2] == payloads[0]["messages"]
        assert messages[2] == {"role": "assistant", "content": contents[index - 1]}
        assert messages[3]["role"] == "user"
        assert _contains_schema(messages[3]["content"])
    second_feedback = payloads[2]["messages"][-1]["content"]
    assert "hypotheses.1.counterfactual.unexpected_field" in second_feedback
    assert "extra_forbidden" in second_feedback
    third_feedback = payloads[3]["messages"][-1]["content"]
    assert "hypotheses.1.confidence" in third_feedback
    assert "literal_error" in third_feedback
    assert "FIRST_NON_JSON_RESPONSE" not in json.dumps(payloads[2])
    assert "SECOND_EXTRA_MARKER" not in json.dumps(payloads[3])
    assert [event.repair_used for event in events] == [False, True, True, True]
    assert [event.usage.total_tokens for event in events] == [4, 5, 6, 7]
    assert client.budget.snapshot()["used_live_calls"] == 4
    assert client.budget.snapshot()["used_total_tokens"] == 22
    assert len(client.cache.writes) == 1
    assert client.cache.writes[0][1] == contents[-1]

    assert client.generate_json(request, _Draft).model_dump() == _valid_data()
    assert len(payloads) == 4
    assert len(client.cache.writes) == 1
    assert events[-1].cached is True


@pytest.mark.parametrize("problem, path, error_type", [
    ("missing", "hypotheses.0.counterfactual.possible_confounders", "missing"),
    ("type", "hypotheses.0.counterfactual.possible_confounders", "list_type"),
])
def test_missing_and_wrong_type_fields_are_reported_to_repair(
    client_factory, problem, path, error_type,
):
    invalid = _valid_data()
    counterfactual = invalid["hypotheses"][0]["counterfactual"]
    if problem == "missing":
        counterfactual.pop("possible_confounders")
    else:
        counterfactual["possible_confounders"] = "must be a list"
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return _response(_content(invalid) if len(payloads) == 1 else _content())

    client = client_factory(handler, repairs=1)
    assert client.generate_json(_request(), _Draft).model_dump() == _valid_data()
    feedback = payloads[1]["messages"][-1]["content"]
    assert path in feedback and error_type in feedback
    assert _contains_schema(feedback)


@pytest.mark.parametrize("repairs", [0, 1, 2, 5])
def test_repair_limit_stops_and_failed_outputs_do_not_enter_cache(client_factory, repairs):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        # A nested validation failure gives the terminal error a specific path.
        return _response('{"hypotheses": [{"confidence": "high", "counterfactual": {}}]}')

    client = client_factory(handler, repairs=repairs)
    with pytest.raises(FormatError) as raised:
        client.generate_json(_request(), _Draft)
    assert len(payloads) == 1 + repairs
    assert "hypotheses.0.counterfactual.possible_confounders" in str(raised.value)
    assert str(repairs) in str(raised.value)
    assert client.cache.values == {}
    assert client.cache.writes == []
    assert client.budget.snapshot()["used_live_calls"] == 1 + repairs

    # A subsequent attempt must go to the provider again, not reuse a failure.
    with pytest.raises(FormatError):
        client.generate_json(_request(), _Draft)
    assert len(payloads) == 2 * (1 + repairs)
    assert client.cache.writes == []


@pytest.mark.parametrize("calls, tokens", [(2, 0), (100, 10)])
def test_each_repair_obeys_call_and_token_budgets(client_factory, calls, tokens):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return _response("invalid output", tokens=5)

    client = client_factory(handler, repairs=5, calls=calls, tokens=tokens)
    events = []
    client.recorder = events.append
    with pytest.raises(BudgetExhausted):
        client.generate_json(_request(), _Draft)
    assert len(payloads) == 2
    assert [event.repair_used for event in events] == [False, True]
    assert client.budget.snapshot()["used_live_calls"] == 2
    assert client.budget.snapshot()["used_total_tokens"] == 10
    assert client.cache.writes == []


@pytest.mark.parametrize("terminal", ["auth", "capability", "truncated"])
def test_terminal_errors_during_repair_are_not_retried_as_format_errors(client_factory, terminal):
    payloads = []
    expected_error = {
        "auth": AuthError,
        "capability": CapabilityError,
        "truncated": TruncatedError,
    }[terminal]

    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return _response("invalid output")
        if terminal == "auth":
            return httpx.Response(401, json={"error": "unauthorized"})
        if terminal == "capability":
            return httpx.Response(404, json={"error": "model unavailable"})
        return _response('{"hypotheses":', finish="length")

    client = client_factory(handler, repairs=5)
    events = []
    client.recorder = events.append
    with pytest.raises(expected_error):
        client.generate_json(_request(), _Draft)
    assert len(payloads) == 2
    assert [event.repair_used for event in events] == [False, True]
    assert client.cache.writes == []


def test_streaming_vision_repairs_preserve_images_prompts_context_and_thinking(
    client_factory, monkeypatch,
):
    contents = ["first bad output", '{"hypotheses": {}}', _content()]
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return _response(contents[len(payloads) - 1], stream=True)

    client = client_factory(handler, repairs=2, provider="openai_compatible", stream=True)
    image = ImagePayload(
        evidence_id="frame-1", frame_id="F0001", timestamp_sec=1.5,
        image_bytes=b"offline-image", image_sha256="image-content-hash",
    )
    request = _request(images=[image], enable_thinking=False)
    original = copy.deepcopy(request)
    built_requests = []
    build = client._build_payload

    def capture_build(request, with_images, json_mode):
        built_requests.append(copy.deepcopy(request))
        return build(request, with_images, json_mode)

    monkeypatch.setattr(client, "_build_payload", capture_build)
    assert client.analyze_images(request, _Draft).model_dump() == _valid_data()
    assert len(payloads) == 3
    assert built_requests == [original, original, original]
    assert request == original
    original_messages = payloads[0]["messages"]
    assert any(part["type"] == "image_url" for part in original_messages[1]["content"])
    for payload in payloads:
        assert payload["messages"][:2] == original_messages
        assert payload["model"] == client.llm.vision_model
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["enable_thinking"] is False
    assert payloads[2]["messages"][2]["content"] == contents[1]
    assert client.budget.snapshot()["used_total_tokens"] == 15


def test_repairs_share_original_deadline(client_factory, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bigmodel_module.time, "monotonic", lambda: now[0])
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        now[0] += 3.0
        return _response("invalid output")

    client = client_factory(handler, repairs=5, request_deadline_seconds=5.0)
    deadlines = []
    post = client._post

    def capture_post(payload, deadline, **kwargs):
        deadlines.append(deadline)
        return post(payload, deadline, **kwargs)

    monkeypatch.setattr(client, "_post", capture_post)
    with pytest.raises(TransientError):
        client.generate_json(_request(), _Draft)
    assert len(payloads) == 2
    assert deadlines and all(deadline == 105.0 for deadline in deadlines)
    assert client.budget.snapshot()["used_live_calls"] == 2
    assert client.cache.writes == []
