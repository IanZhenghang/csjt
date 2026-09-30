"""LLM 客户端基础类型：请求/结果对象、错误体系、客户端接口。

错误分型对应 IMPLEMENTATION_PLAN.md §9.6：
- AuthError：401/403，停止联网运行；
- CapabilityError：模型不存在、参数不支持，记录后不自动换模型；
- TransientError：429、暂时性 5xx、网络超时，有限重试；
- FormatError：JSON 不合规，反馈错误并按配置重试格式修复，仍失败则拒收；
- TruncatedError：输出截断，标记后拆小输入重跑；
- BudgetExhausted：达到预算上限，保存状态并暂停。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class LLMError(Exception):
    pass


class AuthError(LLMError):
    pass


class CapabilityError(LLMError):
    pass


class TransientError(LLMError):
    pass


class FormatError(LLMError):
    pass


class TruncatedError(LLMError):
    def __init__(self, message: str, content: str):
        super().__init__(message)
        self.content = content


class BudgetExhausted(LLMError):
    pass


@dataclass
class ImagePayload:
    evidence_id: str
    frame_id: str
    timestamp_sec: float
    image_bytes: bytes
    image_sha256: str


@dataclass
class LLMRequest:
    task: str
    system_prompt: str
    user_prompt: str
    context: dict[str, Any] = field(default_factory=dict)  # 结构化上下文（mock 使用）
    images: list[ImagePayload] = field(default_factory=list)
    output_schema_name: str = ""
    preprocess_meta: dict[str, Any] = field(default_factory=dict)
    repetition_index: int = 0  # 稳定性实验的独立采样编号
    enable_thinking: bool | None = None  # 覆盖全局思考开关（None=按配置默认）


@dataclass
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class UsageEvent:
    task: str
    model: str
    duration_ms: int
    cached: bool = False
    is_mock: bool = False
    usage: LLMUsage | None = None
    error: str | None = None
    repair_used: bool = False


Recorder = Callable[[UsageEvent], None]


class LLMClient(ABC):
    is_mock: bool = True
    recorder: Recorder | None = None

    @abstractmethod
    def generate_json(self, request: LLMRequest, output_model: type[T]) -> T:
        """文本请求并校验为 output_model 实例。"""

    @abstractmethod
    def analyze_images(self, request: LLMRequest, output_model: type[T]) -> T:
        """图像请求并校验为 output_model 实例。"""


def extract_json_block(text: str) -> str:
    """从模型输出中提取第一个平衡的 JSON 对象文本。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    if start < 0:
        raise FormatError("输出中不含 JSON 对象")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    raise FormatError("JSON 对象未闭合")
