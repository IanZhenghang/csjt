"""T11 存储状态机与原子写入单测。"""

from __future__ import annotations

import json

from defect_agent.storage import RunState, atomic_write_json


def test_task_lifecycle(tmp_path):
    state = RunState(tmp_path / "state.sqlite")
    state.set_task("V|text|w1", "run1", "V", "text", "w1", "running")
    assert state.get_task("V|text|w1")["status"] == "running"
    state.set_task("V|text|w1", "run1", "V", "text", "w1", "succeeded", output_path="x.json")
    row = state.get_task("V|text|w1")
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2
    assert row["output_path"] == "x.json"


def test_invalid_status_rejected(tmp_path):
    state = RunState(tmp_path / "state.sqlite")
    try:
        state.set_task("k", "r", "v", "s", "i", "bogus")
        assert False, "应拒绝非法状态"
    except ValueError:
        pass


def test_reset_running(tmp_path):
    state = RunState(tmp_path / "state.sqlite")
    state.set_task("a", "r", "v", "s", "i1", "running")
    state.set_task("b", "r", "v", "s", "i2", "succeeded")
    assert state.reset_running() == 1  # 中断残留 running → pending
    assert state.get_task("a")["status"] == "pending"
    assert state.get_task("b")["status"] == "succeeded"  # 成功不动


def test_paused_to_pending(tmp_path):
    state = RunState(tmp_path / "state.sqlite")
    state.set_task("a", "r", "v", "s", "i", "paused_budget")
    state.set_task_paused_to_pending("a")
    assert state.get_task("a")["status"] == "pending"


def test_video_status_roundtrip(tmp_path):
    state = RunState(tmp_path / "state.sqlite")
    state.set_video_status("V", "run1", "complete")
    assert state.video_status("V") == "complete"
    state.set_video_status("V", "run1", "partial")
    assert state.all_video_status()["V"] == "partial"


def test_usage_and_errors(tmp_path):
    from defect_agent.llm import LLMUsage, UsageEvent

    state = RunState(tmp_path / "state.sqlite")
    state.record_usage(
        UsageEvent(task="t", model="m", duration_ms=5, cached=False, is_mock=True,
                   usage=LLMUsage(1, 2, 3))
    )
    state.record_error("k", "kind", "message")
    summary = state.usage_summary()
    assert summary["calls"] == 1 and summary["mock_calls"] == 1 and summary["total_tokens"] == 3
    assert summary["errors"] == 1


def test_atomic_write(tmp_path):
    target = tmp_path / "nested" / "out.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert not list(tmp_path.rglob("*.tmp"))
