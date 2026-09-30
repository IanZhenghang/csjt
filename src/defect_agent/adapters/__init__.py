"""输入发现与适配器分发（IMPLEMENTATION_PLAN.md §5.1）。

只扫描数据根目录下包含 agent_input.json 的直接子目录；
通过 manifest 结构与 schema_version 共同识别适配器，未知结构明确报错。
"""

from __future__ import annotations

from pathlib import Path

from ..schemas import NormalizedPackage
from .base import AdapterError, read_json
from .common import package_sort_key
from .v1 import PackageV1Adapter
from .v2 import PackageV2Adapter

__all__ = [
    "AdapterError",
    "discover_packages",
    "load_package",
    "expected_counts",
    "PackageV1Adapter",
    "PackageV2Adapter",
]


def discover_packages(data_root: Path) -> list[Path]:
    """返回按数字顺序排列的数据包目录（v1..v10）。"""
    if not data_root.is_dir():
        raise AdapterError(f"数据根目录不存在: {data_root}")
    found = [
        child
        for child in data_root.iterdir()
        if child.is_dir() and (child / "agent_input.json").is_file()
    ]
    return sorted(found, key=lambda p: package_sort_key(p.name))


def _detect_adapter(package_root: Path) -> type[PackageV1Adapter] | type[PackageV2Adapter]:
    manifest = read_json(package_root / "agent_input.json")
    if not isinstance(manifest, dict):
        raise AdapterError(f"manifest 不是 JSON 对象: {package_root / 'agent_input.json'}")
    schema = manifest.get("schema_version")
    indices = manifest.get("indices")
    if schema == "1.0.0" and isinstance(indices, dict) and "ocr_half_frames" in indices:
        return PackageV1Adapter
    if schema == "1.0" and isinstance(indices, dict) and "segment_inputs" not in indices:
        return PackageV2Adapter
    observed = {
        "schema_version": schema,
        "indices_keys": sorted(indices.keys()) if isinstance(indices, dict) else None,
    }
    raise AdapterError(
        f"未知数据包结构，无适配器可用: {package_root} ({observed})"
    )


def load_package(package_root: Path) -> NormalizedPackage:
    """§6.7 load_package：读取并规范化一个数据包。"""
    adapter_cls = _detect_adapter(Path(package_root))
    return adapter_cls(Path(package_root)).load()


def expected_counts(package: NormalizedPackage) -> dict[str, int]:
    """把两种 schema 的 manifest counts 映射到统一计数键（用于 T03 校验）。"""
    counts = package.manifest_counts
    if package.adapter == "v1":
        return {
            "segments": counts.get("segments", -1),
            "frames": -1
            if "global_frames" not in counts
            else counts["global_frames"] + counts.get("additional_subtitle_timepoints", 0),
            "asr": counts.get("asr_utterances", -1),
            "ocr": counts.get("ocr_default_observations", -1),
            "ocr_spans": counts.get("ocr_grouped_spans", -1),
            "comments": counts.get("comments", -1),
            "frames_full": counts.get("global_frames", -1),
        }
    return {
        "segments": counts.get("segments", -1),
        "frames": counts.get("frames", -1),
        "asr": counts.get("asr_utterances", -1),
        "ocr": counts.get("ocr_default", -1),
        "ocr_spans": counts.get("ocr_observed_spans", -1),
        "comments": counts.get("comments", -1),
        "frames_full": -1,
    }
