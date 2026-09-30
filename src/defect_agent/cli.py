"""命令行入口：doctor / inspect / normalize / run / batch / resume / evaluate。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 控制台不支持时保持默认
                pass


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数（0=禁用格式修复）")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="defect_agent",
        description="智能驾驶缺陷线索提取与归因研究原型",
    )
    parser.add_argument("--version", action="version", version=f"defect_agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="启动自检（默认离线）")
    p_doctor.add_argument("--live", action="store_true", help="联网能力探测（需配置 LLM_API_KEY）")
    p_doctor.add_argument("--config", type=Path, default=None)
    p_doctor.add_argument("--data-root", type=Path, default=None)

    p_inspect = sub.add_parser("inspect", help="扫描数据包并生成校验报告")
    p_inspect.add_argument("--data-root", type=Path, required=True)
    p_inspect.add_argument("--output", type=Path, default=None, help="输出目录（默认 work/inspect）")
    p_inspect.add_argument("--config", type=Path, default=None)

    p_normalize = sub.add_parser("normalize", help="读取数据包并写入规范化结果")
    p_normalize.add_argument("--data-root", type=Path, required=True)
    p_normalize.add_argument("--output", type=Path, default=None, help="输出目录（默认 work/normalized）")
    p_normalize.add_argument("--config", type=Path, default=None)

    p_run = sub.add_parser("run", help="单包完整流程")
    p_run.add_argument("--package", type=Path, required=True, help="数据包目录（含 agent_input.json）")
    p_run.add_argument("--mode", choices=["mock", "live"], default="mock")
    p_run.add_argument("--max-live-calls", type=int, default=None,
                       help="本次运行真实调用上限；0=不限次数；缺省用配置默认（80）")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--config", type=Path, default=None)
    p_run.add_argument("--experiment", choices=["A", "B", "C", "D"], default=None,
                       help="对照实验：A 仅文本 / B 仅视觉 / C 融合无审查 / D 完整流程")

    p_batch = sub.add_parser("batch", help="批量处理数据根目录下全部包")
    p_batch.add_argument("--data-root", type=Path, required=True)
    p_batch.add_argument("--mode", choices=["mock", "live"], default="mock")
    p_batch.add_argument("--max-live-calls", type=int, default=None,
                         help="本次运行真实调用上限；0=不限次数；缺省用配置默认（80）")
    p_batch.add_argument("--run-id", default=None)
    p_batch.add_argument("--config", type=Path, default=None)
    p_batch.add_argument("--only", default=None, help="只处理指定包名列表，逗号分隔，如 v2,v3")

    p_resume = sub.add_parser("resume", help="恢复已有运行")
    p_resume.add_argument("--run-id", required=True)
    p_resume.add_argument("--config", type=Path, default=None)
    p_resume.add_argument("--max-live-calls", type=int, default=None)

    for command_parser in (p_run, p_batch, p_resume):
        command_parser.add_argument(
            "--max-format-repairs", type=_non_negative_int, default=None,
            help="首次输出后的最多格式修复次数；0=禁用；缺省用配置（resume 保留原快照）",
        )

    p_eval = sub.add_parser("evaluate", help="对照人工标注计算指标")
    p_eval.add_argument("--run-id", required=True)
    p_eval.add_argument("--annotations", type=Path, required=True)
    p_eval.add_argument("--iou", type=float, default=0.3)
    p_eval.add_argument("--config", type=Path, default=None)

    p_stab = sub.add_parser("stability", help="稳定性实验：N 次独立采样一致率（绕过缓存）")
    p_stab.add_argument("--package", type=Path, default=None, help="预留：目标数据包")
    p_stab.add_argument("--config", type=Path, default=None)

    p_cv = sub.add_parser("crossvideo", help="跨视频语义聚类：发现同一事故/转载分组")
    p_cv.add_argument("--run-id", required=True)
    p_cv.add_argument("--threshold", type=float, default=None, help="相似度阈值（默认 0.80）")
    p_cv.add_argument("--config", type=Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    from .config import load_config

    if args.command == "doctor":
        config = load_config(args.config, args.data_root)
        from .doctor import run_doctor

        return run_doctor(config, live=args.live)

    if args.command == "inspect":
        from .validation import run_inspect

        return run_inspect(args)

    if args.command == "normalize":
        from .cli_impl import run_normalize

        return run_normalize(args)

    if args.command == "run":
        from .pipeline import run_package

        return run_package(args)

    if args.command == "batch":
        from .pipeline import run_batch

        return run_batch(args)

    if args.command == "resume":
        from .pipeline import run_resume

        return run_resume(args)

    if args.command == "evaluate":
        from .evaluation import run_evaluate

        return run_evaluate(args)

    if args.command == "stability":
        from .evaluation import run_stability

        return run_stability(args)

    if args.command == "crossvideo":
        from .embedding_features import run_crossvideo

        return run_crossvideo(args)

    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
