"""pytest 公共 fixture：合成 v1/v2 迷你数据包、真实数据路径。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REAL_DATA_ROOT = Path("D:/数据汇总/数据汇总")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_jpeg(path: Path, size=(64, 48), color=(30, 60, 90)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG", quality=80)


def _make_clip(path: Path, seconds: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=64x48:d={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def build_v2_package(root: Path) -> Path:
    """schema 1.0 迷你包：2 秒视频、2 全帧 + 1 半秒帧、1 ASR、1 OCR、2 评论。"""
    package = root / "vtest2"
    manifest = {
        "schema_version": "1.0",
        "video_id": "BVTEST00002",
        "duration_sec": 2.0,
        "split": "demo_only",
        "source_video": "source/video.mp4",
        "audio": "audio/audio_16k_mono.wav",
        "metadata": "metadata.json",
        "indices": {"frames": "indices/all_frames.jsonl", "segments": "indices/segment_inputs.jsonl"},
        "text": {"asr": "text/asr.jsonl", "ocr": "text/ocr.jsonl", "comments": "text/comments.jsonl"},
        "optional_source_data": {"ocr_spans": "text/ocr_spans.jsonl"},
        "counts": {
            "segments": 1, "frames": 3, "asr_utterances": 1,
            "ocr_default": 1, "ocr_observed_spans": 1, "comments": 2,
        },
    }
    _write_json(package / "agent_input.json", manifest)
    _write_json(
        package / "metadata.json",
        {
            "video_id": "BVTEST00002",
            "title": "智驾模式开启后突然偏离车道测试标题",
            "description_text": None,
            "description_status": "publisher_placeholder",
            "media_duration_sec": 2.0,
            "published_at": "2026-01-01T00:00:00+00:00",
            "url": "https://example.com",
            "model": None,
            "software_version": None,
        },
    )
    _write_jsonl(
        package / "indices/all_frames.jsonl",
        [
            {"frame_id": "F0000", "timestamp_sec": 0.0, "path": "frames/frame_0000.jpg",
             "width": 64, "height": 48, "split": "demo_only"},
            {"frame_id": "H0000", "timestamp_sec": 0.5, "path": "frames_ocr/half_0000.jpg",
             "width": 64, "height": 48, "split": "demo_only"},
            {"frame_id": "F0001", "timestamp_sec": 1.0, "path": "frames/frame_0001.jpg",
             "width": 64, "height": 48, "split": "demo_only"},
        ],
    )
    _write_jsonl(
        package / "indices/segment_inputs.jsonl",
        [
            {"segment_id": "S000", "start_sec": 0.0, "end_sec": 2.0, "path": "clips/S000.mp4",
             "frame_ids": ["F0000", "F0001"], "asr_ids": ["A000"], "ocr_ids": ["O00000"],
             "all_frame_ids": ["F0000", "H0000", "F0001"]},
        ],
    )
    _write_jsonl(
        package / "text/asr.jsonl",
        [
            {"candidate_id": "AC000", "utterance_id": "A000", "start_sec": 0.0,
             "end_sec": 1.5, "text": "智驾开着呢注意前面", "review_flags": ["not_human_listening_verified"],
             "human_reviewed": False},
        ],
    )
    _write_jsonl(
        package / "text/ocr.jsonl",
        [
            {"ocr_id": "O00000", "timestamp_sec": 0.5, "frame_id": "H0000",
             "text": "pilot全程亮", "pass": "full_frame", "human_reviewed": False,
             "raw_record_ids": ["R00001"]},
        ],
    )
    _write_jsonl(
        package / "text/ocr_spans.jsonl",
        [
            {"span_id": "T0000", "text": "pilot全程亮", "ocr_ids": ["O00000"],
             "first_observed_sec": 0.5, "last_observed_sec": 0.5,
             "source_type": "publisher_video_overlay"},
        ],
    )
    _write_jsonl(
        package / "text/comments.jsonl",
        [
            {"comment_id": "1001", "parent_id": None, "root_id": None,
             "text": "智驾有问题，这都能撞", "video_timestamp_sec": None,
             "created_at": "2026-01-02T00:00:00+00:00"},
            {"comment_id": "1002", "parent_id": "9999", "root_id": None,
             "text": "回复：同感", "video_timestamp_sec": None},
        ],
    )
    _make_jpeg(package / "frames/frame_0000.jpg")
    _make_jpeg(package / "frames/frame_0001.jpg")
    _make_jpeg(package / "frames_ocr/half_0000.jpg")
    _make_clip(package / "clips/S000.mp4", seconds=2.0)
    return package


def build_v1_package(root: Path) -> Path:
    """schema 1.0.0 迷你包：text_id/ocr_id 双 ID、字幕裁剪图。"""
    package = root / "vtest1"
    manifest = {
        "schema_version": "1.0.0",
        "package_id": "BVTEST00001_v1",
        "video_id": "BVTEST00001",
        "duration_sec": 2.0,
        "time_reference": "original_video_pts_seconds_from_zero",
        "metadata": "metadata.json",
        "text": {
            "asr": "text/asr.jsonl",
            "ocr": "text/ocr.jsonl",
            "ocr_grouped": "text/ocr_spans.jsonl",
            "comments": "text/comments.jsonl",
        },
        "indices": {
            "segments": "indices/segments.jsonl",
            "frames": "indices/frames.jsonl",
            "segment_inputs": "indices/segment_inputs.jsonl",
            "ocr_half_frames": "indices/ocr_half_frames.jsonl",
        },
        "quality": {"human_reviewed": False},
        "counts": {
            "segments": 1, "global_frames": 2, "additional_subtitle_timepoints": 1,
            "asr_utterances": 1, "ocr_default_observations": 2, "ocr_grouped_spans": 1,
            "comments": 1,
        },
        "split": "demo_only",
    }
    _write_json(package / "agent_input.json", manifest)
    _write_json(
        package / "metadata.json",
        {"video_id": "BVTEST00001", "title": "v1 测试标题", "description_text": None,
         "media_duration_sec": 2.0, "model": None, "software_version": None},
    )
    _write_jsonl(
        package / "indices/frames.jsonl",
        [
            {"frame_id": "F0000", "timestamp_sec": 0.0, "path": "frames/frame_0000.jpg",
             "width": 64, "height": 48},
            {"frame_id": "F0001", "timestamp_sec": 1.0, "path": "frames/frame_0001.jpg",
             "width": 64, "height": 48},
        ],
    )
    _write_jsonl(
        package / "indices/ocr_half_frames.jsonl",
        [
            {"frame_id": "H0000", "timestamp_sec": 0.5, "crop_path": "ocr_crops/strip_0000.jpg",
             "crop_layout": {"top": [0, 0, 64, 10], "bottom": [0, 38, 64, 48]}},
        ],
    )
    _write_jsonl(
        package / "indices/segment_inputs.jsonl",
        [
            {"segment_id": "S000", "start_sec": 0.0, "end_sec": 2.0,
             "video_path": "clips/S000.mp4", "frame_ids": ["F0000", "F0001"],
             "asr_ids": ["A000"], "ocr_ids": ["O0000", "O0001"]},
        ],
    )
    _write_jsonl(
        package / "indices/segments.jsonl",
        [
            {"segment_id": "S000", "start_sec": 0.0, "end_sec": 2.0, "path": "clips/S000.mp4"},
        ],
    )
    _write_jsonl(
        package / "text/asr.jsonl",
        [{"utterance_id": "A000", "start_sec": 0.1, "end_sec": 1.2,
          "text": "v1 语音测试", "quality_flags": ["possible_homophone_or_omission"]}],
    )
    # 关键：对外 text_id=O0000/O0001，原始 ocr_id=R 前缀
    _write_jsonl(
        package / "text/ocr.jsonl",
        [
            {"ocr_id": "R04750", "text_id": "O0000", "timestamp_sec": 0.5,
             "frame_id": "H0000", "text": "字幕条带上的字", "pass": "subtitle_strip",
             "human_reviewed": False},
            {"ocr_id": "R00038", "text_id": "O0001", "timestamp_sec": 1.0,
             "frame_id": "F0001", "text": "全帧上的字", "pass": "full_frame",
             "human_reviewed": False},
        ],
    )
    _write_jsonl(
        package / "text/ocr_spans.jsonl",
        [{"span_id": "T000", "text": "字幕条带上的字", "observation_ids": ["O0000"],
          "first_observed_sec": 0.5, "last_observed_sec": 0.5}],
    )
    _write_jsonl(
        package / "text/comments.jsonl",
        [{"comment_id": "2001", "parent_id": None, "root_id": None,
          "text": "v1 评论", "video_timestamp_sec": None}],
    )
    _make_jpeg(package / "frames/frame_0000.jpg")
    _make_jpeg(package / "frames/frame_0001.jpg")
    _make_jpeg(package / "ocr_crops/strip_0000.jpg", size=(64, 20))
    _make_clip(package / "clips/S000.mp4", seconds=2.0)
    return package


@pytest.fixture()
def v2_package(tmp_path):
    return build_v2_package(tmp_path)


@pytest.fixture()
def v1_package(tmp_path):
    return build_v1_package(tmp_path)


@pytest.fixture()
def real_data_root():
    if not REAL_DATA_ROOT.is_dir():
        pytest.skip("真实数据目录不存在")
    return REAL_DATA_ROOT
