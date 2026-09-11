import asyncio
from collections import defaultdict

from model_router.config import Config
from model_router.router import Router
from model_router.upstream import BenchResult


def test_adaptive_benchmark_screens_all_then_precisely_measures_successes():
    router = Router(Config(services=[{"id": "s", "name": "S", "base_url": "http://s/v1", "models": [{"name": "fast"}, {"name": "down"}]}]))
    calls = []

    async def fake_bench(model, target_tokens, phase="precise"):
        calls.append((model.name, target_tokens, phase))
        if model.name == "down" and phase == "screen":
            return BenchResult("S", model.name, 0, 0.2, 0, False, "timeout", phase="screen")
        return BenchResult("S", model.name, target_tokens, 0.2, 100, True, phase=phase)

    router.upstreams["s"].bench = fake_bench
    asyncio.run(router.run_bench_round(adaptive=True))
    assert [(name, phase) for name, _, phase in calls] == [("fast", "screen"), ("down", "screen"), ("fast", "precise")]
    asyncio.run(router.close())


def test_adaptive_benchmark_caps_per_service_concurrency():
    router = Router(Config(bench_concurrency=8, services=[{"id": "s", "name": "S", "base_url": "http://s/v1", "models": [{"name": str(i)} for i in range(6)]}]))
    active = 0
    peak = 0

    async def fake_bench(model, target_tokens, phase="precise"):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return BenchResult("S", model.name, target_tokens, 0.1, 100, True, phase=phase)

    router.upstreams["s"].bench = fake_bench
    asyncio.run(router.run_bench_round(adaptive=True))
    assert peak <= 2
    asyncio.run(router.close())


def test_benchmark_snapshot_survives_router_restart(tmp_path):
    from model_router.state import RouterState
    state_path = tmp_path / "state.yaml"
    state = RouterState(str(state_path))
    state.set_benchmark("s/m", {"tps": 88.0, "latency": 0.4, "measured_at": 123.0})
    restored = RouterState(str(state_path))
    assert restored.benchmarks["s/m"]["tps"] == 88.0
