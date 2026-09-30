"""T06 文本提取角色：三种输入批次 → Claim 草稿（§7.3）。

批次：视频背景（标题/简介）、时间文本（ASR + OCR spans，按窗口）、评论（≤30 条/批）。
达到限制时按记录边界拆批，不截尾。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LLMClient, LLMRequest
from ..schemas import ClaimBatchResult, ClaimDraft, NormalizedPackage
from ..windows import Window
from ..evidence import EvidenceStore
from .prompts import load_prompt
from .validation import validate_claim_drafts


@dataclass
class TextBatch:
    batch_kind: str  # background / time_text / comments
    label: str
    records: list[dict] = field(default_factory=list)
    context_lines: list[str] = field(default_factory=list)  # 父评论上下文，不作为可引用记录
    window_id: str | None = None


def _record_from_evidence(record) -> dict:
    time_note = None
    if record.time_scope == "point":
        time_note = f"t={record.timestamp_sec}"
    elif record.time_scope == "interval":
        time_note = f"[{record.start_sec}, {record.end_sec})"
    return {
        "evidence_id": record.evidence_id,
        "source_kind": record.source_kind,
        "text": record.text or "",
        "timestamp_sec": record.timestamp_sec,
        "start_sec": record.start_sec,
        "end_sec": record.end_sec,
        "quality_flags": list(record.quality_flags),
        "source_group_id": record.source_group_id,
        "time_note": time_note,
    }


def _split_by_limits(records: list[dict], max_records: int, max_chars: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for record in records:
        record_chars = len(str(record.get("text") or "")) + 80
        if current and (len(current) >= max_records or chars + record_chars > max_chars):
            batches.append(current)
            current = []
            chars = 0
        current.append(record)
        chars += record_chars
    if current:
        batches.append(current)
    return batches


def build_text_batches(
    package: NormalizedPackage,
    store: EvidenceStore,
    windows: list[Window],
    config: dict,
) -> list[TextBatch]:
    text_cfg = config.get("text", {})
    comment_cfg = config.get("comments", {})
    batches: list[TextBatch] = []

    # ---- 背景批次：标题 + 简介 ----
    background = [_record_from_evidence(r) for r in store.by_kind("title")]
    background += [_record_from_evidence(r) for r in store.by_kind("description")]
    if background:
        batches.append(TextBatch(batch_kind="background", label="background", records=background))

    # ---- 时间文本批次：ASR + OCR spans ----
    # time_text_batching=per_window 按计划书 §7.3 按窗口组织（默认）；
    # merged 把全部窗口的时间文本合并后按上限拆批（每视频约省 14 次调用，
    # 记录自带时间戳，不损失时间信息；见 DECISIONS）。
    batching = str(text_cfg.get("time_text_batching", "per_window"))
    if batching == "merged":
        merged_records: list[dict] = []
        seen_ids: set[str] = set()
        for window in windows:
            for record in [
                _record_from_evidence(r)
                for r in store.asr_in_range(window.ctx_start, window.ctx_end)
            ] + [
                _record_from_evidence(r)
                for r in store.ocr_spans_in_range(window.ctx_start, window.ctx_end)
            ]:
                if record["evidence_id"] not in seen_ids:
                    seen_ids.add(record["evidence_id"])
                    merged_records.append(record)
        for part_index, part in enumerate(
            _split_by_limits(
                merged_records,
                int(text_cfg.get("max_records_per_batch", 80)),
                int(text_cfg.get("max_characters_per_batch", 8000)),
            ),
            start=1,
        ):
            batches.append(
                TextBatch(batch_kind="time_text", label=f"timetext:part{part_index}", records=part)
            )
    else:
        for window in windows:
            records = [
                _record_from_evidence(r)
                for r in store.asr_in_range(window.ctx_start, window.ctx_end)
            ]
            records += [
                _record_from_evidence(r)
                for r in store.ocr_spans_in_range(window.ctx_start, window.ctx_end)
            ]
            for part_index, part in enumerate(
                _split_by_limits(
                    records,
                    int(text_cfg.get("max_records_per_batch", 80)),
                    int(text_cfg.get("max_characters_per_batch", 8000)),
                ),
                start=1,
            ):
                batches.append(
                    TextBatch(
                        batch_kind="time_text",
                        label=f"window:{window.window_id.split('::')[-1]}:part{part_index}",
                        records=part,
                        window_id=window.window_id,
                    )
                )

    # ---- 评论批次（含父评论上下文） ----
    comments = [_record_from_evidence(r) for r in store.by_kind("comment")]
    parent_local = {c.comment_id: c.parent_id for c in package.comments}
    evidence_local = {}
    for comment in package.comments:
        evidence_local[comment.comment_id] = store.kind_id("comment", comment.comment_id)
    local_to_evidence = {
        local: eid for local, eid in (evidence_local.items() if evidence_local else [])
    }
    text_by_evidence = {c["evidence_id"]: c["text"] for c in comments}
    parent_of: dict[str, str] = {}
    for local_id, parent_local_id in parent_local.items():
        child_eid = local_to_evidence.get(local_id)
        parent_eid = local_to_evidence.get(parent_local_id) if parent_local_id else None
        if child_eid and parent_eid:
            parent_of[child_eid] = parent_eid

    for part_index, part in enumerate(
        _split_by_limits(
            comments,
            int(comment_cfg.get("max_records_per_batch", 30)),
            int(comment_cfg.get("max_characters_per_batch", 6000)),
        ),
        start=1,
    ):
        included_ids = {r["evidence_id"] for r in part}
        context_lines = []
        for record in part:
            parent_eid = parent_of.get(record["evidence_id"])
            if parent_eid and parent_eid not in included_ids:
                parent_text = text_by_evidence.get(parent_eid, "")
                note = "父评论缺失，无法提供上下文" if not parent_text else ""
                context_lines.append(
                    f"父评论[{parent_eid}]（仅作上下文，不要为其生成独立陈述{note}）：{parent_text[:300]}"
                )
        batches.append(
            TextBatch(
                batch_kind="comments",
                label=f"comments:part{part_index}",
                records=part,
                context_lines=context_lines,
            )
        )

    return batches


def render_text_prompt(batch: TextBatch) -> str:
    lines = ["以下是待分析的文本记录。每条记录的格式：[证据ID] (来源, 时间) 文本。", ""]
    for record in batch.records:
        time_note = record.get("time_note")
        time_part = f", {time_note}" if time_note else ""
        flags = record.get("quality_flags") or []
        flag_part = f", 质量标记: {';'.join(flags)}" if flags else ""
        lines.append(
            f"[{record['evidence_id']}] ({record['source_kind']}{time_part}{flag_part}) "
            f"{record['text']}"
        )
    if batch.context_lines:
        lines.append("")
        lines.extend(batch.context_lines)
    lines.append("")
    lines.append("请从上述记录提取来源陈述，只返回指定结构的 JSON。")
    return "\n".join(lines)


@dataclass
class ClaimBatch:
    batch: TextBatch
    drafts: list[ClaimDraft] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    is_mock: bool = True


def extract_claims(batch: TextBatch, client: LLMClient) -> ClaimBatch:
    """§6.7 extract_claims：单批文本 → 验证后的 Claim 草稿。"""
    request = LLMRequest(
        task="text_extract",
        system_prompt=load_prompt("text_extract"),
        user_prompt=render_text_prompt(batch),
        context={
            "batch_kind": batch.batch_kind,
            "records": batch.records,
        },
        output_schema_name="ClaimBatchResult:1",
        preprocess_meta={"batch_kind": batch.batch_kind, "label": batch.label},
    )
    result: ClaimBatchResult = client.generate_json(request, ClaimBatchResult)

    allowed = {r["evidence_id"] for r in batch.records}
    validated = validate_claim_drafts(result.claims, allowed, duration=1e12)
    rejected = [(r.reason, r.detail) for r in validated.rejected]
    return ClaimBatch(batch=batch, drafts=validated.accepted, rejected=rejected, is_mock=client.is_mock)
