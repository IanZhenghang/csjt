"""doctor：启动自检。

离线检查不发起任何网络请求；`--live` 仅使用无敏感内容的小样本探测真实接口能力。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import AgentConfig, load_config

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    items: list[str] = field(default_factory=list)


def _check_python() -> CheckResult:
    version = sys.version_info
    detail = f"Python {version.major}.{version.minor}.{version.micro}"
    if version.major == 3 and version.minor == 12:
        return CheckResult("python", PASS, detail)
    return CheckResult("python", WARN, detail + "（计划要求 3.12，建议使用 3.12）")


def _check_config(config: AgentConfig) -> CheckResult:
    if not config.yaml_path.is_file():
        return CheckResult("config", FAIL, f"缺少配置文件 {config.yaml_path}")
    required_sections = ["window", "vision", "text", "comments", "refinement", "budget"]
    missing = [s for s in required_sections if s not in config.raw]
    if missing:
        return CheckResult("config", FAIL, f"配置缺少节: {', '.join(missing)}")
    return CheckResult("config", PASS, f"{config.yaml_path.name} 结构完整")


def _check_env(config: AgentConfig) -> CheckResult:
    has_key = bool(config.llm.api_key)
    mode = config.llm.mode
    if mode == "live" and not has_key:
        return CheckResult("env", FAIL, "LLM_MODE=live 但未配置 LLM_API_KEY（不输出密钥内容）")
    detail = f"LLM_MODE={mode}，LLM_API_KEY {'已配置' if has_key else '未配置'}"
    return CheckResult("env", PASS if has_key else WARN, detail)


def _discover_packages(data_root: Path) -> list[Path]:
    """只扫描包含 agent_input.json 的直接子目录。"""
    if not data_root.is_dir():
        return []
    found = []
    for child in sorted(data_root.iterdir(), key=lambda p: p.name):
        if child.is_dir() and (child / "agent_input.json").is_file():
            found.append(child)
    return found


def _check_data(config: AgentConfig) -> CheckResult:
    root = config.data_root
    if not root.is_dir():
        return CheckResult("data", FAIL, f"数据根目录不存在: {root}")
    packages = _discover_packages(root)
    if not packages:
        return CheckResult("data", FAIL, f"{root} 下未发现包含 agent_input.json 的子目录")
    items = []
    schemas: dict[str, int] = {}
    for pkg in packages:
        try:
            manifest = json.loads((pkg / "agent_input.json").read_text(encoding="utf-8"))
            schema = str(manifest.get("schema_version", "?"))
        except Exception as exc:  # noqa: BLE001 - 逐包报告损坏清单
            items.append(f"{pkg.name}: manifest 读取失败 ({exc})")
            continue
        schemas[schema] = schemas.get(schema, 0) + 1
        items.append(f"{pkg.name}: schema {schema}")
    schema_summary = ", ".join(f"schema {k} × {v}" for k, v in sorted(schemas.items()))
    return CheckResult("data", PASS, f"{len(packages)} 个数据包（{schema_summary}）", items)


def _check_ffmpeg() -> CheckResult:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        return CheckResult("ffmpeg", WARN, f"未找到: {', '.join(missing)}（补帧功能不可用）")
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=15, check=False
        )
        first = (out.stdout or "").splitlines()[0] if out.stdout else "ffmpeg"
    except Exception:  # noqa: BLE001
        first = "ffmpeg（版本读取失败）"
    return CheckResult("ffmpeg", PASS, f"ffmpeg/ffprobe 可用；{first[:80]}")


def _check_deps() -> CheckResult:
    problems = []
    for mod in ("pydantic", "httpx", "PIL", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            problems.append(mod)
    if problems:
        return CheckResult("deps", FAIL, f"缺少依赖: {', '.join(problems)}")
    import pydantic
    import httpx

    return CheckResult("deps", PASS, f"pydantic {pydantic.VERSION}, httpx {httpx.__version__}")


def run_doctor(config: AgentConfig, live: bool = False) -> int:
    results = [
        _check_python(),
        _check_deps(),
        _check_config(config),
        _check_env(config),
        _check_data(config),
        _check_ffmpeg(),
    ]

    if live:
        from .llm.capabilities import probe_capabilities

        results.append(probe_capabilities(config))

    print("== defect_agent doctor ==")
    has_fail = False
    for result in results:
        mark = {"PASS": "[通过]", "WARN": "[警告]", "FAIL": "[失败]"}[result.status]
        if result.status == FAIL:
            has_fail = True
        print(f"{mark} {result.name}: {result.detail}")
        for item in result.items:
            print(f"        - {item}")
    return 1 if has_fail else 0
