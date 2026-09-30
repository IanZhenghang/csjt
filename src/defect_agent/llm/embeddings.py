"""Embedding 客户端：OpenAI 兼容 /embeddings 接口 + 确定性 mock。

- 真实客户端复用预算（每次 API 请求计 1 次）与节流；文本级缓存复用 ResponseCache；
- mock 客户端用 sha256 种子的确定性向量（同文本同向量），保证测试离线可复现；
- 语义相似只用于候选生成与相关性检索，不建立来源关系或改变判定规则。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from typing import Any

import httpx

from ..config import AgentConfig
from .base import AuthError, LLMUsage, Recorder, TransientError, UsageEvent
from .bigmodel import RATE_LIMIT_BACKOFF, retry_after_seconds
from .budget import BudgetTracker
from .cache import ResponseCache
from .scheduling import RequestScheduler

EMBEDDING_MODEL_CANDIDATES = ("text-embedding-v4", "text-embedding-v3", "qwen3-embedding-0.6b")


class EmbeddingError(Exception):
    pass


def _cache_key(
    text: str, model: str, base_url: str = "",
    preprocess_version: str = "embedding-text-v1", dimensions: int | None = None,
) -> str:
    namespace = {
        "endpoint": base_url.rstrip("/"), "model": model, "text": text,
        "preprocess_version": preprocess_version,
        "dimensions": dimensions if dimensions is not None else "auto-validated-v1",
    }
    return "embed:v2:" + hashlib.sha256(
        json.dumps(namespace, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _valid_vector(vector: Any, dimensions: int | None = None) -> list[float]:
    if not isinstance(vector, list) or not vector:
        raise EmbeddingError("embedding 向量必须为非空数组")
    if dimensions is not None and len(vector) != dimensions:
        raise EmbeddingError(f"embedding 向量维度 {len(vector)} != {dimensions}")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
        raise EmbeddingError("embedding 向量包含非法或非有限数值")
    try:
        values = [float(value) for value in vector]
    except (OverflowError, ValueError) as exc:
        raise EmbeddingError("embedding 向量包含非法或非有限数值") from exc
    if not all(math.isfinite(value) for value in values):
        raise EmbeddingError("embedding 向量包含非法或非有限数值")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("embedding 向量范数无效")
    return values


class MockEmbeddingClient:
    """确定性哈希向量：同文本 → 同向量（余弦 1.0），不同文本 ≈ 0。维度 64。"""

    is_mock = True

    def __init__(self, model: str = "mock-embedding", dim: int = 64):
        self.model = model
        self.dim = dim
        self.call_count = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        raw = hashlib.sha256((self.model + "\x00" + text).encode("utf-8")).digest()
        values: list[float] = []
        seed = int.from_bytes(raw[:8], "big")
        state = seed or 1
        for _ in range(self.dim):
            state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
            values.append(((state >> 33) % 20000 - 10000) / 10000.0)
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]

    def probe(self, model: str) -> int:
        return self.dim


class EmbeddingClient:
    is_mock = False

    def __init__(
        self,
        config: AgentConfig,
        model: str,
        budget: BudgetTracker | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        scheduler: RequestScheduler | None = None,
        dimensions: int | None = None,
        preprocess_version: str = "embedding-text-v1",
        recorder: Recorder | None = None,
    ):
        self.config = config
        self.model = model
        self.budget = budget
        self.cache = cache
        self._transport = transport
        self.api_key = api_key or config.llm.api_key
        self._base_url = base_url or config.llm.base_url
        self.endpoint = self._base_url.rstrip("/") + "/embeddings"
        if dimensions is not None and (isinstance(dimensions, bool) or not isinstance(dimensions, int)
                                       or dimensions < 1):
            raise ValueError("embedding dimensions must be positive")
        self.dimensions = dimensions
        self.preprocess_version = preprocess_version
        self.recorder = recorder
        self._response_dimensions = dimensions
        self._dimension_lock = threading.Lock()
        self.scheduler = scheduler or RequestScheduler(
            max_concurrency=2,
            min_interval_seconds=float(config.embedding.get(
                "min_request_interval_seconds", config.llm.min_request_interval_seconds,
            )),
        )
        # 阿里云官方 embedding 接口单请求上限 10 条；配大了自动钳制
        self.batch_size = max(1, min(int(config.embedding.get("batch_size", 10)), 10))
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=config.llm.connect_timeout_seconds,
                read=config.llm.read_timeout_seconds,
                write=config.llm.connect_timeout_seconds,
                pool=config.llm.connect_timeout_seconds,
            ),
            transport=transport,
        )

    # ------------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        """逐文本缓存命中，未命中的按批请求接口。"""
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for index, text in enumerate(texts):
            cached = self.cache.get(self._key(text)) if self.cache else None
            if cached is not None:
                try:
                    entry = json.loads(cached)
                    vector = _valid_vector(entry["vector"], entry["dimensions"])
                    self._validate_dimensions([vector])
                    results[index] = vector
                    self._record(0, cached=True)
                except (EmbeddingError, ValueError, TypeError, KeyError):
                    # Old/corrupt cached vectors never influence candidate pruning.
                    pass
            if results[index] is None:
                missing.append(index)

        for start in range(0, len(missing), self.batch_size):
            chunk = missing[start : start + self.batch_size]
            vectors = self._request([texts[i] for i in chunk])
            for local_index, global_index in enumerate(chunk):
                results[global_index] = vectors[local_index]
                if self.cache is not None:
                    self.cache.set(
                        self._key(texts[global_index]),
                        json.dumps({"dimensions": len(vectors[local_index]),
                                    "vector": vectors[local_index]}, allow_nan=False),
                        self.model,
                    )
        return results  # type: ignore[return-value]

    def _key(self, text: str) -> str:
        return _cache_key(text, self.model, self.endpoint, self.preprocess_version, self.dimensions)

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        with self._dimension_lock:
            expected = self._response_dimensions or len(vectors[0])
            for vector in vectors:
                _valid_vector(vector, expected)
            self._response_dimensions = expected

    def _request(self, texts: list[str]) -> list[list[float]]:
        attempts = 0
        last_error: Exception | None = None
        deadline = time.monotonic() + self.config.llm.request_deadline_seconds
        while attempts < max(1, self.config.llm.max_attempts):
            attempts += 1
            with self.scheduler.slot(deadline):
                if self.budget is not None:
                    self.budget.check_and_reserve()
                started = time.monotonic()
                usage: LLMUsage | None = None
                error: str | None = None
                try:
                    payload: dict[str, Any] = {"model": self.model, "input": texts}
                    if self.dimensions is not None:
                        payload["dimensions"] = self.dimensions
                    response = self._client.post(
                        self.endpoint, json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                    )
                    status = response.status_code
                    if status in (401, 403):
                        raise AuthError(f"embedding HTTP {status}：密钥无效")
                    if status in (400, 404):
                        raise EmbeddingError(
                            f"embedding 模型或参数不支持: {self.model} ({response.text[:150]})"
                        )
                    if status == 429 or status >= 500:
                        last_error = TransientError(f"embedding HTTP {status}")
                        error = str(last_error)
                        fallback = (RATE_LIMIT_BACKOFF[min(attempts - 1, len(RATE_LIMIT_BACKOFF) - 1)]
                                    if status == 429 else min(2.0 * attempts, 10.0))
                        self.scheduler.defer(retry_after_seconds(response.headers.get("Retry-After"), fallback))
                        continue
                    if status != 200:
                        raise EmbeddingError(f"embedding HTTP {status}: {response.text[:150]}")
                    data = response.json()
                    raw = data.get("usage") or {}
                    usage = LLMUsage(
                        prompt_tokens=raw.get("prompt_tokens"),
                        completion_tokens=raw.get("completion_tokens"),
                        total_tokens=raw.get("total_tokens"),
                    )
                    if self.budget is not None:
                        self.budget.record_tokens(usage.total_tokens)
                    vectors = self._parse(data, len(texts))
                    self._validate_dimensions(vectors)
                    return vectors
                except httpx.HTTPError as exc:
                    error = f"embedding 网络错误: {exc}"
                    last_error = TransientError(error)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    self._record(int((time.monotonic() - started) * 1000), usage=usage, error=error)
        raise last_error or EmbeddingError("embedding 重试次数耗尽")

    @staticmethod
    def _parse(data: dict[str, Any], expected: int) -> list[list[float]]:
        entries = data.get("data") or []
        if len(entries) != expected:
            raise EmbeddingError(f"embedding 返回条数 {len(entries)} != 请求 {expected}")
        vectors: list[list[float] | None] = [None] * expected
        for entry in entries:
            if not isinstance(entry, dict):
                raise EmbeddingError("embedding 返回结构异常")
            index = entry.get("index")
            vector = entry.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected:
                raise EmbeddingError("embedding 返回结构异常")
            if vectors[index] is not None:
                raise EmbeddingError("embedding 返回重复索引")
            vectors[index] = _valid_vector(vector)
        if any(vector is None for vector in vectors):
            raise EmbeddingError("embedding 返回缺少索引")
        if len({len(vector) for vector in vectors if vector is not None}) > 1:
            raise EmbeddingError("embedding 返回向量维度不一致")
        return vectors  # type: ignore[return-value]

    def _record(
        self, duration_ms: int, cached: bool = False,
        usage: LLMUsage | None = None, error: str | None = None,
    ) -> None:
        if self.recorder is not None:
            self.recorder(UsageEvent(
                task="embedding", model=self.model, duration_ms=duration_ms,
                cached=cached, is_mock=False, usage=usage, error=error,
            ))

    def probe(self, model: str) -> int:
        """探测指定模型是否可用，成功返回维度（继承本客户端的端点/密钥）。"""
        probe_client = EmbeddingClient(
            config=self.config,
            model=model,
            budget=self.budget,
            cache=self.cache,
            transport=self._transport,
            base_url=self._base_url,
            api_key=self.api_key,
            scheduler=self.scheduler,
            dimensions=self.dimensions,
            preprocess_version=self.preprocess_version,
            recorder=self.recorder,
        )
        try:
            vectors = probe_client._request(["能力探测"])
            if model == self.model:
                with self._dimension_lock:
                    self._response_dimensions = len(vectors[0])
            return len(vectors[0])
        finally:
            probe_client.close()

    def close(self) -> None:
        self._client.close()


def probe_embedding_models(config: AgentConfig, budget: BudgetTracker | None = None) -> tuple[str | None, int | None, list[str]]:
    """按候选顺序探测可用 embedding 模型，返回 (模型名, 维度, 备注)。

    探测走 embedding 专属端点/密钥（EMBEDDING_BASE_URL / EMBEDDING_API_KEY），
    未配置时回落到对话端点。
    """
    client = EmbeddingClient(
        config=config,
        model=EMBEDDING_MODEL_CANDIDATES[0],
        budget=budget,
        cache=None,
        base_url=config.env.get("EMBEDDING_BASE_URL"),
        api_key=config.env.get("EMBEDDING_API_KEY") or config.llm.api_key,
    )
    notes: list[str] = []
    try:
        for model in EMBEDDING_MODEL_CANDIDATES:
            try:
                dim = client.probe(model)
                return model, dim, notes
            except Exception as exc:  # noqa: BLE001 - 逐个候选报告
                notes.append(f"{model}: {exc}")
    finally:
        client.close()
    return None, None, notes


def create_embedding_client(
    config: AgentConfig,
    budget: BudgetTracker | None,
    cache: ResponseCache | None,
    scheduler: RequestScheduler | None = None,
    recorder: Recorder | None = None,
) -> EmbeddingClient | MockEmbeddingClient | None:
    """按配置构建 embedding 客户端；mock 模式或无密钥时优雅降级。

    embedding 可使用独立端点/密钥（EMBEDDING_BASE_URL / EMBEDDING_API_KEY），
    便于"对话走中转、embedding 走官方"的组合。
    """
    settings = config.embedding
    if not settings.get("enabled", True):
        return None
    if config.llm.mode == "mock":
        return MockEmbeddingClient(model=settings.get("model", "mock-embedding"))
    embedding_key = config.env.get("EMBEDDING_API_KEY") or config.llm.api_key
    if not embedding_key:
        return None
    return EmbeddingClient(
        config=config,
        model=str(settings.get("model", EMBEDDING_MODEL_CANDIDATES[0])),
        budget=budget,
        cache=cache,
        base_url=config.env.get("EMBEDDING_BASE_URL"),
        api_key=embedding_key,
        scheduler=(scheduler if (config.env.get("EMBEDDING_BASE_URL") or config.llm.base_url).rstrip("/")
                   == config.llm.base_url.rstrip("/") else None),
        dimensions=settings.get("dimensions"),
        preprocess_version=str(settings.get("preprocess_version", "embedding-text-v1")),
        recorder=recorder,
    )
