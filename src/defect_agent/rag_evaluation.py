"""离线准备人工检索基准、计算召回并汇总运行成本；不调用模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path

from .storage import atomic_write_json

TARGET_PACKAGES = ("v2", "v4", "v7", "v10")


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _local(value: str) -> str:
    return value.split("::")[-1]


def _package(video_dir: Path) -> dict:
    value = _read(video_dir / "normalized" / "normalized.json", {})
    return value.get("package", value)


def _conditions(run_dir: Path) -> dict:
    manifest = _read(run_dir / "run_manifest.json", {})
    snapshot = _read(run_dir / "config_snapshot.json", {})
    declared = manifest.get("benchmark_conditions") or {}
    version = manifest.get("pipeline_version") or manifest.get("pipeline_flags", {}).get("version", "legacy")
    rag = snapshot.get("yaml", {}).get("rag", {})
    workers = rag.get("align_max_workers")
    if version == "rag-v1":
        mode = "rag_serial" if workers == 1 else ("rag_parallel" if isinstance(workers, int) and workers > 1 else "unknown")
    else:
        mode = "legacy" if version in ("legacy", "legacy-v1") else "unknown"
    is_mock = manifest.get("is_mock")
    if is_mock is None and manifest.get("mode") in ("mock", "live"):
        is_mock = manifest["mode"] == "mock"
    return {
        "pipeline_version": version, "alignment_mode": mode, "align_workers": workers,
        "is_mock": is_mock,
        "cache_state": declared.get("cache_state") if declared.get("cache_state") in ("cold", "warm") else "unknown",
        "usage_accounting": manifest.get("usage_accounting"),
        "models": {"text": manifest.get("text_model"), "vision": manifest.get("vision_model"),
                   "embedding": snapshot.get("yaml", {}).get("embedding", {}).get("model")},
        "llm_settings": {key: snapshot.get("llm", {}).get(key) for key in
                         ("base_url", "reasoning_effort", "max_tokens", "enable_thinking")},
    }


def _query_key(video_id: str, query: str, claim: dict) -> str:
    return _hash({"video_id": video_id, "query": query, "evidence_ids": sorted(claim.get("evidence_ids", [])),
                  "start_sec": claim.get("start_sec"), "end_sec": claim.get("end_sec")})


def _event_catalog(video_dir: Path) -> dict[str, dict]:
    observations = {o["observation_id"]: o for o in _jsonl(video_dir / "observations.jsonl")}
    catalog = {}
    for event in _jsonl(video_dir / "events.jsonl"):
        evidence_ids = set(event.get("supporting_evidence_ids", []) + event.get("contradicting_evidence_ids", []))
        for observation_id in event.get("observation_ids", []):
            evidence_ids.update(observations.get(observation_id, {}).get("evidence_ids", []))
        event_id = _local(event["event_id"])
        catalog[event_id] = {
            "event_id": event_id, "start_sec": event.get("start_sec"), "end_sec": event.get("end_sec"),
            "phenomenon_type": event.get("phenomenon_type"), "summary": event.get("phenomenon_summary", ""),
            "evidence_ids": sorted(evidence_ids),
        }
    for cluster in _read(video_dir / "tasks" / "align.json", {}).get("clusters", []):
        event_id = cluster["event_local_id"]
        if event_id not in catalog:
            evidence_ids = {eid for oid in cluster.get("observation_ids", [])
                            for eid in observations.get(oid, {}).get("evidence_ids", [])}
            catalog[event_id] = {
                "event_id": event_id, "start_sec": cluster.get("start_sec"), "end_sec": cluster.get("end_sec"),
                "phenomenon_type": cluster.get("phenomenon_type"), "summary": cluster.get("summary", ""),
                "evidence_ids": sorted(evidence_ids),
            }
    return catalog


def _queries(video_dir: Path) -> tuple[list[dict], list[str]]:
    claims = {c["claim_id"]: c for c in _jsonl(video_dir / "claims.jsonl")}
    rows, issues = [], []
    for path in sorted((video_dir / "tasks").glob("retrieval_align_*.json")):
        trace = _read(path)
        if any(not isinstance(trace.get(key), list) or any(not isinstance(value, str) for value in trace[key])
               for key in ("candidate_ids", "selected_ids")):
            issues.append(f"incomplete_retrieval_trace:{path.name}")
            continue
        claim = claims.get(trace.get("query_id"))
        if claim is None:
            issues.append(f"missing_claim:{trace.get('query_id')}:{path.name}")
            continue
        query = trace.get("query", claim.get("statement", ""))
        rows.append({
            "claim": claim, "trace": trace, "trace_file": str(path.resolve()),
            "query_key": _query_key(video_dir.name, query, claim), "query": query,
        })
    return rows, issues


def _time_bucket(claim: dict, duration: float | None) -> str:
    start, end = claim.get("start_sec"), claim.get("end_sec")
    if start is None or end is None:
        return "video"
    if not duration or duration <= 0:
        return "timed_unknown_duration"
    return ("early", "middle", "late")[min(2, max(0, int(((start + end) / 2) * 3 / duration)))]


def _sample(rows: list[dict], evidence: dict, duration: float | None, limit: int) -> list[dict]:
    groups = defaultdict(deque)
    for row in rows:
        kinds = sorted({evidence[eid].get("source_kind", "unknown") for eid in row["claim"].get("evidence_ids", []) if eid in evidence})
        row["source_kinds"] = kinds
        row["time_bucket"] = _time_bucket(row["claim"], duration)
        groups[("+".join(kinds) or "unknown", row["time_bucket"])].append(row)
    selected = []
    while len(selected) < limit and any(groups.values()):
        for group in sorted(groups):
            if groups[group] and len(selected) < limit:
                selected.append(groups[group].popleft())
    return selected


def prepare_benchmark(run_dir: Path, limit: int = 12) -> dict:
    run_dir = Path(run_dir)
    if not 1 <= limit <= 12:
        raise ValueError("每包基准查询数必须在 1 到 12 之间")
    if not run_dir.is_dir():
        raise ValueError(f"运行目录不存在: {run_dir}")
    videos = {}
    for video_dir in sorted((run_dir / "videos").glob("*")):
        if video_dir.is_dir():
            package = _package(video_dir)
            videos.setdefault(package.get("package_id"), []).append((video_dir, package))
    result = {
        "schema_version": "rag-benchmark:1", "source_run_dir": str(run_dir.resolve()),
        "source_conditions": _conditions(run_dir), "target_packages": list(TARGET_PACKAGES),
        "max_queries_per_package": limit, "package_summary": [], "materials": {}, "items": [],
        "label_policy": "期望标签仅由人工填写；pending/null 不代表无关联，mock 结果不能验证真实语义召回。",
    }
    for package_id in TARGET_PACKAGES:
        matches = videos.get(package_id, [])
        if len(matches) != 1:
            result["package_summary"].append({"package_id": package_id, "video_id": None,
                "status": "missing_video" if not matches else "ambiguous_videos", "available_queries": 0,
                "selected_queries": 0, "shortfall": limit})
            continue
        video_dir, package = matches[0]
        video_id = video_dir.name
        evidence = {e["evidence_id"]: e for e in _jsonl(video_dir / "normalized" / "evidence.jsonl")}
        catalog = _event_catalog(video_dir)
        rows, issues = _queries(video_dir)
        selected = _sample(rows, evidence, package.get("duration_sec"), limit)
        referenced = set()
        for event in catalog.values():
            referenced.update(event["evidence_ids"])
        for row in selected:
            claim, trace = row["claim"], row["trace"]
            referenced.update(claim.get("evidence_ids", []))
            candidate_ids = trace.get("candidate_ids", [])
            missing_events = sorted(set(candidate_ids) - set(catalog))
            required_evidence = set(claim.get("evidence_ids", []))
            for event_id in candidate_ids:
                required_evidence.update(catalog.get(event_id, {}).get("evidence_ids", []))
            missing_evidence = sorted(required_evidence - set(evidence))
            if missing_events or missing_evidence:
                issues.append(f"incomplete_materials:{claim['claim_id']}")
            result["items"].append({
                "benchmark_id": f"{package_id}:{row['query_key'][:16]}", "package_id": package_id,
                "video_id": video_id, "query_id": trace["query_id"], "query_key": row["query_key"],
                "query": row["query"], "claim_type": claim.get("claim_type"),
                "source_evidence_ids": claim.get("evidence_ids", []), "source_kinds": row["source_kinds"],
                "start_sec": claim.get("start_sec"), "end_sec": claim.get("end_sec"), "time_bucket": row["time_bucket"],
                "candidate_ids": candidate_ids, "selected_ids": trace.get("selected_ids", []),
                "source_revision": trace.get("namespace", {}).get("source_revision"),
                "event_catalog_fingerprint": _hash(catalog), "trace_file": row["trace_file"],
                "missing_event_ids": missing_events, "missing_evidence_ids": missing_evidence,
                "expected_event_ids": None, "expected_contradicting_event_ids": None,
                "expected_system_state_event_ids": None, "review_status": "pending",
                "annotation_complete": False, "counterevidence_reviewed": False,
                "system_state_reviewed": False, "replay_reviewed": False,
                "reviewer_id": None, "reviewed_at": None, "review_notes": None, "replay_notes": None,
            })
        result["materials"][video_id] = {
            "package_id": package_id, "package_root": package.get("package_root"),
            "duration_sec": package.get("duration_sec"), "event_catalog": catalog,
            "evidence_catalog": {eid: evidence[eid] for eid in sorted(referenced) if eid in evidence},
            "missing_evidence_ids": sorted(referenced - set(evidence)),
        }
        result["package_summary"].append({
            "package_id": package_id, "video_id": video_id, "status": "ready_for_review" if selected else "missing_retrieval_traces",
            "available_queries": len(rows), "selected_queries": len(selected), "shortfall": max(0, limit - len(selected)),
            "source_kinds": sorted({kind for row in selected for kind in row["source_kinds"]}),
            "time_buckets": sorted({row["time_bucket"] for row in selected}), "issues": issues,
        })
    return result


def _annotation_error(item: dict) -> str | None:
    if item.get("review_status") != "human_reviewed":
        return "not_human_reviewed"
    if item.get("annotation_complete") is not True or not all(item.get(key) is True for key in
            ("counterevidence_reviewed", "system_state_reviewed", "replay_reviewed")):
        return "incomplete_review"
    if not item.get("reviewer_id") or not item.get("reviewed_at"):
        return "missing_reviewer_or_timestamp"
    for key in ("expected_event_ids", "expected_contradicting_event_ids", "expected_system_state_event_ids"):
        if not isinstance(item.get(key), list) or any(not isinstance(value, str) for value in item[key]):
            return "incomplete_expected_labels"
        if set(item[key]) - set(item.get("candidate_ids", [])):
            return "expected_id_outside_candidate_catalog"
    if item.get("missing_event_ids") or item.get("missing_evidence_ids"):
        return "incomplete_source_materials"
    return None


def score_benchmark(benchmark: dict, run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    if benchmark.get("schema_version") != "rag-benchmark:1":
        raise ValueError("不支持的 RAG 基准 schema_version")
    if not run_dir.is_dir():
        raise ValueError(f"运行目录不存在: {run_dir}")
    lookup, catalogs = {}, {}
    for video_dir in sorted((run_dir / "videos").glob("*")):
        if video_dir.is_dir():
            catalogs[video_dir.name] = _hash(_event_catalog(video_dir))
            for row in _queries(video_dir)[0]:
                lookup.setdefault(row["query_key"], []).append(row)
    conditions = _conditions(run_dir)
    metrics = {name: {"expected": 0, "retrieved": 0, "positive_queries": 0} for name in
               ("related", "contradicting", "system_state")}
    report = {"schema_version": "rag-recall:1", "run_dir": str(run_dir.resolve()), "conditions": conditions,
              "eligible_queries": 0, "scored_queries": 0, "excluded": [], "items": [], "metrics": metrics,
              "live_human_labeled_recall_measured": False, "semantic_effectiveness_proven": False,
              "note": "仅计算人工完整标注的候选检索召回；未标注或运行不匹配项不作零分，不能据此证明未观察事件没有遗漏。"}
    for item in benchmark.get("items", []):
        reason = _annotation_error(item)
        if reason is None:
            report["eligible_queries"] += 1
            matches = lookup.get(item["query_key"], [])
            if len(matches) != 1:
                reason = "missing_or_ambiguous_query"
            else:
                trace = matches[0]["trace"]
                revision = trace.get("namespace", {}).get("source_revision")
                if not item.get("source_revision") or revision != item["source_revision"]:
                    reason = "source_revision_mismatch_or_missing"
                elif catalogs.get(item["video_id"]) != item.get("event_catalog_fingerprint"):
                    reason = "event_catalog_changed"
                elif set(trace.get("candidate_ids", [])) != set(item["candidate_ids"]):
                    reason = "candidate_catalog_changed"
        if reason:
            report["excluded"].append({"benchmark_id": item.get("benchmark_id"), "reason": reason})
            continue
        selected = set(trace.get("selected_ids", []))
        entry = {"benchmark_id": item["benchmark_id"], "selected_ids": sorted(selected),
                 "degraded": trace.get("degraded", False), "metrics": {}}
        for name, field in (("related", "expected_event_ids"), ("contradicting", "expected_contradicting_event_ids"),
                            ("system_state", "expected_system_state_event_ids")):
            expected = set(item[field])
            hits = expected & selected
            metrics[name]["expected"] += len(expected)
            metrics[name]["retrieved"] += len(hits)
            metrics[name]["positive_queries"] += bool(expected)
            entry["metrics"][name] = {"expected": len(expected), "retrieved": len(hits),
                                      "recall": len(hits) / len(expected) if expected else None,
                                      "missed_ids": sorted(expected - selected)}
        report["items"].append(entry)
        report["scored_queries"] += 1
    for metric in metrics.values():
        metric["recall"] = metric["retrieved"] / metric["expected"] if metric["expected"] else None
    report["live_human_labeled_recall_measured"] = conditions["is_mock"] is False and metrics["related"]["expected"] > 0
    if conditions["is_mock"] is not False:
        report["note"] += " 当前为 mock 或运行模式未知，分数仅验证评估流程，不验证真实语义效果。"
    return report


def _usage_metrics(events: list[dict], accounting: str | None) -> dict:
    uncached = [event for event in events if event.get("cached") is False]
    cache_flags_complete = all(isinstance(event.get("cached"), bool) for event in events)
    known_tokens = [(event.get("usage") or {}).get("total_tokens") for event in uncached]
    known_tokens = [value for value in known_tokens if isinstance(value, (int, float)) and not isinstance(value, bool)]
    complete_tokens = cache_flags_complete and len(known_tokens) == len(uncached)
    posts_known = cache_flags_complete and accounting == "post-attempt-v1" and all(isinstance(event.get("is_mock"), bool) for event in uncached)
    rate_limited = sum(bool(re.search(r"\b429\b", str(event.get("error", "")))) for event in uncached)
    return {
        "logged_events": len(events),
        "cached_events": sum(event.get("cached") is True for event in events) if cache_flags_complete else None,
        "non_cached_logged_events": len(uncached) if cache_flags_complete else None,
        "actual_posts": sum(event.get("is_mock") is False for event in uncached) if posts_known else None,
        "reported_total_tokens": sum(known_tokens) if complete_tokens else None,
        "known_total_tokens": sum(known_tokens) if known_tokens else None, "token_usage_complete": complete_tokens,
        "logged_429_events": rate_limited, "rate_limit_responses": rate_limited if posts_known else None,
        "billed_cost": None,
    }


def summarize_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ValueError(f"运行目录不存在: {run_dir}")
    conditions = _conditions(run_dir)
    usage_file = run_dir / "usage.jsonl"
    events = _jsonl(usage_file)
    by_task = defaultdict(list)
    for event in events:
        by_task[event.get("task", "unknown")].append(event)
    accounting = conditions["usage_accounting"]
    usage = _usage_metrics(events, accounting) if usage_file.is_file() else None
    videos, inputs = [], {}
    for video_dir in sorted((run_dir / "videos").glob("*")):
        if not video_dir.is_dir():
            continue
        package = _package(video_dir)
        performance = _read(video_dir / "performance.json", {})
        alignment = _read(video_dir / "tasks" / "alignment_metrics.json", {})
        inputs[video_dir.name] = _hash(package["input_hashes"]) if package.get("input_hashes") else None
        videos.append({"video_id": video_dir.name, "package_id": package.get("package_id"),
                       "wall_seconds": performance.get("wall_seconds"), "alignment_wall_seconds": alignment.get("wall_seconds"),
                       "performance_is_mock": performance.get("is_mock"),
                       "alignment": {key: alignment.get(key) for key in
                           ("claims", "completed_claims", "resumed_claims", "full_candidate_pairs", "selected_candidate_pairs",
                            "full_prompt_characters", "selected_prompt_characters", "max_workers")}})
    notes = []
    if accounting != "post-attempt-v1":
        notes.append("旧日志或未知计数口径：non_cached_logged_events 不等于实际 POST；actual_posts 与完整 429 次数不可得。")
    if conditions["cache_state"] == "unknown":
        notes.append("未显式记录冷/暖启动条件；零缓存命中也不能证明冷启动。")
    return {"run_dir": str(run_dir.resolve()), "conditions": conditions, "usage": usage,
            "usage_by_task": {task: _usage_metrics(rows, accounting) for task, rows in sorted(by_task.items())},
            "embedding": _usage_metrics(by_task["embedding"], accounting) if "embedding" in by_task else None,
            "videos": videos, "input_fingerprints": inputs, "billed_cost": None,
            "notes": notes + ["未提供供应商价格和账单，费用保留 null；reported tokens 不等于实际收费。"]}


def compare_runs(run_dirs: list[Path]) -> dict:
    runs = [summarize_run(path) for path in run_dirs]
    reasons = []
    modes = [run["conditions"]["alignment_mode"] for run in runs]
    if sorted(modes) != ["legacy", "rag_parallel", "rag_serial"]:
        reasons.append("必须包含且仅包含 legacy、rag_serial、rag_parallel 各一个运行。")
    if not all(run["conditions"]["is_mock"] is False for run in runs):
        reasons.append("包含 mock 或模式未知运行；只能检查功能，不能据此比较真实 API 性能。")
    cache_states = {run["conditions"]["cache_state"] for run in runs}
    if len(cache_states) != 1 or "unknown" in cache_states:
        reasons.append("冷/暖缓存条件不同或未声明。")
    for key in ("models", "llm_settings"):
        values = [run["conditions"][key] for run in runs]
        if any(value != values[0] for value in values[1:]):
            reasons.append(f"{key} 不一致。")
    inputs = [run["input_fingerprints"] for run in runs]
    if not inputs or not inputs[0] or any(value is None for group in inputs for value in group.values()) or any(group != inputs[0] for group in inputs[1:]):
        reasons.append("输入视频集合/内容不同或缺少输入哈希。")
    if any(not run["videos"] or any(video["wall_seconds"] is None for video in run["videos"]) for run in runs):
        reasons.append("至少一个运行缺少视频 wall_seconds。")
    if any(video["performance_is_mock"] is not run["conditions"]["is_mock"]
           for run in runs for video in run["videos"]):
        reasons.append("视频性能记录的 mock/live 标记缺失或与 manifest 不一致。")
    posts_comparable = not reasons and all(run["usage"] and run["usage"]["actual_posts"] is not None for run in runs)
    return {"schema_version": "rag-comparison:1", "runs": runs, "wall_time_comparable": not reasons,
            "actual_post_counts_comparable": posts_comparable, "comparability_issues": reasons,
            "semantic_quality_validated": False,
            "note": "本表不推断召回质量或费用。旧日志缺少真实 POST 统计时，不与新每次请求日志直接计算调用降幅。"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="生成待人工复核基准，不生成标签")
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    score = commands.add_parser("score", help="仅对完整人工标注项计算检索召回")
    score.add_argument("--benchmark", type=Path, required=True)
    score.add_argument("--run-dir", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare", help="汇总旧流程、RAG 串行、RAG 并行运行")
    compare.add_argument("--run-dirs", type=Path, nargs=3, required=True)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        payload = prepare_benchmark(args.run_dir)
    elif args.command == "score":
        if not args.benchmark.is_file():
            parser.error(f"基准不存在: {args.benchmark}")
        payload = score_benchmark(_read(args.benchmark), args.run_dir)
    else:
        payload = compare_runs(args.run_dirs)
    atomic_write_json(args.output, payload)
    print(f"已写入 {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
