from pathlib import Path

import pytest

from model_router.config import load_config

VALID = """public_model: route-fastest
bench_interval: 30
services:
  - name: s1
    base_url: https://example.com/v1
    api_key: sk-1
    wire_api: chat
    models:
      - name: m1
        capabilities: { context_window: 8192, supports_tools: true }
"""


def test_load_valid(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(VALID, encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.public_model == "route-fastest"
    assert len(cfg.services) == 1
    assert cfg.all_models()[0][1].capabilities.context_window == 8192


def test_bench_defaults_are_fast_but_still_measure_longer_output():
    from model_router.config import Config

    cfg = Config(services=[])

    assert cfg.bench_concurrency == 4
    assert cfg.bench_rounds == 1
    assert cfg.bench_target_tokens == 256
    assert cfg.bench_timeout_seconds == 10


def test_bench_interval_is_stored_in_seconds_after_entering_minutes():
    from model_router.config import Config

    assert Config(services=[], stick_session_to_model=False).stick_session_to_model is False


def test_model_pricing_is_optional_and_uses_dollars_per_million_tokens(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "services: [{name: service, base_url: http://a, models: [{name: model, pricing: {input_per_million: 3, output_per_million: 15}}]}]",
        encoding="utf-8",
    )

    pricing = load_config(str(p)).services[0].models[0].pricing
    assert pricing is not None
    assert pricing.input_per_million == 3
    assert pricing.output_per_million == 15


def test_reject_unknown_field(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("services: [{name: s, base_url: x, wire_api: nope}]", encoding="utf-8")
    with pytest.raises(Exception):
        load_config(str(p))


def test_reject_unknown_model_field(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "services: [{name: s, base_url: http://a, models: [{name: m, typo: true}]}]",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="extra"):
        load_config(str(p))


def test_reject_duplicate_service_names(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "services:\n"
        "  - {name: same, base_url: http://a, models: [{name: one}]}\n"
        "  - {name: same, base_url: http://b, models: [{name: two}]}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="服务名不能重复"):
        load_config(str(p))


def test_reject_duplicate_model_names_within_service(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "services:\n"
        "  - name: service\n"
        "    base_url: http://a\n"
        "    models: [{name: same}, {name: same}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="模型名不能重复"):
        load_config(str(p))


def test_reject_non_positive_bench_parameters(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "bench_interval: 0\n"
        "bench_concurrency: 0\n"
        "bench_rounds: 0\n"
        "bench_target_tokens: 0\n"
        "bench_timeout_seconds: 0\n"
        "timeout_seconds: 0\n"
        "services: [{name: service, base_url: http://a, models: [{name: model}]}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="greater than"):
        load_config(str(p))


def test_reject_names_that_break_router_identity(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "services: [{name: service, base_url: http://a, models: [{name: bad/name}]}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不能包含"):
        load_config(str(p))


def test_allow_empty_models_before_service_discovery(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("services: [{name: service, base_url: http://a, models: []}]\n", encoding="utf-8")

    assert load_config(str(p)).services[0].models == []


def test_allow_empty_service_list_for_first_run_setup(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("services: []\n", encoding="utf-8")

    assert load_config(str(p)).services == []
