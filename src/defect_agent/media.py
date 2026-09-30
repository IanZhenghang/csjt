"""T04/T07 媒体处理：缩放编码（Pillow）与局部补帧（FFmpeg）。

原图永不修改；发送 API 的缩放图写入运行输出目录 derived_media/。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .adapters.base import sha256_file


class MediaError(Exception):
    pass


@dataclass
class PreparedImage:
    evidence_id: str
    frame_id: str
    timestamp_sec: float
    original_path: Path
    scaled_path: Path
    image_bytes: bytes
    image_sha256: str


def prepare_image(
    package_root: Path,
    media_rel_path: str,
    out_dir: Path,
    evidence_id: str,
    frame_id: str,
    timestamp_sec: float,
    max_side: int = 1280,
    jpeg_quality: int = 85,
) -> PreparedImage:
    source = package_root / media_rel_path
    if not source.is_file():
        raise MediaError(f"帧图缺失: {source}")

    out_dir.mkdir(parents=True, exist_ok=True)
    scaled_path = out_dir / f"{_safe_name(evidence_id)}.jpg"

    with Image.open(source) as image:
        image = image.convert("RGB")
        width, height = image.size
        longest = max(width, height)
        if longest > max_side:
            scale = max_side / longest
            image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))))
        image.save(scaled_path, format="JPEG", quality=jpeg_quality)

    image_bytes = scaled_path.read_bytes()
    return PreparedImage(
        evidence_id=evidence_id,
        frame_id=frame_id,
        timestamp_sec=timestamp_sec,
        original_path=source,
        scaled_path=scaled_path,
        image_bytes=image_bytes,
        image_sha256=sha256_file(scaled_path),
    )


def extract_frames(
    clip_path: Path,
    clip_local_times: list[float],
    out_dir: Path,
    prefix: str,
) -> list[Path]:
    """从片段媒体按片段本地时间补抽帧（refinement 用）。

    片段时间到源时间的换算由调用方负责（source = clip + segment.start_sec）。
    """
    if not clip_path.is_file():
        raise MediaError(f"片段媒体缺失: {clip_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, local_t in enumerate(clip_local_times):
        target = out_dir / f"{_safe_name(prefix)}_{i:02d}_t{local_t:.2f}.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, local_t):.3f}",
            "-i",
            str(clip_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(target),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not target.is_file():
            raise MediaError(f"ffmpeg 补帧失败 @ {local_t:.3f}s: {result.stderr[:200]}")
        saved.append(target)
    return saved


def _safe_name(identifier: str) -> str:
    return identifier.replace("::", "__").replace("/", "_").replace("\\", "_")
