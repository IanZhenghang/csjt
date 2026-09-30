"""T10 规则模块：证据分级、缺陷线索判定、业务校验（§8.1、§10.3）。

全部为确定性规则；不给未经校准的真实性概率。
首版不授予 attribution high，不提供 confirmed_defect。
"""

from __future__ import annotations

from .schemas import (
    AttributionHypothesis,
    Claim,
    Event,
    VisualObservation,
)

SYSTEM_STATE_HINT_WORDS = (
    "pilot", "noa", "领航", "智驾", "辅助驾驶", "自动驾驶", "l2", "acc",
)


def _has_frame_evidence(ids: list[str], store) -> bool:
    return any(
        (record := store.get(i)) is not None and record.source_kind in ("frame", "derived_frame")
        for i in ids
    )


def grade_phenomenon(
    event: Event,
    observations: list[VisualObservation],
    claims: list[Claim],
    store,
) -> str:
    visual = [o for o in observations if o.evidence_ids]
    if not visual:
        if any(c.claim_type == "phenomenon_claim" for c in claims):
            return "low"
        return "insufficient"

    has_conflict = bool(event.contradicting_evidence_ids)
    clear = [o for o in visual if o.visibility in ("clear", "partial")]
    if len(visual) >= 2 and clear and not has_conflict:
        # 相邻/重叠观察且无明显冲突
        starts = sorted(o.start_sec for o in visual)
        ends = sorted(o.end_sec for o in visual)
        if starts[-1] - ends[0] < (ends[-1] - starts[0]) + 3.0:
            return "high"
    if clear:
        return "medium"
    return "low"


def grade_system_state(
    event: Event,
    observations: list[VisualObservation],
    claims: list[Claim],
) -> str:
    # high 需人工核验的原生状态显示，首版不自动授予
    for observation in observations:
        state = (observation.system_state or "").lower()
        if observation.system_state and state not in ("unknown", "none", ""):
            return "medium"
    if any(c.claim_type == "system_state_claim" for c in claims):
        return "low"
    return "insufficient"


def grade_attribution(hypotheses: list[AttributionHypothesis], store) -> str:
    if not hypotheses:
        return "insufficient"
    for hypothesis in hypotheses:
        if _has_frame_evidence(hypothesis.supporting_evidence_ids, store):
            return "medium"
    if any(hypothesis.supporting_evidence_ids for hypothesis in hypotheses):
        return "low"
    return "low"


def determine_system_engagement(
    observations: list[VisualObservation],
    claims: list[Claim],
) -> str:
    """保守规则：
    - observed_active 不自动授予（无法区分原生界面与发布者叠加时保持 reported/unknown）；
    - 存在系统状态声称（字幕/标题/评论）→ reported_active；
    - 否则 unknown。
    """
    state_claims = [c for c in claims if c.claim_type == "system_state_claim"]
    if state_claims:
        return "reported_active"
    for observation in observations:
        state = (observation.system_state or "").lower()
        if state and state not in ("unknown", "none", ""):
            # 画面可见状态显示但未经人工核验，不能升级为 observed_active
            return "reported_active"
    return "unknown"


def assess_defect(
    event: Event,
    observations: list[VisualObservation],
    claims: list[Claim],
    grades: dict[str, str],
) -> str:
    if not observations and not claims:
        return "not_applicable"
    if event.contradicting_evidence_ids and not observations:
        return "not_supported"
    # 现象类型为 uncertain 时，画面观察本身未确认现象，不能作为 suspected 依据
    phenomenon_confirmed = event.phenomenon_type not in ("uncertain",)
    phenomenon_supported = (
        phenomenon_confirmed and grades["phenomenon_evidence_grade"] in ("high", "medium")
    )
    system_known = event.system_engagement in ("observed_active", "reported_active")
    major_conflict = bool(event.contradicting_evidence_ids) and grades[
        "phenomenon_evidence_grade"
    ] == "low"
    if phenomenon_supported and system_known and not major_conflict:
        return "suspected"
    return "insufficient_evidence"


def finalize_event(
    event: Event,
    observations: list[VisualObservation],
    claims: list[Claim],
    hypotheses: list[AttributionHypothesis],
    store,
) -> Event:
    """规则分级 + 线索判定 + 不确定性汇总（§8.1）。"""
    grades = {
        "phenomenon_evidence_grade": grade_phenomenon(event, observations, claims, store),
        "system_state_evidence_grade": grade_system_state(event, observations, claims),
        "attribution_evidence_grade": grade_attribution(hypotheses, store),
    }
    event.phenomenon_evidence_grade = grades["phenomenon_evidence_grade"]
    event.system_state_evidence_grade = grades["system_state_evidence_grade"]
    event.attribution_evidence_grade = grades["attribution_evidence_grade"]
    event.system_engagement = determine_system_engagement(observations, claims)
    event.defect_assessment = assess_defect(event, observations, claims, grades)

    uncertainties: list[str] = []
    if event.system_engagement != "observed_active":
        uncertainties.append("事件发生时系统实际控制状态未确认")
    for observation in observations:
        uncertainties.extend(observation.limitations)
    if grades["phenomenon_evidence_grade"] != "high":
        uncertainties.append("稀疏采样可能遗漏瞬时动作；事件边界为粗粒度")
    event.uncertainties = list(dict.fromkeys(uncertainties))[:12]
    event.review_required = True  # 首版全部保留人工复核
    event.processing_status = "finalized"
    return event


# ---------------------------------------------------------------------------
# 业务校验（§10.3 第三层）
# ---------------------------------------------------------------------------


def business_checks(event: Event, observations: list[VisualObservation], claims: list[Claim]) -> list[str]:
    violations: list[str] = []
    # 纯评论不能生成直接视觉观察：观察必须引用帧证据
    for observation in observations:
        if not observation.evidence_ids:
            violations.append(f"{observation.observation_id} 无帧引用")
    # 没有系统状态证据不能输出已观察到系统激活
    if event.system_engagement == "observed_active":
        violations.append(f"{event.event_id} observed_active 首版不允许自动授予")
    return violations
