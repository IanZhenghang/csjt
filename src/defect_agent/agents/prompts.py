"""提示词加载与哈希。"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

ROLE_FILES = {
    "text_extract": "text_extract.md",
    "visual_observe": "visual_observe.md",
    "align": "align.md",
    "review": "critic.md",
    "attribution": "attribution.md",
}


@lru_cache(maxsize=None)
def load_prompt(task: str) -> str:
    filename = ROLE_FILES.get(task)
    if filename is None:
        raise KeyError(f"未知提示词任务: {task}")
    path = PROMPTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"提示词文件缺失: {path}")
    return path.read_text(encoding="utf-8")


def prompt_hash(task: str) -> str:
    return hashlib.sha256(load_prompt(task).encode("utf-8")).hexdigest()
