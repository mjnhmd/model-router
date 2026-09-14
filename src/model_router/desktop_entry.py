"""独立 App 入口：解析启动参数后交给 GUI 运行。"""
from __future__ import annotations

import argparse
import sys

# 打包后本文件作为独立脚本入口运行，必须使用绝对导入（相对导入会 ImportError）。
from model_router.codex_attach import DEFAULT_CODEX_CONFIG
from model_router.gui import DEFAULT_PORT, run


def _arguments(argv: list[str] | None = None) -> list[str]:
    """过滤 Finder 注入的 -psn_* 参数，避免 argparse 直接报错退出。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    return [item for item in raw if not item.startswith("-psn_")]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ModelRouter", description="Model Router 独立 App")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径，默认用应用支持目录")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help="本地代理端口")
    parser.add_argument(
        "--codex-config", default=DEFAULT_CODEX_CONFIG, help="Codex 配置文件路径"
    )
    parser.add_argument(
        "--no-attach", action="store_true", help="本次启动不接管 Codex 配置"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(_arguments(argv))
    run(
        config_file=args.config or "config.yaml",
        port=args.port,
        auto_attach=False if args.no_attach else None,
        codex_config=args.codex_config,
        config_explicit=args.config is not None,
    )


if __name__ == "__main__":
    main()
