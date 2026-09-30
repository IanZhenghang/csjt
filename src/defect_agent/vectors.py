"""轻量向量存储与余弦检索（SQLite，不依赖外部向量数据库）。

数据规模为每视频数百条，纯 Python 余弦即可；embedding 只用于候选生成
与同源标记，不参与分级与判定（计划书 §8.1 的规则不受影响）。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class VectorItem:
    item_id: str
    video_id: str
    kind: str  # claim / event / title
    text: str
    vector: list[float]
    model: str = ""
    meta: dict = field(default_factory=dict)


class VectorStore:
    """SQLite 向量表：upsert / top_k / 阈值配对 / 连通分量聚类。"""

    def __init__(self, path: Path | None = None):
        self._lock = threading.Lock()
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                item_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                model TEXT NOT NULL,
                vec TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert_many(self, items: list[VectorItem]) -> None:
        with self._lock:
            for item in items:
                self._conn.execute(
                    """
                    INSERT INTO vectors (item_id, video_id, kind, text, model, vec, meta, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        video_id = excluded.video_id,
                        kind = excluded.kind,
                        text = excluded.text,
                        model = excluded.model,
                        vec = excluded.vec,
                        meta = excluded.meta
                    """,
                    (
                        item.item_id,
                        item.video_id,
                        item.kind,
                        item.text,
                        item.model,
                        json.dumps(item.vector),
                        json.dumps(item.meta, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self._conn.commit()

    def all_items(self) -> list[VectorItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, video_id, kind, text, model, vec, meta FROM vectors"
            ).fetchall()
        return [
            VectorItem(
                item_id=row[0],
                video_id=row[1],
                kind=row[2],
                text=row[3],
                vector=json.loads(row[5]),
                model=row[4],
                meta=json.loads(row[6]),
            )
            for row in rows
        ]

    def top_k(self, query: list[float], k: int, kinds: list[str] | None = None) -> list[tuple[str, float]]:
        """返回 (item_id, similarity) 前 k 个。"""
        scored = []
        for item in self.all_items():
            if kinds and item.kind not in kinds:
                continue
            scored.append((item.item_id, cosine(query, item.vector)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def pairs_above(
        self, threshold: float, cross_video_only: bool = False
    ) -> list[tuple[VectorItem, VectorItem, float]]:
        """余弦 ≥ threshold 的项对（O(n²)，适合数百条规模）。"""
        items = self.all_items()
        result = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if cross_video_only and items[i].video_id == items[j].video_id:
                    continue
                score = cosine(items[i].vector, items[j].vector)
                if score >= threshold:
                    result.append((items[i], items[j], score))
        result.sort(key=lambda triple: triple[2], reverse=True)
        return result

    def clusters_by_threshold(self, threshold: float, cross_video_only: bool = False) -> list[list[VectorItem]]:
        """按阈值配对做连通分量聚类；跨视频模式只合并不同视频的高相似项。"""
        items = self.all_items()
        parent = {item.item_id: item.item_id for item in items}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for item_a, item_b, _ in self.pairs_above(threshold, cross_video_only=cross_video_only):
            union(item_a.item_id, item_b.item_id)

        groups: dict[str, list[VectorItem]] = {}
        for item in items:
            groups.setdefault(find(item.item_id), []).append(item)
        # 只保留真正的多成员组时由调用方过滤
        return [sorted(members, key=lambda m: m.item_id) for _, members in sorted(groups.items())]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
