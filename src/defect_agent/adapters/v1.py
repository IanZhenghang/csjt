"""PackageV1Adapter：适配 v1 的 schema 1.0.0。

要点（IMPLEMENTATION_PLAN.md §5.2）：
- segment_inputs 用 video_path；
- 默认 OCR 对外引用 ID 是 text_id（O 前缀），原始 ocr_id（R 前缀）另存 raw_record_ids；
- ocr_spans 用 observation_ids 引用 text_id；
- 全画面帧在 indices/frames.jsonl；ocr_half_frames.jsonl 的 crop_path 是字幕裁剪图。
"""

from __future__ import annotations

from pathlib import Path

from ..schemas import (
    FrameRecord,
    NormalizedPackage,
    SegmentRecord,
)
from .base import AdapterError, iter_jsonl, read_json, safe_join
from .common import (
    finalize_counts,
    hash_inputs,
    load_asr_v1,
    load_comments,
    load_metadata,
    load_ocr_record,
    load_span,
)


class PackageV1Adapter:
    name = "v1"

    def __init__(self, package_root: Path):
        self.root = package_root
        self.manifest_path = package_root / "agent_input.json"
        self.manifest = read_json(self.manifest_path)
        if self.manifest.get("schema_version") != "1.0.0":
            raise AdapterError(
                f"v1 适配器要求 schema_version=1.0.0，实际 {self.manifest.get('schema_version')!r}"
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
        full_frame_ids = {f.frame_id for f in frames}
        for rec in iter_jsonl(safe_join(self.root, self._rel(("indices", "ocr_half_frames")))):
            frames.append(
                FrameRecord(
                    frame_id=str(rec["frame_id"]),
                    timestamp_sec=float(rec["timestamp_sec"]),
                    media_path=str(rec["crop_path"]),
                    view_kind="subtitle_crop",
                )
            )

        segments: list[SegmentRecord] = []
        for rec in iter_jsonl(safe_join(self.root, self._rel(("indices", "segment_inputs")))):
            segments.append(
                SegmentRecord(
                    segment_id=str(rec["segment_id"]),
                    start_sec=float(rec["start_sec"]),
                    end_sec=float(rec["end_sec"]),
                    media_path=str(rec["video_path"]),
                    frame_ids=[str(i) for i in rec.get("frame_ids") or []],
                    asr_ids=[str(i) for i in rec.get("asr_ids") or []],
                    ocr_ids=[str(i) for i in rec.get("ocr_ids") or []],
                )
            )

        asr = load_asr_v1(safe_join(self.root, self._rel(("text", "asr"))))

        ocr = []
        for rec in iter_jsonl(safe_join(self.root, self._rel(("text", "ocr")))):
            text_id = rec.get("text_id")
            if not text_id:
                raise AdapterError(
                    f"v1 默认 OCR 记录缺少 text_id（ocr_id={rec.get('ocr_id')!r}），无法与片段引用对齐"
                )
            raw_ids = [str(rec["ocr_id"])] if rec.get("ocr_id") else []
            for extra in rec.get("duplicate_raw_ids") or []:
                raw_ids.append(str(extra))
            ocr.append(load_ocr_record(rec, public_id=str(text_id), raw_ids=raw_ids))

        spans = [
            load_span(rec, "observation_ids")
            for rec in iter_jsonl(safe_join(self.root, self._rel(("text", "ocr_grouped"))))
        ]

        comments = load_comments(safe_join(self.root, self._rel(("text", "comments"))))
        meta = load_metadata(safe_join(self.root, self._rel(("metadata",))))
        meta.video_id = video_id
        if meta.duration_sec is None:
            meta.duration_sec = duration

        loaded_files = [
            "agent_input.json",
            self._rel(("metadata",)),
            self._rel(("indices", "frames")),
            self._rel(("indices", "ocr_half_frames")),
            self._rel(("indices", "segment_inputs")),
            self._rel(("text", "asr")),
            self._rel(("text", "ocr")),
            self._rel(("text", "ocr_grouped")),
            self._rel(("text", "comments")),
        ]

        package = NormalizedPackage(
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
            warnings=[],
            input_hashes=hash_inputs(self.root, loaded_files),
            manifest_counts=dict(self.manifest.get("counts") or {}),
        )
        # 全帧索引若混入 H 帧（该 schema 不应发生），显式告警
        leaked = [s for seg in segments for s in seg.frame_ids if s not in full_frame_ids]
        if leaked:
            package.warnings.append(f"片段 frame_ids 引用了非全画面帧: {sorted(set(leaked))[:5]}")
        return package


__all__ = ["PackageV1Adapter", "finalize_counts"]
