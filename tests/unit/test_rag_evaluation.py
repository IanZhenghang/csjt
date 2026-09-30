"""RAG 评估不得伪造标签、混比运行条件或把缺日志当零成本。"""

import copy
import json

import pytest

from defect_agent.rag_evaluation import compare_runs, main, prepare_benchmark, score_benchmark, summarize_run


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _run(tmp_path, name="run", *, package="v2", count=1, is_mock=False, version="rag-v1", workers=1,
         cache_state="cold", accounting="post-attempt-v1"):
    run_dir = tmp_path / name
    video = run_dir / "videos" / "V"
    _write(run_dir / "run_manifest.json", {
        "pipeline_version": version, "is_mock": is_mock, "usage_accounting": accounting,
        "text_model": "chat", "vision_model": "vision", "benchmark_conditions": {"cache_state": cache_state},
    })
    _write(run_dir / "config_snapshot.json", {"yaml": {"rag": {"align_max_workers": workers},
        "embedding": {"model": "embedding"}}, "llm": {"base_url": "https://example.invalid", "max_tokens": 1000}})
    _write(video / "normalized" / "normalized.json", {
        "package_id": package, "video_id": "V", "duration_sec": 90, "input_hashes": {"input": "sha256-input"},
    })
    evidence = [{"evidence_id": "V::frame::F1", "source_kind": "frame", "media_path": "frames/F1.jpg"}]
    claims = []
    for number in range(count):
        source = ("comment", "asr", "ocr_span", "title")[number % 4]
        evidence_id = f"V::{source}::{number}"
        evidence.append({"evidence_id": evidence_id, "source_kind": source, "text": f"原文{number}"})
        start = None if source in ("comment", "title") else (number % 3) * 30 + 1
        claim = {"claim_id": f"{name}::V::claim::C{number:03d}", "video_id": "V", "statement": f"查询{number}",
                 "evidence_ids": [evidence_id], "claim_type": "phenomenon_claim", "start_sec": start,
                 "end_sec": start + 2 if start is not None else None}
        claims.append(claim)
        _write(video / "tasks" / f"retrieval_align_C{number:03d}.json", {
            "query_id": claim["claim_id"], "query": claim["statement"], "namespace": {"source_revision": "revision"},
            "candidate_ids": ["E001", "E002"], "selected_ids": ["E001"], "degraded": False,
        })
    _write_jsonl(video / "normalized" / "evidence.jsonl", evidence)
    _write_jsonl(video / "claims.jsonl", claims)
    _write_jsonl(video / "events.jsonl", [
        {"event_id": f"{name}::V::event::{event_id}", "start_sec": number * 10, "end_sec": number * 10 + 5,
         "phenomenon_type": "collision_or_contact", "phenomenon_summary": f"事件{event_id}",
         "supporting_evidence_ids": ["V::frame::F1"]}
        for number, event_id in enumerate(("E001", "E002"))
    ])
    _write(video / "performance.json", {"wall_seconds": 12, "pipeline_version": version,
                                        "align_workers": workers, "is_mock": is_mock})
    _write(video / "tasks" / "alignment_metrics.json", {"wall_seconds": 4, "claims": count,
        "completed_claims": count, "resumed_claims": 0, "full_candidate_pairs": count * 2,
        "selected_candidate_pairs": count, "full_prompt_characters": 200, "selected_prompt_characters": 150})
    _write_jsonl(run_dir / "usage.jsonl", [
        {"task": "align", "cached": False, "is_mock": is_mock, "usage": {"total_tokens": 20}, "error": None},
        {"task": "align", "cached": True, "is_mock": is_mock, "usage": {}, "error": None},
        {"task": "embedding", "cached": False, "is_mock": is_mock, "usage": {"total_tokens": 3}, "error": None},
    ])
    return run_dir


def _review(item):
    item.update(review_status="human_reviewed", annotation_complete=True, counterevidence_reviewed=True,
                system_state_reviewed=True, replay_reviewed=True, reviewer_id="reviewer", reviewed_at="2026-09-27",
                expected_event_ids=["E001", "E002"], expected_contradicting_event_ids=["E002"],
                expected_system_state_event_ids=[])


def test_prepare_balances_sources_times_without_inventing_labels(tmp_path):
    run_dir = _run(tmp_path, count=24)
    benchmark = prepare_benchmark(run_dir)
    assert len(benchmark["items"]) == 12
    assert set(benchmark["package_summary"][0]["source_kinds"]) == {"comment", "asr", "ocr_span", "title"}
    assert set(benchmark["package_summary"][0]["time_buckets"]) == {"early", "middle", "late", "video"}
    for item in benchmark["items"]:
        assert item["review_status"] == "pending"
        assert item["expected_event_ids"] is None and item["expected_contradicting_event_ids"] is None
        assert not item["annotation_complete"]
    assert len([row for row in benchmark["package_summary"] if row["status"] == "missing_video"]) == 3
    assert "V::frame::F1" in benchmark["materials"]["V"]["evidence_catalog"]


def test_prepare_reports_small_sample_and_missing_traces(tmp_path):
    run_dir = _run(tmp_path, count=2)
    benchmark = prepare_benchmark(run_dir)
    assert benchmark["package_summary"][0]["shortfall"] == 10
    for path in (run_dir / "videos" / "V" / "tasks").glob("retrieval_align_*.json"):
        path.unlink()
    benchmark = prepare_benchmark(run_dir)
    assert benchmark["package_summary"][0]["status"] == "missing_retrieval_traces"
    assert benchmark["items"] == []


def test_pending_and_partial_labels_are_excluded_not_zero(tmp_path):
    run_dir = _run(tmp_path, count=2)
    benchmark = prepare_benchmark(run_dir)
    benchmark["items"][1]["review_status"] = "human_reviewed"
    result = score_benchmark(benchmark, run_dir)
    assert result["scored_queries"] == 0
    assert result["metrics"]["related"]["recall"] is None
    assert {row["reason"] for row in result["excluded"]} == {"not_human_reviewed", "incomplete_review"}


def test_recall_separates_contradictions_and_handles_no_positive_labels(tmp_path):
    run_dir = _run(tmp_path)
    benchmark = prepare_benchmark(run_dir)
    _review(benchmark["items"][0])
    result = score_benchmark(benchmark, run_dir)
    assert result["scored_queries"] == 1
    assert result["metrics"]["related"]["recall"] == 0.5
    assert result["metrics"]["contradicting"]["recall"] == 0
    assert result["metrics"]["system_state"]["recall"] is None
    assert result["live_human_labeled_recall_measured"]
    assert not result["semantic_effectiveness_proven"]


def test_mock_score_never_certifies_semantic_retrieval(tmp_path):
    run_dir = _run(tmp_path, is_mock=True)
    benchmark = prepare_benchmark(run_dir)
    _review(benchmark["items"][0])
    result = score_benchmark(benchmark, run_dir)
    assert result["metrics"]["related"]["recall"] == 0.5
    assert not result["live_human_labeled_recall_measured"]
    assert not result["semantic_effectiveness_proven"]


def test_cross_run_matching_uses_query_evidence_and_event_content_not_run_ids(tmp_path):
    original, repeated = _run(tmp_path, "original"), _run(tmp_path, "repeated")
    benchmark = prepare_benchmark(original)
    _review(benchmark["items"][0])
    assert score_benchmark(benchmark, repeated)["scored_queries"] == 1
    path = repeated / "videos" / "V" / "events.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[0]["phenomenon_summary"] = "changed event"
    _write_jsonl(path, records)
    result = score_benchmark(benchmark, repeated)
    assert result["scored_queries"] == 0
    assert result["excluded"][0]["reason"] == "event_catalog_changed"


def test_unknown_annotation_ids_and_missing_source_revision_are_not_scored(tmp_path):
    run_dir = _run(tmp_path)
    benchmark = prepare_benchmark(run_dir)
    _review(benchmark["items"][0])
    bad = copy.deepcopy(benchmark)
    bad["items"][0]["expected_event_ids"] = ["fabricated"]
    assert score_benchmark(bad, run_dir)["excluded"][0]["reason"] == "expected_id_outside_candidate_catalog"
    benchmark["items"][0]["source_revision"] = None
    assert score_benchmark(benchmark, run_dir)["excluded"][0]["reason"] == "source_revision_mismatch_or_missing"


def test_compare_preserves_legacy_post_unknown_and_new_counts(tmp_path):
    old = _run(tmp_path, "old", version="legacy", accounting=None)
    serial = _run(tmp_path, "serial", workers=1)
    parallel = _run(tmp_path, "parallel", workers=2)
    result = compare_runs([old, serial, parallel])
    assert [run["conditions"]["alignment_mode"] for run in result["runs"]] == ["legacy", "rag_serial", "rag_parallel"]
    assert result["wall_time_comparable"]
    assert not result["actual_post_counts_comparable"]
    assert result["runs"][0]["usage"]["actual_posts"] is None
    assert result["runs"][1]["usage"]["actual_posts"] == 2
    assert result["runs"][1]["usage_by_task"]["align"]["actual_posts"] == 1
    assert result["runs"][1]["embedding"]["reported_total_tokens"] == 3
    assert all(run["billed_cost"] is None for run in result["runs"])


@pytest.mark.parametrize("overrides", [{"is_mock": True}, {"cache_state": "warm"}, {"cache_state": "unknown"}])
def test_compare_rejects_mock_live_and_cold_warm_mixing(tmp_path, overrides):
    old = _run(tmp_path, "old", version="legacy")
    serial = _run(tmp_path, "serial")
    parallel = _run(tmp_path, "parallel", workers=2, **overrides)
    result = compare_runs([old, serial, parallel])
    assert not result["wall_time_comparable"]
    assert result["comparability_issues"]


def test_missing_usage_performance_and_partial_tokens_stay_unknown(tmp_path):
    run_dir = _run(tmp_path)
    _write_jsonl(run_dir / "usage.jsonl", [
        {"task": "align", "cached": False, "is_mock": False, "usage": {}, "error": "HTTP 429"},
        {"task": "align", "cached": False, "is_mock": False, "usage": {"total_tokens": 20}},
    ])
    metrics = summarize_run(run_dir)
    assert metrics["usage"]["actual_posts"] == 2
    assert metrics["usage"]["rate_limit_responses"] == 1
    assert metrics["usage"]["reported_total_tokens"] is None
    assert metrics["usage"]["known_total_tokens"] == 20
    assert metrics["embedding"] is None
    (run_dir / "usage.jsonl").unlink()
    (run_dir / "videos" / "V" / "performance.json").unlink()
    metrics = summarize_run(run_dir)
    assert metrics["usage"] is None
    assert metrics["videos"][0]["wall_seconds"] is None


def test_cli_prepare_and_score_are_offline(tmp_path):
    run_dir = _run(tmp_path)
    benchmark = tmp_path / "benchmark.json"
    output = tmp_path / "score.json"
    assert main(["prepare", "--run-dir", str(run_dir), "--output", str(benchmark)]) == 0
    assert main(["score", "--benchmark", str(benchmark), "--run-dir", str(run_dir), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["metrics"]["related"]["recall"] is None


def test_incomplete_trace_is_not_scored_as_empty_selection(tmp_path):
    run_dir = _run(tmp_path)
    benchmark = prepare_benchmark(run_dir)
    _review(benchmark["items"][0])
    path = run_dir / "videos" / "V" / "tasks" / "retrieval_align_C000.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    del trace["selected_ids"]
    _write(path, trace)
    result = score_benchmark(benchmark, run_dir)
    assert result["scored_queries"] == 0
    assert result["metrics"]["related"]["recall"] is None
    assert result["excluded"][0]["reason"] == "missing_or_ambiguous_query"


def test_missing_usage_cache_flag_does_not_create_zero_posts(tmp_path):
    run_dir = _run(tmp_path)
    _write_jsonl(run_dir / "usage.jsonl", [{"task": "align", "is_mock": False, "usage": None}])
    usage = summarize_run(run_dir)["usage"]
    assert usage["actual_posts"] is None
    assert usage["cached_events"] is None
    assert usage["reported_total_tokens"] is None
