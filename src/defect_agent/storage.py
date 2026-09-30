"""T11 运行状态：SQLite 状态机、usage/errors 日志与原子写入（§11.2、§11.3）。

任务状态：pending / running / succeeded / failed / skipped / paused_budget
视频状态：complete / partial / failed
只有完整输出通过校验后才标记 succeeded；中断残留的 running 在恢复时转为 pending。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

TASK_STATUSES = ("pending", "running", "succeeded", "failed", "skipped", "paused_budget")
VIDEO_STATUSES = ("complete", "partial", "failed")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件写入后原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def atomic_write_json(path: Path, payload) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


class RunState:
    """runs/<run_id>/state.sqlite 的封装。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                item TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                output_path TEXT,
                error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                task TEXT,
                model TEXT,
                duration_ms INTEGER,
                cached INTEGER NOT NULL DEFAULT 0,
                is_mock INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                repair_used INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                task_key TEXT,
                kind TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # ---- tasks ----
    def set_task(
        self,
        task_key: str,
        run_id: str,
        video_id: str,
        stage: str,
        item: str,
        status: str,
        error: str | None = None,
        output_path: str | None = None,
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (task_key, run_id, video_id, stage, item, status,
                                   attempts, output_path, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, COALESCE(?, (
                            SELECT output_path FROM tasks WHERE task_key = ?)),
                        ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    status = excluded.status,
                    attempts = attempts + 1,
                    output_path = excluded.output_path,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    task_key,
                    run_id,
                    video_id,
                    stage,
                    item,
                    status,
                    output_path,
                    task_key,
                    error,
                    utc_now(),
                ),
            )
            self._conn.commit()

    def get_task(self, task_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM tasks WHERE task_key = ?", (task_key,)
            ).fetchone()

    def tasks(self, video_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM tasks"
        params: tuple = ()
        if video_id:
            query += " WHERE video_id = ?"
            params = (video_id,)
        with self._lock:
            return list(self._conn.execute(query + " ORDER BY rowid", params))

    def reset_running(self) -> int:
        """恢复时：残留 running → pending（待检查重跑，不视为成功）。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET status = 'pending', updated_at = ? WHERE status = 'running'",
                (utc_now(),),
            )
            self._conn.commit()
            return cursor.rowcount

    def paused_tasks(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute("SELECT * FROM tasks WHERE status = 'paused_budget'")
            )

    def set_task_paused_to_pending(self, task_key: str) -> None:
        """resume 时把预算暂停的任务重新入队。"""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'pending', updated_at = ? WHERE task_key = ? AND status = 'paused_budget'",
                (utc_now(), task_key),
            )
            self._conn.commit()

    # ---- videos ----
    def set_video_status(self, video_id: str, run_id: str, status: str) -> None:
        if status not in VIDEO_STATUSES:
            raise ValueError(f"非法视频状态: {status}")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO videos (video_id, run_id, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    status = excluded.status, updated_at = excluded.updated_at
                """,
                (video_id, run_id, status, utc_now()),
            )
            self._conn.commit()

    def video_status(self, video_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return row["status"] if row else None

    def all_video_status(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT video_id, status FROM videos").fetchall()
        return {row["video_id"]: row["status"] for row in rows}

    # ---- usage / errors ----
    def record_usage(self, event) -> None:
        usage = event.usage
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage (ts, task, model, duration_ms, cached, is_mock,
                                   prompt_tokens, completion_tokens, total_tokens,
                                   repair_used, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    event.task,
                    event.model,
                    event.duration_ms,
                    1 if event.cached else 0,
                    1 if event.is_mock else 0,
                    usage.prompt_tokens if usage else None,
                    usage.completion_tokens if usage else None,
                    usage.total_tokens if usage else None,
                    1 if event.repair_used else 0,
                    event.error,
                ),
            )
            self._conn.commit()

    def record_error(self, task_key: str | None, kind: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO errors (ts, task_key, kind, message) VALUES (?, ?, ?, ?)",
                (utc_now(), task_key, kind, message[:2000]),
            )
            self._conn.commit()

    def usage_summary(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cached_calls,
                       SUM(CASE WHEN is_mock = 1 THEN 1 ELSE 0 END) AS mock_calls,
                       SUM(CASE WHEN is_mock = 0 AND cached = 0 THEN 1 ELSE 0 END) AS actual_live_calls,
                       SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                       SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                       SUM(COALESCE(completion_tokens, 0)) AS completion_tokens
                FROM usage
                """
            ).fetchone()
            error_count = self._conn.execute("SELECT COUNT(*) AS n FROM errors").fetchone()["n"]
            by_task = self._conn.execute(
                """
                SELECT task,
                       COUNT(*) AS calls,
                       AVG(duration_ms) AS avg_ms,
                       MAX(duration_ms) AS max_ms
                FROM usage
                WHERE error IS NULL
                GROUP BY task
                ORDER BY task
                """
            ).fetchall()
        return {
            "calls": row["calls"] or 0,
            "cached_calls": row["cached_calls"] or 0,
            "mock_calls": row["mock_calls"] or 0,
            "live_calls": row["actual_live_calls"] or 0,
            "total_tokens": row["total_tokens"] or 0,
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "errors": error_count or 0,
            "by_task": {
                task["task"]: {
                    "calls": task["calls"],
                    "avg_seconds": round((task["avg_ms"] or 0) / 1000, 1),
                    "max_seconds": round((task["max_ms"] or 0) / 1000, 1),
                }
                for task in by_task
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
