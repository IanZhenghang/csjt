"""T12 评估指标单测：IoU 一对一匹配、引用指标。"""

from __future__ import annotations

from defect_agent.evaluation import (
    AnnotationRecord,
    match_events,
)


def _pred(start, end, phenomenon="lane_boundary_crossing", video="V"):
    return {"video_id": video, "start_sec": start, "end_sec": end, "phenomenon_type": phenomenon}


def _gt(start, end, phenomenon="lane_boundary_crossing", video="V"):
    return AnnotationRecord(
        video_id=video, start_sec=start, end_sec=end, phenomenon_type=phenomenon
    )


def test_perfect_match():
    matched, predicted, truth = match_events([_pred(0, 6)], [_gt(0, 6)], 0.3)
    assert (matched, predicted, truth) == (1, 1, 1)


def test_iou_below_threshold_no_match():
    matched, _, _ = match_events([_pred(0, 6)], [_gt(20, 26)], 0.3)
    assert matched == 0


def test_iou_half_threshold_sensitivity():
    # 交并比 0.25：IoU≥0.3 不匹配，IoU≥0.1 匹配 → 敏感性口径差异
    matched_03, _, _ = match_events([_pred(0, 8)], [_gt(0, 2)], 0.3)
    matched_01, _, _ = match_events([_pred(0, 8)], [_gt(0, 2)], 0.1)
    assert matched_03 == 0 and matched_01 == 1


def test_one_to_one_duplicate_predictions_count_as_fp():
    # 重复预测不能全部匹配同一个人工事件
    matched, predicted, truth = match_events(
        [_pred(0, 6), _pred(0, 6)], [_gt(0, 6)], 0.3
    )
    assert matched == 1 and predicted == 2 and truth == 1


def test_phenomenon_type_must_match():
    matched, _, _ = match_events(
        [_pred(0, 6, phenomenon="other")], [_gt(0, 6)], 0.3
    )
    assert matched == 0


def test_cross_video_never_matches():
    matched, _, _ = match_events([_pred(0, 6, video="A")], [_gt(0, 6, video="B")], 0.3)
    assert matched == 0
