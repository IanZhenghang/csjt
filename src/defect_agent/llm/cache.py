"""响应缓存（SQLite）。

缓存键至少包含：输入内容哈希、图片字节哈希、模型、接口地址、模型参数、
提示词哈希、输出 schema 版本、预处理配置、实验重复编号（IMPLEMENTATION_PLAN.md §11.4）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def compute_cache_key(
    request_system_prompt: str,
    request_user_prompt: str,
    image_hashes: list[str],
    model: str,
    base_url: str,
    params: dict[str, Any],
    output_schema_name: str,
    preprocess_meta: dict[str, Any],
    repetition_index: int,
) -> str:
    payload = {
        "input_hash": hashlib.sha256(
            (request_system_prompt + "\x00" + request_user_prompt).encode("utf-8")
        ).hexdigest(),
        "image_hashes": image_hashes,
        "model": model,
        "base_url": base_url.rstrip("/"),
        "params": params,
        "prompt_hash": hashlib.sha256(request_system_prompt.encode("utf-8")).hexdigest(),
        "schema": output_schema_name,
        "preprocess": preprocess_meta,
        "repetition": repetition_index,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    output_json TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def get(self, key: str) -> str | None:
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT output_json FROM cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set(self, key: str, output_json: str, model: str) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (cache_key, created_at, model, output_json) "
                "VALUES (?, ?, ?, ?)",
                (key, datetime.now(timezone.utc).isoformat(), model, output_json),
            )
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
