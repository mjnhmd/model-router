import asyncio

import pytest

from model_router.config import Config
from model_router.router import Router, ModelIdentity
from model_router.state import RouterState
from model_router.upstream import BenchResult
from model_router.upstream import BenchResult


def make_config(n_models=3):
    return Config(
        services=[
            {
                "name": "s1",
                "base_url": "http://x/v1",
                "api_key": "k",
                "wire_api": "chat",
                "models": [
                    {"name": f"m{i}", "capabilities": {"context_window": 16000, "supports_tools": True}}
                    for i in range(n_models)
                ],
            }
        ]
    )


def test_pick_fastest_returns_candidate(tmp_path):
    router = Router(Config.model_validate(make_config()), state=RouterState(str(tmp_path / "state.yaml")))
    router.scores["s1/m0"].observe(10, 1)
    router.scores["s1/m1"].observe(100, 1)
    router.scores["s1/m2"].observe(50, 1)
    cands = router.candidates_for(needs_tools=True)
    assert router.pick_fastest(cands) == "s1/m1"


def test_capability_filters_out_small_window(tmp_path):
    router = Router(Config.model_validate(make_config()), state=RouterState(str(tmp_path / "state.yaml")))
    router.scores["s1/m0"].observe(999, 1)
    cands = router.candidates_for(context_tokens=20000)
    assert "s1/m0" not in cands


def test_routes_across_multiple_services_and_models(tmp_path):
    config = Config.model_validate(
        Config(
            services=[
                {
                    "name": "service-a",
                    "base_url": "http://a/v1",
                    "api_key": "a",
                    "wire_api": "chat",
                    "models": [
                        {"name": "slow", "capabilities": {"context_window": 16000}},
                        {"name": "fast", "capabilities": {"context_window": 16000}},
                    ],
                },
                {
                    "name": "service-b",
                    "base_url": "http://b/v1",
                    "api_key": "b",
                    "wire_api": "responses",
                    "models": [
                        {"name": "medium", "capabilities": {"context_window": 16000}},
                        {"name": "fastest", "capabilities": {"context_window": 16000}},
                    ],
                },
            ]
        )
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))
    router.scores["service-a/slow"].observe(10, 1)
    router.scores["service-a/fast"].observe(30, 1)
    router.scores["service-b/medium"].observe(20, 1)
    router.scores["service-b/fastest"].observe(100, 1)

    assert set(router.candidates_for()) == {
        "service-a/slow",
        "service-a/fast",
        "service-b/medium",
        "service-b/fastest",
    }
    assert router.pick_fastest(router.candidates_for()) == "service-b/fastest"


def test_manual_pool_can_leave_one_model_enabled_across_services(tmp_path):
    router = Router(
        Config.model_validate(make_config(2)),
        state=RouterState(str(tmp_path / "state.yaml")),
    )
    router.toggle_model("s1", "m0", False)

    assert router.enabled == {"s1/m1"}
    assert router.candidates_for() == ["s1/m1"]


def test_toggle_write_failure_does_not_publish_memory_change(tmp_path, monkeypatch):
    state = RouterState(str(tmp_path / "state.yaml"))
    router = Router(Config.model_validate(make_config()), state=state)
    monkeypatch.setattr(state, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        router.toggle_model("s1", "m0", False)

    assert "s1/m0" in router.enabled
    assert not state.is_disabled("s1", "m0")


def test_bench_holds_retired_client_until_probe_finishes(tmp_path):
    router = Router(Config.model_validate(make_config()), state=RouterState(str(tmp_path / "state.yaml")))
    entered, release = asyncio.Event(), asyncio.Event()
    old = router.upstreams["s1"]
    async def probe(model, target):
        entered.set(); await release.wait()
        return BenchResult("s1", model.name, 1, 0.1, 10, True)
    old.bench = probe

    async def run():
        task = asyncio.create_task(router.run_bench_round(model_keys=["s1/m0"], target_tokens=8))
        await entered.wait()
        await router.apply_config(Config(services=[]))
        assert not old._client.is_closed
        release.set()
        await task
        assert old._client.is_closed
        await router.close()
    asyncio.run(run())


def test_candidates_can_be_restricted_to_request_wire_protocol(tmp_path):
    config = Config(
        services=[
            {
                "name": "chat-service",
                "base_url": "http://chat/v1",
                "wire_api": "chat",
                "models": [{"name": "chat-model"}],
            },
            {
                "name": "responses-service",
                "base_url": "http://responses/v1",
                "wire_api": "responses",
                "models": [{"name": "responses-model"}],
            },
        ]
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))

    assert router.candidates_for(wire_api="chat") == ["chat-service/chat-model"]
    assert router.candidates_for(wire_api="responses") == ["responses-service/responses-model"]


def test_unknown_vision_capability_remains_eligible_for_image_requests(tmp_path):
    config = Config(
        services=[
            {
                "name": "service",
                "base_url": "http://service/v1",
                "wire_api": "responses",
                "models": [{"name": "auto-discovered"}],
            }
        ]
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))

    assert router.candidates_for(wire_api="responses", needs_vision=True) == [
        "service/auto-discovered"
    ]


def test_explicitly_non_vision_model_is_filtered_for_image_requests(tmp_path):
    config = Config(
        services=[
            {
                "name": "service",
                "base_url": "http://service/v1",
                "wire_api": "responses",
                "models": [
                    {"name": "text-only", "capabilities": {"supports_vision": False}},
                    {"name": "vision", "capabilities": {"supports_vision": True}},
                ],
            }
        ]
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))

    assert router.candidates_for(wire_api="responses", needs_vision=True) == [
        "service/vision"
    ]


def test_non_reasoning_model_is_eligible_when_request_does_not_require_reasoning(tmp_path):
    config = Config(
        services=[
            {
                "name": "service",
                "base_url": "http://service/v1",
                "wire_api": "chat",
                "models": [
                    {
                        "name": "fast-chat",
                        "capabilities": {"supports_reasoning": False},
                    }
                ],
            }
        ]
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))

    assert router.candidates_for(wire_api="chat", supports_reasoning=False) == [
        "service/fast-chat"
    ]


def test_pick_fastest_always_uses_speed_after_pool_reorder(tmp_path):
    state_file = tmp_path / "state.yaml"
    router = Router(Config.model_validate(make_config(2)), state=RouterState(str(state_file)))
    router.scores["s1/m0"].tps = 10
    router.scores["s1/m1"].tps = 100

    router.set_priority(["s1/m0", "s1/m1"])

    assert router.pick_fastest(router.candidates_for()) == "s1/m1"
    restored = RouterState(str(state_file))
    assert restored.priority == ["s1/m0", "s1/m1"]


def test_all_degraded_models_still_leave_a_routing_fallback(tmp_path):
    router = Router(
        Config.model_validate(make_config(2)),
        state=RouterState(str(tmp_path / "state.yaml")),
    )
    for score in router.scores.values():
        score.error_rate = 0.9
    router.scores["s1/m1"].tps = 10
    router.scores["s1/m0"].tps = 20

    candidates = router.candidates_for()

    assert candidates == ["s1/m0", "s1/m1"]
    assert router.pick_fastest(candidates) == "s1/m0"


def test_bench_can_limit_models_and_report_progress(tmp_path):
    router = Router(
        Config.model_validate(make_config(3)),
        state=RouterState(str(tmp_path / "state.yaml")),
    )
    calls = []
    progress = []

    async def fake_bench(model, target_tokens):
        calls.append((model.name, target_tokens))
        return BenchResult("s1", model.name, 64, 0.5, 128.0, True)

    router.upstreams["s1"].bench = fake_bench

    async def run():
        await router.run_bench_round(
            model_keys=["s1/m1"],
            rounds=1,
            on_progress=lambda key, result, completed, total: progress.append(
                (key, result.ok, completed, total)
            ),
        )
        await router.close()

    asyncio.run(run())

    assert calls == [("m1", 256)]
    assert progress == [("s1/m1", True, 1, 1)]


def test_bench_timeout_isolated_from_real_request_timeout(tmp_path):
    config = Config.model_validate(
        {
            **make_config(1).model_dump(),
            "bench_timeout_seconds": 0.01,
        }
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))
    progress = []

    async def fake_bench(model, target_tokens):
        await asyncio.sleep(0.05)
        return BenchResult("s1", model.name, 64, 0.05, 128.0, True)

    router.upstreams["s1"].bench = fake_bench

    async def run():
        await router.run_bench_round(
            model_keys=["s1/m0"],
            rounds=1,
            on_progress=lambda key, result, completed, total: progress.append(result),
        )
        await router.close()

    asyncio.run(run())

    assert len(progress) == 1
    assert progress[0].ok is False
    assert progress[0].error == "timeout"


def test_bench_timeout_is_capped_at_ten_seconds(tmp_path, monkeypatch):
    config = Config.model_validate(
        {
            **make_config(1).model_dump(),
            "bench_timeout_seconds": 30,
        }
    )
    router = Router(config, state=RouterState(str(tmp_path / "state.yaml")))
    timeouts = []

    async def fake_bench(model, target_tokens):
        return BenchResult("s1", model.name, 64, 0.5, 128.0, True)

    async def fake_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    router.upstreams["s1"].bench = fake_bench
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    async def run():
        await router.run_bench_round(model_keys=["s1/m0"], rounds=1)
        await router.close()

    asyncio.run(run())

    assert timeouts == [10]


def test_bench_without_model_keys_only_tests_enabled_models(tmp_path):
    router = Router(
        Config.model_validate(make_config(2)),
        state=RouterState(str(tmp_path / "state.yaml")),
    )
    router.toggle_model("s1", "m1", False)
    calls = []

    async def fake_bench(model, target_tokens):
        calls.append(model.name)
        return BenchResult("s1", model.name, 64, 0.5, 128.0, True)

    router.upstreams["s1"].bench = fake_bench

    async def run():
        await router.run_bench_round(rounds=1)
        await router.close()

    asyncio.run(run())

    assert calls == ["m0"]
