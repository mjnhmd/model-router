from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import uvicorn

from .config import load_config
from .router import Router
from .codex_attach import attach as _attach, detach as _detach, install_service as _install_service, uninstall_service as _uninstall_service

app = typer.Typer(help="model-router：把请求路由到最快的上游模型")


def _load(path: str):
    return load_config(path)


@app.command()
def serve(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8765, "--port", "-p", help="监听端口"),
):
    """启动代理服务。"""
    config = _load(config_file)
    from .api import create_app

    app_instance = create_app(
        config,
        state_file=Path(config_file).with_name("config.state.yaml"),
        config_file=config_file,
    )
    uvicorn.run(app_instance, host=host, port=port)


@app.command()
def bench(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    rounds: int = typer.Option(1, "--rounds", "-r", help="测几轮"),
):
    """手动触发一轮测速并打印报告。"""
    config = _load(config_file)
    router = Router(config)

    async def run():
        for _ in range(rounds):
            await router.run_bench_round()
        await router.close()

    asyncio.run(run())
    print(f"\n{'服务':<20}{'模型':<24}{'tokens/s':>10}{'延迟s':>10}{'健康':>6}")
    print("-" * 70)
    for ident in router.identities:
        key = ident.key
        s = router.scores[key]
        print(
            f"{ident.service_name:<20}{ident.model_name:<24}{s.tps:>10.1f}{s.latency:>10.2f}{'OK' if s.healthy else 'DOWN':>6}"
        )


@app.command()
def attach(
    port: int = typer.Option(8765, "--port", "-p", help="本地代理端口"),
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="model-router 配置文件"),
    codex_config: str = typer.Option("~/.codex/config.toml", "--codex-config", help="Codex 配置文件路径"),
):
    """把本地代理接入 Codex（写 model_providers.local_router）。"""
    try:
        typer.echo(_attach(port=port, config_file=config_file, codex_config=codex_config))
    except Exception as e:
        typer.echo(f"接入失败: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def detach(
    codex_config: str = typer.Option("~/.codex/config.toml", "--codex-config", help="Codex 配置文件路径"),
):
    """移除 Codex 配置里的 local_router 块。"""
    try:
        typer.echo(_detach(codex_config=codex_config))
    except Exception as e:
        typer.echo(f"移除失败: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def gui(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    port: int = typer.Option(8765, "--port", "-p", help="本地代理端口"),
    no_attach: bool = typer.Option(False, "--no-attach", help="启动时不自动接入 Codex"),
    codex_config: str = typer.Option(
        "~/.codex/config.toml", "--codex-config", help="Codex 配置文件路径"
    ),
):
    """启动独立 App（原生窗口 + 自动接 Codex）。"""
    from .gui import run as _gui_run

    _gui_run(
        config_file=config_file,
        port=port,
        auto_attach=not no_attach,
        codex_config=codex_config,
    )


@app.command()
def service(
    action: str = typer.Argument(..., help="install | uninstall"),
    port: int = typer.Option(8765, "--port", "-p", help="本地代理端口"),
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="model-router 配置文件"),
    project_dir: str = typer.Option(None, "--project-dir", help="项目目录（默认自动推断）"),
):
    """管理 launchd 常驻服务（开机自启 + 崩溃重启）。"""
    try:
        if action == "install":
            typer.echo(_install_service(port=port, config_file=config_file, project_dir=project_dir))
        elif action == "uninstall":
            typer.echo(_uninstall_service())
        else:
            typer.echo("action 必须是 install 或 uninstall", err=True)
            raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"操作失败: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def check(config_file: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径")):
    """校验配置文件。"""
    try:
        cfg = load_config(config_file)
    except Exception as e:
        typer.echo(f"配置无效: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"配置有效: {len(cfg.services)} 个服务, {len(cfg.all_models())} 个模型")
    for s in cfg.services:
        typer.echo(f"  - {s.name} ({s.wire_api}) -> {', '.join(m.name for m in s.models)}")


if __name__ == "__main__":
    app()
