"""适配器共用工具：JSONL 读取、路径安全、哈希。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


class AdapterError(Exception):
    """数据包结构与预期不符时抛出；不做猜测性兼容。"""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdapterError(f"缺少文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AdapterError(f"JSON 解析失败: {path} ({exc})") from exc


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AdapterError(f"缺少文件: {path}") from exc
    for lineno, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"JSONL 第 {lineno} 行解析失败: {path} ({exc})") from exc
        if not isinstance(record, dict):
            raise AdapterError(f"JSONL 第 {lineno} 行不是对象: {path}")
        yield record


def safe_join(package_root: Path, relative: str) -> Path:
    """校验相对路径不逃出数据包目录，返回解析后的绝对路径。"""
    if not isinstance(relative, str) or not relative:
        raise AdapterError(f"非法相对路径: {relative!r}")
    candidate = (package_root / relative).resolve()
    root = package_root.resolve()
    if not candidate.is_relative_to(root):
        raise AdapterError(f"路径逃出数据包目录: {relative} (root={root})")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
