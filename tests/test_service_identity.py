import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from model_router.api import create_app
from model_router.config import Config, load_config
from model_router.router import Router
from model_router.state import RouterState


def config():
    return Config(services=[{
        "name": "original", "base_url": "http://example.invalid/v1",
        "api_key": "original-secret", "models": [{"name": "m"}],
    }])


def test_rename_keeps_identity_credentials_and_selection_after_restart(tmp_path):
    path, state_path = tmp_path / "config.yaml", tmp_path / "state.yaml"
    app = create_app(config(), state_file=state_path, config_file=path)
    app.state.router.toggle_model("original", "m", False)
    app.state.router.set_priority(["original/m"])

    async def run():
        try:
            with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                    payload = (await client.get("/v1/config")).json()
                    assert payload["services"][0]["id"] == "original"
                    payload["services"][0]["name"] = "renamed"
                    payload["services"][0].pop("api_key_set")
                    response = await client.put("/v1/config", json=payload)
                    assert response.status_code == 200
                    snapshot = (await client.get("/v1/status")).json()["models"][0]
                    assert snapshot["key"] == "original/m"
                    assert snapshot["service"] == "renamed"
        finally:
            await app.state.router.close()

    asyncio.run(run())
    saved = load_config(str(path))
    assert saved.services[0].api_key == "original-secret"
    restarted = Router(saved, state=RouterState(str(state_path)))
    assert "original/m" not in restarted.enabled
    assert restarted.state.priority == ["original/m"]
    asyncio.run(restarted.close())


def test_replacement_service_does_not_inherit_deleted_service_secret(tmp_path):
    app = create_app(config(), config_file=tmp_path / "config.yaml", state_file=tmp_path / "state.yaml")

    async def run():
        try:
            with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                    response = await client.put("/v1/config", json={"services": [{
                        "name": "replacement", "base_url": "http://other.invalid/v1",
                        "api_key": "", "models": [{"name": "m"}],
                    }]})
                    assert response.status_code == 200
                    assert response.json()["services"][0]["api_key_set"] is False
        finally:
            await app.state.router.close()

    asyncio.run(run())


def test_status_identifies_instance_and_verified_responses_pool(tmp_path):
    app = create_app(config(), state_file=tmp_path / "state.yaml", instance_id="audit-instance")
    app.state.router.scores["original/m"].observe(10, 0.2)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                return (await client.get("/v1/status")).json()
        finally:
            await app.state.router.close()

    status = asyncio.run(run())
    assert status["instance_id"] == "audit-instance"
    assert status["responses_configured"] is True
    assert status["responses_ready"] is True
    assert status["recommended_models"]["responses"] == "original/m"


def test_success_without_usage_marks_responses_ready(tmp_path):
    app = create_app(config(), state_file=tmp_path / "state.yaml")
    async def handler(request):
        return httpx.Response(200, request=request, json={"output": []})
    async def run():
        client = app.state.router.upstreams["original"]
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://example.invalid/v1")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as http:
                await http.post("/v1/responses", json={"input": "hi"})
                return (await http.get("/v1/status")).json()
        finally:
            await app.state.router.close()
    assert asyncio.run(run())["responses_ready"] is True
