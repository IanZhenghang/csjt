"""性能优化单测：对齐按陈述分批、时间文本合并选项。"""

from __future__ import annotations

from defect_agent.agents.align import EventCluster, align_evidence, build_candidates
from defect_agent.agents.text_extract import build_text_batches
from defect_agent.adapters import load_package
from defect_agent.config import load_config
from defect_agent.evidence import build_evidence_store
from defect_agent.llm import MockLLMClient
from defect_agent.schemas import Claim
from defect_agent.windows import build_windows


def _claims(count: int) -> list[Claim]:
    return [
        Claim(
            claim_id=f"C{i:03d}",
            video_id="V",
            statement=f"陈述 {i}",
            claim_type="phenomenon_claim",
            evidence_ids=["V::ocr::O0001"],
            source_group_id="g",
            temporal_scope="video",
        )
        for i in range(count)
    ]


def _events(count: int) -> list[EventCluster]:
    return [
        EventCluster(
            event_local_id=f"E{i:03d}",
            start_sec=float(i * 10),
            end_sec=float(i * 10 + 5),
            phenomenon_type="other",
            observation_ids=["OBS1"],
            summary="s",
        )
        for i in range(count)
    ]


def test_align_calls_scale_with_claims_not_pairs():
    """对齐调用次数 ≈ 陈述数/批次大小，与事件数无关（旧的按候选对分批为 陈述×事件/40）。"""
    claims = _claims(40)
    events = _events(8)  # 旧方案：40×8=320 对 → 8 次；新方案（批=10）：4 次
    client = MockLLMClient()
    result = align_evidence(claims, events, client)
    assert client.call_count == 4
    assert len(result.associations) == 40 * 8  # 每对都有 mock 关联


def test_align_finest_granularity_one_claim_per_request():
    """claims_per_request=1：每条陈述单独请求（用户选择的默认最细粒度）。"""
    claims = _claims(12)
    events = _events(5)
    client = MockLLMClient()
    result = align_evidence(claims, events, client, claims_per_request=1)
    assert client.call_count == 12
    assert len(result.associations) == 12 * 5


def test_align_empty_inputs_no_calls():
    client = MockLLMClient()
    align_evidence(_claims(5), [], client)
    align_evidence([], _events(5), client)
    assert client.call_count == 0


def test_merged_time_text_batching_reduces_batches(v2_package):
    """merged：全部窗口时间文本合并去重拆批，批次数远少于窗口数。"""
    config = load_config()
    package = load_package(v2_package)
    store = build_evidence_store(package)
    windows = build_windows(package, config.window)

    per_window_batches = build_text_batches(package, store, windows, {**config.raw})
    merged_batches = build_text_batches(
        package, store, windows, {**config.raw, "text": {**config.raw.get("text", {}),
                                                         "time_text_batching": "merged"}}
    )
    per_window_time = [b for b in per_window_batches if b.batch_kind == "time_text"]
    merged_time = [b for b in merged_batches if b.batch_kind == "time_text"]
    # 合成包只有 1 个窗口，记录数少；去重后不超过 1 批
    assert len(merged_time) <= 1
    # 合并不丢记录：去重后记录集合一致
    per_ids = {r["evidence_id"] for b in per_window_batches for r in b.records}
    merged_ids = {r["evidence_id"] for b in merged_batches for r in b.records}
    assert per_ids == merged_ids


def test_truncated_align_does_not_crash_pipeline(v2_package, tmp_path, monkeypatch):
    """对齐输出截断：任务记为 truncated 失败，流程不崩溃，视频状态 partial。"""
    import defect_agent.config as config_module
    import defect_agent.pipeline as pipeline_module
    from defect_agent.llm import MockLLMClient

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_module, "PROJECT_ROOT", tmp_path)
    config = load_config()
    ctx = pipeline_module.create_run(config, "mock", None)
    # 对齐阶段持续截断（真实运行中 10 陈述 × 24 事件曾超出 max_tokens）
    fault_client = MockLLMClient(faults={"align:truncated": 999})
    fault_client.recorder = ctx.client.recorder
    ctx.client = fault_client
    result = pipeline_module.process_video(ctx, v2_package)
    assert result["status"] == "partial"
    assert "align:truncated" in result["note"]

