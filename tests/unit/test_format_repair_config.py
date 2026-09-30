"""Offline coverage for repair limits and run/resume CLI overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from defect_agent.cli import build_parser
from defect_agent.config import AgentConfig, load_config


def test_repair_limit_defaults_to_three(tmp_path):
    config = AgentConfig(yaml_path=tmp_path / "unused.yaml", data_root=tmp_path)
    assert config.budget["max_format_repairs_per_result"] == 3
    assert load_config(env_path=tmp_path / "absent.env").max_format_repairs_per_result == 3


@pytest.mark.parametrize("value", [-1, True, 1.5, "3", None])
def test_invalid_repair_limit_rejected_at_config_creation(tmp_path, value):
    with pytest.raises(ValueError, match="max_format_repairs_per_result"):
        AgentConfig(
            yaml_path=tmp_path / "unused.yaml", data_root=tmp_path,
            raw={"budget": {"max_format_repairs_per_result": value}},
        )


def test_negative_repair_limit_rejected_when_loading_yaml(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("budget:\n  max_format_repairs_per_result: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_format_repairs_per_result"):
        load_config(path, env_path=tmp_path / "absent.env")


@pytest.mark.parametrize("command,required", [
    ("run", ["--package", "sample"]),
    ("batch", ["--data-root", "data"]),
    ("resume", ["--run-id", "old-run"]),
])
@pytest.mark.parametrize("value", ["-1", "1.5"])
def test_cli_rejects_invalid_repair_limit(command, required, value):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([command, *required, "--max-format-repairs", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["run", "batch"])
@pytest.mark.parametrize("limit", [0, 5])
def test_new_run_override_reaches_context_and_snapshot(tmp_path, monkeypatch, command, limit):
    import defect_agent.pipeline as pipeline

    project = tmp_path / "project"
    data_root = tmp_path / "data"
    package = data_root / "sample"
    package.mkdir(parents=True)
    config = AgentConfig(
        yaml_path=tmp_path / "settings.yaml", data_root=data_root,
        raw={"budget": {"max_format_repairs_per_result": 1}, "embedding": {"enabled": False}},
    )
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", project)
    monkeypatch.setattr(pipeline, "load_config", lambda *args: config)
    monkeypatch.setattr(pipeline, "discover_packages", lambda root: [package])
    seen = []

    def inspect_context(ctx, package_root):
        seen.append(ctx)
        assert package_root == package
        assert ctx.config.max_format_repairs_per_result == limit
        return {"status": "complete", "package_id": "sample", "events": 0}

    monkeypatch.setattr(pipeline, "process_video", inspect_context)
    required = ["--package", str(package)] if command == "run" else ["--data-root", str(data_root)]
    args = build_parser().parse_args([
        command, *required, "--run-id", "new-run", "--max-format-repairs", str(limit),
    ])
    try:
        assert getattr(pipeline, "run_package" if command == "run" else "run_batch")(args) == 0
        assert len(seen) == 1
        snapshot = json.loads((project / "runs/new-run/config_snapshot.json").read_text(encoding="utf-8"))
        assert snapshot["yaml"]["budget"]["max_format_repairs_per_result"] == limit
    finally:
        for ctx in seen:
            ctx.state.close()
            ctx.cache.close()


@pytest.mark.parametrize("override,expected", [(None, 1), (0, 0), (4, 4)])
def test_resume_preserves_recorded_limit_unless_explicitly_overridden(tmp_path, monkeypatch, override, expected):
    import defect_agent.pipeline as pipeline

    project = tmp_path / "project"
    data_root = tmp_path / "data"
    (data_root / "sample").mkdir(parents=True)
    run_dir = project / "runs" / "old-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "mode": "mock", "planned_packages": ["sample"], "pipeline_version": "rag-v1",
    }), encoding="utf-8")
    snapshot_path = run_dir / "config_snapshot.json"
    snapshot_path.write_text(json.dumps({
        "data_root": str(data_root), "llm": {},
        "yaml": {"budget": {"max_format_repairs_per_result": 1}, "embedding": {"enabled": False}},
    }), encoding="utf-8")
    today = AgentConfig(yaml_path=Path("today.yaml"), data_root=data_root)
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", project)
    monkeypatch.setattr(pipeline, "load_config", lambda *args: today)
    seen = []

    def inspect_context(ctx, package_root):
        seen.append(ctx)
        assert ctx.config.max_format_repairs_per_result == expected
        return {"status": "complete", "package_id": package_root.name, "events": 0}

    monkeypatch.setattr(pipeline, "process_video", inspect_context)
    argv = ["resume", "--run-id", "old-run"]
    if override is not None:
        argv.extend(["--max-format-repairs", str(override)])
    try:
        assert pipeline.run_resume(build_parser().parse_args(argv)) == 0
        assert len(seen) == 1
        # The override applies to this resume; retain the original run's provenance.
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["yaml"]["budget"]["max_format_repairs_per_result"] == 1
    finally:
        for ctx in seen:
            ctx.state.close()
            ctx.cache.close()


def test_invalid_restored_limit_is_rejected_before_run_files_are_created(tmp_path, monkeypatch):
    import defect_agent.pipeline as pipeline

    config = AgentConfig(yaml_path=tmp_path / "unused.yaml", data_root=tmp_path)
    config.raw = {"budget": {"max_format_repairs_per_result": -1}}
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    run_dir = tmp_path / "invalid-run"
    with pytest.raises(ValueError, match="max_format_repairs_per_result"):
        pipeline.create_run(config, "mock", None, run_dir_override=run_dir)
    assert not run_dir.exists()
