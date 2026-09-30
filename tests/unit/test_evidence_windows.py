"""T04 证据库与窗口单测。"""

from __future__ import annotations

import pytest

from defect_agent.adapters import load_package
from defect_agent.agents.visual_observe import build_frame_batches
from defect_agent.config import load_config
from defect_agent.evidence import build_evidence_store, global_id
from defect_agent.windows import build_windows


def _store_and_windows(package_root, config=None):
    config = config or load_config()
    package = load_package(package_root)
    store = build_evidence_store(package)
    windows = build_windows(package, config.window)
    return package, store, windows


def test_global_ids_no_cross_video_collision(v2_package, v1_package):
    """不同视频存在相同局部 ID（A000/F0000）时全局 ID 不冲突。"""
    package_a = load_package(v2_package)
    package_b = load_package(v1_package)
    store_a = build_evidence_store(package_a)
    store_b = build_evidence_store(package_b)
    id_a = store_a.kind_id("asr", "A000")
    id_b = store_b.kind_id("asr", "A000")
    assert id_a != id_b
    assert id_a == "BVTEST00002::asr::A000"
    assert id_b == "BVTEST00001::asr::A000"


def test_ocr_span_keeps_underlying_refs(v2_package):
    package, store, _ = _store_and_windows(v2_package)
    spans = store.by_kind("ocr_span")
    assert len(spans) == 1
    assert spans[0].raw_refs == [global_id(package.video_id, "ocr", "O00000")]
    underlying = store.get(global_id(package.video_id, "ocr", "O00000"))
    assert underlying.source_group_id == spans[0].source_group_id


def test_subtitle_crop_never_in_visual_batches(v1_package, tmp_path):
    """裁剪图不进入道路全帧视觉输入。"""
    package, store, windows = _store_and_windows(v1_package)
    batches = build_frame_batches(v1_package, store, windows, {}, tmp_path / "media")
    for batch in batches:
        for image in batch.images:
            record = store.get(image.evidence_id)
            assert record.view_kind == "full_frame"


def test_non_integer_timestamps_preserved(v1_package):
    package, store, _ = _store_and_windows(v1_package)
    half_frame = store.get(global_id(package.video_id, "frame", "H0000"))
    assert half_frame.timestamp_sec == 0.5
    asr = store.get(global_id(package.video_id, "asr", "A000"))
    assert asr.start_sec == 0.1 and asr.end_sec == 1.2


def test_short_tail_window_kept(v2_package):
    """v6 式极短尾段（0.099s）仍进入处理计划。"""
    package = load_package(v2_package)
    object.__setattr__(package, "duration_sec", 6.099)
    windows = build_windows(
        package,
        {"core_seconds": 6, "context_before_seconds": 1, "context_after_seconds": 1,
         "initial_sampling_hz": 1, "max_images_per_request": 8},
    )
    assert windows[-1].core_end - windows[-1].core_start == pytest.approx(0.099, abs=1e-6)
    assert any("尾部窗口" in note for note in windows[-1].notes)


def test_comment_time_scope_is_video(v2_package):
    _, store, _ = _store_and_windows(v2_package)
    comments = store.by_kind("comment")
    assert comments
    for comment in comments:
        assert comment.time_scope == "video"
        assert comment.timestamp_sec is None
