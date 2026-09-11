from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config, ModelPricing
from .scoring import ModelScore
from .state import RouterState
from .upstream import BenchResult, UpstreamClient

MAX_BENCH_TARGET_TOKENS = 256
MAX_BENCH_TIMEOUT_SECONDS = 10.0


@dataclass
class ModelIdentity:
    service_name: str
    model_name: str
    wire_api: str
    context_window: int
    supports_tools: bool
    supports_vision: bool | None
    supports_reasoning: bool
    pricing: ModelPricing | None = None
    service_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.service_id or self.service_name}/{self.model_name}"


class Router:
    """按能力过滤 + 实时评分选最快模型。"""

    def __init__(self, config: Config, state: RouterState | None = None):
        self.config = config
        self.state = state or RouterState()
        self.upstreams: dict[str, UpstreamClient] = {
            s.id: UpstreamClient(s, config.timeout_seconds) for s in config.services
        }
        self.identities: list[ModelIdentity] = []
        self.scores: dict[str, ModelScore] = {}
        self.enabled: set[str] = set()
        self.vision_unsupported: set[str] = set()
        self._active_clients: dict[UpstreamClient, int] = {}
        self._retired_upstreams: set[UpstreamClient] = set()
        for s in config.services:
            for m in s.models:
                key = f"{s.id}/{m.name}"
                self.identities.append(
                    ModelIdentity(
                        service_name=s.name,
                        service_id=s.id,
                        model_name=m.name,
                        wire_api=s.wire_api,
                        context_window=m.capabilities.context_window,
                        supports_tools=m.capabilities.supports_tools,
                        supports_vision=m.capabilities.supports_vision,
                        supports_reasoning=m.capabilities.supports_reasoning,
                        pricing=m.pricing,
                    )
                )
                self.scores[key] = ModelScore(key)
                snapshot = self.state.benchmarks.get(key)
                if snapshot:
                    score = self.scores[key]
                    score.tps = float(snapshot.get("tps", score.tps))
                    score.latency = float(snapshot.get("latency", score.latency))
                    score.time_to_first_token = float(snapshot.get("time_to_first_token", 0.0))
                    score.sample_count = int(snapshot.get("sample_count", 0))
                    score.last_update = float(snapshot.get("measured_at", 0.0))
                if not self.state.is_disabled(s.id, m.name):
                    self.enabled.add(key)

    async def close(self) -> None:
        for u in set(self.upstreams.values()) | self._retired_upstreams:
            await u.close()
        self._retired_upstreams.clear()

    async def acquire_client(self, client: UpstreamClient) -> None:
        self._active_clients[client] = self._active_clients.get(client, 0) + 1

    async def release_client(self, client: UpstreamClient) -> None:
        count = self._active_clients.get(client, 0) - 1
        if count > 0:
            self._active_clients[client] = count
            return
        self._active_clients.pop(client, None)
        if client in self._retired_upstreams:
            self._retired_upstreams.remove(client)
            await client.close()

    async def apply_config(self, config: Config) -> None:
        """Replace upstreams and routing identities after a validated config save."""
        old_upstreams = self.upstreams
        self.config = config
        self.upstreams = {
            service.id: UpstreamClient(service, config.timeout_seconds)
            for service in config.services
        }
        self.identities = []
        self.scores = {}
        self.enabled = set()
        self.vision_unsupported = set()
        for service in config.services:
            for model in service.models:
                key = f"{service.id}/{model.name}"
                self.identities.append(
                    ModelIdentity(
                        service_name=service.name,
                        service_id=service.id,
                        model_name=model.name,
                        wire_api=service.wire_api,
                        context_window=model.capabilities.context_window,
                        supports_tools=model.capabilities.supports_tools,
                        supports_vision=model.capabilities.supports_vision,
                        supports_reasoning=model.capabilities.supports_reasoning,
                        pricing=model.pricing,
                    )
                )
                self.scores[key] = ModelScore(key)
                snapshot = self.state.benchmarks.get(key)
                if snapshot:
                    score = self.scores[key]
                    score.tps = float(snapshot.get("tps", score.tps))
                    score.latency = float(snapshot.get("latency", score.latency))
                    score.time_to_first_token = float(snapshot.get("time_to_first_token", 0.0))
                    score.sample_count = int(snapshot.get("sample_count", 0))
                    score.last_update = float(snapshot.get("measured_at", 0.0))
                if not self.state.is_disabled(service.id, model.name):
                    self.enabled.add(key)
        self._retired_upstreams.update(old_upstreams.values())
        await asyncio.gather(*(
            self.release_client(upstream)
            for upstream in old_upstreams.values()
            if self._active_clients.get(upstream, 0) == 0
        ))

    @staticmethod
    def estimate_context_tokens(messages: list[dict]) -> int:
        """粗略估算上下文 token 数：中文约 1 token/字，英文约 0.25 token/字符。"""
        total = 0
        for m in messages:
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            text = str(content)
            # 粗估：中文 1 字≈1 token
            total += len(text)
        return total

    def candidates_for(
        self,
        *,
        wire_api: str | None = None,
        needs_tools: bool = False,
        needs_vision: bool = False,
        context_tokens: int = 0,
        supports_reasoning: bool = True,
    ) -> list[str]:
        out = []
        for ident in self.identities:
            if wire_api is not None:
                service = next(s for s in self.config.services if s.name == ident.service_name)
                if service.wire_api != wire_api:
                    continue
            if needs_tools and not ident.supports_tools:
                continue
            if needs_vision and (
                ident.supports_vision is False
                or ident.key in self.vision_unsupported
            ):
                continue
            if context_tokens > ident.context_window:
                continue
            if supports_reasoning and not ident.supports_reasoning:
                continue
            key = ident.key
            s = self.scores[key]
            if key not in self.enabled:
                continue
            # 健康状态由 pick_fastest 统一处理：有健康模型时优先健康模型；
            # 全部暂时不健康时保留降级候选，避免直接把请求变成 503。
            out.append(key)
        return out

    def resolve_public_model(self, public_model: str) -> str | None:
        """Resolve a displayed mapped model name to its stable service/model key."""
        if self.config.codex.mode != "mapped":
            return None
        selected = set(self.config.codex.models)
        for ident in self.identities:
            if ident.key not in selected:
                continue
            if public_model in (ident.key, f"{ident.service_name}/{ident.model_name}"):
                return ident.key
        return None

    def exposed_models(self) -> list[dict]:
        """Return the models visible to Codex in the current integration mode."""
        if self.config.codex.mode != "mapped":
            return [{"id": self.config.public_model, "key": self.config.public_model}]
        selected = set(self.config.codex.models)
        out = []
        for ident in self.identities:
            if ident.key not in selected:
                continue
            score = self.scores[ident.key]
            out.append({
                "id": f"{ident.service_name}/{ident.model_name}",
                "key": ident.key,
                "service": ident.service_name,
                "service_id": ident.service_id,
                "model": ident.model_name,
                "wire_api": ident.wire_api,
                "tps": score.tps,
                "latency": score.latency,
                "healthy": score.healthy,
            })
        return out

    def default_public_model(self) -> str:
        if self.config.codex.mode == "mapped":
            models = self.exposed_models()
            if models:
                return str(models[0]["id"])
        return self.config.public_model

    def candidates_for_keys(self, keys: list[str], *, needs_tools: bool = False,
                            needs_vision: bool = False, context_tokens: int = 0,
                            supports_reasoning: bool = True) -> list[str]:
        allowed = set(keys)
        out = []
        for ident in self.identities:
            key = ident.key
            if key not in allowed:
                continue
            if needs_tools and not ident.supports_tools:
                continue
            if needs_vision and (ident.supports_vision is False or key in self.vision_unsupported):
                continue
            if context_tokens > ident.context_window:
                continue
            if supports_reasoning and not ident.supports_reasoning:
                continue
            out.append(key)
        return out

    def mark_vision_unsupported(self, key: str) -> None:
        """记住上游拒绝图片请求，避免后续图片请求重复命中该模型。"""
        if key in self.scores:
            self.vision_unsupported.add(key)

    def pick_fastest(self, candidates: list[str]) -> Optional[str]:
        if not candidates:
            return None
        # 排除刚失败的模型
        fresh = [k for k in candidates if self.scores[k].healthy]
        pool = fresh or candidates
        return max(pool, key=lambda k: self.scores[k].tps)

    def set_priority(self, keys: list[str]) -> bool:
        valid = [key for key in keys if key in self.scores]
        existing = [key for key in self.state.priority if key in self.scores and key not in valid]
        return self.state.set_priority(valid + existing)

    def toggle_model(self, service: str, model: str, enabled: bool) -> bool:
        """启用/停用某模型。返回是否真的改变了状态。"""
        service = next((item.id for item in self.config.services if item.name == service), service)
        key = f"{service}/{model}"
        if key not in self.scores:
            return False
        if enabled:
            changed = key not in self.enabled
            self.state.set_disabled(service, model, False)
            self.enabled.add(key)
        else:
            changed = key in self.enabled
            self.state.set_disabled(service, model, True)
            self.enabled.discard(key)
        return changed

    def toggle_models(self, keys: list[str], enabled: bool) -> int:
        valid_keys = {key for key in keys if key in self.scores}
        changed = 0
        self.state.set_disabled_many(valid_keys, not enabled)
        if enabled:
            for key in valid_keys:
                if key not in self.enabled:
                    self.enabled.add(key)
                    changed += 1
        else:
            for key in valid_keys:
                if key in self.enabled:
                    self.enabled.remove(key)
                    changed += 1
        return changed

    def current_model(self) -> str | None:
        """当前路由选中的模型（供状态页显示）。"""
        cands = [k for k in self.enabled if k in self.scores]
        return self.pick_fastest(cands)

    def status_snapshot(self) -> list[dict]:
        """返回所有模型的实时状态（供 /v1/status 展示）。"""
        out = []
        priority = {key: index + 1 for index, key in enumerate(self.state.priority)}
        for ident in self.identities:
            key = ident.key
            s = self.scores[key]
            out.append(
                {
                    "service": ident.service_name,
                    "service_id": ident.service_id,
                    "last_update": s.last_update,
                    "model": ident.model_name,
                    "wire_api": ident.wire_api,
                    "key": key,
                    "enabled": key in self.enabled,
                    "healthy": s.healthy,
                    "tps": round(s.tps, 1),
                    "latency": round(s.latency, 2),
                    "time_to_first_token": round(s.time_to_first_token, 3),
                    "sample_count": s.sample_count,
                    "error_rate": round(s.error_rate, 2),
                    "total_requests": s.total_requests,
                    "failed_requests": s.failed_requests,
                    "context_window": ident.context_window,
                    "supports_tools": ident.supports_tools,
                    "supports_vision": (
                        False
                        if key in self.vision_unsupported
                        else ident.supports_vision
                    ),
                    "supports_reasoning": ident.supports_reasoning,
                    "pricing": ident.pricing.model_dump(exclude_none=True) if ident.pricing else None,
                    "priority": priority.get(key),
                }
            )
        return out

    async def run_bench_round(
        self,
        model_keys: list[str] | None = None,
        rounds: int | None = None,
        target_tokens: int | None = None,
        on_progress: Callable[[str, BenchResult, int, int], None] | None = None,
        adaptive: bool = False,
    ) -> None:
        """运行指定模型的测速，并在每个探针结束后报告进度。"""
        identities = {
            ident.key: ident for ident in self.identities
        }
        selected = set(model_keys) if model_keys is not None else self.enabled
        keys = [key for key in identities if key in selected]
        run_rounds = self.config.bench_rounds if rounds is None else rounds
        if run_rounds <= 0:
            raise ValueError("测速轮数必须大于 0")
        probe_tokens = self.config.bench_target_tokens if target_tokens is None else target_tokens
        if probe_tokens <= 0:
            raise ValueError("测速目标 token 数必须大于 0")
        probe_tokens = min(probe_tokens, MAX_BENCH_TARGET_TOKENS)
        probe_timeout = min(self.config.bench_timeout_seconds, MAX_BENCH_TIMEOUT_SECONDS)
        if adaptive and run_rounds != 1:
            run_rounds = 1
        total = len(keys) * (2 if adaptive else run_rounds)
        if not total:
            return

        sem = asyncio.Semaphore(min(max(1, self.config.bench_concurrency), 8))
        service_sems = {service.id: asyncio.Semaphore(2) for service in self.config.services}
        completed = 0

        async def run_phase(phase_keys: list[str], tokens: int, phase: str) -> list[tuple[str, BenchResult]]:
            nonlocal completed
            phase_results: list[tuple[str, BenchResult]] = []
            phase_total = len(phase_keys)

            async def one(key: str) -> None:
                nonlocal completed
                ident = identities[key]
                service_name = ident.service_name
                model_name = ident.model_name
                spec = next(m for s in self.config.services if s.id == ident.service_id for m in s.models if m.name == model_name)
                async with sem, service_sems[ident.service_id]:
                    client = self.upstreams[ident.service_id or service_name]
                    score = self.scores.get(key)
                    await self.acquire_client(client)
                    try:
                        res = await asyncio.wait_for(client.bench(spec, tokens, phase=phase), timeout=probe_timeout)
                    except asyncio.TimeoutError:
                        res = BenchResult(service_name, model_name, 0, probe_timeout, 0.0, False, "timeout", phase=phase)
                    finally:
                        await self.release_client(client)
                    if score is not None:
                        if res.ok and res.tokens_per_sec > 0:
                            score.observe(res.tokens_per_sec, res.elapsed, time_to_first_token=res.time_to_first_token)
                        elif res.ok:
                            score.observe_probe_ok(res.elapsed)
                        else:
                            score.observe(res.tokens_per_sec, res.elapsed, is_timeout=(res.error == "timeout"))
                        if res.ok:
                            self.state.set_benchmark(key, {
                                "tps": score.tps,
                                "latency": score.latency,
                                "time_to_first_token": score.time_to_first_token,
                                "sample_count": score.sample_count,
                                "measured_at": score.last_update,
                            })
                    phase_results.append((key, res))
                    completed += 1
                    if on_progress is not None:
                        on_progress(key, res, completed, total)

            await asyncio.gather(*(one(key) for key in phase_keys))
            return phase_results

        if adaptive:
            screen_tokens = min(64, probe_tokens)
            screened = await run_phase(keys, screen_tokens, "screen")
            viable = [key for key, result in screened if result.ok]
            if viable:
                precise_tokens = max(128, min(probe_tokens, MAX_BENCH_TARGET_TOKENS))
                await run_phase(viable, precise_tokens, "precise")
            return

        tasks = []
        async def one(key: str) -> None:
            nonlocal completed
            ident = identities[key]
            service_name = ident.service_name
            model_name = ident.model_name
            spec = next(
                m for s in self.config.services if s.name == service_name
                for m in s.models if m.name == model_name
            )
            async with sem:
                for _ in range(run_rounds):
                    client = self.upstreams[ident.service_id or service_name]
                    score = self.scores.get(key)
                    await self.acquire_client(client)
                    try:
                        res = await asyncio.wait_for(
                            client.bench(spec, probe_tokens),
                            timeout=probe_timeout,
                        )
                    except asyncio.TimeoutError:
                        res = BenchResult(
                            service_name,
                            model_name,
                            0,
                            probe_timeout,
                            0.0,
                            False,
                            "timeout",
                        )
                    finally:
                        await self.release_client(client)
                    if score is not None:
                        if res.ok and res.tokens_per_sec > 0:
                            score.observe(res.tokens_per_sec, res.elapsed)
                        elif res.ok:
                            # 探测成功但首块无 usage：只记延迟，不判失败
                            score.observe_probe_ok(res.elapsed)
                        else:
                            score.observe(
                                res.tokens_per_sec, res.elapsed,
                                is_timeout=(res.error == "timeout"),
                            )
                    completed += 1
                    if on_progress is not None:
                        on_progress(key, res, completed, total)

        for key in keys:
            tasks.append(asyncio.create_task(one(key)))
        await asyncio.gather(*tasks)
