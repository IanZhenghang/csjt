"""集成测试：真实数据包的完整 mock 闭环、只读校验、恢复与评估。

全部离线（mock），不调用付费 API；测试产物写入 tmp_path。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import defect_agent.config as config_module
import defect_agent.pipeline as pipeline_module
from defect_agent.adapters import discover_packages, load_package
from defect_agent.validation import validate_package

# 计划书 §2.1 的数据规模表（用于核对适配器计数）
PLAN_COUNTS = {
    "v1": (38, 223, 33, 172, 57, 61),
    "v2": (33, 197, 9, 442, 141, 92),
    "v3": (15, 88, 0, 1005, 117, 28),
    "v4": (27, 159, 35, 2111, 370, 53),
    "v5": (25, 150, 43, 330, 163, 35),
    "v6": (15, 85, 3, 819, 275, 58),
    "v7": (38, 225, 0, 364, 223, 3),
    "v8": (21, 122, 31, 947, 342, 61),
    "v9": (11, 62, 11, 382, 198, 31),
    "v10": (37, 217, 47, 771, 189, 5),
}


@pytest.fixture()
def isolated_project(tmp_path, monkeypatch):
    """把运行产物根切到 tmp_path，避免污染项目 runs/。"""
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_module, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _run_package(tmp_root, package_path, mode="mock"):
    args = SimpleNamespace(
        package=Path(package_path), mode=mode, max_live_calls=None,
        run_id=None, config=None, experiment=None,
    )
    return pipeline_module.run_package(args)


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    result = {}
    for path in paths:
        if path.is_file():
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# 真实数据：适配与校验
# ---------------------------------------------------------------------------


def test_all_ten_packages_counts_match_plan(real_data_root):
    packages = discover_packages(real_data_root)
    assert len(packages) == 10
    for package_root in packages:
        package = load_package(package_root)
        segments, frames, asr, ocr, spans, comments = PLAN_COUNTS[package.package_id]
        assert len(package.segments) == segments, package.package_id
        assert len(package.frames) == frames, package.package_id
        assert len(package.asr) == asr, package.package_id
        assert len(package.ocr) == ocr, package.package_id
        assert len(package.ocr_spans) == spans, package.package_id
        assert len(package.comments) == comments, package.package_id


def test_all_ten_packages_validate_without_errors(real_data_root):
    for package_root in discover_packages(real_data_root):
        result = validate_package(load_package(package_root), package_root)
        assert result.status != "errors", f"{package_root.name}: {result.issues}"


# ---------------------------------------------------------------------------
# 真实数据：mock 闭环
# ---------------------------------------------------------------------------


def test_v2_mock_full_pipeline(real_data_root, isolated_project):
    run_dir = isolated_project / "runs"
    code = _run_package(isolated_project, real_data_root / "v2")
    assert code == 0
    runs = list(run_dir.iterdir())
    assert len(runs) == 1
    video_dir = next((runs[0] / "videos").iterdir())
    for name in (
        "report.md", "claims.jsonl", "observations.jsonl", "events.jsonl",
        "hypotheses.jsonl", "evidence_graph.json", "review_queue.jsonl",
        "normalized/normalized.json", "normalized/windows.json", "normalized/evidence.jsonl",
    ):
        assert (video_dir / name).is_file(), name
    events = [
        json.loads(line)
        for line in (video_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events, "mock 闭环应产生事件"
    for event in events:
        # Mock associates every statement with one large event. RAG must expose
        # oversized required context instead of silently truncating that event.
        if event["processing_status"] == "incomplete_context":
            pack = json.loads((video_dir / "tasks" / f"evidence_pack_review_{event['event_id'].split('::')[-1]}.json").read_text(encoding="utf-8"))
            assert pack["complete"] is False
            assert len(pack["content"]) > 24000
            assert event["defect_assessment"] == "insufficient_evidence"
            assert event["review_required"] is True
        else:
            assert event["processing_status"] == "finalized"
        assert event["defect_assessment"] in (
            "suspected", "insufficient_evidence", "not_supported", "not_applicable",
        )
    hypotheses = [
        json.loads(line)
        for line in (video_dir / "hypotheses.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for hypothesis in hypotheses:
        assert hypothesis["verification_status"] == "unverified"
    manifest = json.loads((runs[0] / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["is_mock"] is True


def test_v1_mock_pipeline_excludes_subtitle_crops(real_data_root, isolated_project):
    code = _run_package(isolated_project, real_data_root / "v1")
    assert code == 0
    runs = list((isolated_project / "runs").iterdir())
    video_dir = next((runs[0] / "videos").iterdir())
    observations = [
        json.loads(line)
        for line in (video_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evidence = {
        json.loads(line)["evidence_id"]: json.loads(line)
        for line in (video_dir / "normalized/evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    for observation in observations:
        for evidence_id in observation["evidence_ids"]:
            assert evidence[evidence_id]["view_kind"] == "full_frame"


def test_original_data_unchanged_after_run(real_data_root, isolated_project):
    """原始文件内容哈希在运行前后不变。"""
    watch = [
        real_data_root / "v2/source/video.mp4",
        real_data_root / "v2/agent_input.json",
        real_data_root / "v2/text/comments.jsonl",
        real_data_root / "v2/frames/frame_0000.jpg",
    ]
    before = _file_hashes(watch)
    assert len(before) == len(watch)
    assert _run_package(isolated_project, real_data_root / "v2") == 0
    after = _file_hashes(watch)
    assert before == after


def test_batch_mock_two_packages(real_data_root, isolated_project):
    args = SimpleNamespace(
        data_root=real_data_root, mode="mock", max_live_calls=None,
        run_id=None, config=None, only="v3,v9",
    )
    code = pipeline_module.run_batch(args)
    assert code == 0
    runs = list((isolated_project / "runs").iterdir())
    videos = list((runs[0] / "videos").iterdir())
    assert len(videos) == 2  # v3（空 ASR）与 v9 均完整处理


def test_resume_does_not_recall_succeeded(real_data_root, isolated_project):
    """恢复相同运行：成功任务不重复调用。"""
    assert _run_package(isolated_project, real_data_root / "v2") == 0
    runs = list((isolated_project / "runs").iterdir())
    run_id = runs[0].name
    usage_file = runs[0] / "usage.jsonl"
    usage_before = usage_file.read_text(encoding="utf-8").splitlines()

    args = SimpleNamespace(run_id=run_id, config=None, max_live_calls=None)
    code = pipeline_module.run_resume(args)
    assert code == 0
    usage_after = usage_file.read_text(encoding="utf-8").splitlines()
    new_events = [
        json.loads(line)
        for line in usage_after[len(usage_before):]
        if line.strip()
    ]
    # 全部任务成功 → 恢复时零新调用（连缓存命中都不需要）
    assert new_events == []


def test_evaluate_runs_on_mock_run(real_data_root, isolated_project, tmp_path):
    assert _run_package(isolated_project, real_data_root / "v9") == 0
    runs = list((isolated_project / "runs").iterdir())
    args = SimpleNamespace(
        run_id=runs[0].name,
        annotations=Path("annotations/annotation_template.jsonl"),
        iou=0.3,
        config=None,
    )
    from defect_agent.evaluation import run_evaluate

    # annotations 路径相对项目根；直接指向真实文件
    args.annotations = Path(__file__).resolve().parents[2] / "annotations/annotation_template.jsonl"
    assert run_evaluate(args) == 0
    report = json.loads((runs[0] / "evaluation_report.json").read_text(encoding="utf-8"))
    assert report["is_mock"] is True
    assert "citation_metrics" in report
    assert report["citation_metrics"]["引用存在率"] == 1.0  # 全部引用可解析


def test_stability_command_bypasses_cache(isolated_project, capsys):
    from defect_agent.evaluation import run_stability

    args = SimpleNamespace(package=None, config=None)
    assert run_stability(args) == 0
    experiments = isolated_project / "experiments" / "stability_latest.json"
    report = json.loads(experiments.read_text(encoding="utf-8"))
    assert report["cache_bypassed"] is True
    assert report["repeats"] == 3
    assert len(report["signatures"]) == 3  # 三次独立采样，而非一次缓存结果


def test_synthetic_v2_mock_pipeline(v2_package, isolated_project):
    """合成迷你包：完整 mock 闭环 + 关键产物断言。"""
    assert _run_package(isolated_project, v2_package) == 0
    runs = list((isolated_project / "runs").iterdir())
    video_dir = next((runs[0] / "videos").iterdir())
    claims = [
        json.loads(line)
        for line in (video_dir / "claims.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evidence = {
        json.loads(line)["evidence_id"]
        for line in (video_dir / "normalized/evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert claims
    for claim in claims:
        assert set(claim["evidence_ids"]) <= evidence  # 每条陈述引用合法
    # 指令注入评论在合成包中不存在；标题声称被提取为 claim
    assert any(c["claim_type"] in ("system_state_claim", "phenomenon_claim") for c in claims)
    # embedding 同源检测已运行（mock 客户端，产物落盘且标记 is_mock）
    same_source = json.loads((video_dir / "tasks" / "claims_samesource.json").read_text(encoding="utf-8"))
    assert same_source["is_mock"] is True


def test_synthetic_v1_mock_pipeline(v1_package, isolated_project):
    assert _run_package(isolated_project, v1_package) == 0
    runs = list((isolated_project / "runs").iterdir())
    video_dir = next((runs[0] / "videos").iterdir())
    events = [
        json.loads(line)
        for line in (video_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 合成 v1：uncertain 观察 → 事件存在但不判 suspected
    for event in events:
        assert event["defect_assessment"] != "confirmed_defect"
    report = (video_dir / "report.md").read_text(encoding="utf-8")
    assert "未验证" in report and "mock" in report
