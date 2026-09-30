"""embedding 增强模块单测：向量库、客户端、同源检测、预筛、跨视频聚类。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from defect_agent.config import AgentConfig, LLMSettings
from defect_agent.llm import BudgetTracker, ResponseCache
from defect_agent.llm.embeddings import (
    EmbeddingClient,
    EmbeddingError,
    MockEmbeddingClient,
    _cache_key,
    create_embedding_client,
)
from defect_agent.schemas import Claim
from defect_agent.vectors import VectorItem, VectorStore, cosine


# ---------------------------------------------------------------------------
# 向量库
# ---------------------------------------------------------------------------


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_vector_store_top_k_and_pairs():
    store = VectorStore()
    store.upsert_many(
        [
            VectorItem("e1", "V1", "event", "车辆偏离车道", [1.0, 0.0]),
            VectorItem("e2", "V1", "event", "完全无关内容", [0.0, 1.0]),
            VectorItem("c1", "V1", "claim", "车辆偏离车道指控", [0.99, 0.1]),
        ]
    )
    top = store.top_k([1.0, 0.05], k=2, kinds=["event"])
    assert top[0][0] == "e1"
    assert top[0][1] > 0.99
    pairs = store.pairs_above(0.9)
    assert {(a.item_id, b.item_id) for a, b, _ in pairs} == {("e1", "c1")}  # 按插入顺序配对
    # 跨视频模式：同视频配对被排除
    assert store.pairs_above(0.0, cross_video_only=True) == []


def test_clusters_by_threshold():
    store = VectorStore()
    store.upsert_many(
        [
            VectorItem("a", "V1", "event", "x", [1.0, 0.0]),
            VectorItem("b", "V2", "event", "x", [0.99, 0.05]),   # 与 a 相似 → 同组
            VectorItem("c", "V3", "event", "y", [0.0, 1.0]),     # 独立
        ]
    )
    groups = {tuple(sorted(m.item_id for m in g)) for g in store.clusters_by_threshold(0.9)}
    assert ("a", "b") in groups
    assert ("c",) in groups


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_mock_embedding_deterministic():
    client = MockEmbeddingClient()
    a = client.embed(["智驾有问题"])[0]
    b = client.embed(["智驾有问题"])[0]
    c = client.embed(["完全不同的文本"])[0]
    assert a == b
    assert cosine(a, c) < 0.5  # 不同文本不相似
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9  # 已归一化


def _embedding_config(model="text-embedding-v4") -> AgentConfig:
    return AgentConfig(
        yaml_path=None,
        data_root=None,
        env={"EMBEDDING_MODEL": model},
        raw={"embedding": {"model": model}},
        llm=LLMSettings(api_key="k", provider="openai_compatible"),
    )


def _ok_embedding_response(texts):
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": i, "embedding": [0.1 * (i + 1), 0.2]} for i in range(len(texts))
            ],
            "usage": {"total_tokens": 10},
        },
    )


def test_embedding_client_parses_and_batches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        return _ok_embedding_response(payload["input"])

    config = _embedding_config()
    config.raw["embedding"]["batch_size"] = 2
    client = EmbeddingClient(
        config=config, model="text-embedding-v4",
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=ResponseCache(None), transport=httpx.MockTransport(handler),
    )
    vectors = client.embed(["a", "b", "c"])
    # 响应按“请求内索引”生成向量：批1 [a,b] → 前2条；批2 [c] → 索引0 重新计数
    assert vectors == [[0.1, 0.2], [0.2, 0.2], [0.1, 0.2]]
    assert len(calls) == 2  # 3 条文本按批 2 拆成 2 次请求
    assert calls[0]["model"] == "text-embedding-v4"


def test_batch_size_clamped_to_provider_limit():
    """官方 embedding 接口单请求上限 10 条：配大了自动钳制。"""
    config = _embedding_config()
    config.raw["embedding"]["batch_size"] = 32
    client = EmbeddingClient(
        config=config, model="text-embedding-v3",
        budget=None, cache=None,
        transport=httpx.MockTransport(lambda r: _ok_embedding_response(json.loads(r.content.decode())["input"])),
    )
    assert client.batch_size == 10


def test_embedding_cache_prevents_second_call(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        return _ok_embedding_response(payload["input"])

    cache = ResponseCache(tmp_path / "c.sqlite")
    client = EmbeddingClient(
        config=_embedding_config(), model="m",
        budget=BudgetTracker(max_live_calls=10, max_total_tokens=0),
        cache=cache, transport=httpx.MockTransport(handler),
    )
    client.embed(["同一段文本"])
    client.embed(["同一段文本"])
    assert len(calls) == 1  # 文本级缓存命中
    cache.close()


def test_embedding_model_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    client = EmbeddingClient(
        config=_embedding_config(), model="bad-model",
        budget=None, cache=None, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(EmbeddingError):
        client.embed(["x"])


def test_embedding_separate_endpoint_and_key():
    """embedding 可用独立端点/密钥（对话走中转、embedding 走官方）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return _ok_embedding_response(["x"])

    config = AgentConfig(
        yaml_path=None, data_root=None,
        env={"EMBEDDING_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "EMBEDDING_API_KEY": "sk-emb-key"},
        llm=LLMSettings(api_key="chat-key", provider="openai_compatible"),
    )
    client = EmbeddingClient(
        config=config, model="text-embedding-v3",
        base_url=config.env.get("EMBEDDING_BASE_URL"),
        api_key=config.env.get("EMBEDDING_API_KEY"),
        transport=httpx.MockTransport(handler),
    )
    client.embed(["x"])
    assert captured["url"].startswith("https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings")
    assert captured["auth"] == "Bearer sk-emb-key"


def test_create_embedding_client_fallbacks(tmp_path):
    # mock 模式 → mock 客户端
    config = AgentConfig(yaml_path=None, data_root=None, llm=LLMSettings(mode="mock"))
    assert isinstance(create_embedding_client(config, None, None), MockEmbeddingClient)
    # live 无密钥 → None
    config = AgentConfig(yaml_path=None, data_root=None, llm=LLMSettings(mode="live", api_key=None))
    assert create_embedding_client(config, None, None) is None
    # 配置禁用 → None
    config = AgentConfig(
        yaml_path=None, data_root=None,
        raw={"embedding": {"enabled": False}},
        llm=LLMSettings(mode="live", api_key="k"),
    )
    assert create_embedding_client(config, None, None) is None


# ---------------------------------------------------------------------------
# 功能 B：同源复述检测
# ---------------------------------------------------------------------------


def _claim(claim_id, statement):
    return Claim(
        claim_id=claim_id, video_id="V", statement=statement,
        claim_type="phenomenon_claim", evidence_ids=["V::ocr::O1"],
        source_group_id="g", temporal_scope="video",
    )


def test_same_source_marks_but_never_deletes():
    from defect_agent.embedding_features import same_source_pass

    claims = [
        _claim("C1", "智驾开启时车辆突然撞上护栏"),
        _claim("C2", "智驾开启时车辆突然撞上护栏"),  # 完全相同 → mock 向量余弦 1
        _claim("C3", "今天天气很好适合出游"),        # 无关
    ]
    relations, records = same_source_pass(claims, MockEmbeddingClient(), 0.90, "V")
    assert len(relations) == 1 and relations[0].relation == "same_source"
    assert records[0]["claim_a"] == "C1" and records[0]["claim_b"] == "C2"
    assert len(claims) == 3  # 不删除任何 claim


def test_same_source_disabled_when_no_client():
    from defect_agent.embedding_features import same_source_pass

    relations, records = same_source_pass([_claim("C1", "a"), _claim("C2", "b")], None, 0.9, "V")
    assert relations == [] and records == []


# ---------------------------------------------------------------------------
# 功能 C：对齐预筛
# ---------------------------------------------------------------------------


def test_prefilter_keeps_temporal_and_limits_video_level():
    from defect_agent.agents.align import EventCluster, build_candidates
    from defect_agent.embedding_features import build_prefilter_retrieval

    events = [
        EventCluster("E001", 1.0, 5.0, "lane_boundary_crossing", ["O1"], "车辆压线驶出车道"),
        EventCluster("E002", 30.0, 36.0, "collision_or_contact", ["O2"], "车辆撞上护栏起火"),
        EventCluster("E003", 60.0, 66.0, "other", ["O3"], "驾驶员接管方向盘"),
    ]
    video_claim = _claim("C1", "车辆压线驶出车道")  # 无时间 → 视频级
    temporal_claim = Claim(
        claim_id="C2", video_id="V", statement="撞车瞬间",
        claim_type="phenomenon_claim", evidence_ids=["V::asr::A1"],
        source_group_id="g", temporal_scope="interval", start_sec=31.0, end_sec=34.0,
    )

    retrieval, log = build_prefilter_retrieval(
        [video_claim, temporal_claim], events, MockEmbeddingClient(), top_k=1
    )
    # 视频级陈述被限到语义最近的 1 个事件（mock 下与 E001 文本一致 → 余弦 1）
    assert retrieval["C1"] == {"E001"}
    assert len(log) == 1 and "E002" in log[0]["filtered_out"]

    candidates = build_candidates([video_claim, temporal_claim], events, retrieval=retrieval)
    pairs = {(c.claim_id, c.event_local_id) for c in candidates}
    # 时间级候选不受预筛影响：C2 与 E002 时间重叠 → 保留
    assert ("C2", "E002") in pairs
    # 视频级只保留 top-1
    assert ("C1", "E001") in pairs
    assert ("C1", "E002") not in pairs and ("C1", "E003") not in pairs


def test_prefilter_off_by_default():
    from defect_agent.agents.align import EventCluster, build_candidates

    events = [EventCluster("E001", 1.0, 5.0, "other", ["O1"], "s")]
    claim = _claim("C1", "无时间陈述")
    assert len(build_candidates([claim], events)) == 1  # 无 retrieval → 全量候选


# ---------------------------------------------------------------------------
# 功能 A：跨视频聚类（命令级，mock embedding）
# ---------------------------------------------------------------------------


def test_crossvideo_clusters_identical_events(tmp_path, monkeypatch):
    import defect_agent.config as config_module
    from defect_agent.embedding_features import run_crossvideo

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    for video, event_id, summary in [
        ("BV1", "r1::BV1::event::E001", "车辆在弯道撞上护栏，智驾开启"),
        ("BV2", "r1::BV2::event::E001", "车辆在弯道撞上护栏，智驾开启"),  # 同一事故转载
        ("BV3", "r1::BV3::event::E001", "完全不同的城市道路夜间行驶画面"),
    ]:
        video_dir = run_dir / "videos" / video
        video_dir.mkdir(parents=True)
        (video_dir / "events.jsonl").write_text(
            json.dumps({
                "event_id": event_id, "video_id": video,
                "start_sec": 1.0, "end_sec": 5.0,
                "phenomenon_type": "collision_or_contact", "phenomenon_summary": summary,
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    args = SimpleNamespace(run_id="r1", threshold=0.90, config=None)
    assert run_crossvideo(args) == 0
    payload = json.loads((run_dir / "crossvideo_groups.json").read_text(encoding="utf-8"))
    cross_groups = [g for g in payload["groups"] if g["cross_video"]]
    assert len(cross_groups) == 1
    assert {m["video_id"] for m in cross_groups[0]["members"]} == {"BV1", "BV2"}
    assert payload["is_mock"] is True
