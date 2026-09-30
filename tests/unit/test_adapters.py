"""T02/T03 适配器与数据校验单测。"""

from __future__ import annotations

import json

import pytest

from defect_agent.adapters import AdapterError, discover_packages, load_package
from defect_agent.validation import validate_package


def test_v1_text_id_is_public_ocr_key(v1_package):
    """v1 的 Oxxxx 与 Rxxxxx 是不同 ID：片段引用 O0000 必须连到默认 OCR（text_id）。"""
    package = load_package(v1_package)
    ocr_ids = {record.ocr_id for record in package.ocr}
    assert ocr_ids == {"O0000", "O0001"}  # 对外 ID 是 text_id
    raw_ids = {raw for record in package.ocr for raw in record.raw_record_ids}
    assert raw_ids == {"R04750", "R00038"}  # 原始 R 前缀 ID 另存
    # 片段引用可解析
    segment = package.segments[0]
    assert set(segment.ocr_ids) <= ocr_ids


def test_v1_missing_text_id_raises(v1_package):
    """v1 OCR 记录缺 text_id 时明确报错，不猜测兼容。"""
    path = v1_package / "text/ocr.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    del records[0]["text_id"]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    with pytest.raises(AdapterError, match="text_id"):
        load_package(v1_package)


def test_v1_subtitle_crop_marked(v1_package):
    package = load_package(v1_package)
    crops = [f for f in package.frames if f.view_kind == "subtitle_crop"]
    assert len(crops) == 1 and crops[0].frame_id == "H0000"
    assert crops[0].media_path == "ocr_crops/strip_0000.jpg"


def test_v2_adapter_fields(v2_package):
    package = load_package(v2_package)
    assert package.adapter == "v2"
    assert package.segments[0].media_path == "clips/S000.mp4"
    asr_ids = {record.asr_id for record in package.asr}
    assert asr_ids == {"A000"}  # utterance_id 为主键
    assert package.asr[0].raw_refs == ["AC000"]  # candidate_id 回溯


def test_unknown_schema_rejected(tmp_path):
    package = tmp_path / "vbad"
    package.mkdir(parents=True)
    (package / "agent_input.json").write_text(
        json.dumps({"schema_version": "9.9", "video_id": "X"}), encoding="utf-8"
    )
    with pytest.raises(AdapterError, match="未知数据包结构"):
        load_package(package)


def test_empty_asr_is_legal(tmp_path, v2_package):
    """v3/v7 式空 ASR：记录缺失但其余模态继续处理。"""
    (v2_package / "text/asr.jsonl").write_text("", encoding="utf-8")
    seg_path = v2_package / "indices/segment_inputs.jsonl"
    records = [json.loads(l) for l in seg_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    records[0]["asr_ids"] = []
    seg_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    manifest_path = v2_package / "agent_input.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["asr_utterances"] = 0  # 与数据一致
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    package = load_package(v2_package)
    assert package.asr == []
    result = validate_package(package, v2_package)
    codes = [issue.code for issue in result.issues]
    assert "asr_empty" in codes
    assert result.status in ("ok", "warnings")


def test_comments_have_no_video_time(v2_package):
    package = load_package(v2_package)
    assert all(comment.video_timestamp_sec is None for comment in package.comments)


def test_missing_parent_comment_flagged(v2_package):
    package = load_package(v2_package)
    result = validate_package(package, v2_package)
    codes = [issue.code for issue in result.issues]
    assert "comment_parent_missing" in codes  # 1002 的父评论 9999 缺失


def test_dangling_reference_detected(v2_package):
    """悬空 ID 必须被校验发现。"""
    path = v2_package / "indices/segment_inputs.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    records[0]["asr_ids"] = ["A999"]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    package = load_package(v2_package)
    result = validate_package(package, v2_package)
    assert result.status == "errors"
    assert any(issue.code == "dangling_segment_ref" for issue in result.issues)


def test_path_escape_rejected(tmp_path, v2_package):
    """路径逃出数据包目录必须报错。"""
    manifest_path = v2_package / "agent_input.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["text"]["ocr"] = "../outside.jsonl"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AdapterError, match="逃出数据包目录"):
        load_package(v2_package)


def test_discover_packages_order(real_data_root):
    packages = discover_packages(real_data_root)
    names = [p.name for p in packages]
    assert names == [f"v{i}" for i in range(1, 11)]
