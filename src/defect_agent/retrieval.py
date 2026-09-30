"""持久证据索引与可复现混合检索；只选择候选，不作事实判定。

索引按视频、输入版本、embedding 命名空间和派生版本隔离。一次 replace
发布完整快照，读取者只能看到替换前或替换后的完整记录。语义检索失败时
默认保留全部候选，避免将无效向量的任意排序当成可靠的候选筛选。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .llm.base import AuthError, BudgetExhausted


@dataclass
class IndexedItem:
    item_id: str
    kind: str
    text: str
    video_id: str
    start_sec: float | None = None
    end_sec: float | None = None
    evidence_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    vector: list[float] | None = None


def _unit_vector(vector) -> list[float] | None:
    if not isinstance(vector, (list, tuple)) or not vector:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vector):
        return None
    try:
        values = [float(v) for v in vector]
        norm = math.hypot(*values)
    except (ValueError, TypeError, OverflowError):
        return None
    if not all(math.isfinite(v) for v in values) or not math.isfinite(norm) or norm == 0:
        return None
    return [v / norm for v in values]


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


class EvidenceIndex:
    """同一个 SQLite 文件中可并存多个互不污染的证据快照。"""

    def __init__(
        self, path: Path | str, *, video_id: str, source_revision: str,
        model_namespace: str, derived_revision: str = "",
    ):
        self.namespace = {
            "video_id": video_id,
            "source_revision": source_revision,
            "model_namespace": model_namespace,
            "derived_revision": derived_revision,
        }
        self.namespace_id = hashlib.sha256(_json(self.namespace).encode("utf-8")).hexdigest()
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence_index_snapshots (
                namespace_id TEXT PRIMARY KEY,
                namespace_json TEXT NOT NULL,
                snapshot_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_index_items (
                namespace_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (namespace_id, item_id)
            );
            """
        )
        self._conn.commit()

    def replace(self, items: list[IndexedItem]) -> None:
        """原子发布当前命名空间；坏向量存为缺失，检索时明确降级。"""
        rows = []
        seen: set[str] = set()
        for item in sorted(items, key=lambda entry: entry.item_id):
            if item.video_id != self.namespace["video_id"]:
                raise ValueError("证据所属视频与索引命名空间不一致")
            if not item.item_id or item.item_id in seen:
                raise ValueError(f"证据索引 ID 为空或重复: {item.item_id!r}")
            seen.add(item.item_id)
            payload = asdict(item)
            if _unit_vector(item.vector) is None:
                payload["vector"] = None
            rows.append((self.namespace_id, item.item_id, item.kind, _json(payload)))
        snapshot_id = hashlib.sha256(
            _json({"namespace": self.namespace, "items": [row[3] for row in rows]}).encode("utf-8")
        ).hexdigest()
        with self._lock, self._conn:
            # DELETE starts the transaction; the snapshot header and all rows commit together.
            self._conn.execute(
                "DELETE FROM evidence_index_items WHERE namespace_id = ?", (self.namespace_id,),
            )
            self._conn.executemany(
                "INSERT INTO evidence_index_items (namespace_id, item_id, kind, payload) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.execute(
                "INSERT INTO evidence_index_snapshots VALUES (?, ?, ?) "
                "ON CONFLICT(namespace_id) DO UPDATE SET "
                "namespace_json = excluded.namespace_json, snapshot_id = excluded.snapshot_id",
                (self.namespace_id, _json(self.namespace), snapshot_id),
            )

    def _snapshot(self, kinds: list[str] | None = None) -> tuple[str, list[IndexedItem]]:
        # A single SELECT reads both the snapshot identity and its records consistently.
        join = "s.namespace_id = i.namespace_id"
        params: list = []
        if kinds is not None:
            if kinds:
                join += " AND i.kind IN (" + ",".join("?" for _ in kinds) + ")"
                params.extend(kinds)
            else:
                join += " AND 0"
        params.append(self.namespace_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.snapshot_id, i.payload FROM evidence_index_snapshots s "
                f"LEFT JOIN evidence_index_items i ON {join} "
                "WHERE s.namespace_id = ? ORDER BY i.item_id", params,
            ).fetchall()
        return (
            rows[0]["snapshot_id"] if rows else "",
            [IndexedItem(**json.loads(row["payload"])) for row in rows if row["payload"] is not None],
        )

    def items(self, kinds: list[str] | None = None) -> list[IndexedItem]:
        return self._snapshot(kinds)[1]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass
class RetrievalTrace:
    query_id: str
    query: str
    namespace: dict[str, str]
    snapshot_id: str
    mode: str
    candidate_ids: list[str]
    selected_ids: list[str] = field(default_factory=list)
    filtered_out_ids: list[str] = field(default_factory=list)
    semantic_scores: dict[str, float] = field(default_factory=dict)
    lexical_scores: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    degraded: bool = False
    fallback_reason: str | None = None
    kinds: list[str] | None = None
    semantic_top_k: int = 5
    lexical_top_k: int = 5
    start_sec: float | None = None
    end_sec: float | None = None
    temporal_only: bool = False
    retriever_version: str = "hybrid-bm25-rrf:1"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalResult:
    items: list[IndexedItem]
    trace: RetrievalTrace


def _terms(text: str) -> list[str]:
    """连续中文二元组；单字串保留单字，英文和数字按词切分。"""
    result = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.casefold()):
        if "\u3400" <= part[0] <= "\u9fff" and len(part) > 1:
            result.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return result


def _bm25(query: str, items: list[IndexedItem]) -> dict[str, float]:
    terms = sorted(set(_terms(query)))
    documents = [Counter(_terms(item.text)) for item in items]
    average_length = sum(sum(doc.values()) for doc in documents) / max(1, len(documents))
    frequencies = {term: sum(term in doc for doc in documents) for term in terms}
    scores = {}
    for item, doc in zip(items, documents):
        score = 0.0
        for term in terms:
            count = doc.get(term, 0)
            if not count:
                continue
            idf = math.log(1 + (len(items) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            denominator = count + 1.2 * (0.25 + 0.75 * sum(doc.values()) / (average_length or 1))
            score += idf * count * 2.2 / denominator
        scores[item.item_id] = score
    return scores


def _temporal_reason(item: IndexedItem, start: float, end: float) -> str | None:
    if item.start_sec is None or item.end_sec is None:
        return None
    if not all(math.isfinite(value) for value in (item.start_sec, item.end_sec)):
        return None
    if item.start_sec > item.end_sec:
        return None
    if item.start_sec < end and item.end_sec > start:
        return "temporal_overlap"
    if max(item.start_sec - end, start - item.end_sec, 0) <= 2.0:
        return "temporal_near"
    return None


class Retriever:
    def __init__(
        self, index: EvidenceIndex, embedding_client=None,
        semantic_top_k: int = 5, lexical_top_k: int = 5,
    ):
        if semantic_top_k < 0 or lexical_top_k < 0:
            raise ValueError("检索 top_k 不能为负数")
        self.index = index
        self.embedding_client = embedding_client
        self.semantic_top_k = semantic_top_k
        self.lexical_top_k = lexical_top_k

    def retrieve(
        self, query: str, *, kinds: list[str] | None = None, query_id: str = "",
        start_sec: float | None = None, end_sec: float | None = None,
        temporal_only: bool = False, fallback_all_on_vector_failure: bool = True,
    ) -> RetrievalResult:
        has_time = start_sec is not None and end_sec is not None
        if (start_sec is None) != (end_sec is None) or (temporal_only and not has_time):
            raise ValueError("时间检索必须同时提供 start_sec 和 end_sec")
        if has_time and (not math.isfinite(start_sec) or not math.isfinite(end_sec) or start_sec > end_sec):
            raise ValueError("查询时间区间无效")
        snapshot_id, items = self.index._snapshot(kinds)
        trace = RetrievalTrace(
            query_id=query_id, query=query, namespace=dict(self.index.namespace),
            snapshot_id=snapshot_id, mode="temporal" if temporal_only else "hybrid",
            candidate_ids=[item.item_id for item in items], kinds=list(kinds) if kinds is not None else None,
            semantic_top_k=self.semantic_top_k, lexical_top_k=self.lexical_top_k,
            start_sec=start_sec, end_sec=end_sec, temporal_only=temporal_only,
            reasons={item.item_id: [] for item in items},
        )
        selected: set[str] = set()
        ranks: dict[str, float] = {}
        if has_time:
            for item in items:
                reason = _temporal_reason(item, start_sec, end_sec)
                if reason:
                    selected.add(item.item_id)
                    trace.reasons[item.item_id].append(reason)
        if not temporal_only and items:
            trace.lexical_scores = _bm25(query, items)
            lexical = sorted(trace.lexical_scores, key=lambda key: (-trace.lexical_scores[key], key))
            lexical = [key for key in lexical if trace.lexical_scores[key] > 0][:self.lexical_top_k]
            self._select(lexical, "lexical_top_k", selected, ranks, trace)
            if self.semantic_top_k:
                scores, failure = self._semantic(query, items)
                if failure:
                    trace.degraded = True
                    trace.fallback_reason = failure
                    trace.mode = "fallback_all" if fallback_all_on_vector_failure else "lexical_fallback"
                    if fallback_all_on_vector_failure:
                        selected.update(trace.candidate_ids)
                        for item_id in trace.candidate_ids:
                            trace.reasons[item_id].append("vector_failure_all_candidates")
                else:
                    trace.semantic_scores = scores
                    semantic = sorted(scores, key=lambda key: (-scores[key], key))[:self.semantic_top_k]
                    self._select(semantic, "semantic_top_k", selected, ranks, trace)
        result_items = sorted(
            [item for item in items if item.item_id in selected],
            key=lambda item: (-ranks.get(item.item_id, 0.0), item.item_id),
        )
        trace.selected_ids = [item.item_id for item in result_items]
        trace.filtered_out_ids = [item.item_id for item in items if item.item_id not in selected]
        for item_id in trace.filtered_out_ids:
            trace.reasons[item_id].append("outside_time_range" if temporal_only else "outside_top_k")
        return RetrievalResult(result_items, trace)

    @staticmethod
    def _select(ids, reason, selected, ranks, trace) -> None:
        for rank, item_id in enumerate(ids, start=1):
            selected.add(item_id)
            trace.reasons[item_id].append(reason)
            ranks[item_id] = ranks.get(item_id, 0.0) + 1.0 / (60 + rank)

    def _semantic(self, query: str, items: list[IndexedItem]) -> tuple[dict[str, float], str | None]:
        if self.embedding_client is None:
            return {}, "embedding_unavailable"
        vectors = {item.item_id: _unit_vector(item.vector) for item in items}
        if any(vector is None for vector in vectors.values()):
            return {}, "missing_or_invalid_index_vector"
        dimensions = {len(vector) for vector in vectors.values()}
        if len(dimensions) != 1:
            return {}, "index_vector_dimension_mismatch"
        try:
            response = self.embedding_client.embed([query])
            query_vector = _unit_vector(response[0]) if isinstance(response, (list, tuple)) and len(response) == 1 else None
        except (AuthError, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - retrieval must retain evidence on provider failure
            return {}, f"query_embedding_failed:{type(exc).__name__}"
        if query_vector is None:
            return {}, "invalid_query_vector"
        if len(query_vector) not in dimensions:
            return {}, "query_vector_dimension_mismatch"
        return {
            item_id: max(-1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
            for item_id, vector in vectors.items()
        }, None
