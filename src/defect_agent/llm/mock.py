"""MockLLMClient：确定性测试客户端。

- 不发起任何网络请求；根据 request.context 生成可通过结构校验的输出；
- 所有结果标记 is_mock = true，不进入科研效果指标；
- 支持故障注入（bad_json / bad_json_always / fake_ids / truncated / auth），
  用于验证格式修复、引用拒收、截断标记与鉴权中止等工程路径。
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel

from .base import (
    AuthError,
    FormatError,
    LLMRequest,
    LLMUsage,
    LLMClient,
    TruncatedError,
    UsageEvent,
)
from .cache import ResponseCache, compute_cache_key


def _guess_claim_type(text: str) -> str:
    """确定性关键词启发，仅用于 mock 输出的多样性，不代表真实判断。"""
    system_words = ("智驾", "自动驾驶", "辅助驾驶", "noa", "pilot", "l2", "领航")
    cause_words = ("有问题", "故障", "失灵", "bug", "缺陷", "翻车", "出事")
    phenomenon_words = ("撞", "碰撞", "偏离", "急刹", "刹不住", "甩尾", "车道", "变道", " intervention")
    scene_words = ("路口", "道路", "标线", "弯道", "高速", "电瓶车", "行人")
    lowered = text.lower()
    if any(w in lowered for w in cause_words) and any(w in lowered for w in system_words):
        return "cause_claim"
    if any(w in lowered for w in system_words):
        if any(w in text for w in ("开", "亮", "启用", "开启")):
            return "system_state_claim"
        return "phenomenon_claim"
    if any(w in lowered for w in phenomenon_words):
        return "phenomenon_claim"
    if any(w in lowered for w in scene_words):
        return "scene_claim"
    return "other"


class MockLLMClient(LLMClient):
    is_mock = True

    def __init__(
        self,
        cache: ResponseCache | None = None,
        faults: dict[str, int] | None = None,
        model_name: str = "mock-model",
        count_calls: bool = True,
    ):
        self.cache = cache
        self.model_name = model_name
        self.faults = dict(faults or {})
        self.call_count = 0

    # ------------------------------------------------------------------
    def generate_json(self, request: LLMRequest, output_model: type[BaseModel]):
        return self._complete(request, output_model)

    def analyze_images(self, request: LLMRequest, output_model: type[BaseModel]):
        if not request.images:
            raise FormatError("视觉请求未携带图片")
        return self._complete(request, output_model)

    # ------------------------------------------------------------------
    def _fault_active(self, name: str, task: str) -> bool:
        """支持全局故障（name）与任务限定故障（"task:name"）。"""
        return self.faults.get(name, 0) > 0 or self.faults.get(f"{task}:{name}", 0) > 0

    def _consume_fault(self, name: str, task: str) -> None:
        key = f"{task}:{name}" if self.faults.get(f"{task}:{name}", 0) > 0 else name
        if self.faults.get(key, 0) > 0:
            self.faults[key] -= 1

    def _complete(self, request: LLMRequest, output_model: type[BaseModel]):
        self.call_count += 1
        started = time.monotonic()
        task = request.task

        if self._fault_active("auth", task):
            self._consume_fault("auth", task)
            self._record(request, 0, error="AuthError")
            raise AuthError("mock：401 鉴权失败")

        cache_key = None
        if self.cache is not None:
            cache_key = compute_cache_key(
                request_system_prompt=request.system_prompt,
                request_user_prompt=request.user_prompt,
                image_hashes=[img.image_sha256 for img in request.images],
                model=self.model_name,
                base_url="mock://local",
                params={"mock": True},
                output_schema_name=f"{output_model.__module__}.{output_model.__name__}:{request.output_schema_name}",
                preprocess_meta=request.preprocess_meta,
                repetition_index=request.repetition_index,
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._record(request, 0, cached=True)
                return self._validate(cached, output_model)

        if self._fault_active("truncated", task):
            self._consume_fault("truncated", task)
            self._record(request, 0, error="TruncatedError")
            raise TruncatedError("mock：输出截断", content="{}")

        if self._fault_active("bad_json", task) or self._fault_active("bad_json_always", task):
            if self._fault_active("bad_json", task):
                self._consume_fault("bad_json", task)
            content = "这不是 JSON：模型输出了一段自由文本。"
            self._record(request, int((time.monotonic() - started) * 1000))
            # 模拟“一次格式修复后成功”
            payload = self._fabricate(request)
        elif self._fault_active("fake_ids", task):
            self._consume_fault("fake_ids", task)
            payload = self._fabricate(request, fake_ids=True)
        else:
            payload = self._fabricate(request)

        content = json.dumps(payload, ensure_ascii=False)
        if self.cache is not None and cache_key is not None:
            self.cache.set(cache_key, content, self.model_name)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._record(request, duration_ms)
        if self._fault_active("bad_json_always", task):
            self._consume_fault("bad_json_always", task)
            raise FormatError("mock：持续输出非法 JSON")
        return self._validate(content, output_model)

    def _validate(self, content: str, output_model: type[BaseModel]):
        parsed = json.loads(content)
        return output_model.model_validate(parsed)

    def _record(
        self,
        request: LLMRequest,
        duration_ms: int,
        cached: bool = False,
        error: str | None = None,
    ) -> None:
        if self.recorder is not None:
            self.recorder(
                UsageEvent(
                    task=request.task,
                    model=self.model_name,
                    duration_ms=duration_ms,
                    cached=cached,
                    is_mock=True,
                    usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    error=error,
                )
            )

    # ------------------------------------------------------------------
    def _fabricate(self, request: LLMRequest, fake_ids: bool = False) -> dict[str, Any]:
        task = request.task
        context = request.context or {}

        if task == "text_extract":
            records = context.get("records", [])
            claims = []
            for record in records:
                text = str(record.get("text") or "")
                evidence_id = record.get("evidence_id", "")
                if fake_ids:
                    evidence_id = f"{evidence_id}::FAKE"
                temporal_scope = "video"
                start = record.get("start_sec")
                end = record.get("end_sec")
                if start is not None and end is not None:
                    temporal_scope = "interval"
                elif record.get("timestamp_sec") is not None:
                    temporal_scope = "point"
                    start = record.get("timestamp_sec")
                    end = record.get("timestamp_sec")
                claims.append(
                    {
                        "statement": text[:200] or "（空陈述）",
                        "claim_type": _guess_claim_type(text),
                        "evidence_ids": [evidence_id],
                        "temporal_scope": temporal_scope,
                        "start_sec": start,
                        "end_sec": end,
                    }
                )
            return {"claims": claims}

        if task == "visual_observe":
            images = context.get("images", [])
            if not images:
                return {"observations": []}
            first_ts = float(images[0]["timestamp_sec"])
            last_ts = float(images[-1]["timestamp_sec"])
            evidence_ids = (
                [f"{img['evidence_id']}::FAKE" for img in images]
                if fake_ids
                else [img["evidence_id"] for img in images]
            )
            return {
                "observations": [
                    {
                        "description": (
                            "mock 观察：窗口内出现道路场景与车辆行驶画面"
                            "（占位输出，用于流程验证，不代表真实视觉分析）"
                        ),
                        "phenomenon_type": "uncertain",
                        "evidence_ids": evidence_ids,
                        "start_sec": first_ts,
                        "end_sec": max(first_ts, last_ts),
                        "visibility": "partial",
                        "continuity": "unknown",
                        "system_state": "unknown",
                        "limitations": ["mock 输出，未经真实视觉分析", "稀疏采样可能遗漏瞬时动作"],
                    }
                ]
            }

        if task == "align":
            associations = []
            for candidate in context.get("candidates", []):
                relation = "possible_match" if candidate.get("related") else "unrelated"
                associations.append(
                    {
                        "claim_id": candidate["claim_id"],
                        "event_id": candidate["event_id"],
                        "relation": relation,
                        "proposition": candidate.get(
                            "proposition", "陈述与画面观察可能指向同一现象"
                        ),
                        "reason": candidate.get("reason", "mock 对齐：时间相邻或现象相关"),
                    }
                )
            return {"associations": associations}

        if task == "review":
            return {
                "issues": [],
                "evidence_requests": [],
                "notes": "mock 审查：未发现可自动复核的问题（占位输出）",
            }

        if task == "attribution":
            event = context.get("event", {})
            if not event.get("observation_ids"):
                return {
                    "hypotheses": [],
                    "reason": "当前证据不足以提出具有事件针对性的归因假设",
                }
            supporting = event.get("supporting_evidence_ids", [])[:3]
            if fake_ids:
                supporting = [f"{s}::FAKE" for s in supporting]
            return {
                "hypotheses": [
                    {
                        "hypothesis": (
                            "mock 假设：画面现象需要结合系统实际控制状态、驾驶员操作"
                            "与环境条件才能归因（占位输出，未经验证）"
                        ),
                        "supporting_evidence_ids": supporting,
                        "contradicting_evidence_ids": [],
                        "alternative_explanations": ["合理驾驶操作或驾驶员主动干预"],
                        "missing_information": ["系统控制状态", "驾驶员操作", "车辆日志"],
                        "counterfactual": {
                            "changed_condition": "假设系统状态或环境条件不同",
                            "held_constant_conditions": ["车辆初始位置", "其他交通参与者"],
                            "expected_difference": "现象可能不出现或表现不同",
                            "reasoning_summary": "mock 反事实（未经验证）",
                            "possible_confounders": ["视频剪辑", "视角限制"],
                            "required_validation": ["车辆日志", "受控场景复现"],
                        },
                    }
                ],
                "reason": "",
            }

        raise FormatError(f"mock 客户端未实现任务类型: {task}")
