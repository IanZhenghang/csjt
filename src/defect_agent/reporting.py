"""T10 报告模块：单视频 Markdown 报告、复核队列与运行摘要（§15.1、§15.2）。

报告正文由模板生成；模型只提供已通过校验的结构化字段，
不在报告阶段扩写新事实。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .schemas import (
    AttributionHypothesis,
    Claim,
    Event,
    EvidenceRelation,
    ReviewItem,
    VisualObservation,
)

ASSESSMENT_LABELS = {
    "suspected": "疑似线索（需人工复核）",
    "insufficient_evidence": "证据不足",
    "not_supported": "有相反证据",
    "not_applicable": "与目标驾驶行为无关",
}

ENGAGEMENT_LABELS = {
    "observed_active": "画面可见系统激活（含界面性质说明）",
    "reported_active": "来源声称系统开启",
    "observed_inactive": "画面可见系统未激活",
    "unknown": "系统状态未知",
}


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.2f}s"


def render_report(
    *,
    video_id: str,
    run_id: str,
    is_mock: bool,
    model: str,
    package_id: str,
    duration_sec: float,
    coverage: dict,
    events: list[Event],
    observations: list[VisualObservation],
    claims: list[Claim],
    hypotheses: list[AttributionHypothesis],
    relations: list[EvidenceRelation],
    review_items: list[ReviewItem],
    stats: dict,
    output_path: Path,
) -> Path:
    lines: list[str] = []
    lines.append("# 视频线索分析报告")
    lines.append("")
    lines.append("## 运行信息")
    lines.append("")
    lines.append(f"- 视频 ID：{video_id}（数据包 {package_id}，时长 {duration_sec:.3f}s）")
    lines.append(f"- 运行 ID：{run_id}")
    lines.append(f"- 模式：{'mock（非真实调用，结果不进入科研指标）' if is_mock else 'live 真实调用'}")
    lines.append(f"- 模型：{model}")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 处理状态：{stats.get('video_status', 'unknown')}")
    lines.append("")
    lines.append("## 数据覆盖")
    lines.append("")
    for key, value in coverage.items():
        lines.append(f"- {key}：{value}")
    lines.append(
        "- 说明：视觉输入为稀疏采样（默认 1 Hz 全画面帧），可能遗漏瞬时动作；"
        "裁剪字幕图未作为道路画面证据。"
    )
    lines.append("")
    lines.append("## 事件列表")
    lines.append("")
    if not events:
        lines.append("未形成任何事件级线索。")
    else:
        lines.append("| 事件 | 时间区间 | 现象类型 | 系统状态 | 判定 | 现象证据 |")
        lines.append("|---|---|---|---|---|---|")
        for event in events:
            lines.append(
                f"| {event.event_id.split('::')[-1]} "
                f"| [{_fmt_time(event.start_sec)}, {_fmt_time(event.end_sec)}) "
                f"| {event.phenomenon_type} "
                f"| {ENGAGEMENT_LABELS[event.system_engagement]} "
                f"| {ASSESSMENT_LABELS[event.defect_assessment]} "
                f"| {event.phenomenon_evidence_grade} |"
            )
    lines.append("")
    lines.append("## 事件详情")
    lines.append("")
    hypothesis_by_event: dict[str, list[AttributionHypothesis]] = {}
    for hypothesis in hypotheses:
        hypothesis_by_event.setdefault(hypothesis.event_id, []).append(hypothesis)
    for event in events:
        event_observations = [o for o in observations if o.observation_id in event.observation_ids]
        event_claims = [c for c in claims if c.claim_id in event.claim_ids]
        lines.append(f"### {event.event_id.split('::')[-1]} {_fmt_time(event.start_sec)}–{_fmt_time(event.end_sec)}")
        lines.append("")
        lines.append(f"- 现象：{event.phenomenon_type}——{event.phenomenon_summary}")
        lines.append(f"- 系统参与：{ENGAGEMENT_LABELS[event.system_engagement]}")
        lines.append(
            f"- 证据等级：现象 {event.phenomenon_evidence_grade} / "
            f"系统状态 {event.system_state_evidence_grade} / "
            f"归因 {event.attribution_evidence_grade}"
        )
        lines.append(f"- 线索判定：{ASSESSMENT_LABELS[event.defect_assessment]}")
        lines.append("")
        lines.append("画面观察：")
        if event_observations:
            for observation in event_observations:
                lines.append(
                    f"- {observation.observation_id.split('::')[-1]} "
                    f"[{_fmt_time(observation.start_sec)}, {_fmt_time(observation.end_sec)}) "
                    f"{observation.description}"
                )
                lines.append(
                    f"  - 帧引用：{', '.join(observation.evidence_ids)}"
                )
        else:
            lines.append("- （无）")
        lines.append("")
        lines.append("来源陈述：")
        if event_claims:
            for claim in event_claims:
                lines.append(
                    f"- {claim.claim_id.split('::')[-1]}（{claim.claim_type}）{claim.statement}"
                )
                lines.append(f"  - 证据：{', '.join(claim.evidence_ids)}")
        else:
            lines.append("- （无）")
        lines.append("")
        lines.append(f"- 支持证据：{', '.join(event.supporting_evidence_ids) or '（无）'}")
        lines.append(f"- 冲突证据：{', '.join(event.contradicting_evidence_ids) or '（无）'}")
        lines.append(f"- 未知信息：{'；'.join(event.uncertainties) or '（无）'}")
        lines.append("")
        event_hypotheses = hypothesis_by_event.get(event.event_id, [])
        lines.append("候选归因（全部未验证）：")
        if event_hypotheses:
            for hypothesis in event_hypotheses:
                lines.append(f"- {hypothesis.hypothesis}")
                lines.append(f"  - 支持证据：{', '.join(hypothesis.supporting_evidence_ids) or '（无）'}")
                lines.append(f"  - 反对证据：{', '.join(hypothesis.contradicting_evidence_ids) or '（无）'}")
                lines.append(f"  - 缺失信息：{'；'.join(hypothesis.missing_information) or '（无）'}")
                cf = hypothesis.counterfactual
                lines.append(
                    f"  - 反事实：若「{cf.changed_condition}」（保持 "
                    f"{'、'.join(cf.held_constant_conditions) or '其他条件'}），"
                    f"预期「{cf.expected_difference}」；"
                    f"验证方式：{'；'.join(cf.required_validation) or '待定'}；"
                    f"验证状态：unverified"
                )
        else:
            lines.append("- （证据不足或未启用归因）")
        lines.append("")

    lines.append("## 人工复核清单")
    lines.append("")
    if review_items:
        for item in review_items:
            lines.append(
                f"- [{item.priority}] {item.reason}（建议：{item.suggested_action}；"
                f"证据：{', '.join(item.evidence_ids) or '无'}）"
            )
    else:
        lines.append("- 本轮无自动生成的复核项（全部事件仍需人工复核后才能作为结论使用）。")
    lines.append("")
    lines.append("## 运行统计")
    lines.append("")
    for key, value in stats.items():
        if key != "video_status":
            lines.append(f"- {key}：{value}")
    lines.append("")
    lines.append(
        "> 本报告区分可观察现象、来源陈述与候选归因三类内容；"
        "候选归因均为未验证假设，不构成对任何系统缺陷或事故根因的认定。"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def write_review_queue(review_items: list[ReviewItem], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for item in review_items:
            handle.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return output_path


def render_run_summary(run_id: str, video_results: list[dict], output_path: Path) -> Path:
    lines = [f"# 运行摘要 {run_id}", ""]
    lines.append("| 视频 | 数据包 | 状态 | 事件数 | 陈述数 | 观察数 | 假设数 | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for result in video_results:
        lines.append(
            f"| {result.get('video_id', '-')} | {result.get('package_id', '-')} "
            f"| {result.get('status', '-')} | {result.get('events', 0)} "
            f"| {result.get('claims', 0)} | {result.get('observations', 0)} "
            f"| {result.get('hypotheses', 0)} | {result.get('note', '')} |"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
