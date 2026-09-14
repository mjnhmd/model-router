"""独立 App 入口：起本地服务 + 自动接 Codex + 原生窗口显示控制台。"""
from __future__ import annotations

import html
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import uvicorn
import webview

from .api import create_app
from .codex_attach import DEFAULT_CODEX_CONFIG
from .codex_session import CodexSession
from .config import Config, load_config, save_config

DEFAULT_PORT = 8765
STATUS_INTERVAL = 1.0


class _ServerHandle:
    def __init__(self, server: uvicorn.Server):
        self.server = server
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="model-router-server", daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:
            # uvicorn raises SystemExit when binding fails, including occupied ports.
            self.error = exc

    def is_alive(self) -> bool:
        return self.thread.is_alive() and self.error is None

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("本地代理未能停止，请退出 Model Router 进程后重试")


def _start_server(
    config_file: str, port: int, state_file: str | None, instance_id: str
) -> _ServerHandle:
    config = load_config(config_file)
    app = create_app(config, state_file=state_file, config_file=config_file, instance_id=instance_id)
    server = _ServerHandle(uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2,
    )))
    server.thread.start()
    return server


def _read_status(port: int, instance_id: str) -> dict | None:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/status", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if (isinstance(payload, dict) and payload.get("model_router") == "model-router"
                and payload.get("instance_id") == instance_id):
            return payload
    except Exception:
        pass
    return None


def _wait_ready(
    port: int, instance_id: str, server: _ServerHandle, timeout: float = 15.0
) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server.is_alive():
            return None
        payload = _read_status(port, instance_id)
        if payload is not None and server.is_alive():
            return payload
        time.sleep(min(0.3, max(0, deadline - time.monotonic())))
    return None


class _CodexMonitor:
    def __init__(
        self,
        session: CodexSession,
        port: int,
        instance_id: str,
        server: _ServerHandle,
        attach_enabled: bool | None = True,
    ):
        self.session = session
        self.port = port
        self.instance_id = instance_id
        self.server = server
        # None follows the live config switch; bool is used by explicit CLI overrides.
        self.attach_enabled = attach_enabled
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="model-router-codex", daemon=True)
        self.public_model: str | None = None
        self.catalog: dict | None = None
        self.missed_status = 0
        self.error: str | None = None
        self.on_error: Callable[[], None] | None = None

    def sync(self, status: dict | None) -> None:
        if self.stopped.is_set():
            return
        alive = self.server.is_alive()
        if status is None:
            self.missed_status += 1
            if alive and self.missed_status < 3:
                return
        else:
            self.missed_status = 0
        configured = alive and status is not None and status.get("responses_configured") is True
        enabled = self.attach_enabled if self.attach_enabled is not None else (
            status is not None and status.get("codex_enabled") is True
        )
        public_model = status.get("public_model") if status is not None else None
        catalog = status.get("codex_catalog") if status is not None else None
        if self.session.active and (not enabled or not configured or public_model != self.public_model
                                    or catalog != self.catalog):
            self.session.restore()
            self.public_model = None
            self.catalog = None
        if (not self.session.active and enabled and configured and status.get("responses_ready") is True
                and isinstance(public_model, str) and public_model.strip()
                and not self.stopped.is_set()):
            if catalog is None:
                self.session.start(port=self.port, public_model=public_model)
            else:
                self.session.start(port=self.port, public_model=public_model, catalog=catalog)
            self.catalog = catalog
            self.public_model = public_model

    def _run(self) -> None:
        while not self.stopped.wait(STATUS_INTERVAL):
            try:
                self.sync(_read_status(self.port, self.instance_id))
                if not self.server.is_alive():
                    return
            except Exception as exc:
                self.error = f"Codex 自动接管已停止：{exc}；当前配置与备份已保留，请检查后重启。"
                print(self.error)
                if self.on_error is not None:
                    self.on_error()
                return

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError("Codex 接管监控未能停止，请退出 Model Router 进程后检查配置")


class _CodexControl:
    """Desktop-only actions; reconnect requires an explicit workbench click."""

    def __init__(self, monitor: _CodexMonitor | None):
        self._monitor = monitor
        self._lock = threading.Lock()

    def get_codex_state(self) -> dict:
        monitor = self._monitor
        if monitor is None:
            return {"state": "disabled", "message": "本次启动未启用 Codex 接入。"}
        if monitor.error:
            return {"state": "paused", "message": monitor.error}
        if monitor.session.active:
            return {"state": "active", "message": "Codex 正在使用 Model Router。"}
        return {"state": "waiting", "message": "等待本地代理就绪后自动接入。"}

    def reconnect_codex(self) -> dict:
        with self._lock:
            monitor = self._monitor
            if monitor is None or not monitor.error:
                return self.get_codex_state()
            try:
                monitor.stop()
                # The user explicitly accepts the current file as the new baseline.
                monitor.session.use_current_as_baseline()
                monitor.error = None
                monitor.public_model = None
                monitor.catalog = None
                monitor.stopped.clear()
                monitor.sync(_read_status(monitor.port, monitor.instance_id))
                monitor.thread = threading.Thread(
                    target=monitor._run, name="model-router-codex", daemon=True
                )
                monitor.start()
            except Exception as exc:
                monitor.error = f"Codex 接入仍然失败：{exc}；当前配置与备份已保留。"
            return self.get_codex_state()

    def restore_codex(self) -> dict:
        """Restore the original Codex config after the user stops using a mode."""
        with self._lock:
            monitor = self._monitor
            if monitor is None:
                return {"state": "disabled", "message": "本次启动未启用 Codex 接入。"}
            try:
                if monitor.session.active:
                    monitor.session.restore()
                monitor.public_model = None
                monitor.catalog = None
                return self.get_codex_state()
            except Exception as exc:
                monitor.error = f"Codex 原配置恢复失败：{exc}；当前配置与备份已保留。"
                return self.get_codex_state()


def _bind_monitor_errors(window: webview.Window, monitor: _CodexMonitor) -> None:
    def show_error() -> None:
        if monitor.error and not window.events.closed.is_set():
            try:
                window.evaluate_js(f"toast({json.dumps(monitor.error)}, true)")
            except Exception:
                # Closing the native window can interrupt JavaScript evaluation.
                print(monitor.error)

    monitor.on_error = lambda: show_error() if window.events.loaded.is_set() else None
    window.events.loaded += show_error
    window.events.closed += monitor.stopped.set


def run(
    config_file: str = "config.yaml",
    port: int = DEFAULT_PORT,
    auto_attach: bool | None = None,
    codex_config: str = DEFAULT_CODEX_CONFIG,
    config_explicit: bool = False,
) -> None:
    """启动独立 App；仅在 Codex 接入开关开启时修改 Codex 配置。

    ``config_explicit`` 表示调用方通过 ``-c`` 指定了配置路径，打包后也必须原样使用。
    """
    try:
        config_file = _resolve_config_file(config_file, explicit=config_explicit)
        app_config = _load_config_for_app(config_file)
    except Exception as exc:
        _report_error(f"配置无法加载：{exc}")
        return
    # The default desktop mode keeps a monitor alive so changes made in the UI
    # take effect immediately. --no-attach remains a hard opt-out for this run.
    session = CodexSession(codex_config) if auto_attach is not False else None
    recovery_error = None
    if session is not None:
        try:
            session.recover_previous()
        except Exception as exc:
            recovery_error = f"{exc}。点击“备份当前配置并接入”会以当前 Codex 配置为准重新接入。"

    state_file = str(Path(config_file).with_name("config.state.yaml"))
    instance_id = uuid.uuid4().hex
    try:
        server = _start_server(config_file, port, state_file, instance_id)
    except Exception as exc:
        _report_error(f"本地代理启动失败：{exc}。请检查配置和端口占用。")
        return

    monitor: _CodexMonitor | None = None
    errors: list[str] = []
    try:
        status = _wait_ready(port, instance_id, server)
        if status is None:
            errors.append(f"本地代理启动失败：端口 {port} 未就绪。请检查配置和端口占用。")
            return
        if session is not None:
            monitor = _CodexMonitor(
                session,
                port,
                instance_id,
                server,
                attach_enabled=auto_attach,
            )
            try:
                if recovery_error:
                    monitor.error = recovery_error
                else:
                    monitor.sync(status)
                    monitor.start()
            except Exception as exc:
                monitor.stopped.set()
                monitor.error = f"Codex 接入已暂停：{exc}；请在工作台重新接入。"
        url = f"http://127.0.0.1:{port}/console/"
        window = webview.create_window(
            "Model Router", url, width=1280, height=820, min_size=(900, 600),
            js_api=_CodexControl(monitor)
        )
        if monitor is not None and window is not None:
            _bind_monitor_errors(window, monitor)
        webview.start()
    finally:
        monitor_stopped = True
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as exc:
                monitor_stopped = False
                errors.append(str(exc))
        if session is not None and monitor_stopped:
            try:
                session.restore()
            except Exception as exc:
                print(f"Codex 接入已暂停：{exc}；当前配置与备份已保留。")
        try:
            server.stop()
        except Exception as exc:
            errors.append(str(exc))
        for message in errors:
            _report_error(message)
        print("App 已退出")


def _report_error(message: str) -> None:
    content = html.escape(message).replace("\n", "<br>")
    try:
        webview.create_window(
            "Model Router",
            html=(
                "<html><body style='font: -apple-system-body; padding: 28px'>"
                "<h2>Model Router 无法启动</h2>"
                f"<p>{content}</p></body></html>"
            ),
            width=680,
            height=360,
        )
        webview.start()
    except Exception:
        print(f"Model Router: {message}")


def _resolve_config_file(config_file: str, explicit: bool = False) -> str:
    path = Path(config_file).expanduser()
    if not getattr(sys, "frozen", False) or explicit or path.name != "config.yaml":
        return str(path)

    app_data = Path.home() / "Library" / "Application Support" / "ModelRouter"
    user_config = app_data / "config.yaml"
    if user_config.exists():
        return str(user_config)

    app_data.mkdir(parents=True, exist_ok=True)
    if not user_config.exists():
        save_config(user_config, Config(services=[]))
    return str(user_config)


def _load_config_for_app(config_file: str) -> Config:
    try:
        return load_config(config_file)
    except FileNotFoundError:
        config = Config(services=[])
        save_config(config_file, config)
        return config


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Model Router 独立 App")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help="本地代理端口")
    parser.add_argument(
        "--codex-config",
        default=DEFAULT_CODEX_CONFIG,
        help="Codex 配置文件路径",
    )
    parser.add_argument("--no-attach", action="store_true", help="本次启动不接入 Codex")
    args = parser.parse_args()
    run(
        config_file=args.config or "config.yaml",
        port=args.port,
        auto_attach=False if args.no_attach else None,
        codex_config=args.codex_config,
        config_explicit=args.config is not None,
    )
