from pathlib import Path

import pytest

from model_router.codex_attach import attach, detach
from model_router.codex_session import CodexConfigConflictError


def test_attach_and_detach_round_trip_preserves_original_bytes(tmp_path: Path):
    codex = tmp_path / "config.toml"
    original = (
        b"# keep\nmodel = \"old\"\nmodel_provider = \"custom\"\n\n"
        b"[model_providers.custom]\nname = \"custom\"\n"
    )
    codex.write_bytes(original)

    attach(port=8765, config_file=str(tmp_path / "missing.yaml"), codex_config=str(codex))
    managed = codex.read_text(encoding="utf-8")
    assert 'model = "route-fastest"' in managed
    assert 'model_provider = "local_router"' in managed
    assert '[model_providers.custom]' in managed
    assert (tmp_path / "config.toml.model-router.bak").exists()

    detach(codex_config=str(codex))
    assert codex.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()


def test_detach_refuses_to_overwrite_external_edit(tmp_path: Path):
    codex = tmp_path / "config.toml"
    codex.write_text('model = "old"\n', encoding="utf-8")
    attach(port=8765, config_file=str(tmp_path / "missing.yaml"), codex_config=str(codex))
    codex.write_text('model = "edited"\n', encoding="utf-8")

    with pytest.raises(CodexConfigConflictError, match="外部修改"):
        detach(codex_config=str(codex))

    assert codex.read_text(encoding="utf-8") == 'model = "edited"\n'
    assert (tmp_path / "config.toml.model-router.bak").exists()


def test_attach_rejects_invalid_toml_without_writing(tmp_path: Path):
    codex = tmp_path / "config.toml"
    original = b"[broken\n"
    codex.write_bytes(original)

    with pytest.raises(ValueError, match="TOML"):
        attach(port=8765, config_file=str(tmp_path / "missing.yaml"), codex_config=str(codex))

    assert codex.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()


def test_attach_rejects_invalid_router_yaml_without_writing(tmp_path: Path):
    codex = tmp_path / "config.toml"
    original = b'model = "old"\n'
    codex.write_bytes(original)
    router_config = tmp_path / "config.yaml"
    router_config.write_text("services: [", encoding="utf-8")

    with pytest.raises(ValueError, match="配置"):
        attach(port=8765, config_file=str(router_config), codex_config=str(codex))

    assert codex.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()
