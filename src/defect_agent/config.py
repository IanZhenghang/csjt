"""配置加载：YAML 项目参数 + 环境变量 / .env 中的 LLM 设置。

密钥只从环境变量或 .env 读取，绝不写入代码、日志或运行产物。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
DEFAULT_DATA_ROOT = Path("D:/数据汇总/数据汇总")

_ENV_KEYS = [
    "LLM_MODE",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "DASHSCOPE_API_KEY",  # 未配置 LLM_API_KEY 时的备用密钥来源（阿里云百炼）
    "LLM_PROVIDER",
    "TEXT_MODEL",
    "VISION_MODEL",
    "LLM_REASONING_EFFORT",
    "LLM_ENABLE_THINKING",
    "LLM_STREAM",
    "LLM_CONNECT_TIMEOUT_SECONDS",
    "LLM_READ_TIMEOUT_SECONDS",
    "LLM_REQUEST_DEADLINE_SECONDS",
    "LLM_MAX_CONCURRENCY",
    "LLM_MAX_ATTEMPTS",
    "LLM_MIN_REQUEST_INTERVAL_SECONDS",
    "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
]


def load_dotenv(path: Path) -> dict[str, str]:
    """解析简单 KEY=VALUE 格式的 .env 文件；不存在则返回空。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(env_path: Path | None = None) -> dict[str, str]:
    """环境变量优先于 .env 文件；只返回本项目认识的键。"""
    dotenv = load_dotenv(env_path or PROJECT_ROOT / ".env")
    merged: dict[str, str] = {}
    for key in _ENV_KEYS:
        if key in os.environ:
            merged[key] = os.environ[key]
        elif key in dotenv:
            merged[key] = dotenv[key]
    return merged


@dataclass
class LLMSettings:
    mode: str = "mock"
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str | None = None
    provider: str = "zhipu"  # zhipu | openai_compatible；None 时按 base_url 推断
    text_model: str = "glm-5.3-flash"
    vision_model: str = "glm-5.3-flash"
    reasoning_effort: str = "low"  # 仅 zhipu 方言使用
    enable_thinking: bool = True  # openai_compatible 方言（如 Qwen enable_thinking）
    stream: bool | None = None  # None=按供应商默认（zhipu 非流式 / openai_compatible 流式）
    max_tokens: int = 8192
    connect_timeout_seconds: float = 15.0
    read_timeout_seconds: float = 180.0
    request_deadline_seconds: float = 240.0
    max_concurrency: int = 1
    max_attempts: int = 3
    min_request_interval_seconds: float = 0.0  # 供应商限流节流（如账户约 10 次/分钟则设 8）


@dataclass
class AgentConfig:
    yaml_path: Path
    data_root: Path
    raw: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    llm: LLMSettings = field(default_factory=LLMSettings)

    def __post_init__(self) -> None:
        # Fail before creating a run or making requests when the repair limit is invalid.
        self.max_format_repairs_per_result

    # ---- 嵌套参数访问（带默认值，缺键不崩溃） ----
    @property
    def window(self) -> dict[str, Any]:
        defaults = {"core_seconds": 6, "context_before_seconds": 1, "context_after_seconds": 1}
        defaults.update(self.raw.get("window", {}))
        return defaults

    @property
    def vision(self) -> dict[str, Any]:
        defaults = {
            "initial_sampling_hz": 1,
            "max_images_per_request": 8,
            "image_max_side": 1280,
            "jpeg_quality": 85,
        }
        defaults.update(self.raw.get("vision", {}))
        return defaults

    @property
    def text(self) -> dict[str, Any]:
        defaults = {"max_records_per_batch": 80, "max_characters_per_batch": 8000}
        defaults.update(self.raw.get("text", {}))
        return defaults

    @property
    def comments(self) -> dict[str, Any]:
        defaults = {"max_records_per_batch": 30, "max_characters_per_batch": 6000}
        defaults.update(self.raw.get("comments", {}))
        return defaults

    @property
    def refinement(self) -> dict[str, Any]:
        defaults = {
            "sampling_hz": 4,
            "max_seconds_per_event": 6,
            "max_images_total_per_event": 24,
            "max_images_per_request": 8,
            "max_rounds_per_event": 1,
        }
        defaults.update(self.raw.get("refinement", {}))
        return defaults

    @property
    def budget(self) -> dict[str, Any]:
        defaults = {
            "max_live_calls_per_run": 80,
            "max_total_tokens_per_run": 1000000,
            "max_format_repairs_per_result": 3,
        }
        defaults.update(self.raw.get("budget", {}))
        repairs = defaults["max_format_repairs_per_result"]
        if isinstance(repairs, bool) or not isinstance(repairs, int) or repairs < 0:
            raise ValueError("budget.max_format_repairs_per_result 必须是非负整数（0=禁用格式修复）")
        return defaults

    @property
    def max_format_repairs_per_result(self) -> int:
        return self.budget["max_format_repairs_per_result"]

    @property
    def stability(self) -> dict[str, Any]:
        defaults = {"repeats": 3}
        defaults.update(self.raw.get("stability", {}))
        return defaults

    @property
    def pipeline(self) -> dict[str, Any]:
        defaults = {
            "version": "rag-v1",
            "enable_text": True,
            "enable_vision": True,
            "enable_review": True,
            "enable_attribution": True,
        }
        defaults.update(self.raw.get("pipeline", {}))
        return defaults

    @property
    def rag(self) -> dict[str, Any]:
        defaults = {
            "semantic_top_k": 5,
            "lexical_top_k": 5,
            "align_max_workers": 2,
        }
        # Preserve legacy keys for checkpoint identity; max_context_characters
        # no longer limits alignment, review or attribution inputs.
        defaults.update(self.raw.get("rag", {}))
        return defaults

    @property
    def embedding(self) -> dict[str, Any]:
        defaults = {
            "enabled": True,
            "model": "text-embedding-v3",
            "batch_size": 10,  # 官方百炼 embedding 接口批量上限 10
            "same_source_threshold": 0.90,
            "prefilter_top_k": 5,
        }
        defaults.update(self.raw.get("embedding", {}))
        if self.env.get("EMBEDDING_MODEL"):
            defaults["model"] = self.env["EMBEDDING_MODEL"]
        return defaults


def load_config(
    config_path: Path | None = None,
    data_root: Path | None = None,
    env_path: Path | None = None,
) -> AgentConfig:
    path = config_path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    env = load_env(env_path)
    api_key = env.get("LLM_API_KEY") or env.get("DASHSCOPE_API_KEY") or None
    base_url = env.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    provider = env.get("LLM_PROVIDER") or (
        "zhipu" if "bigmodel.cn" in base_url else "openai_compatible"
    )
    stream_value = env.get("LLM_STREAM", "").strip().lower()
    stream: bool | None = None
    if stream_value in ("true", "1", "yes"):
        stream = True
    elif stream_value in ("false", "0", "no"):
        stream = False
    llm = LLMSettings(
        mode=env.get("LLM_MODE", "mock"),
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        text_model=env.get("TEXT_MODEL", "glm-5.3-flash"),
        vision_model=env.get("VISION_MODEL", "glm-5.3-flash"),
        reasoning_effort=env.get("LLM_REASONING_EFFORT", "low"),
        enable_thinking=env.get("LLM_ENABLE_THINKING", "true").strip().lower()
        not in ("false", "0", "no"),
        stream=stream,
        connect_timeout_seconds=float(env.get("LLM_CONNECT_TIMEOUT_SECONDS", 15)),
        read_timeout_seconds=float(env.get("LLM_READ_TIMEOUT_SECONDS", 180)),
        request_deadline_seconds=float(env.get("LLM_REQUEST_DEADLINE_SECONDS", 240)),
        max_concurrency=int(env.get("LLM_MAX_CONCURRENCY", 1)),
        max_attempts=int(env.get("LLM_MAX_ATTEMPTS", 3)),
        min_request_interval_seconds=float(env.get("LLM_MIN_REQUEST_INTERVAL_SECONDS", 0)),
    )

    root = data_root or Path(raw.get("data_root", str(DEFAULT_DATA_ROOT)))
    return AgentConfig(yaml_path=path, data_root=root, raw=raw, env=env, llm=llm)
