"""T03 数据校验：引用、时间、路径与只读检查（IMPLEMENTATION_PLAN.md §7.1）。

校验只做确定性检查，不调用模型，不修改原始数据。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .adapters import AdapterError, discover_packages, expected_counts, load_package
from .config import PROJECT_ROOT


class ValidationIssue(BaseModel):
    severity: str  # error / warning / info
    code: str
    detail: str


class PackageValidation(BaseModel):
    package: str
    video_id: str
    adapter: str
    status: str  # ok / warnings / errors / failed
    issues: list[ValidationIssue] = Field(default_factory=list)
    counts_expected: dict[str, int] = Field(default_factory=dict)
    counts_actual: dict[str, int] = Field(default_factory=dict)


class InventoryEntry(BaseModel):
    package: str
    video_id: str
    adapter: str
    schema_version: str
    duration_sec: float
    counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: str
    data_root: str
    packages: list[PackageValidation] = Field(default_factory=list)
    inventory: list[InventoryEntry] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)


def _media_exists(package_root: Path, rel: str, issues: list[ValidationIssue], code: str) -> None:
    target = package_root / rel
    if not target.is_file():
        issues.append(ValidationIssue(severity="error", code=code, detail=f"媒体文件缺失: {rel}"))


def validate_package(package, package_root: Path) -> PackageValidation:
    issues: list[ValidationIssue] = []
    frame_ids = {f.frame_id for f in package.frames}
    asr_ids = {a.asr_id for a in package.asr}
    ocr_ids = {o.ocr_id for o in package.ocr}
    span_ids = {s.span_id for s in package.ocr_spans}
    comment_ids = {c.comment_id for c in package.comments}
    duration = package.duration_sec

    # ---- 计数与 manifest 一致 ----
    expected = expected_counts(package)
    actual = {
        "segments": len(package.segments),
        "frames": len(package.frames),
        "asr": len(package.asr),
        "ocr": len(package.ocr),
        "ocr_spans": len(package.ocr_spans),
        "comments": len(package.comments),
    }
    for key, exp in expected.items():
        if key in actual and exp >= 0 and exp != actual[key]:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="count_mismatch",
                    detail=f"{key}: 实际 {actual[key]} != manifest {exp}",
                )
            )

    # ---- ID 唯一性 ----
    all_local_ids = [f.frame_id for f in package.frames] + [a.asr_id for a in package.asr]
    all_local_ids += [o.ocr_id for o in package.ocr] + [s.span_id for s in package.ocr_spans]
    all_local_ids += [c.comment_id for c in package.comments]
    duplicates = {i for i in all_local_ids if all_local_ids.count(i) > 1}
    if duplicates:
        issues.append(
            ValidationIssue(
                severity="error", code="duplicate_id", detail=f"重复局部 ID: {sorted(duplicates)[:5]}"
            )
        )

    # ---- 片段引用解析 + 时间 + 媒体存在 ----
    shortest = None
    for seg in package.segments:
        missing = [i for i in seg.frame_ids if i not in frame_ids]
        missing += [i for i in seg.asr_ids if i not in asr_ids]
        missing += [i for i in seg.ocr_ids if i not in ocr_ids]
        if missing:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="dangling_segment_ref",
                    detail=f"{seg.segment_id} 悬空引用: {missing[:5]}",
                )
            )
        if not (0 <= seg.start_sec < seg.end_sec <= duration + 0.5):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="segment_time_invalid",
                    detail=f"{seg.segment_id} 区间 [{seg.start_sec}, {seg.end_sec}) 超出时长 {duration}",
                )
            )
        span_len = seg.end_sec - seg.start_sec
        if shortest is None or span_len < shortest[1]:
            shortest = (seg.segment_id, span_len)
        _media_exists(package_root, seg.media_path, issues, "segment_media_missing")

    if shortest and shortest[1] < 0.5:
        issues.append(
            ValidationIssue(
                severity="info",
                code="very_short_segment",
                detail=f"{shortest[0]} 时长 {shortest[1]:.3f}s（正常保留，不丢弃）",
            )
        )

    # ---- 帧：时间与媒体存在 ----
    for frame in package.frames:
        if not (0 <= frame.timestamp_sec <= duration + 0.5):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="frame_time_out_of_range",
                    detail=f"{frame.frame_id} timestamp {frame.timestamp_sec} 超出时长",
                )
            )
        _media_exists(package_root, frame.media_path, issues, "frame_media_missing")

    # ---- ASR 时间 ----
    for rec in package.asr:
        if not (0 <= rec.start_sec < rec.end_sec <= duration + 0.5):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="asr_time_invalid",
                    detail=f"{rec.asr_id} 区间 [{rec.start_sec}, {rec.end_sec}) 无效",
                )
            )

    # ---- OCR 引用与时间 ----
    for rec in package.ocr:
        if rec.frame_id and rec.frame_id not in frame_ids:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="ocr_frame_ref_dangling",
                    detail=f"{rec.ocr_id} 引用不存在的帧 {rec.frame_id}",
                )
            )
        if not (0 <= rec.timestamp_sec <= duration + 0.5):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="ocr_time_out_of_range",
                    detail=f"{rec.ocr_id} timestamp {rec.timestamp_sec} 超出时长",
                )
            )

    # ---- spans 引用 ----
    for span in package.ocr_spans:
        missing = [i for i in span.ocr_ids if i not in ocr_ids]
        if missing:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="span_ref_dangling",
                    detail=f"{span.span_id} 悬空 OCR 引用: {missing[:5]}",
                )
            )

    # ---- 评论：父评论缺失 / 视频内时间必须为空 ----
    for comment in package.comments:
        if comment.parent_id is not None and comment.parent_id not in comment_ids:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="comment_parent_missing",
                    detail=f"评论 {comment.comment_id} 的父评论 {comment.parent_id} 不在数据中（保留缺失标记）",
                )
            )
        if comment.video_timestamp_sec is not None:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="comment_unexpected_timestamp",
                    detail=f"评论 {comment.comment_id} 带有非空视频内时间 {comment.video_timestamp_sec}",
                )
            )

    # ---- ASR 为空是合法状态（v3/v7），只提示 ----
    if not package.asr:
        issues.append(
            ValidationIssue(
                severity="info",
                code="asr_empty",
                detail="默认 ASR 为空（不代表视频无语音），其余模态继续处理",
            )
        )

    has_error = any(i.severity == "error" for i in issues)
    has_warning = any(i.severity == "warning" for i in issues)
    status = "errors" if has_error else ("warnings" if has_warning else "ok")
    return PackageValidation(
        package=package.package_id,
        video_id=package.video_id,
        adapter=package.adapter,
        status=status,
        issues=issues,
        counts_expected=expected,
        counts_actual=actual,
    )


def run_inspect(args) -> int:
    output_dir = args.output or (PROJECT_ROOT / "work" / "inspect")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = ValidationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        data_root=str(args.data_root),
    )
    totals = {"errors": 0, "warnings": 0, "packages_failed": 0}
    try:
        package_roots = discover_packages(Path(args.data_root))
    except AdapterError as exc:
        print(f"[失败] {exc}")
        return 1

    for package_root in package_roots:
        try:
            package = load_package(package_root)
        except AdapterError as exc:
            report.packages.append(
                PackageValidation(
                    package=package_root.name,
                    video_id="",
                    adapter="",
                    status="failed",
                    issues=[ValidationIssue(severity="error", code="load_failed", detail=str(exc))],
                )
            )
            totals["packages_failed"] += 1
            continue

        result = validate_package(package, package_root)
        report.packages.append(result)
        report.inventory.append(
            InventoryEntry(
                package=package.package_id,
                video_id=package.video_id,
                adapter=package.adapter,
                schema_version=package.schema_version,
                duration_sec=package.duration_sec,
                counts={
                    "segments": len(package.segments),
                    "frames": len(package.frames),
                    "asr": len(package.asr),
                    "ocr": len(package.ocr),
                    "ocr_spans": len(package.ocr_spans),
                    "comments": len(package.comments),
                },
                warnings=package.warnings,
            )
        )
        errors = sum(1 for i in result.issues if i.severity == "error")
        warnings = sum(1 for i in result.issues if i.severity == "warning")
        totals["errors"] += errors
        totals["warnings"] += warnings
        print(
            f"{result.package}: {result.status}"
            f"（错误 {errors}，警告 {warnings}，信息 {len(result.issues) - errors - warnings}）"
        )

    report.totals = totals
    (output_dir / "validation_report.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "inventory.json").write_text(
        json.dumps(
            [e.model_dump(mode="json") for e in report.inventory], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"已写入 {output_dir / 'validation_report.json'}")
    return 1 if totals["errors"] or totals["packages_failed"] else 0
