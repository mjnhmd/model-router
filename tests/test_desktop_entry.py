from model_router import desktop_entry


def test_packaged_entry_forwards_cli_options_to_gui(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(desktop_entry, "run", lambda **kwargs: calls.update(kwargs))

    desktop_entry.main(
        [
            "-c",
            "/tmp/nested/config.yaml",
            "-p",
            "9100",
            "--no-attach",
            "--codex-config",
            "/tmp/codex/config.toml",
        ]
    )

    assert calls == {
        "config_file": "/tmp/nested/config.yaml",
        "port": 9100,
        "auto_attach": False,
        "codex_config": "/tmp/codex/config.toml",
        "config_explicit": True,
    }


def test_packaged_entry_defaults_stay_on_app_managed_config(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(desktop_entry, "run", lambda **kwargs: calls.update(kwargs))

    desktop_entry.main([])

    assert calls["config_file"] == "config.yaml"
    assert calls["config_explicit"] is False
    assert calls["port"] == desktop_entry.DEFAULT_PORT
    assert calls["auto_attach"] is None


def test_packaged_entry_ignores_finder_injected_psn_argument(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(desktop_entry, "run", lambda **kwargs: calls.update(kwargs))

    desktop_entry.main(["-psn_0_123456", "--no-attach"])

    assert calls["auto_attach"] is False
    assert calls["config_file"] == "config.yaml"


def test_packaged_entry_runs_as_standalone_script():
    """PyInstaller 入口脚本没有父包，相对导入会让打包后的 App 直接崩溃。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    entry = Path(__file__).parents[1] / "src" / "model_router" / "desktop_entry.py"
    env = {**os.environ, "PYTHONPATH": str(entry.parents[1])}
    result = subprocess.run(
        [sys.executable, str(entry), "--help"], capture_output=True, text=True, env=env, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert "--no-attach" in result.stdout
