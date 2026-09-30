"""LLM 客户端包：mock 与智谱官方实现。"""

from .base import (
    AuthError,
    BudgetExhausted,
    CapabilityError,
    FormatError,
    ImagePayload,
    LLMClient,
    LLMRequest,
    LLMUsage,
    TruncatedError,
    TransientError,
    UsageEvent,
    extract_json_block,
)
from .bigmodel import BigModelLLMClient
from .budget import BudgetTracker
from .cache import ResponseCache, compute_cache_key
from .embeddings import (
    EMBEDDING_MODEL_CANDIDATES,
    EmbeddingClient,
    EmbeddingError,
    MockEmbeddingClient,
    create_embedding_client,
    probe_embedding_models,
)
from .mock import MockLLMClient

__all__ = [
    "AuthError",
    "BudgetExhausted",
    "CapabilityError",
    "FormatError",
    "ImagePayload",
    "LLMClient",
    "LLMRequest",
    "LLMUsage",
    "TruncatedError",
    "TransientError",
    "UsageEvent",
    "extract_json_block",
    "BigModelLLMClient",
    "BudgetTracker",
    "ResponseCache",
    "compute_cache_key",
    "MockLLMClient",
    "EMBEDDING_MODEL_CANDIDATES",
    "EmbeddingClient",
    "EmbeddingError",
    "MockEmbeddingClient",
    "create_embedding_client",
    "probe_embedding_models",
]
