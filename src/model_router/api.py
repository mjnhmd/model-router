from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from collections import deque
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable, Mapping

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__ as source_version
from .config import Config, ServiceSpec, save_config
from .client_identity import client_headers as inbound_client_headers
from .codex_catalog import build_catalog
from .router import MAX_BENCH_TARGET_TOKENS, Router
from .state import RouterState
from .upstream import UpstreamClient
from .updates import check_for_update
from .translation import (
    translate_chat_request,
    translate_chat_response,
    translate_chat_stream,
    translate_responses_request,
    translate_responses_response,
    translate_responses_stream,
)

MAX_REQUEST_RETRIES = 3
REQUEST_RETRY_BUDGET_SECONDS = 30.0

try:
    # 打包后的 App 没有发行元数据，回落到包内版本，避免界面显示 0.0.0
    CURRENT_VERSION = package_version("model-router")
except PackageNotFoundError:
    CURRENT_VERSION = source_version

_PROCESS_STARTED_AT = time.time()


def _app_build_time() -> float:
    """打包 App 取运行中二进制的修改时间，用于发现磁盘已被新版本替换。

    源码模式返回 0：开发时随时改文件，不参与“需要重启”判定。
    """
    if not getattr(sys, "frozen", False):
        return 0.0
    try:
        return Path(sys.executable).stat().st_mtime
    except OSError:
        return 0.0


def _iso_time(stamp: float) -> str:
    if not stamp:
        return ""
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")

def create_app(
    config: Config,
    state_file: str | Path | None = None,
    config_file: str | Path | None = None,
    instance_id: str | None = None,
) -> FastAPI:
    router = Router(config, state=RouterState(str(state_file)) if state_file else None)
    bench_task: asyncio.Task | None = None
    bench_run_task: asyncio.Task | None = None
    bench_lock = asyncio.Lock()
    bench_state = {
        "running": False,
        "started_at": None,
        "finished_at": None,
        "last_result": None,
        "last_error": None,
        "scope": "当前已选模型",
        "rounds": 1,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "timed_out": 0,
        "total": len(router.enabled),
    }
    request_logs: deque[dict] = deque(maxlen=500)
    session_models: OrderedDict[str, str] = OrderedDict()
    session_models_lock = asyncio.Lock()
    request_stats = {
        "total_requests": 0,
        "success_requests": 0,
        "failed_requests": 0,
        "total_latency": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "models": {},
    }

    def record_request(
        endpoint: str,
        key: str,
        status_code: int,
        elapsed: float,
        stream: bool,
        ok: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str = "",
        hint: str = "",
    ) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        request_stats["total_requests"] += 1
        request_stats["success_requests"] += int(ok)
        request_stats["failed_requests"] += int(not ok)
        request_stats["total_latency"] += elapsed
        request_stats["total_input_tokens"] += input_tokens
        request_stats["total_output_tokens"] += output_tokens
        model_stats = request_stats["models"].setdefault(
            key,
            {
                "model": key,
                "requests": 0,
                "success_requests": 0,
                "failed_requests": 0,
                "total_latency": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        model_stats["requests"] += 1
        model_stats["success_requests"] += int(ok)
        model_stats["failed_requests"] += int(not ok)
        model_stats["total_latency"] += elapsed
        model_stats["input_tokens"] += input_tokens
        model_stats["output_tokens"] += output_tokens
        request_logs.appendleft(
            {
                "time": now,
                "endpoint": endpoint,
                "model": key,
                "status_code": status_code,
                "ok": ok,
                "stream": stream,
                "latency": round(elapsed, 3),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
                "hint": hint,
            }
        )

    def stats_snapshot() -> dict:
        total = request_stats["total_requests"]
        models = []
        for item in request_stats["models"].values():
            model = dict(item)
            model["avg_latency"] = round(
                item["total_latency"] / item["requests"], 3
            ) if item["requests"] else 0.0
            del model["total_latency"]
            models.append(model)
        models.sort(key=lambda item: (-item["requests"], item["model"]))
        return {
            "total_requests": total,
            "success_requests": request_stats["success_requests"],
            "failed_requests": request_stats["failed_requests"],
            "avg_latency": round(request_stats["total_latency"] / total, 3) if total else 0.0,
            "total_input_tokens": request_stats["total_input_tokens"],
            "total_output_tokens": request_stats["total_output_tokens"],
            "models": models,
        }

    def bench_keys(enabled_only: bool = True) -> list[str]:
        enabled = router.enabled
        return [
            ident.key
            for ident in router.identities
            if not enabled_only or ident.key in enabled
        ]

    def launch_bench(
        model_keys: list[str] | None = None,
        rounds: int | None = None,
        scope: str = "当前已选模型",
    ) -> asyncio.Task | None:
        nonlocal bench_run_task
        if bench_state["running"]:
            return None
        selected_keys = bench_keys() if model_keys is None else model_keys
        run_rounds = 1 if rounds is None else rounds
        target_tokens = min(router.config.bench_target_tokens, MAX_BENCH_TARGET_TOKENS)
        bench_state.update(
            {
                "running": True,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "finished_at": None,
                "last_result": None,
                "last_error": None,
                "scope": scope,
                "rounds": run_rounds,
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "timed_out": 0,
                "total": len(selected_keys) * 2,
            }
        )
        bench_run_task = asyncio.create_task(
            run_bench(selected_keys, run_rounds, target_tokens)
        )
        return bench_run_task

    async def run_bench(model_keys: list[str], rounds: int, target_tokens: int) -> None:
        async with bench_lock:
            def on_progress(key, result, completed, total):
                bench_state["completed"] = completed
                bench_state["succeeded"] += int(result.ok)
                bench_state["failed"] += int(not result.ok)
                bench_state["timed_out"] += int(result.error == "timeout")

            try:
                await router.run_bench_round(
                    model_keys=model_keys,
                    rounds=rounds,
                    target_tokens=target_tokens,
                    on_progress=on_progress,
                    adaptive=True,
                )
            except asyncio.CancelledError:
                bench_state["last_result"] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                bench_state["last_result"] = "failed"
                bench_state["last_error"] = str(exc)[:200]
            else:
                bench_state["last_result"] = "success"
            finally:
                bench_state["running"] = False
                bench_state["finished_at"] = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal bench_task, bench_run_task
        # 后台启动测速，不阻塞端口监听。评分数据就绪前路由会打回退默认值。
        bench_task = asyncio.create_task(_initial_bench_then_loop(router, launch_bench))
        yield
        bench_task.cancel()
        if bench_run_task is not None:
            bench_run_task.cancel()
        for task in (bench_task, bench_run_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await router.close()

    app = FastAPI(title="model-router", lifespan=lifespan)
    app.state.router = router

    @app.get("/v1/models")
    async def models() -> dict:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": item["id"], "object": "model", "created": 0, "owned_by": "model-router"}
                    for item in router.exposed_models()
                ],
            }
        )

    @app.get("/v1/status")
    async def status() -> dict:
        build_time = _app_build_time()
        responses = router.candidates_for(wire_api="responses", supports_reasoning=False)
        chat = router.candidates_for(wire_api="chat", supports_reasoning=False)
        exposed = router.exposed_models()
        exposed_ready = any(
            item["key"] in router.scores
            and router.scores[item["key"]].last_update > 0
            and router.scores[item["key"]].healthy
            for item in exposed
        )
        return JSONResponse(
            {
                "model_router": "model-router",
                "instance_id": instance_id,
                "version": CURRENT_VERSION,
                "app_build_time": _iso_time(build_time),
                "app_restart_required": bool(build_time and build_time > _PROCESS_STARTED_AT),
                "public_model": router.default_public_model(),
                "current_model": router.current_model(),
                "codex_enabled": router.config.codex.enabled,
                "codex_mode": router.config.codex.mode,
                "codex_usage_mode": router.config.codex.mode if router.config.codex.enabled else "none",
                "codex_catalog": build_catalog(exposed, router.status_snapshot()),
                "enabled_count": len(router.enabled),
                "responses_configured": bool(responses) or bool(exposed),
                "responses_ready": any(
                    router.scores[key].last_update > 0 and router.scores[key].healthy
                    for key in responses
                ) or exposed_ready,
                "recommended_models": {
                    "responses": router.pick_fastest(responses),
                    "chat": router.pick_fastest(chat),
                },
                "bench": dict(bench_state),
                "models": router.status_snapshot(),
            }
        )

    @app.get("/v1/config")
    async def get_config() -> dict:
        return JSONResponse(content=_public_config(router.config))

    @app.get("/v1/update")
    async def update_check() -> dict:
        return JSONResponse(content=await check_for_update(CURRENT_VERSION))

    @app.post("/v1/bench")
    async def manual_bench() -> dict:
        selected_keys = bench_keys(enabled_only=True)
        if not selected_keys:
            raise HTTPException(status_code=409, detail="请先勾选至少一个模型")
        task = launch_bench(
            model_keys=selected_keys,
            rounds=1,
            scope="当前已选模型",
        )
        if task is None:
            raise HTTPException(status_code=409, detail="测速正在进行中")
        return JSONResponse(
            status_code=202,
            content={"started": True, "bench": dict(bench_state)},
        )

    @app.post("/v1/bench/cancel")
    async def cancel_bench() -> dict:
        if not bench_state["running"] or bench_run_task is None:
            raise HTTPException(status_code=409, detail="当前没有正在进行的测速")
        bench_run_task.cancel()
        try:
            await bench_run_task
        except asyncio.CancelledError:
            pass
        return JSONResponse({"cancelled": True, "bench": dict(bench_state)})

    @app.get("/v1/logs")
    async def logs(limit: int = 100) -> dict:
        limit = max(1, min(limit, 500))
        return JSONResponse({"logs": list(request_logs)[:limit]})

    @app.get("/v1/stats")
    async def stats() -> dict:
        return JSONResponse(stats_snapshot())

    @app.put("/v1/config")
    async def update_config(req: Request) -> dict:
        nonlocal bench_task, bench_run_task
        if config_file is None:
            raise HTTPException(status_code=409, detail="当前启动方式未提供可写配置文件")
        raw = await req.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="配置必须是 JSON 对象")

        merged = router.config.model_dump()
        merged.update(raw)
        existing_services = list(router.config.services)
        if "services" in raw:
            if not isinstance(raw["services"], list):
                raise HTTPException(status_code=400, detail="services 必须是数组")
            services = []
            for service in raw["services"]:
                if not isinstance(service, dict):
                    services.append(service)
                    continue
                item = dict(service)
                existing = next((old for old in existing_services if (
                    old.id == item["id"] if item.get("id") else old.name == item.get("name")
                )), None)
                if existing is not None:
                    item["id"] = existing.id
                if not item.get("api_key"):
                    item["api_key"] = existing.api_key if existing else ""
                if not item.get("models"):
                    item["models"] = await _discover_model_specs(
                        item, router.config.timeout_seconds
                    )
                services.append(item)
            merged["services"] = services

        try:
            updated = Config.model_validate(merged)
            save_config(config_file, updated)
            for task in (bench_task, bench_run_task):
                if task is not None:
                    task.cancel()
            for task in (bench_task, bench_run_task):
                if task is not None:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            bench_task = None
            bench_run_task = None
            await router.apply_config(updated)
            session_models.clear()
            bench_task = asyncio.create_task(_initial_bench_then_loop(router, launch_bench))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(content=_public_config(updated))

    @app.post("/v1/services/discover")
    async def discover_service(req: Request) -> dict:
        raw = await req.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="服务必须是 JSON 对象")
        existing = next(
            (service for service in router.config.services if (
                service.id == raw["id"] if raw.get("id") else service.name == raw.get("name")
            )),
            None,
        )
        if not raw.get("api_key") and existing is not None:
            raw["api_key"] = existing.api_key
        try:
            models = await _discover_model_specs(raw, router.config.timeout_seconds)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"模型自动发现失败：{exc}") from exc
        return JSONResponse({"models": models})

    @app.post("/v1/models/toggle")
    async def toggle(req: Request) -> dict:
        body = await req.json()
        service = body.get("service")
        model = body.get("model")
        enabled = bool(body.get("enabled"))
        if not service or not model:
            raise HTTPException(status_code=400, detail="需要 service 和 model")
        changed = router.toggle_model(service, model, enabled)
        return JSONResponse({"ok": True, "changed": changed, "enabled": enabled})

    @app.post("/v1/models/toggle-batch")
    async def toggle_batch(req: Request) -> dict:
        body = await req.json()
        keys = body.get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise HTTPException(status_code=400, detail="keys 必须是模型 key 数组")
        if len(keys) > 5000:
            raise HTTPException(status_code=400, detail="一次最多操作 5000 个模型")
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
        changed = router.toggle_models(keys, enabled)
        return JSONResponse({"ok": True, "changed": changed, "count": len(keys), "enabled": enabled})

    @app.post("/v1/models/reorder")
    async def reorder_models(req: Request) -> dict:
        body = await req.json()
        keys = body.get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise HTTPException(status_code=400, detail="keys 必须是模型 key 数组")
        if len(keys) > 5000:
            raise HTTPException(status_code=400, detail="一次最多排序 5000 个模型")
        changed = router.set_priority(keys)
        return JSONResponse({"ok": True, "changed": changed})

    def _session_key(req_body: dict) -> str | None:
        if not router.config.stick_session_to_model:
            return None
        prompt_cache_key = req_body.get("prompt_cache_key")
        if isinstance(prompt_cache_key, str) and prompt_cache_key:
            digest = hashlib.sha256(prompt_cache_key.encode("utf-8")).hexdigest()
            return f"cache:{digest}"
        user = req_body.get("user")
        if isinstance(user, str) and user:
            digest = hashlib.sha256(user.encode("utf-8")).hexdigest()
            return f"user:{digest}"
        for message in _messages_of(req_body):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                text = " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") in ("text", "input_text")
                )
            else:
                text = str(content or "")
            if text:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                return f"message:{digest}"
        return None

    def _request_candidates(req_body: dict, wire_api: str) -> list[str]:
        mapped_key = router.resolve_public_model(str(req_body.get("model", "")))
        if mapped_key is not None:
            return router.candidates_for_keys(
                [mapped_key],
                needs_tools=True if req_body.get("tools") or req_body.get("tool_choice") else False,
                needs_vision=_needs_vision(req_body),
                context_tokens=Router.estimate_context_tokens(_messages_of(req_body)),
                supports_reasoning=_needs_reasoning(req_body),
            )
        return router.candidates_for(
            wire_api=wire_api,
            needs_tools=True if req_body.get("tools") or req_body.get("tool_choice") else False,
            needs_vision=_needs_vision(req_body),
            context_tokens=Router.estimate_context_tokens(_messages_of(req_body)),
            supports_reasoning=_needs_reasoning(req_body),
        )

    async def _pick_model(
        req_body: dict, wire_api: str, excluded_keys: set[str] | None = None
    ) -> str:
        excluded_keys = excluded_keys or set()
        candidates = [
            key for key in _request_candidates(req_body, wire_api)
            if key not in excluded_keys
        ]
        if not candidates:
            if router.config.codex.mode == "mapped" and req_body.get("model"):
                raise HTTPException(status_code=400, detail="所选模型不可用或不满足当前请求能力")
            raise HTTPException(status_code=503, detail="没有可用模型，请检查上游服务")
        session_key = _session_key(req_body)
        if session_key is not None:
            sticky_key = session_models.get(session_key)
            if sticky_key in candidates:
                score = router.scores.get(sticky_key)
                if score is not None and score.healthy:
                    session_models.move_to_end(session_key)
                    return sticky_key
            session_models.pop(session_key, None)
        chosen = router.pick_fastest(candidates)
        if chosen is None:
            raise HTTPException(status_code=503, detail="没有可用模型，请检查上游服务")
        if session_key is not None:
            async with session_models_lock:
                session_models[session_key] = chosen
                session_models.move_to_end(session_key)
                while len(session_models) > 10000:
                    session_models.popitem(last=False)
        return chosen

    async def _forward_selected(
        body: dict, wire_api: str, endpoint: str, chosen: str, inbound_headers: Mapping[str, str]
    ):
        service_name, model_name = chosen.split("/", 1)
        client = router.upstreams[service_name]
        headers = inbound_client_headers(inbound_headers)
        headers["Authorization"] = f"Bearer {client.service.api_key}"
        requested_model = body.get("model")
        outbound = dict(body)
        outbound["model"] = model_name
        target_endpoint = endpoint.removeprefix("/v1")
        if wire_api != client.service.wire_api:
            if wire_api == "responses" and client.service.wire_api == "chat":
                outbound = translate_responses_request(outbound)
                target_endpoint = "/chat/completions"
            elif wire_api == "chat" and client.service.wire_api == "responses":
                outbound = translate_chat_request(outbound)
                target_endpoint = "/responses"
        target = client.service.base_url.rstrip("/") + target_endpoint
        return await _forward(
            client,
            target,
            headers,
            outbound,
            router,
            chosen,
            client.service.wire_api,
            endpoint,
            record_request,
            requested_model=requested_model,
            inbound_wire_api=wire_api,
        )

    async def _handle(request: Request, wire_api: str, endpoint: str):
        body = await request.json()
        excluded_keys: set[str] = set()
        has_image = _needs_vision(body)
        last_response = None
        attempts = 0
        retry_deadline = time.monotonic() + min(
            float(router.config.timeout_seconds), REQUEST_RETRY_BUDGET_SECONDS
        )
        while True:
            if attempts >= MAX_REQUEST_RETRIES or time.monotonic() >= retry_deadline:
                if last_response is not None:
                    return last_response
                raise HTTPException(status_code=504, detail="上游重试超时，请稍后重试")
            try:
                chosen = await _pick_model(body, wire_api, excluded_keys)
            except HTTPException:
                if last_response is not None:
                    return last_response
                raise
            attempts += 1
            try:
                response = await asyncio.wait_for(
                    _forward_selected(body, wire_api, endpoint, chosen, request.headers),
                    timeout=max(0.001, retry_deadline - time.monotonic()),
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(status_code=504, detail="上游重试超时，请稍后重试") from exc
            except HTTPException as exc:
                if router.config.codex.mode == "mapped" and router.resolve_public_model(str(body.get("model", ""))):
                    raise exc
                score = router.scores.get(chosen)
                if score is not None:
                    score.observe(0, 0)
                excluded_keys.add(chosen)
                if any(k not in excluded_keys for k in _request_candidates(body, wire_api)):
                    continue
                raise exc
            last_response = response
            if router.config.codex.mode == "mapped" and router.resolve_public_model(str(body.get("model", ""))):
                return response
            retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
            vision_capability_error = has_image and response.status_code == 400 and _is_vision_capability_error(response)
            if not retryable and not vision_capability_error:
                if response.status_code != 400 or not has_image:
                    return response
            if not (retryable or vision_capability_error):
                return response
            excluded_keys.add(chosen)
            score = router.scores.get(chosen)
            if score is not None:
                score.observe(0, 0, is_timeout=response.status_code == 408)
            if vision_capability_error:
                router.mark_vision_unsupported(chosen)
            session_key = _session_key(body)
            if session_key is not None:
                session_models.pop(session_key, None)

    @app.post("/v1/responses")
    async def responses_handler(request: Request):
        return await _handle(request, "responses", "/v1/responses")

    @app.post("/v1/chat/completions")
    async def chat_handler(request: Request):
        return await _handle(request, "chat", "/v1/chat/completions")

    # 挂载 Web 控制台（单 HTML）
    import sys

    bundled_root = getattr(sys, "_MEIPASS", None)
    web_candidates = (
        [Path(bundled_root) / "web", Path(bundled_root) / "model_router" / "web"]
        if bundled_root
        else [Path(__file__).resolve().parent / "web", Path(__file__).resolve().parent.parent.parent / "web"]
    )
    web_dir = next((candidate for candidate in web_candidates if candidate.exists()), web_candidates[0])
    if web_dir.exists():
        app.mount("/console", StaticFiles(directory=str(web_dir), html=True), name="console")

    return app


def _public_config(config: Config) -> dict:
    data = config.model_dump()
    for service in data["services"]:
        service["api_key_set"] = bool(service.get("api_key"))
        service["api_key"] = ""
    return data


async def _discover_model_specs(raw: dict, timeout_seconds: int) -> list[dict]:
    service = {
        "name": raw.get("name", "discovery"),
        "base_url": raw.get("base_url", ""),
        "api_key": raw.get("api_key", ""),
        "wire_api": raw.get("wire_api", "responses"),
        "models": [{"name": "__discovery__"}],
    }
    client = UpstreamClient(ServiceSpec.model_validate(service), timeout_seconds)
    try:
        discovered = await client.list_models()
    finally:
        await client.close()
    specs = []
    for item in discovered:
        if isinstance(item, str):
            specs.append({"name": item})
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            spec = {"name": item["name"]}
            if isinstance(item.get("pricing"), dict):
                spec["pricing"] = item["pricing"]
            specs.append(spec)
    if not specs:
        raise ValueError("上游 /models 没有返回可用模型")
    return specs


def _messages_of(body: dict) -> list[dict]:
    if "messages" in body:
        return body["messages"]
    inp = body.get("input")
    if isinstance(inp, str):
        return [{"content": inp}]
    if isinstance(inp, list):
        msgs = []
        for item in inp:
            if isinstance(item, dict) and "content" in item:
                msgs.append(item)
        return msgs
    return []


def _needs_vision(body: dict) -> bool:
    def scan(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type") == "image_url" or value.get("type") == "input_image":
                return True
            return any(scan(v) for v in value.values())
        if isinstance(value, list):
            return any(scan(v) for v in value)
        return False

    return scan(body)


def _needs_reasoning(body: dict) -> bool:
    reasoning = body.get("reasoning")
    if reasoning is None:
        return False
    if isinstance(reasoning, dict):
        return reasoning.get("effort") not in (None, "none")
    return bool(reasoning)


def _usage_counts(data: dict, wire_api: str) -> tuple[int, int]:
    usage = data.get("usage") or {}
    if wire_api == "responses":
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


async def _forward(
    client: Any,
    target: str,
    headers: dict,
    body: dict,
    router: Router,
    key: str,
    wire_api: str,
    endpoint: str,
    record_request: Callable[..., None],
    requested_model: str | None = None,
    inbound_wire_api: str | None = None,
):
    stream = body.get("stream", False)
    started = time.monotonic()
    resp = None
    recorded = False
    leased = False
    score = router.scores.get(key)

    def record(
        status_code: int,
        ok: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str = "",
        hint: str = "",
    ) -> None:
        nonlocal recorded
        if recorded:
            return
        recorded = True
        record_request(
            endpoint,
            key,
            status_code,
            time.monotonic() - started,
            stream,
            ok,
            input_tokens,
            output_tokens,
            error,
            hint,
        )

    def observe(elapsed: float, out_tokens: int):
        tps = out_tokens / elapsed if elapsed > 0 else 0.0
        if score is not None:
            score.observe(tps, elapsed)

    try:
        await router.acquire_client(client)
        leased = True
        req = client._client.build_request("POST", target, json=body, headers=headers)
        resp = await client._client.send(req, stream=True)
        if resp.status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            record(
                resp.status_code,
                False,
                error=f"upstream_status_{resp.status_code}",
                hint=_upstream_error_hint(resp.status_code, text, headers),
            )
            return JSONResponse(status_code=resp.status_code, content=_safe_json(text))
        if not stream:
            try:
                data = await resp.aread()
                parsed = json.loads(data)
                if inbound_wire_api == "responses" and wire_api == "chat":
                    parsed = translate_chat_response(parsed, str(requested_model or parsed.get("model", "")))
                elif inbound_wire_api == "chat" and wire_api == "responses":
                    parsed = translate_responses_response(parsed, str(requested_model or parsed.get("model", "")))
                input_tokens, output_tokens = _usage_counts(parsed, wire_api)
                elapsed = time.monotonic() - started
                if output_tokens > 0:
                    observe(elapsed, output_tokens)
                elif score is not None:
                    score.observe_probe_ok(elapsed)
                record(resp.status_code, True, input_tokens, output_tokens)
                return JSONResponse(status_code=resp.status_code, content=parsed)
            except Exception as exc:  # noqa: BLE001
                record(502, False, error=str(exc)[:200])
                raise HTTPException(status_code=502, detail=f"上游响应无效: {exc}") from exc
            finally:
                await resp.aclose()
                await router.release_client(client)
                leased = False
        # 流式：延迟读取，交给生成器
        source = resp.aiter_bytes()
        if inbound_wire_api == "responses" and wire_api == "chat":
            stream_source = translate_chat_stream(source, str(requested_model or body.get("model", "")))
            stream_wire_api = "responses"
        elif inbound_wire_api == "chat" and wire_api == "responses":
            stream_source = translate_responses_stream(source, str(requested_model or body.get("model", "")))
            stream_wire_api = "chat"
        else:
            stream_source = None
            stream_wire_api = wire_api
        streaming_response = StreamingResponse(
            _sse_gen(
                resp,
                stream_wire_api,
                key,
                router,
                lambda ok, input_tokens, output_tokens, error: record(
                    200 if ok else 502,
                    ok,
                    input_tokens,
                    output_tokens,
                    error,
                ),
                on_release=lambda: router.release_client(client),
                score=score,
                source=stream_source,
            ),
            media_type="text/event-stream",
        )
        leased = False  # the generator owns the lease from here
        return streaming_response
    except httpx.HTTPError as e:
        if resp is not None:
            await resp.aclose()
        record(502, False, error=str(e)[:200])
        raise HTTPException(status_code=502, detail=f"上游错误: {e}") from e
    finally:
        if leased:
            await router.release_client(client)


def _sse_usage(line: bytes, wire_api: str) -> tuple[int, int, bool]:
    """从 SSE data 行提取输出 token 数：chat chunks 的 usage 或 responses 的 completed 事件。"""
    if not line.startswith(b"data: "):
        return 0, 0, False
    payload = line[6:]
    if payload == b"[DONE]":
        return 0, 0, True
    try:
        obj = json.loads(payload)
    except Exception:
        return 0, 0, False
    if wire_api == "chat":
        usage = obj.get("usage") or {}
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), False
    # responses 协议：流式最终事件 response.completed 带 usage
    if obj.get("type") == "response.completed":
        usage = (obj.get("response") or {}).get("usage") or {}
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), True
    return 0, 0, False


async def _sse_gen(
    resp,
    wire_api: str,
    key: str,
    router: Router,
    on_complete: Callable[[bool, int, int, str], None] | None = None,
    on_release: Callable[[], Any] | None = None,
    score: Any | None = None,
    source=None,
):
    buffer = b""
    started = time.monotonic()
    out_tokens = 0
    input_tokens = 0
    completed = False
    # 流式请求客户端断开也要保证评分不崩
    upstream_closed = False
    terminal_seen = False
    score = score if score is not None else router.scores.get(key)
    try:
        async for chunk in (source or resp.aiter_bytes()):
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.rstrip(b"\r")
                forwarded = raw_line + b"\n"
                if not line:
                    yield forwarded
                    continue
                yield forwarded
                # 统计 usage（可能在任一 chunk 出现，取累计最大值）
                usage_line = line if line.startswith(b"data: ") else line.replace(b"data:", b"data: ", 1)
                got_input, got_output, got_completed = _sse_usage(usage_line, wire_api)
                input_tokens = max(input_tokens, got_input)
                out_tokens = max(out_tokens, got_output)
                completed = completed or got_completed
                terminal_seen = terminal_seen or got_completed
            if terminal_seen and not buffer:
                break
        if buffer:
            yield buffer
    except (httpx.StreamClosed, httpx.ReadError):
        upstream_closed = True  # 客户端断开或上游中断
    finally:
        await resp.aclose()
        if on_release is not None:
            await on_release()
        elapsed = time.monotonic() - started
        # 只有收到终止/有完整计数才算有效观测；否则不惩罚（避免客户端主动断开误伤评分）
        if out_tokens > 0 and elapsed > 0:
            if score is not None:
                score.observe(out_tokens / elapsed, elapsed)
        if on_complete is not None:
            on_complete(
                completed,
                input_tokens,
                out_tokens,
                "" if completed else "stream ended before completion",
            )


_OFFICIAL_CLIENT_MARKERS = (
    "codex official client",
    "official clients",
    "only allows codex",
)


def _looks_like_codex_client(forwarded: Mapping[str, str]) -> bool:
    """只有真正带 Codex 标识的请求才算“已转发身份”；普通 SDK 的 UA 不算。"""
    if (forwarded.get("originator") or "").strip():
        return True
    agent = (forwarded.get("user-agent") or "").strip().lower()
    return agent.startswith("codex") or "codex_cli_rs" in agent


def _upstream_error_hint(status_code: int, text: str, forwarded: Mapping[str, str]) -> str:
    """把上游的准入类拒绝翻成可执行的中文说明；不改写上游响应正文。"""
    if status_code != 403 or not text:
        return ""
    lowered = text.lower()
    if not any(marker in lowered for marker in _OFFICIAL_CLIENT_MARKERS):
        return ""
    identity = _looks_like_codex_client(forwarded)
    if identity:
        return (
            "上游只放行 Codex 官方客户端：本次已带 Codex 标识仍被拒。"
            "若你用的就是 Codex，多半是运行中的 Model Router 还是旧进程，"
            "请退出并重新打开 App 后重试；手工构造的请求请改用 Codex 客户端，"
            "或换用不校验身份的服务。"
        )
    return (
        "上游只放行 Codex 官方客户端，而本次请求没有携带 Codex 客户端标识"
        "（Originator / User-Agent）；请从 Codex 客户端发起，或换用不校验身份的服务。"
    )


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {"error": {"message": text[:500], "type": "upstream_error"}}


def _is_vision_capability_error(response: JSONResponse) -> bool:
    try:
        payload = json.loads(response.body)
    except Exception:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    if "invalid_image" in code or "invalid image" in message:
        return False
    markers = (
        "vision not supported", "image input", "image modality",
        "does not support image", "image_not_supported", "image_url_not_supported",
    )
    return any(marker in code or marker in message for marker in markers)


async def _initial_bench_then_loop(
    router: Router,
    launch_bench: Callable[[], asyncio.Task | None] | None = None,
):
    if launch_bench is None:
        try:
            await router.run_bench_round(
                model_keys=list(router.enabled),
                rounds=1,
                target_tokens=MAX_BENCH_TARGET_TOKENS,
                adaptive=True,
            )
        except Exception:
            pass
    else:
        task = launch_bench()
        if task is not None:
            await task
    while True:
        await asyncio.sleep(router.config.bench_interval)
        if launch_bench is None:
            try:
                await router.run_bench_round(
                    model_keys=list(router.enabled),
                    rounds=1,
                    target_tokens=MAX_BENCH_TARGET_TOKENS,
                )
            except Exception:
                pass
        else:
            task = launch_bench()
            if task is not None:
                await task
