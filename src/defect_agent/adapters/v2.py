"""PackageV2Adapter：适配 v2–v10 的 schema 1.0。

要点（IMPLEMENTATION_PLAN.md §5.2）：
- segment_inputs 是合并表，媒体路径字段为 path，另含 all_frame_ids；
- frames 入口为 indices/all_frames.jsonl；F 帧（整秒）与 H 帧（frames_ocr/，半秒）
  实测均为完整道路画面，统一标记 full_frame（见 DECISIONS D-2026-09-25-04）；
- OCR 主键 ocr_id（O 前缀），raw_record_ids 回溯原始识别；
- ocr_spans 用 ocr_ids 引用；位于 optional_source_data（非 QA 文件，仍加载用于压缩）。
"""

from __future__ import annotations

from pathlib import Path

from ..schemas import FrameRecord, NormalizedPackage, SegmentRecord
from .base import AdapterError, iter_jsonl, read_json, safe_join
from .common import (
    hash_inputs,
    load_asr_v2,
    load_comments,
    load_metadata,
    load_ocr_record,
    load_span,
)


class PackageV2Adapter:
    name = "v2"

    def __init__(self, package_root: Path):
        self.root = package_root
        self.manifest_path = package_root / "agent_input.json"
        self.manifest = read_json(self.manifest_path)
        if self.manifest.get("schema_version") != "1.0":
            raise AdapterError(
                f"v2 适配器要求 schema_version=1.0，实际 {self.manifest.get('schema_version')!r}"
            )

    def _rel(self, key_path: tuple[str, ...]) -> str:
        node: object = self.manifest
        for key in key_path:
            if not isinstance(node, dict) or key not in node:
                raise AdapterError(f"manifest 缺少 {'.'.join(key_path)}")
            node = node[key]
        if not isinstance(node, str):
            raise AdapterError(f"manifest 字段 {'.'.join(key_path)} 不是路径字符串")
        safe_join(self.root, node)
        return node

    def load(self) -> NormalizedPackage:
        video_id = str(self.manifest["video_id"])
        duration = float(self.manifest["duration_sec"])

        # 实测确认（DECISIONS D-2026-09-25-04）：v2+ 的 H 帧（frames_ocr/，半秒位置）
        # 与 F 帧同分辨率且为完整道路画面，属于全画面帧；仅 v1 的 ocr_crops 是字幕条带裁剪。
        frames: list[FrameRecord] = []
        for rec in iter_jsonl(safe_join(self.root, self._rel(("indices", "frames")))):
            frames.append(
                FrameRecord(
                    frame_id=str(rec["frame_id"]),
                    timestamp_sec=float(rec["timestamp_sec"]),
                    media_path=str(rec["path"]),
                    width=rec.get("width"),
                    height=rec.get("height"),
                    view_kind="full_frame",
                )
            )

        segments: list[SegmentRecord] = []
        for rec in iter_jsonl(safe_join(self.root, self._rel(("indices", "segments")))):
            segments.append(
                SegmentRecord(
                    segment_id=str(rec["segment_id"]),
                    start_sec=float(rec["start_sec"]),
                    end_sec=float(rec["end_sec"]),
                    media_path=str(rec["path"]),
                    frame_ids=[str(i) for i in rec.get("frame_ids") or []],
                    asr_ids=[str(i) for i in rec.get("asr_ids") or []],
                    ocr_ids=[str(i) for i in rec.get("ocr_ids") or []],
                    all_frame_ids=[str(i) for i in rec.get("all_frame_ids") or []],
                )
            )

        asr = load_asr_v2(safe_join(self.root, self._rel(("text", "asr"))))

        ocr = []
        for rec in iter_jsonl(safe_join(self.root, self._rel(("text", "ocr")))):
            raw_ids = [str(i) for i in rec.get("raw_record_ids") or []]
            ocr.append(load_ocr_record(rec, public_id=str(rec["ocr_id"]), raw_ids=raw_ids))

        warnings: list[str] = []
        spans = []
        spans_rel = self.manifest.get("optional_source_data", {}).get("ocr_spans")
        if spans_rel:
            spans = [
                load_span(rec, "ocr_ids")
                for rec in iter_jsonl(safe_join(self.root, str(spans_rel)))
            ]
        else:
            warnings.append("manifest 未提供 ocr_spans，文本压缩将退化为逐条 OCR")

        comments = load_comments(safe_join(self.root, self._rel(("text", "comments"))))
        meta = load_metadata(safe_join(self.root, self._rel(("metadata",))))
        meta.video_id = video_id
        if meta.duration_sec is None:
            meta.duration_sec = duration

        loaded_files = [
            "agent_input.json",
            self._rel(("metadata",)),
            self._rel(("indices", "frames")),
            self._rel(("indices", "segments")),
            self._rel(("text", "asr")),
            self._rel(("text", "ocr")),
            self._rel(("text", "comments")),
        ]
        if spans_rel:
            loaded_files.append(str(spans_rel))

        return NormalizedPackage(
            package_id=self.root.name,
            package_root=str(self.root.resolve()),
            video_id=video_id,
            schema_version=str(self.manifest.get("schema_version")),
            adapter=self.name,
            duration_sec=duration,
            segments=segments,
            frames=frames,
            asr=asr,
            ocr=ocr,
            ocr_spans=spans,
            comments=comments,
            meta=meta,
            source_files=loaded_files,
            warnings=warnings,
            input_hashes=hash_inputs(self.root, loaded_files),
            manifest_counts=dict(self.manifest.get("counts") or {}),
        )


__all__ = ["PackageV2Adapter"]
