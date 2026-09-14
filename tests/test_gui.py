from pathlib import Path
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from model_router import gui
from model_router.config import Config


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: test_home)
    real_save = gui.save_config
    real_session = gui.CodexSession

    def guarded_save(path, config):
        assert Path(path).expanduser().resolve().is_relative_to(tmp_path.resolve())
        return real_save(path, config)

    def guarded_session(path):
        assert Path(path).expanduser().resolve().is_relative_to(tmp_path.resolve())
        return real_session(path)

    monkeypatch.setattr(gui, "save_config", guarded_save)
    monkeypatch.setattr(gui, "CodexSession", guarded_session)


class StatusResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_wait_ready_rejects_other_model_router_instance(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: StatusResponse({
        "model_router": "model-router", "instance_id": "other-instance",
    }))
    server = SimpleNamespace(is_alive=lambda: True)

    assert gui._wait_ready(8765, "this-instance", server, timeout=0.01) is None


def test_wait_ready_rejects_finished_server_even_if_status_matches(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: StatusResponse({
        "model_router": "model-router", "instance_id": "this-instance",
    }))
    server = SimpleNamespace(is_alive=lambda: False)

    assert gui._wait_ready(8765, "this-instance", server, timeout=0.01) is None


def test_started_server_can_be_stopped_and_joined(monkeypatch, tmp_path):
    running = threading.Event()

    class WaitingServer:
        should_exit = False
        force_exit = False

        def __init__(self, config):
            pass

        def run(self):
            running.set()
            while not self.should_exit:
                threading.Event().wait(0.01)

    monkeypatch.setattr(gui, "load_config", lambda path: Config(services=[]))
    monkeypatch.setattr(gui, "create_app", lambda config, **kwargs: object())
    monkeypatch.setattr(gui.uvicorn, "Server", WaitingServer)
    server = gui._start_server(str(tmp_path / "config.yaml"), 8765, None, "this-instance")
    assert running.wait(1)
    server.stop()
    assert not server.is_alive()


def ready_status(**overrides):
    return {
        "model_router": "model-router", "instance_id": "this-instance",
        "responses_ready": True, "responses_configured": True,
        "public_model": "route-fastest", "models": [], **overrides,
    }


def make_monitor(tmp_path):
    config = tmp_path / "codex.toml"
    config.write_text('model = "original"\nmodel_provider = "custom"\n')
    server = SimpleNamespace(is_alive=lambda: True)
    monitor = gui._CodexMonitor(gui.CodexSession(config), 8765, "this-instance", server)
    return monitor, config


def test_codex_monitor_follows_runtime_codex_enabled_switch(tmp_path):
    config = tmp_path / "codex.toml"
    original = b'model = "original"\nmodel_provider = "custom"\n'
    config.write_bytes(original)
    server = SimpleNamespace(is_alive=lambda: True)
    monitor = gui._CodexMonitor(
        gui.CodexSession(config), 8765, "this-instance", server, attach_enabled=None
    )

    monitor.sync(ready_status(codex_enabled=False))
    assert config.read_bytes() == original
    monitor.sync(ready_status(codex_enabled=True))
    assert 'model = "route-fastest"' in config.read_text()
    monitor.sync(ready_status(codex_enabled=False))
    assert config.read_bytes() == original


def test_restore_codex_control_restores_original_config_immediately(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status())
    assert config.read_bytes() != original

    result = gui._CodexControl(monitor).restore_codex()

    assert result["state"] == "waiting"
    assert config.read_bytes() == original


def test_codex_attaches_after_first_configuration_becomes_ready(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status(responses_ready=False, responses_configured=False))
    assert config.read_bytes() == original
    monitor.sync(ready_status(responses_ready=False))
    assert config.read_bytes() == original
    monitor.sync(ready_status())
    assert 'model = "route-fastest"' in config.read_text()
    monitor.session.restore()


def test_codex_stays_attached_on_health_failure_but_restores_for_empty_pool(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status())
    managed = config.read_bytes()
    monitor.sync(ready_status(responses_ready=False))
    assert config.read_bytes() == managed
    monitor.sync(ready_status(responses_ready=False, responses_configured=False))
    assert config.read_bytes() == original


def test_codex_model_change_waits_for_new_ready_pool(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status())
    monitor.sync(ready_status(public_model="new-route", responses_ready=False))
    assert config.read_bytes() == original
    monitor.sync(ready_status(public_model="new-route"))
    assert 'model = "new-route"' in config.read_text()
    monitor.session.restore()
    assert config.read_bytes() == original


def test_codex_status_failure_retries_before_restoring(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status())
    managed = config.read_bytes()
    monitor.sync(None)
    monitor.sync(None)
    assert config.read_bytes() == managed
    monitor.sync(None)
    assert config.read_bytes() == original


def test_codex_never_attaches_after_server_exits(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.server.is_alive = lambda: False
    monitor.sync(ready_status())
    assert config.read_bytes() == original


def test_codex_restores_immediately_when_server_exits(tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    monitor.sync(ready_status())
    monitor.server.is_alive = lambda: False
    monitor.sync(None)
    assert config.read_bytes() == original


def test_stop_prevents_attach_from_status_response_already_in_flight(monkeypatch, tmp_path):
    monitor, config = make_monitor(tmp_path)
    original = config.read_bytes()
    requested, release = threading.Event(), threading.Event()

    def blocked_status(port, instance_id):
        requested.set()
        assert release.wait(2)
        return ready_status()

    monkeypatch.setattr(gui, "STATUS_INTERVAL", 0.001)
    monkeypatch.setattr(gui, "_read_status", blocked_status)
    monitor.start()
    assert requested.wait(1)
    stopper = threading.Thread(target=monitor.stop)
    stopper.start()
    assert monitor.stopped.wait(1)
    release.set()
    stopper.join(1)
    assert not stopper.is_alive()
    assert not monitor.thread.is_alive()
    assert config.read_bytes() == original


def test_server_system_exit_is_reported_as_failed_start(monkeypatch, tmp_path):
    class FailedServer:
        should_exit = False
        force_exit = False

        def __init__(self, config):
            pass

        def run(self):
            raise SystemExit(1)

    monkeypatch.setattr(gui, "load_config", lambda path: Config(services=[]))
    monkeypatch.setattr(gui, "create_app", lambda config, **kwargs: object())
    monkeypatch.setattr(gui.uvicorn, "Server", FailedServer)
    server = gui._start_server(str(tmp_path / "config.yaml"), 8765, None, "this-instance")
    server.thread.join(1)
    assert not server.is_alive()
    assert isinstance(server.error, SystemExit)
    server.stop()


def test_owned_http_server_stops_and_releases_its_loopback_port(monkeypatch, tmp_path):
    from fastapi import FastAPI

    def local_app(config, state_file, config_file, instance_id):
        app = FastAPI()

        @app.get("/v1/status")
        def status():
            return ready_status(instance_id=instance_id, responses_ready=False,
                                responses_configured=False)

        return app

    monkeypatch.setattr(gui, "load_config", lambda path: Config(services=[]))
    monkeypatch.setattr(gui, "create_app", local_app)
    server = gui._start_server(str(tmp_path / "config.yaml"), 0, None, "this-instance")
    try:
        deadline = time.monotonic() + 2
        while not server.server.started and server.is_alive() and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert server.server.started
        port = server.server.servers[0].sockets[0].getsockname()[1]
        status = gui._wait_ready(port, "this-instance", server, timeout=1)
        assert status["instance_id"] == "this-instance"
        assert not status["responses_ready"]
    finally:
        server.stop()

    assert not server.is_alive()
    with socket.socket() as client:
        client.settimeout(0.5)
        assert client.connect_ex(("127.0.0.1", port)) != 0


class FakeSession:
    restored = False
    started = False

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.active = False

    def recover_previous(self):
        return False

    def start(self, port: int, public_model: str):
        FakeSession.started = True
        self.active = True

    def restore(self):
        if self.active:
            FakeSession.restored = True
            self.active = False


class FakeServer:
    def __init__(self, events):
        self.events = events
        self.running = True

    def is_alive(self):
        return self.running

    def stop(self):
        self.running = False
        self.events.append("stop")


class FakeWindowEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def set(self):
        for callback in self.callbacks:
            callback()
        super().set()


class FakeWindow:
    def __init__(self):
        self.events = SimpleNamespace(loaded=FakeWindowEvent(), closed=FakeWindowEvent())
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


def stub_desktop(monkeypatch, events, status=None):
    server = FakeServer(events)
    server.window = FakeWindow()

    def start_server(config_file, port, state_file, instance_id):
        events.append("server")
        server.instance_id = instance_id
        return server

    def read_status(port, instance_id):
        assert instance_id == server.instance_id
        return ready_status(instance_id=instance_id, **(status or {}))

    def create_window(*args, **kwargs):
        events.append("window")
        return server.window

    monkeypatch.setattr(gui, "_start_server", start_server)
    monkeypatch.setattr(gui, "_read_status", read_status)
    monkeypatch.setattr(gui.webview, "create_window", create_window)
    monkeypatch.setattr(gui.webview, "start", lambda: events.append("close"))
    monkeypatch.setattr(gui, "load_config", lambda path: Config(services=[]))
    return server


def test_codex_attach_error_is_visible_when_console_loads(monkeypatch, tmp_path):
    server = stub_desktop(monkeypatch, [])

    class FailedSession(FakeSession):
        def start(self, port, public_model):
            raise RuntimeError('用户修改了 "local_router" 配置')

    monkeypatch.setattr(gui, "CodexSession", FailedSession)
    monkeypatch.setattr(gui, "_report_error", lambda message: None)

    def open_console():
        assert server.window.scripts == []
        server.window.events.loaded.set()
        assert len(server.window.scripts) == 1
        assert server.window.scripts[0].startswith("toast(")
        message = json.loads(server.window.scripts[0][len("toast("):-len(", true)")])
        assert '用户修改了 "local_router" 配置' in message

    monkeypatch.setattr(gui.webview, "start", open_console)
    gui.run(str(tmp_path / "config.yaml"), auto_attach=True)


def test_background_restore_conflict_stops_monitor_and_notifies_open_console(monkeypatch, tmp_path):
    monitor, config = make_monitor(tmp_path)
    monitor.sync(ready_status())
    edited = config.read_text().replace("http://127.0.0.1:8765/v1", "http://user-router/v1")
    config.write_text(edited)
    window = FakeWindow()
    gui._bind_monitor_errors(window, monitor)
    window.events.loaded.set()
    monkeypatch.setattr(gui, "STATUS_INTERVAL", 0.001)
    monkeypatch.setattr(gui, "_read_status", lambda *args: ready_status(
        responses_ready=False, responses_configured=False,
    ))
    monitor.start()
    monitor.thread.join(1)

    assert not monitor.thread.is_alive()
    assert config.read_text() == edited
    assert monitor.session.backup_path is not None
    assert len(window.scripts) == 1
    assert "local_router" in window.scripts[0]
    monitor.stop()


def test_gui_restores_codex_after_window_closes(monkeypatch, tmp_path: Path):
    events = []
    FakeSession.restored = False
    FakeSession.started = False
    server = stub_desktop(monkeypatch, events)
    monkeypatch.setattr(gui, "CodexSession", FakeSession)

    gui.run(str(tmp_path / "config.yaml"), 8765, auto_attach=True)

    assert events == ["server", "window", "close", "stop"]
    assert not server.is_alive()
    assert FakeSession.started
    assert FakeSession.restored


def test_gui_first_setup_attaches_in_background_then_restores_isolated_home(monkeypatch, tmp_path):
    codex = Path.home() / ".codex" / "config.toml"
    codex.parent.mkdir()
    original = b'model = "original"\nmodel_provider = "custom"\n'
    codex.write_bytes(original)
    events = []
    status = {"responses_ready": False, "responses_configured": False}
    server = stub_desktop(monkeypatch, events, status)
    threads_before = set(threading.enumerate())
    monkeypatch.setattr(gui, "STATUS_INTERVAL", 0.005)

    def setup_then_close():
        assert codex.read_bytes() == original
        status.update(responses_ready=True, responses_configured=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if 'model = "route-fastest"' in codex.read_text():
                break
            threading.Event().wait(0.005)
        assert 'model = "route-fastest"' in codex.read_text()
        events.append("close")

    monkeypatch.setattr(gui.webview, "start", setup_then_close)
    gui.run(str(tmp_path / "config.yaml"), auto_attach=True, codex_config=str(codex))

    assert codex.read_bytes() == original
    assert not list(codex.parent.glob("*.model-router.bak"))
    assert not server.is_alive()
    assert not [t for t in threading.enumerate() if t not in threads_before
                and t.name == "model-router-codex"]


def test_gui_window_failure_still_restores_codex_and_stops_server(monkeypatch, tmp_path):
    codex = tmp_path / "codex.toml"
    original = b'model = "original"\n'
    codex.write_bytes(original)
    server = stub_desktop(monkeypatch, [])

    def fail_window(*args, **kwargs):
        assert 'model = "route-fastest"' in codex.read_text()
        raise RuntimeError("native window failed")

    monkeypatch.setattr(gui.webview, "create_window", fail_window)
    with pytest.raises(RuntimeError, match="native window failed"):
        gui.run(str(tmp_path / "config.yaml"), auto_attach=True, codex_config=str(codex))

    assert codex.read_bytes() == original
    assert not server.is_alive()


def test_gui_without_auto_attach_never_creates_codex_config(monkeypatch, tmp_path):
    server = stub_desktop(monkeypatch, [])
    gui.run(str(tmp_path / "config.yaml"), auto_attach=False)
    assert not (Path.home() / ".codex").exists()
    assert not server.is_alive()


def test_gui_does_not_open_window_when_server_is_not_ready(monkeypatch, tmp_path: Path):
    events = []
    FakeSession.restored = False
    FakeSession.started = False
    server = stub_desktop(monkeypatch, events)
    monkeypatch.setattr(gui, "_wait_ready", lambda *args: None)
    monkeypatch.setattr(gui, "_report_error", lambda message: events.append("error"))
    monkeypatch.setattr(gui, "CodexSession", FakeSession)

    gui.run(str(tmp_path / "config.yaml"), 8765, auto_attach=True)

    assert "window" not in events
    assert "error" in events
    assert not server.is_alive()
    assert not FakeSession.started
    assert not FakeSession.restored


def test_frozen_default_config_returns_app_config_path_for_in_app_setup(monkeypatch, tmp_path: Path):
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "config.example.yaml").write_text("public_model: route-fastest\n", encoding="utf-8")
    app_data = Path.home() / "Library" / "Application Support" / "ModelRouter"
    user_config = app_data / "config.yaml"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)
    assert gui._resolve_config_file("config.yaml") == str(user_config)
    assert user_config.exists()
    assert gui._load_config_for_app(str(user_config)).services == []


def test_wait_ready_rejects_unrelated_http_200(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"status": "ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    server = SimpleNamespace(is_alive=lambda: True)
    assert gui._wait_ready(8765, "this-instance", server, timeout=0.01) is None


def test_gui_does_not_attach_when_config_has_no_responses_model(monkeypatch, tmp_path: Path):
    stub_desktop(monkeypatch, [], {"responses_ready": False, "responses_configured": False})
    monkeypatch.setattr(gui, "CodexSession", FakeSession)
    FakeSession.started = False

    gui.run(str(tmp_path / "config.yaml"), 8765, auto_attach=True)

    assert not FakeSession.started


def test_missing_config_uses_empty_in_app_config(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config = gui._load_config_for_app(str(config_path))
    assert isinstance(config, Config)
    assert config.services == []


def test_gui_conflict_opens_workbench_and_does_not_show_error_window(monkeypatch, tmp_path):
    events = []
    class ConflictSession(FakeSession):
        def recover_previous(self):
            raise RuntimeError("Codex 配置已被外部修改")
    server = stub_desktop(monkeypatch, events)
    monkeypatch.setattr(gui, "CodexSession", ConflictSession)
    monkeypatch.setattr(gui, "_report_error", lambda message: events.append("error-window"))
    gui.run(str(tmp_path / "config.yaml"), 8765, auto_attach=True)
    assert events == ["server", "window", "close", "stop"]
    assert not server.is_alive()


def test_gui_reports_restore_conflict_after_window_closes(monkeypatch, tmp_path: Path):
    events = []

    class RestoreConflictSession(FakeSession):
        def restore(self):
            raise RuntimeError("Codex 配置已被外部修改")

    monkeypatch.setattr(gui, "CodexSession", RestoreConflictSession)
    server = stub_desktop(monkeypatch, events)
    monkeypatch.setattr(gui, "_report_error", lambda message: events.append(message))

    gui.run(str(tmp_path / "config.yaml"), 8765, auto_attach=True)

    assert events[:3] == ["server", "window", "close"]
    assert not server.is_alive()
    assert events == ["server", "window", "close", "stop"]


def test_monitor_updates_catalog_when_second_model_changes(tmp_path):
    monitor, config = make_monitor(tmp_path)
    first = {'models': [{'slug': 'route-fastest'}, {'slug': 'B/old'}]}
    second = {'models': [{'slug': 'route-fastest'}, {'slug': 'B/new'}]}
    try:
        monitor.sync(ready_status(codex_catalog=first))
        from model_router.codex_session import tomllib
        path = Path(tomllib.loads(config.read_text())['model_catalog_json'])
        assert gui.json.loads(path.read_text()) == first
        monitor.sync(ready_status(codex_catalog=second))
        path = Path(tomllib.loads(config.read_text())['model_catalog_json'])
        assert gui.json.loads(path.read_text()) == second
    finally:
        monitor.session.restore()


def test_workbench_reconnect_resumes_after_conflict_and_restores_new_baseline(monkeypatch, tmp_path):
    monitor, config = make_monitor(tmp_path)
    previous = gui.CodexSession(config)
    previous.start(8765, 'old-route')
    previous._release_lock()
    config.write_text(config.read_text().replace('127.0.0.1:8765', 'external.example:443'))
    baseline = config.read_bytes()
    monitor.error = 'external conflict'
    control = gui._CodexControl(monitor)
    assert control.get_codex_state()['state'] == 'paused'
    monkeypatch.setattr(gui, '_read_status', lambda *args: ready_status())
    try:
        assert control.reconnect_codex()['state'] == 'active'
        assert '127.0.0.1:8765' in config.read_text()
    finally:
        monitor.stop()
        monitor.session.restore()
    assert config.read_bytes() == baseline


def test_frozen_app_honours_explicit_config_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    explicit = tmp_path / "nested" / "config.yaml"
    explicit.parent.mkdir()
    explicit.write_text("public_model: route-fastest\n", encoding="utf-8")

    app_config = str(
        Path.home() / "Library" / "Application Support" / "ModelRouter" / "config.yaml"
    )

    assert gui._resolve_config_file(str(explicit), explicit=True) == str(explicit)
    # 未显式指定时仍以应用支持目录为准，避免双击启动时误读当前工作目录的 config.yaml
    assert gui._resolve_config_file(str(explicit)) == app_config
    assert gui._resolve_config_file("config.yaml") == app_config
