"""第二层（引用）校验：ID 存在性、归属与时间范围。

语义缺证据的输出由调用方拒收或降级，不通过修改引用 ID 伪装为有效结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import (
    AttributionDraft,
    ClaimDraft,
    ObservationDraft,
    AssociationDraft,
)


@dataclass
class Rejection:
    reason: str
    detail: str


@dataclass
class ValidationResult:
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)  # list[Rejection]

    @property
    def ok(self) -> bool:
        return not self.rejected


def _check_ids(
    ids: list[str],
    allowed: set[str],
    label: str,
    index: int,
    rejections: list[Rejection],
) -> bool:
    invalid = [i for i in ids if i not in allowed]
    if invalid:
        rejections.append(
            Rejection(
                reason="fabricated_evidence_id",
                detail=f"{label}[{index}] 引用不存在的证据 ID: {invalid[:3]}",
            )
        )
        return False
    return True


def validate_claim_drafts(
    drafts: list[ClaimDraft], allowed_ids: set[str], duration: float
) -> ValidationResult:
    result = ValidationResult()
    for index, draft in enumerate(drafts):
        if not draft.statement.strip():
            result.rejected.append(Rejection("empty_statement", f"claim[{index}] 陈述为空"))
            continue
        if not _check_ids(draft.evidence_ids, allowed_ids, "claim", index, result.rejected):
            continue
        if draft.temporal_scope == "interval" and draft.start_sec is not None and draft.end_sec is not None:
            # 允许退化区间（如仅采样一次的 OCR span，首末观察时间相同）
            if not (0 <= draft.start_sec <= draft.end_sec <= duration + 0.5):
                result.rejected.append(
                    Rejection("time_out_of_range", f"claim[{index}] 时间区间超出视频范围")
                )
                continue
        if not draft.evidence_ids:
            result.rejected.append(Rejection("no_evidence", f"claim[{index}] 未引用任何证据"))
            continue
        result.accepted.append(draft)
    return result


def validate_observation_drafts(
    drafts: list[ObservationDraft],
    allowed_frame_ids: set[str],
    window_range: tuple[float, float],
    duration: float,
) -> ValidationResult:
    result = ValidationResult()
    lo, hi = window_range
    for index, draft in enumerate(drafts):
        if not _check_ids(draft.evidence_ids, allowed_frame_ids, "observation", index, result.rejected):
            continue
        if not draft.evidence_ids:
            result.rejected.append(Rejection("no_evidence", f"observation[{index}] 未引用任何帧"))
            continue
        if not (0 <= draft.start_sec <= draft.end_sec <= duration + 0.5):
            result.rejected.append(Rejection("time_invalid", f"observation[{index}] 时间区间无效"))
            continue        # 观察时间应落在窗口上下文范围（含少量容差）
        if draft.end_sec < lo - 1.0 or draft.start_sec > hi + 1.0:
            result.rejected.append(
                Rejection("time_out_of_window", f"observation[{index}] 时间超出窗口范围")
            )
            continue
        result.accepted.append(draft)
    return result


def validate_associations(
    associations: list[AssociationDraft], claim_ids: set[str], event_ids: set[str]
) -> ValidationResult:
    result = ValidationResult()
    for index, association in enumerate(associations):
        if association.claim_id not in claim_ids:
            result.rejected.append(
                Rejection("unknown_claim", f"association[{index}] 引用不存在的 claim {association.claim_id}")
            )
            continue
        if association.event_id not in event_ids:
            result.rejected.append(
                Rejection("unknown_event", f"association[{index}] 引用不存在的 event {association.event_id}")
            )
            continue
        result.accepted.append(association)
    return result


def validate_attribution_drafts(
    drafts: list[AttributionDraft], allowed_ids: set[str]
) -> ValidationResult:
    result = ValidationResult()
    for index, draft in enumerate(drafts):
        ids = list(draft.supporting_evidence_ids) + list(draft.contradicting_evidence_ids)
        if not _check_ids(ids, allowed_ids, "hypothesis", index, result.rejected):
            continue
        if not draft.hypothesis.strip():
            result.rejected.append(Rejection("empty_hypothesis", f"hypothesis[{index}] 假设为空"))
            continue
        result.accepted.append(draft)
    return result
