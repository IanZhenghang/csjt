"""normalize 子命令实现：批量读取数据包并写规范化 JSON。"""

from __future__ import annotations

import json
from pathlib import Path

from .adapters import AdapterError, discover_packages, load_package
from .config import PROJECT_ROOT


def run_normalize(args) -> int:
    output_dir = args.output or (PROJECT_ROOT / "work" / "normalized")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        packages = discover_packages(args.data_root)
    except AdapterError as exc:
        print(f"[失败] {exc}")
        return 1

    summary: list[dict] = []
    failures = 0
    print(
        f"{'包':<5} {'video_id':<14} {'片段':>4} {'全帧':>4} {'裁剪帧':>5} "
        f"{'ASR':>4} {'OCR':>5} {'spans':>5} {'评论':>4}"
    )
    for package_root in packages:
        try:
            package = load_package(package_root)
        except AdapterError as exc:
            print(f"[失败] {package_root.name}: {exc}")
            summary.append({"package": package_root.name, "status": "failed", "error": str(exc)})
            failures += 1
            continue

        target = output_dir / package.package_id
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "package": package.model_dump(mode="json"),
        }
        (target / "normalized.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        full_frames = sum(1 for f in package.frames if f.view_kind == "full_frame")
        crop_frames = len(package.frames) - full_frames
        print(
            f"{package.package_id:<5} {package.video_id:<14} {len(package.segments):>4} "
            f"{full_frames:>4} {crop_frames:>5} {len(package.asr):>4} {len(package.ocr):>5} "
            f"{len(package.ocr_spans):>5} {len(package.comments):>4}"
        )
        summary.append(
            {
                "package": package.package_id,
                "video_id": package.video_id,
                "status": "ok",
                "adapter": package.adapter,
                "counts": {
                    "segments": len(package.segments),
                    "frames_full": full_frames,
                    "frames_crop": crop_frames,
                    "asr": len(package.asr),
                    "ocr": len(package.ocr),
                    "ocr_spans": len(package.ocr_spans),
                    "comments": len(package.comments),
                },
                "warnings": package.warnings,
            }
        )

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {output_dir / 'summary.json'}；失败 {failures} 个")
    return 1 if failures else 0
