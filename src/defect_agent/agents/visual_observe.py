"""T07 视觉观察角色：窗口帧序列 → 画面观察；含局部补帧（§7.4、§7.7）。

首轮请求只含固定任务说明、帧 ID/时间戳与图像；不加入标题、评论或已有结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import ImagePayload, LLMClient, LLMRequest
from ..media import MediaError, PreparedImage, extract_frames, prepare_image
from ..schemas import NormalizedPackage, ObservationBatchResult, ObservationDraft
from ..windows import Window
from .prompts import load_prompt
from .validation import validate_observation_drafts


@dataclass
class FrameBatch:
    window: Window
    images: list[PreparedImage] = field(default_factory=list)


def build_frame_batches(
    package_root,
    store,
    windows: list[Window],
    config: dict,
    derived_dir,
) -> list[FrameBatch]:
    vision = config.get("vision", {})
    batches = []
    for window in windows:
        images: list[PreparedImage] = []
        for frame_id, timestamp in zip(window.frame_ids, window.frame_timestamps):
            evidence_id = store.kind_id("frame", frame_id)
            frame_record = store.records[evidence_id]
            if frame_record.view_kind != "full_frame":
                # 裁剪图（字幕条带等）不进入道路全帧输入
                continue
            images.append(
                prepare_image(
                    package_root=package_root,
                    media_rel_path=frame_record.source_file,
                    out_dir=derived_dir,
                    evidence_id=evidence_id,
                    frame_id=frame_id,
                    timestamp_sec=timestamp,
                    max_side=int(vision.get("image_max_side", 1280)),
                    jpeg_quality=int(vision.get("jpeg_quality", 85)),
                )
            )
        batches.append(FrameBatch(window=window, images=images))
    return batches


def render_visual_prompt(batch: FrameBatch) -> str:
    window = batch.window
    lines = [
        "以下内容为待分析的视频帧。请按指定结构输出观察结果。",
        "",
        f"视频总时长参考：窗口核心区间 [{window.core_start:.3f}, {window.core_end:.3f})，"
        f"上下文区间 [{window.ctx_start:.3f}, {window.ctx_end:.3f})。",
        "每张图前有其 frame_id 与 timestamp_sec（原视频秒数）。",
    ]
    if window.notes:
        lines.append("窗口说明：" + "；".join(window.notes))
    lines.append("")
    lines.append("请观察驾驶相关现象，区分道路画面观察、车辆界面观察与画面文字声称。只返回 JSON。")
    return "\n".join(lines)


@dataclass
class ObservationBatch:
    batch: FrameBatch
    drafts: list[ObservationDraft] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    is_mock: bool = True


def observe_frames(batch: FrameBatch, client: LLMClient, duration: float) -> ObservationBatch:
    """§6.7 observe_frames：单窗口帧 → 验证后的观察草稿。"""
    if not batch.images:
        return ObservationBatch(batch=batch)
    request = LLMRequest(
        task="visual_observe",
        system_prompt=load_prompt("visual_observe"),
        user_prompt=render_visual_prompt(batch),
        context={
            "window": {
                "window_id": batch.window.window_id,
                "core_start": batch.window.core_start,
                "core_end": batch.window.core_end,
                "ctx_start": batch.window.ctx_start,
                "ctx_end": batch.window.ctx_end,
            },
            "images": [
                {
                    "evidence_id": img.evidence_id,
                    "frame_id": img.frame_id,
                    "timestamp_sec": img.timestamp_sec,
                    "image_sha256": img.image_sha256,
                }
                for img in batch.images
            ],
        },
        images=[
            ImagePayload(
                evidence_id=img.evidence_id,
                frame_id=img.frame_id,
                timestamp_sec=img.timestamp_sec,
                image_bytes=img.image_bytes,
                image_sha256=img.image_sha256,
            )
            for img in batch.images
        ],
        output_schema_name="ObservationBatchResult:1",
        preprocess_meta={
            "window_id": batch.window.window_id,
            "frame_ids": batch.window.frame_ids,
        },
    )
    result: ObservationBatchResult = client.analyze_images(request, ObservationBatchResult)

    allowed_frames = {img.evidence_id for img in batch.images}
    validated = validate_observation_drafts(
        result.observations,
        allowed_frame_ids=allowed_frames,
        window_range=(batch.window.ctx_start, batch.window.ctx_end),
        duration=duration,
    )
    rejected = [(r.reason, r.detail) for r in validated.rejected]
    return ObservationBatch(
        batch=batch, drafts=validated.accepted, rejected=rejected, is_mock=client.is_mock
    )


# ---------------------------------------------------------------------------
# 局部补证：从片段媒体按实际媒体时间补帧（§7.7，每事件最多一轮）
# ---------------------------------------------------------------------------


def refine_event_frames(
    package: NormalizedPackage,
    event_start: float,
    event_end: float,
    config: dict,
    derived_dir,
    evidence_prefix: str = "RF",
) -> list[tuple[PreparedImage, str]]:
    """在事件中心附近最多 6 秒、按 refinement.sampling_hz 补帧。

    返回 (PreparedImage, local_evidence_id) 列表；local_evidence_id 形如 RF001，
    由调用方注册为 {video_id}::derived_frame::RF001 证据。
    """
    refinement = config.get("refinement", {})
    hz = float(refinement.get("sampling_hz", 4))
    max_seconds = float(refinement.get("max_seconds_per_event", 6))
    max_images = int(refinement.get("max_images_total_per_event", 24))

    center = (event_start + event_end) / 2.0
    half = min(max_seconds / 2.0, max(event_end - event_start, 0.5))
    start = max(0.0, center - half)
    end = min(package.duration_sec, center + half)

    step = 1.0 / hz
    times: list[float] = []
    t = start
    while t < end - 1e-6 and len(times) < max_images:
        times.append(round(t, 3))
        t += step

    prepared: list[tuple[PreparedImage, str]] = []
    index = 0
    for segment in package.segments:
        local_times = [t - segment.start_sec for t in times if segment.start_sec <= t < segment.end_sec]
        if not local_times:
            continue
        try:
            saved = extract_frames(
                clip_path=_resolve_segment_path(package, segment.media_path),
                clip_local_times=local_times,
                out_dir=derived_dir / f"refine_{evidence_prefix}",
                prefix=f"{evidence_prefix}_{segment.segment_id}",
            )
        except MediaError:
            continue
        for saved_path, local_t in zip(saved, local_times):
            index += 1
            local_id = f"{evidence_prefix}{index:03d}"
            with open(saved_path, "rb") as handle:
                image_bytes = handle.read()
            from ..adapters.base import sha256_bytes

            prepared.append(
                (
                    PreparedImage(
                        evidence_id=f"{package.video_id}::derived_frame::{local_id}",
                        frame_id=local_id,
                        timestamp_sec=round(segment.start_sec + local_t, 3),
                        original_path=saved_path,
                        scaled_path=saved_path,
                        image_bytes=image_bytes,
                        image_sha256=sha256_bytes(image_bytes),
                    ),
                    local_id,
                )
            )
    return prepared


def _resolve_segment_path(package: NormalizedPackage, media_rel_path: str):
    """片段媒体路径 = 数据包根目录 + 相对路径（只读，防路径逃逸）。"""
    from pathlib import Path

    from ..adapters.base import safe_join

    return safe_join(Path(package.package_root), media_rel_path)
