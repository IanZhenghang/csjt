"""流水线任务执行流的单测：持续限流自动暂停、任务进度与状态。"""

from __future__ import annotations

import json
from pathlib import Path

from defect_agent.llm import BudgetTracker, TransientError
from defect_agent.pipeline import RunContext, _run_task
from defect_agent.storage import RunState


def _ctx(tmp_path: Path) -> RunContext:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "errors.jsonl").touch()
    return RunContext(
        run_id="test-run",
        run_dir=run_dir,
        config=None,  # type: ignore[arg-type]
        mode="live",
        client=None,  # type: ignore[arg-type]
        budget=BudgetTracker(max_live_calls=100, max_total_tokens=0),
        cache=None,  # type: ignore[arg-type]
        state=RunState(run_dir / "state.sqlite"),
        is_mock=False,
    )


def _rate_limit_error() -> TransientError:
    error = TransientError("HTTP 429，第 3 次尝试后将等待 40.0s")
    error.rate_limited = True  # type: ignore[attr-defined]
    return error


def test_three_consecutive_rate_limit_failures_pause_run(tmp_path):
    ctx = _ctx(tmp_path)

    def fail_fn():
        raise _rate_limit_error()

    for index in range(3):
        result = _run_task(ctx, "V", "text", f"w{index}", fail_fn, tmp_path / f"out{index}.json")
        assert result is None
    # 第三个任务后应进入暂停状态
    assert ctx.stop_reason == "rate_limited"
    rows = ctx.state.tasks("V")
    statuses = {row["item"]: row["status"] for row in rows}
    assert statuses["w2"] == "paused_budget"
    assert statuses["w0"] == "failed"
    assert statuses["w1"] == "failed"
    # 后续任务不再执行
    result = _run_task(ctx, "V", "text", "w3", fail_fn, tmp_path / "out3.json")
    assert result is None


def test_rate_limit_counter_resets_on_success(tmp_path):
    ctx = _ctx(tmp_path)

    def ok_fn():
        return {"ok": True}

    def fail_fn():
        raise _rate_limit_error()

    _run_task(ctx, "V", "text", "a", fail_fn, tmp_path / "a.json")
    _run_task(ctx, "V", "text", "b", fail_fn, tmp_path / "b.json")
    _run_task(ctx, "V", "text", "c", ok_fn, tmp_path / "c.json")
    _run_task(ctx, "V", "text", "d", fail_fn, tmp_path / "d.json")
    assert ctx.stop_reason is None  # 成功重置计数，两次失败不触发暂停
    _run_task(ctx, "V", "text", "e", fail_fn, tmp_path / "e.json")
    assert ctx.stop_reason is None
    _run_task(ctx, "V", "text", "f", fail_fn, tmp_path / "f.json")
    assert ctx.stop_reason == "rate_limited"  # 连续第三次才触发


def test_succeeded_task_skipped_on_reload(tmp_path):
    ctx = _ctx(tmp_path)
    output = tmp_path / "t.json"

    def fn():
        return {"value": 42}

    first = _run_task(ctx, "V", "text", "t", fn, output)
    assert first == {"value": 42}
    # 再次执行：直接读文件，不再调用 fn
    called = {"n": 0}

    def fn2():
        called["n"] += 1
        return {"value": 99}

    second = _run_task(ctx, "V", "text", "t", fn2, output)
    assert second == {"value": 42}
    assert called["n"] == 0
