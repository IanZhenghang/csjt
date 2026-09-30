"""T08 对齐与事件合并单测。"""

from __future__ import annotations

from defect_agent.agents.align import (
    build_candidates,
    merge_observations,
)
from defect_agent.schemas import Claim, VisualObservation


def _obs(obs_id, start, end, phenomenon="lane_boundary_crossing", continuity="continuous", window="W001"):
    return VisualObservation(
        observation_id=obs_id,
        video_id="V",
        window_id=f"V::window::{window}",
        description="测试观察",
        phenomenon_type=phenomenon,
        evidence_ids=["V::frame::F0000"],
        start_sec=start,
        end_sec=end,
        visibility="clear",
        continuity=continuity,
    )


def test_adjacent_same_type_merge():
    observations = [_obs("O1", 0.0, 6.0, window="W001"), _obs("O2", 6.0, 12.0, window="W002")]
    events = merge_observations(observations)
    assert len(events) == 1
    assert events[0].start_sec == 0.0 and events[0].end_sec == 12.0
    assert events[0].observation_ids == ["O1", "O2"]


def test_gap_beyond_one_second_keeps_separate():
    observations = [_obs("O1", 0.0, 6.0), _obs("O2", 8.5, 12.0)]
    events = merge_observations(observations)
    assert len(events) == 2


def test_edit_suspicion_blocks_merge_and_marks_related():
    observations = [
        _obs("O1", 0.0, 6.0),
        _obs("O2", 6.2, 12.0, continuity="replay_suspected"),
    ]
    events = merge_observations(observations)
    assert len(events) == 2  # 剪辑/回放疑点 → 不强行合并
    assert events[0].possibly_related == ["E002"]


def test_different_types_not_merged():
    observations = [
        _obs("O1", 0.0, 6.0, phenomenon="lane_boundary_crossing"),
        _obs("O2", 5.0, 8.0, phenomenon="apparent_deceleration"),
    ]
    events = merge_observations(observations)
    assert len(events) == 2


def test_none_observed_never_forms_event():
    observations = [_obs("O1", 0.0, 6.0, phenomenon="none_observed")]
    assert merge_observations(observations) == []


def _claim(claim_id, start=None, end=None):
    return Claim(
        claim_id=claim_id,
        video_id="V",
        statement="陈述",
        claim_type="phenomenon_claim",
        evidence_ids=["V::comment::1"],
        source_group_id="g",
        temporal_scope="interval" if start is not None else "video",
        start_sec=start,
        end_sec=end,
    )


def test_comments_only_get_video_level_candidates():
    """评论没有时间：只能建立视频级候选关联，不产生时间级关联。"""
    from defect_agent.agents.align import EventCluster

    events = [EventCluster(event_local_id="E001", start_sec=1.0, end_sec=5.0,
                           phenomenon_type="other", observation_ids=["O1"], summary="s")]
    comment_claim = _claim("C1")  # 无时间
    temporal_claim = _claim("C2", start=2.0, end=4.0)
    candidates = build_candidates([comment_claim, temporal_claim], events)
    kinds = {(c.claim_id, c.kind) for c in candidates}
    assert ("C1", "video_level") in kinds
    assert ("C2", "temporal") in kinds
    assert ("C1", "temporal") not in kinds


def test_far_temporal_claim_not_candidate():
    from defect_agent.agents.align import EventCluster

    events = [EventCluster(event_local_id="E001", start_sec=50.0, end_sec=55.0,
                           phenomenon_type="other", observation_ids=["O1"], summary="s")]
    claim = _claim("C2", start=2.0, end=4.0)
    candidates = build_candidates([claim], events)
    assert candidates == []  # 时间相距远 → 不给候选（评论除外）


def test_comment_claim_time_stays_none():
    """评论 claim 不因对齐而获得时间。"""
    claim = _claim("C1")
    assert claim.start_sec is None and claim.end_sec is None
