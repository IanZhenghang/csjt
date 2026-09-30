"""v1 / v2 适配器共用逻辑：评论、metadata、ASR/OCR 通用解析。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schemas import (
    AsrRecord,
    CommentRecord,
    NormalizedPackage,
    OcrRecord,
    OcrSpanRecord,
    VideoMeta,
)
from .base import AdapterError, iter_jsonl, read_json, safe_join, sha256_file


def load_comments(path: Path) -> list[CommentRecord]:
    records: list[CommentRecord] = []
    for rec in iter_jsonl(path):
        records.append(
            CommentRecord(
                comment_id=str(rec["comment_id"]),
                parent_id=str(rec["parent_id"]) if rec.get("parent_id") is not None else None,
                root_id=str(rec["root_id"]) if rec.get("root_id") is not None else None,
                text=rec.get("text") or "",
                created_at=rec.get("created_at"),
                likes_at_retrieval=rec.get("likes_at_retrieval"),
                video_timestamp_sec=rec.get("video_timestamp_sec"),
            )
        )
    return records


def load_metadata(path: Path) -> VideoMeta:
    data = read_json(path)
    description = data.get("description_text")
    if not description or not str(description).strip():
        description = None
    return VideoMeta(
        video_id=str(data.get("video_id", "")),
        title=data.get("title"),
        description=description,
        description_status=data.get("description_status"),
        duration_sec=data.get("media_duration_sec"),
        published_at=data.get("published_at"),
        url=data.get("url"),
        author_name=data.get("author_name"),
        model=data.get("model"),
        software_version=data.get("software_version"),
    )


def load_asr_v1(path: Path) -> list[AsrRecord]:
    """v1：主键 utterance_id（A000），质量标记在 quality_flags。"""
    records: list[AsrRecord] = []
    for rec in iter_jsonl(path):
        records.append(
            AsrRecord(
                asr_id=str(rec["utterance_id"]),
                start_sec=float(rec["start_sec"]),
                end_sec=float(rec["end_sec"]),
                text=rec.get("text") or "",
                method=rec.get("method"),
                human_reviewed=bool(rec.get("human_reviewed", False)),
                quality_flags=list(rec.get("quality_flags") or []),
            )
        )
    return records


def load_asr_v2(path: Path) -> list[AsrRecord]:
    """v2+：主键 candidate_id（AC 前缀），segment_inputs 引用 utterance_id，
    统一以 utterance_id 为主键；review_flags 并入 quality_flags。"""
    records: list[AsrRecord] = []
    for rec in iter_jsonl(path):
        if rec.get("excluded_from_default"):
            continue
        quality_flags = list(rec.get("quality_flags") or [])
        for flag in rec.get("review_flags") or []:
            if flag not in quality_flags:
                quality_flags.append(flag)
        utterance_id = rec.get("utterance_id")
        if not utterance_id:
            raise AdapterError(f"v2 ASR 记录缺少 utterance_id: {rec.get('candidate_id')}")
        records.append(
            AsrRecord(
                asr_id=str(utterance_id),
                start_sec=float(rec["start_sec"]),
                end_sec=float(rec["end_sec"]),
                text=rec.get("text") or "",
                method=rec.get("method"),
                human_reviewed=bool(rec.get("human_reviewed", False)),
                quality_flags=quality_flags,
                raw_refs=[str(rec["candidate_id"])] if rec.get("candidate_id") else [],
            )
        )
    return records


def load_ocr_record(
    rec: dict[str, Any],
    public_id: str,
    raw_ids: list[str],
) -> OcrRecord:
    return OcrRecord(
        ocr_id=public_id,
        timestamp_sec=float(rec["timestamp_sec"]),
        frame_id=str(rec["frame_id"]) if rec.get("frame_id") is not None else None,
        text=rec.get("text") if rec.get("text") else rec.get("text_raw") or "",
        source_type=rec.get("source_type"),
        source_pass=rec.get("pass"),
        human_reviewed=bool(rec.get("human_reviewed", False)),
        quality_flags=(
            [f"source_pass:{rec['pass']}"] if rec.get("pass") == "subtitle_strip" else []
        ),
        raw_record_ids=raw_ids,
    )


def load_span(rec: dict[str, Any], id_field: str) -> OcrSpanRecord:
    return OcrSpanRecord(
        span_id=str(rec["span_id"]),
        text=rec.get("text") or "",
        ocr_ids=[str(i) for i in rec.get(id_field) or []],
        first_observed_sec=(
            float(rec["first_observed_sec"]) if rec.get("first_observed_sec") is not None else None
        ),
        last_observed_sec=(
            float(rec["last_observed_sec"]) if rec.get("last_observed_sec") is not None else None
        ),
        source_type=rec.get("source_type"),
    )


def hash_inputs(package_root: Path, relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in relative_paths:
        absolute = safe_join(package_root, rel)
        if absolute.is_file():
            hashes[rel] = sha256_file(absolute)
    return hashes


def package_sort_key(package_id: str) -> tuple[int, str]:
    """v1..v10 按数字排序，其他名称按字典序排后。"""
    if package_id.startswith("v") and package_id[1:].isdigit():
        return (int(package_id[1:]), "")
    return (10**9, package_id)


def finalize_counts(package: NormalizedPackage) -> dict[str, int]:
    return {
        "segments": len(package.segments),
        "frames": len(package.frames),
        "asr": len(package.asr),
        "ocr": len(package.ocr),
        "ocr_spans": len(package.ocr_spans),
        "comments": len(package.comments),
    }
