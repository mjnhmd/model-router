import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from model_router.api import create_app
from model_router.config import Config


def mapped_config():
    return Config(
        codex={"enabled": True, "mode": "mapped", "models": ["a/gpt", "b/sonnet"]},
        services=[
            {"id": "a", "name": "A", "base_url": "http://a/v1", "wire_api": "responses", "models": [{"name": "gpt"}]},
            {"id": "b", "name": "B", "base_url": "http://b/v1", "wire_api": "responses", "models": [{"name": "sonnet"}]},
        ],
    )


def test_models_endpoint_exposes_selected_mapped_models(tmp_path):
    app = create_app(mapped_config(), state_file=tmp_path / "state.yaml")

    async def run():
        with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                response = await client.get("/v1/models")
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert [item["id"] for item in response.json()["data"]] == ["A/gpt", "B/sonnet"]


def test_mapped_request_targets_exact_upstream_without_fallback(tmp_path):
    app = create_app(mapped_config(), state_file=tmp_path / "state.yaml")
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, request=request, json={"id": "ok", "output": []})

    app.state.router.upstreams["b"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://b/v1"
    )

    async def run():
        with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                response = await client.post("/v1/responses", json={"model": "B/sonnet", "input": "hi"})
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    assert calls == ["sonnet"]


def test_responses_request_to_chat_service_returns_responses_shape(tmp_path):
    cfg = Config(
        codex={"enabled": True, "mode": "mapped", "models": ["b/chat-model"]},
        services=[{"id": "b", "name": "B", "base_url": "http://b/v1", "wire_api": "chat", "models": [{"name": "chat-model"}]}],
    )
    app = create_app(cfg, state_file=tmp_path / "state.yaml")
    async def handler(request):
        return httpx.Response(200, request=request, json={"id": "c", "model": "chat-model", "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    app.state.router.upstreams["b"]._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://b/v1")
    async def run():
        with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                response = await client.post("/v1/responses", json={"model": "B/chat-model", "input": "hi"})
        await app.state.router.close()
        return response
    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "ok"


def test_status_provides_codex_catalog_for_selected_models_only(tmp_path):
    app = create_app(mapped_config(), state_file=tmp_path / 'state.yaml')
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://router') as client:
            result = (await client.get('/v1/status')).json()
        await app.state.router.close()
        return result
    catalog = asyncio.run(run())['codex_catalog']
    assert [m['slug'] for m in catalog['models']] == ['A/gpt', 'B/sonnet']
    assert all(m['visibility'] == 'list' and m['supported_in_api'] for m in catalog['models'])
    assert 'http://' not in json.dumps(catalog)


def test_status_reports_codex_mode_or_none_as_current_usage(tmp_path):
    app = create_app(mapped_config(), state_file=tmp_path / "state.yaml")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
            active = (await client.get("/v1/status")).json()
            await app.state.router.close()
            return active

    active = asyncio.run(run())
    assert active["codex_usage_mode"] == "mapped"

    disabled = create_app(Config(services=[]), state_file=tmp_path / "disabled-state.yaml")

    async def read_disabled():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled), base_url="http://router") as client:
            result = (await client.get("/v1/status")).json()
        await disabled.state.router.close()
        return result

    assert asyncio.run(read_disabled())["codex_usage_mode"] == "none"
