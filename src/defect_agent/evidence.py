"""T04 证据库：全局 ID、原始证据构建与查询（IMPLEMENTATION_PLAN.md §5.3、§6.1）。

全局 ID 格式：{video_id}::{kind}::{local_id}
OCR span 与其底层 OCR 记录共享 source_group，避免重复计数；
压缩进提示词的记录仍保留全部底层引用（raw_refs）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import EvidenceRecord, NormalizedPackage


def global_id(video_id: str, kind: str, local_id: str) -> str:
    return f"{video_id}::{kind}::{local_id}"


def parse_global_id(evidence_id: str) -> tuple[str, str, str]:
    parts = evidence_id.split("::")
    if len(parts) != 3:
        raise ValueError(f"非法全局证据 ID: {evidence_id!r}")
    return parts[0], parts[1], parts[2]


@dataclass
class EvidenceStore:
    video_id: str
    duration_sec: float
    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    # 局部 ID 映射（kind -> local_id -> evidence_id）
    _by_kind: dict[str, dict[str, str]] = field(default_factory=dict)

    # ---- 构建 ----
    def add(self, record: EvidenceRecord) -> None:
        if record.evidence_id in self.records:
            raise ValueError(f"证据 ID 重复: {record.evidence_id}")
        self.records[record.evidence_id] = record
        _, kind, local_id = parse_global_id(record.evidence_id)
        self._by_kind.setdefault(kind, {})[local_id] = record.evidence_id

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self.records.get(evidence_id)

    def kind_id(self, kind: str, local_id: str) -> str | None:
        return self._by_kind.get(kind, {}).get(local_id)

    def by_kind(self, kind: str) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if parse_global_id(r.evidence_id)[1] == kind]

    def allowed_ids(self) -> set[str]:
        return set(self.records.keys())

    # ---- 时间查询 ----
    def frames_in_range(self, start_sec: float, end_sec: float, full_only: bool = True) -> list[EvidenceRecord]:
        result = []
        for record in self.by_kind("frame"):
            if full_only and record.view_kind != "full_frame":
                continue
            if record.timestamp_sec is None:
                continue
            if start_sec <= record.timestamp_sec < end_sec:
                result.append(record)
        result.sort(key=lambda r: r.timestamp_sec or 0.0)
        return result

    def asr_in_range(self, start_sec: float, end_sec: float) -> list[EvidenceRecord]:
        """按区间相交纳入窗口（§5.4）。"""
        result = []
        for record in self.by_kind("asr"):
            if record.start_sec is None or record.end_sec is None:
                continue
            if record.start_sec < end_sec and record.end_sec > start_sec:
                result.append(record)
        result.sort(key=lambda r: r.start_sec or 0.0)
        return result

    def ocr_spans_in_range(self, start_sec: float, end_sec: float) -> list[EvidenceRecord]:
        result = []
        for record in self.by_kind("ocr_span"):
            if record.start_sec is None or record.end_sec is None:
                continue
            # span 时间是采样观察，不是精确出现/消失边界（§5.4）
            if record.start_sec < end_sec and record.end_sec > start_sec:
                result.append(record)
        result.sort(key=lambda r: r.start_sec or 0.0)
        return result


def build_evidence_store(package: NormalizedPackage) -> EvidenceStore:
    video_id = package.video_id
    store = EvidenceStore(video_id=video_id, duration_sec=package.duration_sec)

    # ---- 帧（含裁剪图，裁剪性质通过 view_kind 标记） ----
    for frame in package.frames:
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "frame", frame.frame_id),
                video_id=video_id,
                source_kind="frame",
                source_file=frame.media_path,
                source_record_id=frame.frame_id,
                text=None,
                time_scope="point",
                timestamp_sec=frame.timestamp_sec,
                media_path=frame.media_path,
                view_kind=frame.view_kind,
                source_group_id=f"{video_id}::sg::frames_source",
                human_reviewed=False,
                quality_flags=[],
                raw_refs=[],
            )
        )

    # ---- ASR ----
    for rec in package.asr:
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "asr", rec.asr_id),
                video_id=video_id,
                source_kind="asr",
                source_file="text/asr.jsonl",
                source_record_id=rec.asr_id,
                text=rec.text,
                time_scope="interval",
                start_sec=rec.start_sec,
                end_sec=rec.end_sec,
                media_path=None,
                view_kind=None,
                source_group_id=f"{video_id}::sg::asr::{rec.asr_id}",
                human_reviewed=rec.human_reviewed,
                quality_flags=list(rec.quality_flags),
                raw_refs=list(rec.raw_refs),
            )
        )

    # ---- OCR（逐条）与 span（压缩组，共享 source_group） ----
    span_groups: dict[str, str] = {}
    for span in package.ocr_spans:
        group = f"{video_id}::sg::ocrspan::{span.span_id}"
        span_groups[span.span_id] = group
        quality = ["sample_observations_not_exact_boundaries"]
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "ocr_span", span.span_id),
                video_id=video_id,
                source_kind="ocr_span",
                source_file="text/ocr_spans.jsonl",
                source_record_id=span.span_id,
                text=span.text,
                time_scope="interval",
                start_sec=span.first_observed_sec,
                end_sec=span.last_observed_sec,
                media_path=None,
                view_kind=None,
                source_group_id=group,
                human_reviewed=False,
                quality_flags=quality,
                raw_refs=[global_id(video_id, "ocr", i) for i in span.ocr_ids],
            )
        )
    default_group = f"{video_id}::sg::ocr_ungrouped"
    for rec in package.ocr:
        group = default_group
        for span in package.ocr_spans:
            if rec.ocr_id in span.ocr_ids:
                group = span_groups[span.span_id]
                break
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "ocr", rec.ocr_id),
                video_id=video_id,
                source_kind="ocr",
                source_file="text/ocr.jsonl",
                source_record_id=rec.ocr_id,
                text=rec.text,
                time_scope="point",
                timestamp_sec=rec.timestamp_sec,
                media_path=None,
                view_kind=None,
                source_group_id=group,
                human_reviewed=rec.human_reviewed,
                quality_flags=list(rec.quality_flags),
                raw_refs=[global_id(video_id, "frame", rec.frame_id)] if rec.frame_id else [],
            )
        )

    # ---- 评论（视频级，无时间） ----
    for comment in package.comments:
        flags = list(comment.quality_flags)
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "comment", comment.comment_id),
                video_id=video_id,
                source_kind="comment",
                source_file="text/comments.jsonl",
                source_record_id=comment.comment_id,
                text=comment.text,
                time_scope="video",
                timestamp_sec=None,
                start_sec=None,
                end_sec=None,
                media_path=None,
                view_kind=None,
                source_group_id=f"{video_id}::sg::comment::{comment.comment_id}",
                human_reviewed=False,
                quality_flags=flags,
                raw_refs=[],
            )
        )

    # ---- 标题 / 简介（发布者陈述） ----
    if package.meta.title:
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "title", "T"),
                video_id=video_id,
                source_kind="title",
                source_file="metadata.json",
                source_record_id="title",
                text=package.meta.title,
                time_scope="video",
                source_group_id=f"{video_id}::sg::publisher",
                human_reviewed=False,
                quality_flags=[],
                raw_refs=[],
            )
        )
    if package.meta.description:
        store.add(
            EvidenceRecord(
                evidence_id=global_id(video_id, "description", "D"),
                video_id=video_id,
                source_kind="description",
                source_file="metadata.json",
                source_record_id="description",
                text=package.meta.description,
                time_scope="video",
                source_group_id=f"{video_id}::sg::publisher",
                human_reviewed=False,
                quality_flags=[],
                raw_refs=[],
            )
        )

    return store
