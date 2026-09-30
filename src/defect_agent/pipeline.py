"""T11 流水线：run / batch / resume 的完整编排（§7、§11）。

流程：normalize → 证据库/窗口 → 文本提取 → 视觉观察 → 对齐与事件 →
审查（有限补证）→ 归因 → 规则分级 → 报告。
每个 LLM 任务有独立状态与输出文件；成功任务在恢复时不重复调用。
"""

from __future__ import annotations

import json
import time
from queue import SimpleQueue, Empty
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import AdapterError, discover_packages, load_package
from .agents.align import (
    AlignmentResult,
    associations_to_relations,
    merge_observations,
    align_evidence,
)
from .agents.attribution import propose_attribution
from .agents.prompts import prompt_hash
from .agents.review import AUTO_EXECUTABLE_REQUESTS, review_event
from .agents.text_extract import TextBatch, build_text_batches, extract_claims, render_text_prompt
from .agents.visual_observe import (
    FrameBatch,
    build_frame_batches,
    observe_frames,
    refine_event_frames,
)
from .config import PROJECT_ROOT, AgentConfig, load_config
from .evidence import EvidenceRecord, EvidenceStore, build_evidence_store, global_id
from .llm import (
    AuthError,
    BigModelLLMClient,
    BudgetExhausted,
    BudgetTracker,
    FormatError,
    LLMClient,
    MockLLMClient,
    ResponseCache,
    TransientError,
    TruncatedError,
)
from .reporting import render_report, render_run_summary, write_review_queue
from .rules import business_checks, finalize_event
from .schemas import (
    AttributionHypothesis,
    Claim,
    Event,
    EvidenceRelation,
    ReviewItem,
    VisualObservation,
)
from .storage import RunState, atomic_write_json, atomic_write_text
from .validation import validate_package
from .windows import build_windows

EXPERIMENT_PRESETS = {
    "A": {"enable_text": True, "enable_vision": False, "enable_review": False, "enable_attribution": False},
    "B": {"enable_text": False, "enable_vision": True, "enable_review": False, "enable_attribution": False},
    "C": {"enable_text": True, "enable_vision": True, "enable_review": False, "enable_attribution": True},
    "D": {"enable_text": True, "enable_vision": True, "enable_review": True, "enable_attribution": True},
}


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    config: AgentConfig
    mode: str
    client: LLMClient
    budget: BudgetTracker
    cache: ResponseCache
    state: RunState
    is_mock: bool
    pipeline_flags: dict = field(default_factory=dict)
    stop_reason: str | None = None
    consecutive_rate_limits: int = 0
    embedding_client: object = None  # EmbeddingClient | MockEmbeddingClient | None
    usage_queue: SimpleQueue = field(default_factory=SimpleQueue)
    usage_sink: object = None

    def drain_usage(self) -> None:
        while True:
            try:
                event = self.usage_queue.get_nowait()
            except Empty:
                break
            if self.usage_sink is not None:
                self.usage_sink(event)

    def should_stop(self) -> bool:
        return self.stop_reason is not None

    def log_error(self, task_key: str | None, kind: str, message: str) -> None:
        self.state.record_error(task_key, kind, message)
        with (self.run_dir / "errors.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"ts": datetime.now(timezone.utc).isoformat(), "task": task_key,
                     "kind": kind, "message": message[:1000]},
                    ensure_ascii=False,
                )
                + "\n"
            )


def _safe_task_key_part(text: str) -> str:
    # Windows 文件名禁止 : | / \ 等字符
    for char in (":", "|", "/", "\\", "<", ">", '"', "?", "*"):
        text = text.replace(char, "_")
    return text


def _run_task(ctx: RunContext, video_id: str, stage: str, item: str, fn, output_path: Path,
              input_fingerprint: str = "", already_running: bool = False):
    try:
        return _execute_task(ctx, video_id, stage, item, fn, output_path, input_fingerprint, already_running)
    finally:
        ctx.drain_usage()


def _execute_task(ctx: RunContext, video_id: str, stage: str, item: str, fn, output_path: Path,
                  input_fingerprint: str = "", already_running: bool = False):
    """通用任务执行：成功跳过、失败记录、预算/鉴权/持续限流中止。"""
    task_key = f"{video_id}|{stage}|{_safe_task_key_part(item)}"
    existing = ctx.state.get_task(task_key)
    if existing is not None and existing["status"] == "succeeded" and output_path.is_file():
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        if not input_fingerprint or saved.get("__input_fingerprint") == input_fingerprint:
            print(f"  [{stage}] {item}：已完成（跳过）", flush=True)
            return saved
    if existing is not None and existing["status"] == "paused_budget":
        ctx.stop_reason = ctx.stop_reason or "budget"
        return None
    if ctx.should_stop() and not already_running:
        return None

    if not already_running:
        print(f"  [{stage}] {item} …", flush=True)
        ctx.state.set_task(task_key, ctx.run_id, video_id, stage, item, "running")
    try:
        payload = fn()
        if input_fingerprint:
            payload["__input_fingerprint"] = input_fingerprint
        atomic_write_json(output_path, payload)
        ctx.state.set_task(
            task_key, ctx.run_id, video_id, stage, item, "succeeded", output_path=str(output_path)
        )
        ctx.consecutive_rate_limits = 0
        return payload
    except BudgetExhausted as exc:
        ctx.state.set_task(task_key, ctx.run_id, video_id, stage, item, "paused_budget", error=str(exc))
        ctx.stop_reason = "budget"
        ctx.log_error(task_key, "budget_exhausted", str(exc))
        print(f"  [{stage}] {item}：预算暂停", flush=True)
        return None
    except AuthError as exc:
        ctx.state.set_task(task_key, ctx.run_id, video_id, stage, item, "failed", error=str(exc))
        ctx.stop_reason = "auth"
        ctx.log_error(task_key, "auth_error", str(exc))
        print(f"  [{stage}] {item}：鉴权失败，停止联网运行", flush=True)
        return None
    except TransientError as exc:
        rate_limited = bool(getattr(exc, "rate_limited", False))
        ctx.state.set_task(task_key, ctx.run_id, video_id, stage, item, "failed", error=str(exc))
        ctx.log_error(task_key, "rate_limited" if rate_limited else "transient", str(exc))
        if rate_limited:
            ctx.consecutive_rate_limits += 1
            if ctx.consecutive_rate_limits >= 3:
                ctx.state.set_task(
                    task_key, ctx.run_id, video_id, stage, item, "paused_budget",
                    error=f"持续限流：{exc}",
                )
                ctx.stop_reason = "rate_limited"
                print(
                    f"  [{stage}] {item}：连续 {ctx.consecutive_rate_limits} 个任务触发限流，"
                    "自动暂停（进度已保存，稍后可 resume 继续）",
                    flush=True,
                )
                return None
        print(f"  [{stage}] {item}：失败（{exc}）", flush=True)
        return None
    except TruncatedError as exc:
        ctx.state.set_task(
            task_key, ctx.run_id, video_id, stage, item, "failed",
            error=f"truncated: {exc}",
        )
        ctx.log_error(task_key, "truncated", str(exc))
        print(f"  [{stage}] {item}：输出截断", flush=True)
        return {"__truncated__": True}
    except Exception as exc:  # noqa: BLE001 - 单任务失败不阻断其他任务
        ctx.state.set_task(task_key, ctx.run_id, video_id, stage, item, "failed", error=str(exc))
        ctx.log_error(task_key, "task_failed", f"{type(exc).__name__}: {exc}")
        print(f"  [{stage}] {item}：失败（{type(exc).__name__}: {exc}）", flush=True)
        return None


# ---------------------------------------------------------------------------
# 单视频处理
# ---------------------------------------------------------------------------


def process_video(ctx: RunContext, package_root: Path) -> dict:
    ctx.active_retrieval = None
    try:
        return _process_video(ctx, package_root)
    finally:
        if ctx.active_retrieval is not None:
            ctx.active_retrieval.close()
            ctx.active_retrieval = None
        ctx.drain_usage()


def _process_video(ctx: RunContext, package_root: Path) -> dict:
    started_at = time.monotonic()
    flags = ctx.pipeline_flags
    rag_enabled = flags.get("version", "legacy") == "rag-v1"
    rag_runtime = None
    video_summary = {"status": "failed", "events": 0, "claims": 0, "observations": 0, "hypotheses": 0}

    # ---- normalize ----
    package = load_package(package_root)
    video_id = package.video_id
    video_summary["video_id"] = video_id
    video_summary["package_id"] = package.package_id
    video_dir = ctx.run_dir / "videos" / video_id
    tasks_dir = video_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    validation = validate_package(package, package_root)
    if validation.status == "errors":
        for issue in validation.issues:
            if issue.severity == "error":
                ctx.log_error(f"{video_id}|normalize", "validation_error", issue.detail)
        ctx.state.set_video_status(video_id, ctx.run_id, "failed")
        video_summary["note"] = "数据校验未通过"
        return video_summary

    atomic_write_json(video_dir / "normalized" / "normalized.json", package.model_dump(mode="json"))

    store = build_evidence_store(package)
    windows = build_windows(package, ctx.config.window)
    atomic_write_json(video_dir / "normalized" / "windows.json", [w.model_dump() for w in windows])
    with (video_dir / "normalized" / "evidence.jsonl").open("w", encoding="utf-8") as handle:
        for record in store.records.values():
            handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

    task_failures: list[str] = []
    relations: list[EvidenceRelation] = []

    # ---- 文本提取 ----
    claims: list[Claim] = []
    if flags.get("enable_text", True):
        batches = build_text_batches(package, store, windows, ctx.config.raw)
        claim_drafts = []
        claim_rejections: list[dict] = []
        for batch in batches:
            output_path = tasks_dir / f"text_{_safe_task_key_part(batch.label)}.json"

            def _run_text(batch=batch):
                return _extract_batch_with_truncation_split(ctx, batch, package.duration_sec)

            text_fingerprint = ""
            if rag_enabled:
                text_fingerprint = _stage_task_fingerprint(ctx, "text_extract", {
                    "source": package.input_hashes, "prompt": render_text_prompt(batch),
                    "duration": package.duration_sec,
                })
            payload = _run_task(ctx, video_id, "text", batch.label, _run_text, output_path, text_fingerprint)
            if payload is None:
                task_failures.append(f"text:{batch.label}")
                continue
            if payload.get("__truncated__"):
                task_failures.append(f"text:{batch.label}:truncated")
                continue
            claim_drafts.extend(payload.get("drafts", []))
            claim_rejections.extend(payload.get("rejected", []))

        # 去重（多窗口重复引用同一记录 → 单一陈述）并分配 ID
        seen: set[tuple] = set()
        claims = []
        for index, draft in enumerate(claim_drafts, start=1):
            key = (draft["claim_type"], draft["statement"][:120], tuple(sorted(draft["evidence_ids"])))
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                Claim(
                    claim_id=f"{ctx.run_id}::{video_id}::claim::C{index:03d}",
                    video_id=video_id,
                    statement=draft["statement"],
                    claim_type=draft["claim_type"],
                    evidence_ids=draft["evidence_ids"],
                    source_group_id=store.get(draft["evidence_ids"][0]).source_group_id
                    if store.get(draft["evidence_ids"][0])
                    else f"{video_id}::sg::unknown",
                    temporal_scope=draft["temporal_scope"],
                    start_sec=draft["start_sec"],
                    end_sec=draft["end_sec"],
                )
            )
        if claim_rejections:
            atomic_write_json(tasks_dir / "text_rejections.json", claim_rejections)

    with (video_dir / "claims.jsonl").open("w", encoding="utf-8") as handle:
        for claim in claims:
            handle.write(json.dumps(claim.model_dump(mode="json"), ensure_ascii=False) + "\n")
    video_summary["claims"] = len(claims)

    # ---- 同源复述检测（embedding，只标记不删除；不可用时跳过） ----
    try:
        from .embedding_features import same_source_pass

        same_source_relations, same_source_records = same_source_pass(
            claims,
            ctx.embedding_client if not ctx.should_stop() else None,
            threshold=float(ctx.config.embedding.get("same_source_threshold", 0.90)),
            video_id=video_id,
            provenance_verified=not rag_enabled,
        )
        relations.extend(same_source_relations)
        if ctx.embedding_client is not None:
            atomic_write_json(
                tasks_dir / "claims_samesource.json",
                {
                    "threshold": float(ctx.config.embedding.get("same_source_threshold", 0.90)),
                    "embedding_model": getattr(ctx.embedding_client, "model", ""),
                    "is_mock": bool(getattr(ctx.embedding_client, "is_mock", False)),
                    "pairs": same_source_records,
                },
            )
    except (BudgetExhausted, AuthError) as exc:
        ctx.stop_reason = "budget" if isinstance(exc, BudgetExhausted) else "auth"
        ctx.log_error(f"{video_id}|same_source", ctx.stop_reason, str(exc))
    except Exception as exc:  # noqa: BLE001 - embedding 失败不阻断主流程
        ctx.log_error(f"{video_id}|same_source", "embedding_failed", f"{type(exc).__name__}: {exc}")
        print(f"  [embedding] 同源检测跳过（{exc}）", flush=True)
    ctx.drain_usage()

    # ---- 视觉观察 ----
    observations: list[VisualObservation] = []
    if flags.get("enable_vision", True) and not ctx.should_stop():
        frame_batches = build_frame_batches(
            package_root, store, windows, ctx.config.raw, video_dir / "derived_media"
        )
        obs_index = 0
        for frame_batch in frame_batches:
            label = frame_batch.window.window_id.split("::")[-1]
            output_path = tasks_dir / f"visual_{_safe_task_key_part(label)}.json"

            def _run_visual(frame_batch=frame_batch):
                result = observe_frames(frame_batch, ctx.client, package.duration_sec)
                return {
                    "drafts": [d.model_dump() for d in result.drafts],
                    "rejected": result.rejected,
                    "is_mock": result.is_mock,
                }

            visual_fingerprint = ""
            if rag_enabled:
                visual_fingerprint = _stage_task_fingerprint(ctx, "visual_observe", {
                    "window": frame_batch.window.model_dump(), "duration": package.duration_sec,
                    "images": [image.image_sha256 for image in frame_batch.images],
                })
            payload = _run_task(ctx, video_id, "visual", label, _run_visual, output_path, visual_fingerprint)
            if payload is None or payload.get("__truncated__"):
                task_failures.append(f"visual:{label}" + (":truncated" if payload else ""))
                continue
            for draft in payload.get("drafts", []):
                obs_index += 1
                observations.append(
                    VisualObservation(
                        observation_id=f"{ctx.run_id}::{video_id}::obs::OBS{obs_index:03d}",
                        video_id=video_id,
                        window_id=frame_batch.window.window_id,
                        description=draft["description"],
                        phenomenon_type=draft["phenomenon_type"],
                        evidence_ids=draft["evidence_ids"],
                        start_sec=draft["start_sec"],
                        end_sec=draft["end_sec"],
                        visibility=draft["visibility"],
                        continuity=draft["continuity"],
                        system_state=draft["system_state"],
                        limitations=draft["limitations"],
                    )
                )

    with (video_dir / "observations.jsonl").open("w", encoding="utf-8") as handle:
        for observation in observations:
            handle.write(json.dumps(observation.model_dump(mode="json"), ensure_ascii=False) + "\n")
    video_summary["observations"] = len(observations)

    # ---- 对齐与事件 ----
    events: list[Event] = []
    if not ctx.should_stop():
        clusters = merge_observations(observations)
        event_id_map = {
            cluster.event_local_id: f"{ctx.run_id}::{video_id}::event::{cluster.event_local_id}"
            for cluster in clusters
        }
        align_output = tasks_dir / "align.json"
        align_cfg = ctx.config.raw.get("align", {})
        summary_chars = int(align_cfg.get("event_summary_chars", 0))
        claims_per_request = max(1, int(align_cfg.get("claims_per_request", 1)))
        use_prefilter = bool(align_cfg.get("use_embedding_prefilter", False)) and not rag_enabled
        retrieval: dict[str, set[str]] | None = None

        if use_prefilter and ctx.embedding_client is None:
            print(
                "  [embedding] 预筛已配置但 embedding 不可用，自动退回全量候选",
                flush=True,
            )
        if use_prefilter and ctx.embedding_client is not None:
            try:
                from .embedding_features import build_prefilter_retrieval

                retrieval, filtered_log = build_prefilter_retrieval(
                    claims, clusters, ctx.embedding_client,
                    top_k=int(ctx.config.embedding.get("prefilter_top_k", 5)),
                )
                atomic_write_json(
                    tasks_dir / "align_prefilter_log.json",
                    {
                        "top_k": int(ctx.config.embedding.get("prefilter_top_k", 5)),
                        "embedding_model": getattr(ctx.embedding_client, "model", ""),
                        "filtered": filtered_log,
                        "note": "仅视频级候选被预筛；时间级候选全部保留",
                    },
                )
                print(
                    f"  [embedding] 对齐预筛启用：{len(filtered_log)} 条视频级陈述的候选被缩减（日志已落盘）",
                    flush=True,
                )
            except (BudgetExhausted, AuthError) as exc:
                ctx.stop_reason = "budget" if isinstance(exc, BudgetExhausted) else "auth"
                ctx.log_error(f"{video_id}|prefilter", ctx.stop_reason, str(exc))
            except Exception as exc:  # noqa: BLE001 - 预筛失败退回全量候选
                retrieval = None
                ctx.log_error(f"{video_id}|prefilter", "embedding_failed", f"{type(exc).__name__}: {exc}")
                print(f"  [embedding] 预筛失败，退回全量候选（{exc}）", flush=True)

        def _run_align():
            result = align_evidence(
                claims, clusters, ctx.client,
                claims_per_request=claims_per_request, summary_chars=summary_chars,
                retrieval=retrieval,
            )
            return {
                "associations": [a.model_dump() for a in result.associations],
                "rejected": result.rejected,
                "clusters": [
                    {
                        "event_local_id": c.event_local_id,
                        "start_sec": c.start_sec,
                        "end_sec": c.end_sec,
                        "phenomenon_type": c.phenomenon_type,
                        "observation_ids": c.observation_ids,
                        "possibly_related": c.possibly_related,
                        "summary": c.summary,
                    }
                    for c in clusters
                ],
            }

        if rag_enabled:
            from .rag_runtime import build_video_retrieval, run_claim_alignment
            try:
                rag_runtime = build_video_retrieval(
                    ctx, package, store, claims, observations, clusters,
                    PROJECT_ROOT / "work" / "evidence_index.sqlite", tasks_dir,
                )
                ctx.active_retrieval = rag_runtime
                payload = run_claim_alignment(ctx, rag_runtime, claims, clusters, store, package)
                task_failures.extend(payload.get("failed", []))
                atomic_write_json(align_output, payload)
            except (BudgetExhausted, AuthError) as exc:
                ctx.stop_reason = "budget" if isinstance(exc, BudgetExhausted) else "auth"
                ctx.log_error(f"{video_id}|rag", ctx.stop_reason, str(exc))
                payload = None
        else:
            payload = _run_task(ctx, video_id, "align", "video", _run_align, align_output)
        associations = []
        if payload is None:
            task_failures.append("align")
        elif payload.get("__truncated__"):
            task_failures.append("align:truncated")
        else:
            from .schemas import AssociationDraft

            associations = [AssociationDraft.model_validate(a) for a in payload["associations"]]

        relations.extend(associations_to_relations(associations, event_id_map))

        observations_by_id = {o.observation_id: o for o in observations}
        claims_by_id = {c.claim_id: c for c in claims}
        for cluster in clusters:
            event_observations = [observations_by_id[i] for i in cluster.observation_ids if i in observations_by_id]
            supporting: list[str] = []
            contradicting: list[str] = []
            event_claim_ids: list[str] = []
            for association in associations:
                if association.event_id != cluster.event_local_id:
                    continue
                claim = claims_by_id.get(association.claim_id)
                if claim is None:
                    continue
                event_claim_ids.append(claim.claim_id)
                if association.relation == "supports":
                    claim.alignment_status = (
                        "explicit_time_match"
                        if claim.start_sec is not None
                        else "possible_match"
                    )
                    supporting.extend(claim.evidence_ids)
                elif association.relation == "contradicts":
                    claim.alignment_status = "unassigned"
                    contradicting.extend(claim.evidence_ids)
                elif association.relation == "possible_match":
                    claim.alignment_status = "possible_match"
                claim.candidate_event_ids.append(event_id_map[cluster.event_local_id])

            for observation in event_observations:
                supporting.extend(observation.evidence_ids)

            events.append(
                Event(
                    event_id=event_id_map[cluster.event_local_id],
                    video_id=video_id,
                    start_sec=cluster.start_sec,
                    end_sec=cluster.end_sec,
                    boundary_status="coarse",
                    phenomenon_type=cluster.phenomenon_type,
                    phenomenon_summary=cluster.summary,
                    observation_ids=list(cluster.observation_ids),
                    claim_ids=list(dict.fromkeys(event_claim_ids)),
                    supporting_evidence_ids=list(dict.fromkeys(supporting)),
                    contradicting_evidence_ids=list(dict.fromkeys(contradicting)),
                    processing_status="aligned",
                )
            )

    # ---- 审查（有限补证）----
    review_items: list[ReviewItem] = []
    incomplete_context_events: set[str] = set()

    def _event_pack(event, event_observations, event_claims, stage):
        if not rag_enabled:
            return None
        if rag_runtime is None or ctx.should_stop():
            incomplete_context_events.add(event.event_id)
            task_failures.append(f"{stage}:{event.event_id}:retrieval_unavailable")
            return None
        from .rag_runtime import make_event_pack
        try:
            pack = make_event_pack(ctx, rag_runtime, package, store, event,
                                   event_observations, event_claims, stage)
        except (BudgetExhausted, AuthError) as exc:
            ctx.stop_reason = "budget" if isinstance(exc, BudgetExhausted) else "auth"
            ctx.log_error(f"{video_id}|{stage}", ctx.stop_reason, str(exc))
            incomplete_context_events.add(event.event_id)
            task_failures.append(f"{stage}:{event.event_id}:{ctx.stop_reason}")
            return None
        if not pack.complete:
            incomplete_context_events.add(event.event_id)
            task_failures.append(f"{stage}:{event.event_id}:incomplete_context")
            event.uncertainties.append(pack.reason)
            review_items.append(ReviewItem(
                review_item_id=f"{ctx.run_id}::{video_id}::rev::R{len(review_items) + 1:03d}",
                event_id=event.event_id, reason=pack.reason,
                evidence_ids=pack.evidence_ids,
                suggested_action="核对完整证据；修复缺失引用或显式调整上下文上限后重跑", priority="high",
            ))
        return pack

    if flags.get("enable_review", True) and not ctx.should_stop():
        for event in events:
            output_path = tasks_dir / f"review_{_safe_task_key_part(event.event_id.split('::')[-1])}.json"
            event_observations = [o for o in observations if o.observation_id in event.observation_ids]
            event_claims = [c for c in claims if c.claim_id in event.claim_ids]
            evidence_pack = _event_pack(event, event_observations, event_claims, "review")
            if rag_enabled and evidence_pack is None:
                continue

            def _run_review(event=event, event_observations=event_observations, event_claims=event_claims,
                            evidence_pack=evidence_pack):
                result = review_event(event, event_observations, event_claims, ctx.client, evidence_pack=evidence_pack)
                return {
                    "issues": [i.model_dump() for i in result.draft.issues],
                    "evidence_requests": [r.model_dump() for r in result.draft.evidence_requests],
                    "notes": result.draft.notes,
                    "rejected": result.rejected,
                }

            payload = _run_task(ctx, video_id, "review", event.event_id.split("::")[-1], _run_review, output_path,
                                _pack_task_fingerprint(evidence_pack, "review", ctx) if evidence_pack else "")
            if payload is None or payload.get("__truncated__"):
                task_failures.append(f"review:{event.event_id}" + (":truncated" if payload else ""))
                continue
            for issue in payload.get("issues", []):
                event.uncertainties.append(issue["description"])
                review_items.append(
                    ReviewItem(
                        review_item_id=f"{ctx.run_id}::{video_id}::rev::R{len(review_items) + 1:03d}",
                        event_id=event.event_id,
                        reason=issue["description"],
                        evidence_ids=issue.get("evidence_ids", []),
                        suggested_action="人工复核该问题并核对原始证据",
                        priority="medium",
                    )
                )
            refinement_attempted = False
            for evidence_request in payload.get("evidence_requests", []):
                request_type = evidence_request["request_type"]
                if request_type in AUTO_EXECUTABLE_REQUESTS and not ctx.should_stop():
                    if refinement_attempted:
                        continue
                    refinement_attempted = True
                    refined = _apply_automatic_refinement(
                        ctx, package, store, event, video_id, tasks_dir,
                        original_resolution=(request_type == "request_original_resolution"),
                    )
                    if rag_enabled:
                        refinement_state = ctx.state.get_task(f"{video_id}|refine|{event.event_id.split('::')[-1]}")
                        if refinement_state is None or refinement_state["status"] != "succeeded":
                            task_failures.append(f"refine:{event.event_id}:incomplete")
                    if refined:
                        observations.extend(refined)
                        event.observation_ids.extend(o.observation_id for o in refined)
                        event.boundary_status = "refined"
                        if rag_runtime is not None:
                            rag_runtime.register_refinement(store, refined)
                else:
                    review_items.append(
                        ReviewItem(
                            review_item_id=f"{ctx.run_id}::{video_id}::rev::R{len(review_items) + 1:03d}",
                            event_id=event.event_id,
                            reason=evidence_request["reason"],
                            evidence_ids=[],
                            suggested_action=f"人工处理：{request_type}",
                            priority="high",
                        )
                    )

    # ---- 归因 ----
    hypotheses: list[AttributionHypothesis] = []
    if flags.get("enable_attribution", True) and not ctx.should_stop():
        for event in events:
            output_path = tasks_dir / f"attribution_{_safe_task_key_part(event.event_id.split('::')[-1])}.json"
            event_observations = [o for o in observations if o.observation_id in event.observation_ids]
            event_claims = [c for c in claims if c.claim_id in event.claim_ids]
            evidence_pack = _event_pack(event, event_observations, event_claims, "attribution")
            if rag_enabled and evidence_pack is None:
                continue

            def _run_attribution(event=event, event_observations=event_observations, event_claims=event_claims,
                                 evidence_pack=evidence_pack):
                result = propose_attribution(event, event_observations, event_claims, ctx.client, evidence_pack=evidence_pack)
                return {
                    "hypotheses": [h.model_dump() for h in result.draft.hypotheses],
                    "reason": result.draft.reason,
                    "rejected": result.rejected,
                }

            payload = _run_task(ctx, video_id, "attribution", event.event_id.split("::")[-1], _run_attribution, output_path,
                                _pack_task_fingerprint(evidence_pack, "attribution", ctx) if evidence_pack else "")
            if payload is None or payload.get("__truncated__"):
                task_failures.append(f"attribution:{event.event_id}" + (":truncated" if payload else ""))
                continue
            for draft in payload.get("hypotheses", []):
                hypotheses.append(
                    AttributionHypothesis(
                        hypothesis_id=f"{ctx.run_id}::{video_id}::hyp::H{len(hypotheses) + 1:03d}",
                        event_id=event.event_id,
                        hypothesis=draft["hypothesis"],
                        supporting_evidence_ids=draft["supporting_evidence_ids"],
                        contradicting_evidence_ids=draft["contradicting_evidence_ids"],
                        alternative_explanations=draft["alternative_explanations"],
                        missing_information=draft["missing_information"],
                        counterfactual=draft["counterfactual"],
                    )
                )

    # ---- 规则分级与业务校验 ----
    observations_by_id = {o.observation_id: o for o in observations}
    claims_by_id = {c.claim_id: c for c in claims}
    for event in events:
        event_observations = [observations_by_id[i] for i in event.observation_ids if i in observations_by_id]
        event_claims = [claims_by_id[i] for i in event.claim_ids if i in claims_by_id]
        event_hypotheses = [h for h in hypotheses if h.event_id == event.event_id]
        finalize_event(event, event_observations, event_claims, event_hypotheses, store)
        if event.event_id in incomplete_context_events:
            event.defect_assessment = "insufficient_evidence"
            event.processing_status = "incomplete_context"
        violations = business_checks(event, event_observations, event_claims)
        if violations:
            event.defect_assessment = "insufficient_evidence"
            event.uncertainties.append("业务校验发现问题：" + "；".join(violations))
            for violation in violations:
                ctx.log_error(f"{video_id}|finalize|{event.event_id}", "business_check", violation)

    for event in events:
        review_items.append(
            ReviewItem(
                review_item_id=f"{ctx.run_id}::{video_id}::rev::R{len(review_items) + 1:03d}",
                event_id=event.event_id,
                reason="首版流程所有事件均需人工复核后才能作为结论使用",
                evidence_ids=event.supporting_evidence_ids[:5],
                suggested_action="人工查看事件证据链与原始画面",
                priority="high" if event.defect_assessment == "suspected" else "low",
            )
        )

    # ---- 输出 ----
    # Include refined observations and frame records in the exported evidence universe.
    for path, records in ((video_dir / "observations.jsonl", observations),
                          (video_dir / "claims.jsonl", claims),
                          (video_dir / "normalized" / "evidence.jsonl", store.records.values())):
        atomic_write_text(path, "".join(json.dumps(r.model_dump(mode="json"), ensure_ascii=False) + "\n" for r in records))
    with (video_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    with (video_dir / "hypotheses.jsonl").open("w", encoding="utf-8") as handle:
        for hypothesis in hypotheses:
            handle.write(json.dumps(hypothesis.model_dump(mode="json"), ensure_ascii=False) + "\n")
    atomic_write_json(
        video_dir / "evidence_graph.json",
        {
            "schema_version": "1.0",
            "nodes": [
                {"id": record.evidence_id, "kind": "evidence", "source_kind": record.source_kind}
                for record in store.records.values()
            ]
            + [{"id": c.claim_id, "kind": "claim"} for c in claims]
            + [{"id": o.observation_id, "kind": "observation"} for o in observations]
            + [{"id": e.event_id, "kind": "event"} for e in events]
            + [{"id": h.hypothesis_id, "kind": "hypothesis"} for h in hypotheses],
            "edges": [relation.model_dump(mode="json") for relation in relations],
        },
    )
    write_review_queue(review_items, video_dir / "review_queue.jsonl")

    usage = ctx.state.usage_summary()
    stats = {
        "video_status": "pending",
        "任务失败项": ", ".join(task_failures) or "无",
        "调用数（本运行累计）": usage["calls"],
        "其中缓存命中": usage["cached_calls"],
        "tokens（本运行累计）": usage["total_tokens"],
        "预算": ctx.budget.snapshot(),
    }
    if ctx.is_mock:
        stats["模式"] = "mock（未调用真实 API）"

    status = "failed" if "normalize" in task_failures else (
        "partial" if task_failures or ctx.should_stop() else "complete"
    )
    if not flags.get("enable_vision", True) and status == "complete":
        stats["模式"] = stats.get("模式", "") + "；画面未验证（视觉阶段关闭）"
    ctx.state.set_video_status(video_id, ctx.run_id, status)
    stats["video_status"] = status
    video_summary["status"] = status
    video_summary["events"] = len(events)
    video_summary["hypotheses"] = len(hypotheses)
    video_summary["observations"] = len(observations)
    video_summary["note"] = "、".join(task_failures)[:200] if task_failures else ""

    render_report(
        video_id=video_id,
        run_id=ctx.run_id,
        is_mock=ctx.is_mock,
        model=ctx.client.model_name if hasattr(ctx.client, "model_name") else ctx.config.llm.text_model,
        package_id=package.package_id,
        duration_sec=package.duration_sec,
        coverage={
            "处理窗口": len(windows),
            "发送图像": sum(1 for _ in (video_dir / "derived_media").rglob("*.jpg")) if (video_dir / "derived_media").exists() else 0,
            "ASR 记录": len(package.asr),
            "OCR 记录": len(package.ocr),
            "OCR spans": len(package.ocr_spans),
            "评论": len(package.comments),
            "提取陈述": len(claims),
            "画面观察": len(observations),
        },
        events=events,
        observations=observations,
        claims=claims,
        hypotheses=hypotheses,
        relations=relations,
        review_items=review_items,
        stats=stats,
        output_path=video_dir / "report.md",
    )
    atomic_write_json(video_dir / "performance.json", {
        "wall_seconds": round(time.monotonic() - started_at, 3),
        "pipeline_version": flags.get("version", "legacy"),
        "align_workers": int(ctx.config.rag["align_max_workers"]) if rag_enabled else 1,
        "is_mock": ctx.is_mock, "status": status,
    })
    return video_summary


def _pack_task_fingerprint(pack, stage, ctx):
    return _stage_task_fingerprint(ctx, stage, {"pack": pack.fingerprint})


def _stage_task_fingerprint(ctx, stage, payload):
    from .rag_runtime import fingerprint, model_identity
    return fingerprint({"input": payload, "prompt": prompt_hash(stage),
                        "pipeline": "rag-v1", "model": model_identity(ctx)})


def _extract_batch_with_truncation_split(ctx: RunContext, batch: TextBatch, duration: float) -> dict:
    """文本批次截断时按记录边界拆半重跑（最多一次）。"""
    try:
        result = extract_claims(batch, ctx.client)
        return {
            "drafts": [d.model_dump() for d in result.drafts],
            "rejected": result.rejected,
            "is_mock": result.is_mock,
        }
    except TruncatedError:
        if len(batch.records) <= 1:
            raise
        middle = len(batch.records) // 2
        halves = [
            TextBatch(
                batch_kind=batch.batch_kind,
                label=f"{batch.label}:split{i}",
                records=part,
                context_lines=batch.context_lines,
                window_id=batch.window_id,
            )
            for i, part in enumerate((batch.records[:middle], batch.records[middle:]), start=1)
        ]
        drafts = []
        rejected = []
        for half in halves:
            result = extract_claims(half, ctx.client)
            drafts.extend(d.model_dump() for d in result.drafts)
            rejected.extend(result.rejected)
        return {"drafts": drafts, "rejected": rejected, "is_mock": ctx.client.is_mock, "split": True}


def _apply_automatic_refinement_legacy(
    ctx: RunContext,
    package,
    store: EvidenceStore,
    event: Event,
    video_id: str,
    tasks_dir: Path,
    original_resolution: bool,
) -> list[VisualObservation]:
    """自动补证：补帧或原图分辨率重观察；每事件最多一轮。"""
    output_path = tasks_dir / f"refine_{_safe_task_key_part(event.event_id.split('::')[-1])}.json"
    refined_marker = f"{video_id}|refine|{event.event_id.split('::')[-1]}"
    existing = ctx.state.get_task(refined_marker)
    if existing is not None and existing["status"] == "succeeded":
        return []

    video_dir = tasks_dir.parent

    def _run_refine():
        prepared = refine_event_frames(
            package,
            event.start_sec,
            event.end_sec,
            ctx.config.raw,
            video_dir / "derived_media",
        )
        if not prepared:
            return {"observations": [], "note": "补帧失败或无可用片段"}
        from .windows import Window

        images = []
        for image, local_id in prepared:
            store.add(
                EvidenceRecord(
                    evidence_id=f"{video_id}::derived_frame::{local_id}",
                    video_id=video_id,
                    source_kind="derived_frame",
                    source_file=str(image.scaled_path),
                    source_record_id=local_id,
                    text=None,
                    time_scope="point",
                    timestamp_sec=image.timestamp_sec,
                    media_path=str(image.scaled_path),
                    view_kind="full_frame",
                    source_group_id=f"{video_id}::sg::derived_frames",
                    human_reviewed=False,
                    quality_flags=["derived_from_source_video"],
                    raw_refs=[],
                )
            )
            images.append(image)
        frame_batch = FrameBatch(
            window=Window(
                window_id=f"refine::{event.event_id}",
                video_id=video_id,
                core_start=event.start_sec,
                core_end=event.end_sec,
                ctx_start=event.start_sec,
                ctx_end=event.end_sec,
                frame_ids=[i.frame_id for i in images],
                frame_timestamps=[i.timestamp_sec for i in images],
                sample_points=[i.timestamp_sec for i in images],
                notes=["补证轮：局部补帧，每事件最多一轮"],
            ),
            images=images,
        )
        result = observe_frames(frame_batch, ctx.client, package.duration_sec)
        return {
            "observations": [d.model_dump() for d in result.drafts],
            "rejected": result.rejected,
            "frames": [p[1] for p in prepared],
        }

    payload = _run_task(ctx, video_id, "refine", event.event_id.split("::")[-1], _run_refine, output_path)
    if not payload:
        return []
    new_observations = []
    for draft in payload.get("observations", []):
        observation = VisualObservation(
            observation_id=f"{ctx.run_id}::{video_id}::obs::REF{len(new_observations) + 1:03d}",
            video_id=video_id,
            window_id=f"refine::{event.event_id}",
            description=draft["description"],
            phenomenon_type=draft["phenomenon_type"],
            evidence_ids=draft["evidence_ids"],
            start_sec=draft["start_sec"],
            end_sec=draft["end_sec"],
            visibility=draft["visibility"],
            continuity=draft["continuity"],
            system_state=draft["system_state"],
            limitations=draft["limitations"] + ["来自补证轮观察"],
        )
        new_observations.append(observation)
        event.supporting_evidence_ids.extend(observation.evidence_ids)
    event.supporting_evidence_ids = list(dict.fromkeys(event.supporting_evidence_ids))
    return new_observations


def _apply_automatic_refinement(ctx, package, store, event, video_id, tasks_dir, original_resolution):
    if ctx.pipeline_flags.get("version", "legacy") != "rag-v1":
        return _apply_automatic_refinement_legacy(ctx, package, store, event, video_id, tasks_dir, original_resolution)
    from .rag_runtime import fingerprint
    from .windows import Window
    from .adapters.base import safe_join, sha256_file

    event_local = _safe_task_key_part(event.event_id.split("::")[-1])
    output_path = tasks_dir / f"refine_{event_local}.json"
    center = (event.start_sec + event.end_sec) / 2
    margin = float(ctx.config.refinement["max_seconds_per_event"]) / 2
    clip_hashes = {}
    for segment in package.segments:
        if segment.start_sec < center + margin and segment.end_sec > center - margin:
            media = safe_join(Path(package.package_root), segment.media_path)
            clip_hashes[segment.segment_id] = sha256_file(media) if media.is_file() else "missing"
    input_hash = _stage_task_fingerprint(ctx, "visual_observe", {"event": event.event_id, "start": event.start_sec, "end": event.end_sec,
                              "source": package.input_hashes, "config": ctx.config.refinement,
                              "media": clip_hashes, "version": "rag-v1"})

    def execute():
        prepared = refine_event_frames(package, event.start_sec, event.end_sec, ctx.config.raw,
                                        tasks_dir.parent / "derived_media", evidence_prefix=f"{event_local}_RF")
        if not prepared:
            raise ValueError("补证没有可用画面；需要人工复核原始媒体")
        records = [EvidenceRecord(
            evidence_id=image.evidence_id, video_id=video_id, source_kind="derived_frame",
            source_file=str(image.scaled_path), source_record_id=local_id,
            time_scope="point", timestamp_sec=image.timestamp_sec, media_path=str(image.scaled_path),
            view_kind="full_frame", source_group_id=f"{video_id}::sg::frames_source",
            human_reviewed=False, quality_flags=["derived_from_source_video"], raw_refs=[],
        ) for image, local_id in prepared]
        drafts = []
        batch_size = max(1, min(8, int(ctx.config.refinement["max_images_per_request"])))
        for start in range(0, len(prepared), batch_size):
            images = [image for image, _ in prepared[start:start + batch_size]]
            first_time = min(image.timestamp_sec for image in images)
            last_time = max(image.timestamp_sec for image in images)
            window = Window(
                window_id=f"refine::{event.event_id}::{start // batch_size}", video_id=video_id,
                core_start=first_time, core_end=min(package.duration_sec, last_time + 0.25),
                ctx_start=first_time, ctx_end=min(package.duration_sec, last_time + 0.25),
                frame_ids=[i.frame_id for i in images], frame_timestamps=[i.timestamp_sec for i in images],
                sample_points=[i.timestamp_sec for i in images], notes=["单轮补证的媒体传输分片"],
            )
            batch = FrameBatch(window=window, images=images)
            def observe(batch=batch):
                result = observe_frames(batch, ctx.client, package.duration_sec)
                return {"drafts": [d.model_dump(mode="json") for d in result.drafts], "rejected": result.rejected}
            part = f"{event_local}_{start // batch_size}"
            part_fingerprint = fingerprint({"input": input_hash, "frames": [i.image_sha256 for i in images]})
            result = _run_task(ctx, video_id, "refine_part", part, observe,
                               tasks_dir / f"refine_part_{part}.json", part_fingerprint)
            if result is None or result.get("__truncated__"):
                raise ValueError("补证画面分片未完整处理，保留已完成分片并等待恢复")
            drafts.extend(result["drafts"])
        return {"observations": drafts, "evidence_records": [r.model_dump(mode="json") for r in records],
                "version": "rag-v1"}

    payload = _run_task(ctx, video_id, "refine", event_local, execute, output_path, input_hash)
    if payload is None or payload.get("__truncated__"):
        event.uncertainties.append("局部补证未完成，需要恢复或人工核验")
        return []
    # Rehydration runs equally on new and resumed tasks, with no model request.
    for raw in payload.get("evidence_records", []):
        record = EvidenceRecord.model_validate(raw)
        if store.get(record.evidence_id) is None:
            store.add(record)
    refined = []
    for index, draft in enumerate(payload.get("observations", []), start=1):
        observation = VisualObservation(
            observation_id=f"{ctx.run_id}::{video_id}::obs::{event_local}_REF{index:03d}",
            video_id=video_id, window_id=f"refine::{event.event_id}",
            description=draft["description"], phenomenon_type=draft["phenomenon_type"],
            evidence_ids=draft["evidence_ids"], start_sec=draft["start_sec"], end_sec=draft["end_sec"],
            visibility=draft["visibility"], continuity=draft["continuity"], system_state=draft["system_state"],
            limitations=draft["limitations"] + ["来自补证轮观察"],
        )
        refined.append(observation)
        event.supporting_evidence_ids.extend(observation.evidence_ids)
    event.supporting_evidence_ids = list(dict.fromkeys(event.supporting_evidence_ids))
    return refined


# ---------------------------------------------------------------------------
# 运行创建 / run / batch / resume
# ---------------------------------------------------------------------------


def create_run(
    config: AgentConfig, mode: str, max_live_calls: int | None, run_dir_override: Path | None = None
) -> RunContext:
    # Config may have been restored or overridden since construction; validate before I/O.
    config.max_format_repairs_per_result
    # The explicit CLI mode is authoritative, including for embedding clients.
    config.llm.mode = mode
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ("-" + mode)
    run_dir = run_dir_override if run_dir_override is not None else PROJECT_ROOT / "runs" / run_id
    if run_dir_override is not None:
        run_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=True)
    is_mock = mode == "mock"

    cache = ResponseCache(PROJECT_ROOT / "work" / "cache.sqlite")
    state = RunState(run_dir / "state.sqlite")

    if is_mock:
        client = MockLLMClient(cache=cache)
        budget = BudgetTracker(max_live_calls=1, max_total_tokens=0)  # mock 不消耗真实预算
    else:
        if not config.llm.api_key:
            raise SystemExit("[失败] live 模式需要在 .env 或环境变量中配置 LLM_API_KEY")
        # 显式 0 = 不限调用次数；未传时用配置默认值
        if max_live_calls is not None:
            max_calls = int(max_live_calls)
        else:
            max_calls = int(config.budget.get("max_live_calls_per_run", 80))
        budget = BudgetTracker(
            max_live_calls=max_calls,
            max_total_tokens=int(config.budget.get("max_total_tokens_per_run", 1000000)),
        )
        client = BigModelLLMClient(config=config, budget=budget, cache=cache)

    ctx = RunContext(
        run_id=run_id,
        run_dir=run_dir,
        config=config,
        mode=mode,
        client=client,
        budget=budget,
        cache=cache,
        state=state,
        is_mock=is_mock,
        pipeline_flags=dict(config.pipeline),
    )

    def _recorder(event) -> None:
        state.record_usage(event)
        with (run_dir / "usage.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "task": event.task,
                "model": event.model,
                "duration_ms": event.duration_ms,
                "cached": event.cached,
                "is_mock": event.is_mock,
                "usage": {
                    "prompt_tokens": event.usage.prompt_tokens if event.usage else None,
                    "completion_tokens": event.usage.completion_tokens if event.usage else None,
                    "total_tokens": event.usage.total_tokens if event.usage else None,
                },
                "repair_used": event.repair_used,
                "error": event.error,
            }, ensure_ascii=False) + "\n")

    ctx.usage_sink = _recorder
    client.recorder = ctx.usage_queue.put
    (run_dir / "errors.jsonl").touch()

    from .llm import EMBEDDING_MODEL_CANDIDATES, EmbeddingClient, create_embedding_client
    ctx.embedding_client = create_embedding_client(
        config, budget=None if is_mock else budget, cache=cache,
        scheduler=getattr(client, "scheduler", None), recorder=ctx.usage_queue.put,
    )
    if isinstance(ctx.embedding_client, EmbeddingClient):
        resolved_model = None
        candidates = [ctx.embedding_client.model]
        if config.pipeline.get("version", "legacy") == "legacy":
            candidates.extend(EMBEDDING_MODEL_CANDIDATES)
        for candidate in dict.fromkeys(candidates):
            try:
                ctx.embedding_client.probe(candidate)
                resolved_model = candidate
                break
            except (BudgetExhausted, AuthError) as exc:
                ctx.stop_reason = "budget" if isinstance(exc, BudgetExhausted) else "auth"
                ctx.log_error(None, ctx.stop_reason, str(exc))
                break
            except Exception as exc:
                ctx.log_error(None, "embedding_probe_failed", f"{type(exc).__name__}: {exc}")
        if resolved_model:
            ctx.embedding_client.model = resolved_model
            print(f"[embedding] 可用（模型 {resolved_model}）", flush=True)
        else:
            ctx.embedding_client.close()
            ctx.embedding_client = None
            print("[embedding] 当前配置模型不可用，检索保守退回全候选（已记录降级）", flush=True)
    ctx.drain_usage()
    return ctx


def _write_run_manifest(ctx: RunContext, config: AgentConfig, planned: list[str], args_mode: str) -> None:
    atomic_write_json(
        ctx.run_dir / "run_manifest.json",
        {
            "schema_version": "1.0",
            "pipeline_version": ctx.pipeline_flags.get("version", "legacy"),
            "usage_accounting": "post-attempt-v1",
            "run_id": ctx.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": args_mode,
            "is_mock": ctx.is_mock,
            "defect_agent_version": __version__,
            "text_model": config.llm.text_model,
            "vision_model": config.llm.vision_model,
            "planned_packages": planned,
            "prompt_hashes": {task: prompt_hash(task) for task in
                              ("text_extract", "visual_observe", "align", "review", "attribution")},
            "pipeline_flags": ctx.pipeline_flags,
            "budget": ctx.budget.snapshot(),
        },
    )
    atomic_write_json(
        ctx.run_dir / "config_snapshot.json",
        {
            "yaml": config.raw,
            "llm": {
                "mode": config.llm.mode,
                "base_url": config.llm.base_url,
                "text_model": config.llm.text_model,
                "vision_model": config.llm.vision_model,
                "reasoning_effort": config.llm.reasoning_effort,
                "max_tokens": config.llm.max_tokens,
                "provider": config.llm.provider,
                "enable_thinking": config.llm.enable_thinking,
                "stream": config.llm.stream,
                "min_request_interval_seconds": config.llm.min_request_interval_seconds,
                "embedding_model": config.embedding["model"],
                "embedding_base_url": config.env.get("EMBEDDING_BASE_URL", config.llm.base_url),
                "api_key_configured": bool(config.llm.api_key),
            },
            "data_root": str(config.data_root),
        },
    )


def _apply_experiment(ctx: RunContext, experiment: str | None) -> None:
    if experiment:
        ctx.pipeline_flags.update(EXPERIMENT_PRESETS[experiment])


def _finish_run(ctx: RunContext, video_results: list[dict]) -> int:
    ctx.drain_usage()
    render_run_summary(ctx.run_id, video_results, ctx.run_dir / "summary.md")
    usage = ctx.state.usage_summary()
    atomic_write_json(ctx.run_dir / "usage_summary.json", usage)
    print(
        f"运行 {ctx.run_id} 结束："
        f"调用 {usage['calls']}（缓存 {usage['cached_calls']}），"
        f"tokens {usage['total_tokens']}，错误 {usage['errors']}"
    )
    if ctx.stop_reason == "budget":
        print("[暂停] 已达到预算上限，状态已保存，可用 resume --run-id 继续")
        return 3
    if ctx.stop_reason == "rate_limited":
        print("[暂停] 供应商限流持续（连续 3 个任务失败），进度已保存；")
        print("       建议稍等几分钟到几十分钟（配额窗口）后 resume --run-id 继续，")
        print("       或到供应商控制台确认该模型的配额/限流档位")
        return 5
    if ctx.stop_reason == "auth":
        print("[停止] 鉴权失败，请检查 LLM_API_KEY")
        return 4
    if usage.get("by_task"):
        print("各阶段调用耗时（平均/最大，秒）：")
        for task, stats in usage["by_task"].items():
            print(f"  {task:<14} {stats['calls']:>3} 次   平均 {stats['avg_seconds']:>6}s   最大 {stats['max_seconds']:>6}s")
    return 0 if all(result.get("status") in ("complete", "partial") for result in video_results) else 1


def _apply_format_repair_override(config: AgentConfig, args) -> None:
    max_repairs = getattr(args, "max_format_repairs", None)
    if max_repairs is not None:
        config.raw.setdefault("budget", {})["max_format_repairs_per_result"] = max_repairs
    config.max_format_repairs_per_result


def run_package(args) -> int:
    config = load_config(args.config)
    _apply_format_repair_override(config, args)
    output_dir = PROJECT_ROOT / "runs" / args.run_id if args.run_id else None
    if output_dir is not None and (output_dir / "run_manifest.json").exists():
        raise SystemExit("运行 ID 已存在；请使用 resume 或指定新 ID")
    ctx = create_run(config, args.mode, args.max_live_calls, run_dir_override=output_dir)
    _apply_experiment(ctx, args.experiment)
    package_root = Path(args.package)
    _write_run_manifest(ctx, config, [package_root.name], args.mode)
    print(f"运行 {ctx.run_id}：处理 {package_root}（模式 {args.mode}）")
    try:
        result = process_video(ctx, package_root)
    except AdapterError as exc:
        ctx.log_error(None, "adapter_error", str(exc))
        print(f"[失败] {exc}")
        return _finish_run(ctx, [])
    except KeyboardInterrupt:
        print("\n[中断] 已手动停止，进度已保存；稍后可用 resume --run-id 继续")
        return 130
    return _finish_run(ctx, [result])


def run_batch(args) -> int:
    config = load_config(args.config, args.data_root)
    _apply_format_repair_override(config, args)
    output_dir = PROJECT_ROOT / "runs" / args.run_id if args.run_id else None
    if output_dir is not None and (output_dir / "run_manifest.json").exists():
        raise SystemExit("运行 ID 已存在；请使用 resume 或指定新 ID")
    ctx = create_run(config, args.mode, args.max_live_calls, run_dir_override=output_dir)
    _apply_experiment(ctx, getattr(args, "experiment", None))
    try:
        packages = discover_packages(Path(args.data_root))
    except AdapterError as exc:
        print(f"[失败] {exc}")
        return 1
    if getattr(args, "only", None):
        wanted = {part.strip() for part in args.only.split(",")}
        packages = [p for p in packages if p.name in wanted]
    planned = [p.name for p in packages]
    _write_run_manifest(ctx, config, planned, args.mode)
    print(f"运行 {ctx.run_id}：批量处理 {len(planned)} 个包（模式 {args.mode}）")
    results = []
    for package_root in packages:
        if ctx.should_stop():
            break
        try:
            result = process_video(ctx, package_root)
        except AdapterError as exc:
            ctx.log_error(None, "adapter_error", str(exc))
            result = {"package_id": package_root.name, "status": "failed", "note": str(exc)[:200]}
        except KeyboardInterrupt:
            print("\n[中断] 已手动停止，进度已保存；稍后可用 resume --run-id 继续")
            return 130
        results.append(result)
        print(
            f"  {result.get('package_id', package_root.name)}: {result.get('status', 'failed')} "
            f"（事件 {result.get('events', 0)}，陈述 {result.get('claims', 0)}，"
            f"观察 {result.get('observations', 0)}）"
        )
    return _finish_run(ctx, results)


def run_resume(args) -> int:
    run_dir = PROJECT_ROOT / "runs" / args.run_id
    if not run_dir.is_dir():
        print(f"[失败] 运行目录不存在: {run_dir}")
        return 1
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    snapshot = json.loads((run_dir / "config_snapshot.json").read_text(encoding="utf-8"))
    config = load_config(args.config)
    mode = manifest.get("mode", "mock")
    # Restore the recorded pipeline, not today's defaults. Credentials remain
    # environment-owned; changing model/source requires a new run.
    config.raw = snapshot.get("yaml", {})
    _apply_format_repair_override(config, args)
    version = manifest.get("pipeline_version", "legacy")
    config.raw.setdefault("pipeline", {})["version"] = version
    for key in ("base_url", "text_model", "vision_model", "reasoning_effort", "max_tokens",
                "provider", "enable_thinking", "stream", "min_request_interval_seconds"):
        if key in snapshot.get("llm", {}):
            setattr(config.llm, key, snapshot["llm"][key])
    if "provider" not in snapshot.get("llm", {}) and "base_url" in snapshot.get("llm", {}):
        config.llm.provider = "zhipu" if "bigmodel.cn" in config.llm.base_url else "openai_compatible"
    if snapshot.get("llm", {}).get("embedding_model"):
        config.env["EMBEDDING_MODEL"] = snapshot["llm"]["embedding_model"]
    if snapshot.get("llm", {}).get("embedding_base_url"):
        config.env["EMBEDDING_BASE_URL"] = snapshot["llm"]["embedding_base_url"]

    max_calls_arg = args.max_live_calls
    if max_calls_arg is None:
        max_calls_arg = manifest.get("budget", {}).get("max_live_calls")
    ctx = create_run(
        config, mode, max_calls_arg,
        run_dir_override=run_dir,
    )
    ctx.pipeline_flags = dict(manifest.get("pipeline_flags", config.pipeline))
    ctx.pipeline_flags["version"] = version
    state = ctx.state
    reset = state.reset_running()
    paused = [row["task_key"] for row in state.paused_tasks()]
    for task_key in paused:
        state.set_task_paused_to_pending(task_key)
    if reset:
        print(f"恢复：{reset} 个中断 running 任务转为待检查")
    if paused:
        print(f"恢复：{len(paused)} 个预算暂停任务重新入队")

    planned = manifest.get("planned_packages", [])
    data_root = Path(snapshot.get("data_root", str(config.data_root)))
    results = []
    for package_name in planned:
        package_root = data_root / package_name
        if not package_root.is_dir():
            print(f"[跳过] 数据包不存在: {package_root}")
            continue
        try:
            result = process_video(ctx, package_root)
        except AdapterError as exc:
            ctx.log_error(None, "adapter_error", str(exc))
            result = {"package_id": package_name, "status": "failed", "note": str(exc)[:200]}
        except KeyboardInterrupt:
            print("\n[中断] 已手动停止，进度已保存；稍后可用 resume --run-id 继续")
            return 130
        results.append(result)
        print(
            f"  {result.get('package_id', package_name)}: {result.get('status', 'failed')} "
            f"（事件 {result.get('events', 0)}）"
        )
        if ctx.should_stop():
            break
    return _finish_run(ctx, results)
