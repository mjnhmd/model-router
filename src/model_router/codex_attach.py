from __future__ import annotations

from pathlib import Path

from .codex_session import CodexSession
from .config import load_config

DEFAULT_CODEX_CONFIG = "~/.codex/config.toml"


def attach(
    port: int = 8765,
    config_file: str = "config.yaml",
    codex_config: str = DEFAULT_CODEX_CONFIG,
) -> str:
    """安全持久接入 Codex；detach 可按备份恢复原始字节。"""
    if Path(config_file).expanduser().exists():
        public_model = load_config(config_file).public_model
    else:
        public_model = "route-fastest"

    session = CodexSession(Path(codex_config).expanduser())
    session.install_persistent(port=port, public_model=public_model)
    return (
        f"已接入 Codex: {session.config_path}\n"
        f"  provider=model_providers.local_router base_url=http://127.0.0.1:{port}/v1\n"
        f"  使用模型名: {public_model}\n"
        "启动代理后，Codex 选择 local_router 即可使用"
    )


def detach(codex_config: str = DEFAULT_CODEX_CONFIG) -> str:
    """安全移除本工具创建的 Codex 接入并恢复原始配置。"""
    session = CodexSession(Path(codex_config).expanduser())
    session.remove_persistent()
    return f"已从 {session.config_path} 移除 local_router 配置并恢复原文件"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def install_service(
    port: int = 8765,
    config_file: str = "config.yaml",
    project_dir: str | None = None,
) -> str:
    """安装 launchd 常驻服务（开机自启 + 崩溃重启）。返回描述文本。"""
    import subprocess

    if project_dir is None:
        project_dir = str(Path(__file__).resolve().parent.parent.parent)
    candidate = Path("/opt/homebrew/bin/python3")
    if not candidate.exists():
        candidate = Path("/usr/local/bin/python3")
    if not candidate.exists():
        raise FileNotFoundError("未找到 Homebrew python3，请安装 python")
    cfg_abs = str(Path(project_dir) / config_file)
    plist_path = _launch_agents_dir() / "com.model-router.plist"

    plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.model-router</string>
    <key>ProgramArguments</key>
    <array>
        <string>{candidate}</string>
        <string>-m</string>
        <string>model_router.cli</string>
        <string>serve</string>
        <string>--config</string>
        <string>{cfg_abs}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{project_dir}/src</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/model-router.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/model-router.err.log</string>
</dict>
</plist>
'''
    _launch_agents_dir().mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    return (
        f"launchd 服务已安装并启动: {plist_path}\n"
        "  开机自启 + 崩溃自动重启\n"
        "  日志: /tmp/model-router.log / /tmp/model-router.err.log\n"
        f"  停止: launchctl unload {plist_path}"
    )


def uninstall_service() -> str:
    """卸载 launchd 常驻服务。"""
    import subprocess

    plist_path = _launch_agents_dir() / "com.model-router.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        return f"launchd 服务已卸载: {plist_path}"
    return "未找到 model-router launchd 服务"
