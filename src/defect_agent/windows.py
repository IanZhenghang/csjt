"""T04 分析窗口构建（IMPLEMENTATION_PLAN.md §7.2）。

6 秒核心窗口 + 前后各 1 秒上下文；从现有全画面帧中选最接近采样点的实际帧；
同一帧在请求内不重复；最后不足 6 秒的窗口仍处理。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from .schemas import NormalizedPackage


class Window(BaseModel):
    window_id: str
    video_id: str
    core_start: float
    core_end: float
    ctx_start: float
    ctx_end: float
    frame_ids: list[str] = Field(default_factory=list)  # 局部帧 ID，按时间排序
    frame_timestamps: list[float] = Field(default_factory=list)
    sample_points: list[float] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_windows(package: NormalizedPackage, config: dict) -> list[Window]:
    core = float(config.get("core_seconds", 6))
    before = float(config.get("context_before_seconds", 1))
    after = float(config.get("context_after_seconds", 1))
    hz = float(config.get("initial_sampling_hz", 1))
    max_images = int(config.get("max_images_per_request", 8))

    duration = package.duration_sec
    full_frames = sorted(
        (f for f in package.frames if f.view_kind == "full_frame"),
        key=lambda f: f.timestamp_sec,
    )
    if not full_frames:
        raise ValueError(f"{package.package_id} 没有全画面帧，无法构建视觉窗口")

    windows: list[Window] = []
    index = 0
    start = 0.0
    while start < duration - 1e-9:
        index += 1
        core_start = start
        core_end = min(start + core, duration)
        ctx_start = max(0.0, core_start - before)
        ctx_end = min(duration, core_end + after)

        step = 1.0 / hz
        points: list[float] = []
        point = ctx_start
        while point < ctx_end - 1e-9 and len(points) < max_images:
            points.append(round(point, 3))
            point += step

        chosen: list[tuple[float, str]] = []
        seen: set[str] = set()
        for target in points:
            nearest = min(
                full_frames,
                key=lambda f: abs(f.timestamp_sec - target),
            )
            if nearest.frame_id not in seen:
                seen.add(nearest.frame_id)
                chosen.append((nearest.timestamp_sec, nearest.frame_id))
        chosen.sort()

        notes: list[str] = []
        if core_end - core_start < core:
            notes.append(f"尾部窗口核心时长 {core_end - core_start:.3f}s < {core}s，仍纳入处理")
        if len(points) < max_images and (ctx_end - ctx_start) * hz > len(points):
            notes.append("采样点受 max_images_per_request 限制截断")

        windows.append(
            Window(
                window_id=f"{package.video_id}::window::W{index:03d}",
                video_id=package.video_id,
                core_start=core_start,
                core_end=core_end,
                ctx_start=ctx_start,
                ctx_end=ctx_end,
                frame_ids=[fid for _, fid in chosen],
                frame_timestamps=[ts for ts, _ in chosen],
                sample_points=points,
                notes=notes,
            )
        )
        start = core_end

    return windows


def interval_iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0
