"""Run three isolated mock pipelines over v2/v4/v7/v10 without touching user runs.

The parent process only orchestrates subprocesses and produces evaluation files.
Each child owns its PROJECT_ROOT, cache, index and run directory. This is a
functional validation protocol, never evidence of live speed or semantic recall.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))
SCHEMES = ("legacy", "rag_serial", "rag_parallel")
PACKAGES = ("v2", "v4", "v7", "v10")


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write(path: Path, value) -> None:
    from defect_agent.storage import atomic_write_json

    atomic_write_json(path, value)


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _source_manifest() -> dict:
    paths = list((SOURCE_ROOT / "src" / "defect_agent").rglob("*.py"))
    paths += list((SOURCE_ROOT / "prompts").glob("*.md"))
    paths += [SOURCE_ROOT / "config" / "default.yaml", Path(__file__).resolve()]
    hashes = {path.relative_to(SOURCE_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(paths)}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()
    return {"fingerprint": fingerprint, "files": hashes, "python": sys.version,
            "executable": sys.executable, "source_root": str(SOURCE_ROOT)}


def _run_status(run_dir: Path) -> dict:
    videos, tasks = [], []
    state = run_dir / "state.sqlite"
    if state.is_file():
        with sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            videos = [dict(row) for row in connection.execute("SELECT video_id, status FROM videos ORDER BY video_id")]
            tasks = [dict(row) for row in connection.execute(
                "SELECT stage, item, status, error FROM tasks WHERE status != 'succeeded' ORDER BY stage, item"
            )]
    for video in videos:
        directory = run_dir / "videos" / video["video_id"]
        package = _read(directory / "normalized" / "normalized.json", {})
        video["package_id"] = package.get("package", package).get("package_id")
        video["performance"] = _read(directory / "performance.json", {})
        video["alignment_metrics"] = _read(directory / "tasks" / "alignment_metrics.json", {})
    errors = _jsonl(run_dir / "errors.jsonl")
    context_failures = [error for error in errors if "context" in str(error).lower()
                        and any(word in str(error).lower() for word in ("exceed", "missing", "incomplete"))]
    return {"video_status_counts": dict(Counter(video["status"] for video in videos)),
            "videos": videos, "non_succeeded_tasks": tasks, "errors": errors,
            "context_failures": context_failures,
            "missing_packages": sorted(set(PACKAGES) - {video.get("package_id") for video in videos}),
            "actual_tokens": None, "billed_cost": None}


def _worker(args) -> int:
    """The only entry point that executes a pipeline; always a fresh process."""
    import httpx
    import yaml

    from defect_agent import config as config_module
    from defect_agent import pipeline
    from defect_agent.config import AgentConfig, LLMSettings

    scheme_root = args._scheme_root.resolve()
    data_root = args.data_root.resolve()
    if _source_manifest()["fingerprint"] != args._source_hash:
        raise RuntimeError("源码已变化，拒绝以另一版本开始方案")
    if (scheme_root / "runs").exists() or (scheme_root / "work").exists():
        raise RuntimeError("方案根已有运行或缓存，拒绝覆盖或复用")
    raw = copy.deepcopy(yaml.safe_load((SOURCE_ROOT / "config" / "default.yaml").read_text(encoding="utf-8")))
    raw.setdefault("pipeline", {})["version"] = "legacy" if args._worker == "legacy" else "rag-v1"
    raw.setdefault("rag", {})["align_max_workers"] = 2 if args._worker == "rag_parallel" else 1
    # No .env or API key is read. Quality, sampling, budgets and 24k context limit
    # retain the checked-in defaults; the protocol does not hide partial outputs.
    config = AgentConfig(
        yaml_path=SOURCE_ROOT / "config" / "default.yaml", data_root=data_root, raw=raw, env={},
        llm=LLMSettings(mode="mock", api_key=None, base_url="https://offline.invalid/v1",
                        provider="openai_compatible", text_model="offline-mock", vision_model="offline-mock"),
    )
    config_module.PROJECT_ROOT = scheme_root
    pipeline.PROJECT_ROOT = scheme_root
    pipeline.load_config = lambda *unused_args, **unused_kwargs: copy.deepcopy(config)
    blocked_http = []

    def block_http(*unused_args, **unused_kwargs):
        blocked_http.append("HTTP request blocked")
        raise RuntimeError("离线验证禁止 HTTP 请求")

    httpx.Client.send = block_http
    httpx.AsyncClient.send = block_http
    original_manifest_writer = pipeline._write_run_manifest

    def write_manifest(ctx, current_config, planned, mode):
        if mode != "mock" or not ctx.is_mock:
            raise RuntimeError("离线验证只允许 mock 模式")
        original_manifest_writer(ctx, current_config, planned, "mock")
        path = ctx.run_dir / "run_manifest.json"
        manifest = _read(path)
        manifest["benchmark_conditions"] = {
            "cache_state": "cold", "mode": "mock", "scheme": args._worker,
            "isolated_project_root": str(scheme_root), "fresh_cache_and_index": True,
            "source_fingerprint": args._source_hash,
        }
        manifest["offline_validation_note"] = "Mock token placeholders do not estimate actual tokens or billed cost."
        _write(path, manifest)

    pipeline._write_run_manifest = write_manifest
    # Mock clients emit zero-token placeholders. Preserve their unknown real
    # token cost in the experiment's raw usage stream as well as aggregate files.
    original_create_run = pipeline.create_run

    def create_mock_run(*call_args, **call_kwargs):
        ctx = original_create_run(*call_args, **call_kwargs)
        if not ctx.is_mock:
            raise RuntimeError("离线验证拒绝真实客户端")
        original_recorder = ctx.client.recorder

        def record_mock(event):
            if not event.is_mock:
                raise RuntimeError("离线验证出现非 mock usage")
            event.usage = None
            original_recorder(event)

        ctx.client.recorder = record_mock
        return ctx

    pipeline.create_run = create_mock_run
    run_dir = scheme_root / "runs" / "offline-mock"
    started = time.monotonic()
    result = {"scheme": args._worker, "project_root": str(scheme_root), "run_dir": str(run_dir),
              "is_mock": True, "cache_state": "cold", "source_fingerprint": args._source_hash,
              "max_context_characters": config.rag["max_context_characters"]}
    try:
        result["pipeline_exit_code"] = pipeline.run_batch(SimpleNamespace(
            config=None, data_root=data_root, mode="mock", max_live_calls=None, run_id="offline-mock",
            only=",".join(PACKAGES), experiment=None,
        ))
        result["execution_status"] = "finished"
    except BaseException as exc:
        result["execution_status"] = "crashed"
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        result["pipeline_exit_code"] = 130 if isinstance(exc, KeyboardInterrupt) else 1
    result["protocol_wall_seconds"] = round(time.monotonic() - started, 3)
    result["blocked_http_attempts"] = len(blocked_http)
    result["source_unchanged"] = _source_manifest()["fingerprint"] == args._source_hash
    result.update(_run_status(run_dir))
    summary_path = run_dir / "usage_summary.json"
    if summary_path.is_file():
        summary = _read(summary_path)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            summary[name] = None
        summary["mock_token_note"] = "No real token usage measured; mock counters are not cost estimates."
        _write(summary_path, summary)
    _write(scheme_root / "worker_result.json", result)
    print(json.dumps({key: result[key] for key in
                      ("scheme", "execution_status", "video_status_counts", "missing_packages", "blocked_http_attempts")},
                     ensure_ascii=False), flush=True)
    return 0 if result["execution_status"] == "finished" and not blocked_http and result["source_unchanged"] else 1


def _clear_mock_cost_estimates(comparison: dict) -> None:
    for run in comparison.get("runs", []):
        for stats in [run.get("usage"), run.get("embedding"), *run.get("usage_by_task", {}).values()]:
            if stats is not None:
                stats["reported_total_tokens"] = None
                stats["known_total_tokens"] = None
                stats["token_usage_complete"] = False
                stats["billed_cost"] = None
        run["billed_cost"] = None
        run["notes"].append("This isolated mock run measures no actual tokens, billed cost, live speed or semantic recall.")


def _parent(args) -> int:
    from defect_agent.rag_evaluation import compare_runs, prepare_benchmark, score_benchmark

    data_root, output_root = args.data_root.resolve(), args.output_root.resolve()
    if not data_root.is_dir():
        raise ValueError(f"数据目录不存在: {data_root}")
    protected = [data_root, SOURCE_ROOT / "runs", SOURCE_ROOT / "work"]
    if any(output_root == path.resolve() or output_root.is_relative_to(path.resolve()) for path in protected):
        raise ValueError("实验输出不能位于原始数据或用户 runs/work 目录内")
    output_root.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never delete, resume or overwrite an earlier experiment.
    experiment_root = output_root / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8])
    experiment_root.mkdir(exist_ok=False)
    source = _source_manifest()
    _write(experiment_root / "source_manifest.json", source)
    manifest = {"schema_version": "rag-offline-validation:1", "mode": "mock", "cache_state": "cold",
                "experiment_root": str(experiment_root), "data_root": str(data_root), "packages": list(PACKAGES),
                "source_fingerprint": source["fingerprint"], "status": "running", "schemes": [],
                "artifacts": {}, "actual_tokens": None, "billed_cost": None,
                "limitations": "Only functional/offline validation. No claim about live speed, semantic recall or billing."}
    manifest_path = experiment_root / "experiment_manifest.json"
    _write(manifest_path, manifest)
    print(f"离线实验目录：{experiment_root}", flush=True)
    for scheme in SCHEMES:
        if _source_manifest()["fingerprint"] != source["fingerprint"]:
            manifest["status"] = "source_changed"
            manifest["error"] = "源码在方案间变化，后续方案未启动；请用新目录重新运行。"
            _write(manifest_path, manifest)
            print(manifest["error"], flush=True)
            return 1
        scheme_root = experiment_root / scheme
        scheme_root.mkdir(exist_ok=False)
        log_path = scheme_root / "output.log"
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker", scheme,
                   "--_scheme-root", str(scheme_root), "--_source-hash", source["fingerprint"],
                   "--data-root", str(data_root)]
        print(f"运行 {scheme}（mock，独立冷缓存；日志 {log_path}）", flush=True)
        with log_path.open("x", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=SOURCE_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       check=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        result = _read(scheme_root / "worker_result.json", {
            "scheme": scheme, "execution_status": "crashed", "error": "worker_result.json missing",
            "run_dir": str(scheme_root / "runs" / "offline-mock"), "video_status_counts": {},
        })
        result["process_exit_code"] = completed.returncode
        result["log_file"] = str(log_path)
        manifest["schemes"].append(result)
        _write(manifest_path, manifest)
        print(f"{scheme}: {result['execution_status']}，视频状态 {result.get('video_status_counts', {})}，"
              f"上下文失败 {len(result.get('context_failures', []))}", flush=True)
        if not result.get("source_unchanged", False):
            manifest["status"] = "source_changed_or_worker_crashed"
            _write(manifest_path, manifest)
            return 1
    run_dirs = [Path(result["run_dir"]) for result in manifest["schemes"]]
    comparison = compare_runs(run_dirs)
    _clear_mock_cost_estimates(comparison)
    comparison["offline_video_outcomes"] = [{"scheme": result["scheme"],
        "video_status_counts": result.get("video_status_counts"), "missing_packages": result.get("missing_packages"),
        "context_failure_count": len(result.get("context_failures", []))} for result in manifest["schemes"]]
    benchmark = prepare_benchmark(run_dirs[1])
    score = score_benchmark(benchmark, run_dirs[1])
    artifacts = {"compare": experiment_root / "compare.json", "manual_benchmark": experiment_root / "manual_benchmark.json",
                 "score_unreviewed": experiment_root / "score_unreviewed.json"}
    for name, value in (("compare", comparison), ("manual_benchmark", benchmark), ("score_unreviewed", score)):
        _write(artifacts[name], value)
    manifest["artifacts"] = {name: str(path) for name, path in artifacts.items()}
    manifest["status"] = "finished"
    manifest["all_videos_complete"] = all(result.get("video_status_counts") == {"complete": len(PACKAGES)}
                                           for result in manifest["schemes"])
    manifest["source_unchanged"] = _source_manifest()["fingerprint"] == source["fingerprint"]
    _write(manifest_path, manifest)
    print(f"生成对比、人工待复核基准（{len(benchmark['items'])} 条）与未标注评分；"
          f"已评分 {score['scored_queries']} 条，真实召回未知。", flush=True)
    print(f"全部视频完成：{manifest['all_videos_complete']}；详见 {manifest_path}", flush=True)
    print("mock 实验不测量真实 tokens/费用，不据此判断真实 API 提速或语义召回。", flush=True)
    return 0 if manifest["source_unchanged"] and all(result.get("execution_status") == "finished"
                   and not result.get("blocked_http_attempts") and not result.get("missing_packages")
                   for result in manifest["schemes"]) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("D:/数据汇总/数据汇总"))
    parser.add_argument("--output-root", type=Path, default=SOURCE_ROOT / "experiments" / "rag_validation")
    parser.add_argument("--_worker", choices=SCHEMES, help=argparse.SUPPRESS)
    parser.add_argument("--_scheme-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_source-hash", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        if args._scheme_root is None or not args._source_hash:
            parser.error("内部 worker 缺少隔离根或源码指纹")
        return _worker(args)
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
