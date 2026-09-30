"""T12 评估：对照人工标注的事件指标与引用指标（§14.3），mock 与 live 结果严格区分。

事件匹配：同视频 + 现象类别一致 + 时间 IoU ≥ 阈值 + 一对一匹配；
同时报告 IoU ≥ 0.5 的敏感性结果。纯评论指控（无时间边界）不混入事件指标。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from . import config as config_module
from .config import load_config
from .windows import interval_iou


class AnnotationRecord(BaseModel):
    video_id: str
    event_id: str | None = None
    start_sec: float
    end_sec: float
    phenomenon_type: str
    system_engagement: str = "unknown"
    supporting_evidence_ids: list[str] = []
    contradicting_evidence_ids: list[str] = []
    uncertain_fields: list[str] = []
    reviewer_id: str = ""
    review_time: str = ""
    annotation_version: str = "1"


def load_events(run_dir: Path) -> list[dict]:
    events = []
    for events_file in sorted(run_dir.glob("videos/*/events.jsonl")):
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def load_known_ids(run_dir: Path) -> set[str]:
    """运行内全部可引用 ID（证据、陈述、观察、事件、假设）。"""
    known: set[str] = set()
    for evidence_file in run_dir.glob("videos/*/normalized/evidence.jsonl"):
        for line in evidence_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                known.add(json.loads(line)["evidence_id"])
    for name in ("claims.jsonl", "observations.jsonl", "events.jsonl", "hypotheses.jsonl"):
        for records_file in run_dir.glob(f"videos/*/{name}"):
            for line in records_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    key = "claim_id" if "claim_id" in record else (
                        "observation_id" if "observation_id" in record else (
                            "event_id" if "event_id" in record else "hypothesis_id"
                        )
                    )
                    known.add(record[key])
    return known


def match_events(
    predictions: list[dict], ground_truth: list[AnnotationRecord], iou_threshold: float
) -> tuple[int, int, int]:
    """返回 (匹配对数, 预测数, 人工事件数)，一对一贪心匹配。"""
    pairs = []
    for pred_index, pred in enumerate(predictions):
        for gt_index, gt in enumerate(ground_truth):
            if pred.get("video_id") != gt.video_id:
                continue
            if pred.get("phenomenon_type") != gt.phenomenon_type:
                continue
            iou = interval_iou(
                (float(pred["start_sec"]), float(pred["end_sec"])),
                (gt.start_sec, gt.end_sec),
            )
            if iou >= iou_threshold:
                pairs.append((iou, pred_index, gt_index))
    pairs.sort(reverse=True)
    used_pred: set[int] = set()
    used_gt: set[int] = set()
    matched = 0
    for iou, pred_index, gt_index in pairs:
        if pred_index in used_pred or gt_index in used_gt:
            continue
        used_pred.add(pred_index)
        used_gt.add(gt_index)
        matched += 1
    return matched, len(predictions), len(ground_truth)


def citation_metrics(run_dir: Path, known_ids: set[str]) -> dict:
    total = 0
    resolvable = 0
    for name, fields in (
        ("events.jsonl", ("supporting_evidence_ids", "contradicting_evidence_ids")),
        ("claims.jsonl", ("evidence_ids",)),
        ("observations.jsonl", ("evidence_ids",)),
        ("hypotheses.jsonl", ("supporting_evidence_ids", "contradicting_evidence_ids")),
    ):
        for records_file in run_dir.glob(f"videos/*/{name}"):
            for line in records_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                for field_name in fields:
                    for reference in record.get(field_name) or []:
                        total += 1
                        if reference in known_ids:
                            resolvable += 1
    return {
        "引用总数": total,
        "可解析引用数": resolvable,
        "引用存在率": round(resolvable / total, 4) if total else None,
    }


def run_evaluate(args) -> int:
    run_dir = config_module.PROJECT_ROOT / "runs" / args.run_id
    if not run_dir.is_dir():
        print(f"[失败] 运行目录不存在: {run_dir}")
        return 1
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    is_mock = bool(manifest.get("is_mock"))

    annotations: list[AnnotationRecord] = []
    annotation_path = Path(args.annotations)
    if annotation_path.is_file():
        for line in annotation_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                annotations.append(AnnotationRecord.model_validate(json.loads(line)))
    else:
        print(f"[警告] 标注文件不存在: {annotation_path}（事件匹配指标将为空）")

    events = load_events(run_dir)
    known_ids = load_known_ids(run_dir)

    report: dict = {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "is_mock": is_mock,
        "note": "mock 运行结果不进入科研效果指标" if is_mock else "live 真实调用结果",
        "totals": {"events": len(events), "annotations": len(annotations)},
    }

    for threshold in (args.iou, 0.5):
        matched, predicted, truth = match_events(events, annotations, threshold)
        precision = matched / predicted if predicted else None
        recall = matched / truth if truth else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall > 0
            else None
        )
        report[f"event_metrics@iou{threshold}"] = {
            "matched": matched,
            "predicted": predicted,
            "ground_truth": truth,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4) if f1 is not None else None,
        }

    report["citation_metrics"] = citation_metrics(run_dir, known_ids)
    suspected = [e for e in events if e.get("defect_assessment") == "suspected"]
    unsupported_suspected = [e for e in suspected if not e.get("supporting_evidence_ids")]
    report["assessment_metrics"] = {
        "suspected_events": len(suspected),
        "insufficient_events": sum(1 for e in events if e.get("defect_assessment") == "insufficient_evidence"),
        "unsupported_suspected": len(unsupported_suspected),
        "弃答比例(事件级)": round(
            sum(1 for e in events if e.get("defect_assessment") == "insufficient_evidence") / len(events), 4
        )
        if events
        else None,
    }
    usage_file = run_dir / "usage_summary.json"
    if usage_file.is_file():
        report["usage"] = json.loads(usage_file.read_text(encoding="utf-8"))

    output = run_dir / "evaluation_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"已写入 {output}")
    return 0


def run_stability(args) -> int:
    """稳定性实验（§8.2）：对单包执行 N 次独立采样，绕过缓存，报告一致率。"""
    import time

    from .agents.prompts import load_prompt
    from .llm import LLMRequest, MockLLMClient, ResponseCache
    from .schemas import ObservationBatchResult

    config = load_config(args.config)
    repeats = int(config.stability.get("repeats", 3))
    client = MockLLMClient(cache=None)  # 稳定性实验绕过响应缓存
    outcomes = []
    for repetition in range(repeats):
        request = LLMRequest(
            task="visual_observe",
            system_prompt=load_prompt("visual_observe"),
            user_prompt=f"稳定性实验 第 {repetition + 1} 次",
            context={"images": [{"evidence_id": "v::frame::F0000", "frame_id": "F0000", "timestamp_sec": 0.0}]},
            output_schema_name="ObservationBatchResult:stability",
            repetition_index=repetition,  # 不同重复编号 → 不同采样
        )
        result: ObservationBatchResult = client.generate_json(request, ObservationBatchResult)
        signature = tuple(
            (o.phenomenon_type, tuple(o.evidence_ids)) for o in result.observations
        )
        outcomes.append(signature)
        time.sleep(0)

    counts: dict[tuple, int] = {}
    for signature in outcomes:
        counts[signature] = counts.get(signature, 0) + 1
    most = max(counts.values())
    stability = most / repeats
    report = {
        "schema_version": "1.0",
        "repeats": repeats,
        "cache_bypassed": True,
        "is_mock": client.is_mock,
        "stability": stability,
        "signatures": [list(map(str, s)) for s in outcomes],
        "note": "一致性衡量重复输出的稳定程度，不代表正确率；mock 输出仅验证机制",
    }
    out_dir = config_module.PROJECT_ROOT / "experiments"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "stability_latest.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"已写入 {output}")
    return 0
