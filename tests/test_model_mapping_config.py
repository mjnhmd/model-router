from pathlib import Path

import pytest

from model_router.config import Config, load_config
from model_router.state import RouterState


def test_codex_defaults_to_disabled_fastest_mode():
    cfg = Config(services=[])
    assert cfg.codex.enabled is False
    assert cfg.codex.mode == "fastest"
    assert cfg.codex.models == []


def test_codex_mapped_models_validate_stable_keys(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "codex:\n"
        "  enabled: true\n"
        "  mode: mapped\n"
        "  models: [service-a/gpt-5, service-b/sonnet]\n"
        "services: []\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.codex.models == ["service-a/gpt-5", "service-b/sonnet"]


def test_codex_rejects_duplicate_or_malformed_mapping_keys():
    with pytest.raises(ValueError, match="映射"):
        Config(
            services=[],
            codex={"mode": "mapped", "models": ["service-a/model", "service-a/model"]},
        )
    with pytest.raises(ValueError, match="映射"):
        Config(services=[], codex={"mode": "mapped", "models": ["model-only"]})


def test_mapped_models_are_independent_from_disabled_state(tmp_path: Path):
    state = RouterState(str(tmp_path / "state.yaml"))
    state.set_mapped_models(["service-a/model"])
    state.set_disabled("service-a", "model", True)
    restored = RouterState(str(tmp_path / "state.yaml"))
    assert restored.mapped_models == {"service-a/model"}
    assert restored.is_disabled("service-a", "model")
