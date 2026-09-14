import asyncio
import json
from types import SimpleNamespace

import httpx
from unittest.mock import AsyncMock, patch

from model_router.api import _sse_gen, create_app
from model_router.config import Config
from model_router.scoring import ModelScore
from model_router.state import RouterState


def test_api_routes_each_protocol_to_the_fastest_model_in_the_enabled_pool(tmp_path):
    config = Config(
        services=[
            {
                "name": "chat-a",
                "base_url": "http://chat-a/v1",
                "api_key": "a",
                "wire_api": "chat",
                "models": [{"name": "slow"}, {"name": "fast"}],
            },
            {
                "name": "responses-b",
                "base_url": "http://responses-b/v1",
                "api_key": "b",
                "wire_api": "responses",
                "models": [{"name": "only"}],
            },
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")
    calls = []

    async def run():
        async def chat_handler(request: httpx.Request) -> httpx.Response:
            calls.append(("chat", json.loads(request.content)["model"]))
            return httpx.Response(
                200,
                request=request,
                json={"id": "chat", "object": "chat.completion", "choices": [], "usage": {"completion_tokens": 1}},
            )

        async def responses_handler(request: httpx.Request) -> httpx.Response:
            calls.append(("responses", json.loads(request.content)["model"]))
            return httpx.Response(
                200,
                request=request,
                json={"id": "response", "object": "response", "output": [], "usage": {"output_tokens": 1}},
            )

        app_router = app.state.router
        app_router.upstreams["chat-a"]._client = httpx.AsyncClient(
            transport=httpx.MockTransport(chat_handler), base_url="http://chat-a/v1"
        )
        app_router.upstreams["responses-b"]._client = httpx.AsyncClient(
            transport=httpx.MockTransport(responses_handler), base_url="http://responses-b/v1"
        )
        app_router.scores["chat-a/slow"].tps = 10
        app_router.scores["chat-a/fast"].tps = 100
        app_router.scores["responses-b/only"].tps = 50
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            chat = await client.post(
                "/v1/chat/completions",
                json={"model": "route-fastest", "messages": [{"role": "user", "content": "hi"}]},
            )
            responses = await client.post(
                "/v1/responses",
                json={"model": "route-fastest", "input": "hi"},
            )
            disabled = await client.post(
                "/v1/models/toggle",
                json={"service": "chat-a", "model": "fast", "enabled": False},
            )
            chat_after_toggle = await client.post(
                "/v1/chat/completions",
                json={"model": "route-fastest", "messages": [{"role": "user", "content": "hi"}]},
            )
        await app_router.close()
        return chat, responses, disabled, chat_after_toggle

    chat, responses, disabled, chat_after_toggle = asyncio.run(run())
    assert chat.status_code == 200
    assert responses.status_code == 200
    assert disabled.json()["changed"] is True
    assert chat_after_toggle.status_code == 200
    assert calls == [("chat", "fast"), ("responses", "only"), ("chat", "slow")]


def test_sticky_session_keeps_model_until_it_becomes_unhealthy(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "chat-service",
                    "base_url": "http://chat-service/v1",
                    "wire_api": "chat",
                    "models": [{"name": "fast"}, {"name": "slow"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    calls = []

    async def chat_handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            request=request,
            json={"id": "chat", "object": "chat.completion", "choices": []},
        )

    app.state.router.upstreams["chat-service"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(chat_handler),
        base_url="http://chat-service/v1",
    )
    app.state.router.scores["chat-service/fast"].tps = 100
    app.state.router.scores["chat-service/slow"].tps = 10

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            first = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route-fastest",
                    "prompt_cache_key": "session-a",
                    "messages": [{"role": "user", "content": "first"}],
                },
            )
            app.state.router.scores["chat-service/fast"].tps = 10
            app.state.router.scores["chat-service/slow"].tps = 100
            second = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route-fastest",
                    "prompt_cache_key": "session-a",
                    "messages": [{"role": "user", "content": "first"}],
                },
            )
            app.state.router.scores["chat-service/fast"].error_rate = 0.2
            third = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route-fastest",
                    "prompt_cache_key": "session-a",
                    "messages": [{"role": "user", "content": "first"}],
                },
            )
        await app.state.router.close()
        return first, second, third

    first, second, third = asyncio.run(run())

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert calls == ["fast", "fast", "slow"]


def test_responses_image_request_routes_to_model_with_unknown_vision_capability(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "responses-service",
                    "base_url": "http://responses-service/v1",
                    "api_key": "key",
                    "wire_api": "responses",
                    "models": [{"name": "auto-discovered"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )

    async def responses_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"id": "response", "object": "response", "output": []},
        )

    app.state.router.upstreams["responses-service"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(responses_handler),
        base_url="http://responses-service/v1",
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={
                    "model": "route-fastest",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64,AA==",
                                }
                            ],
                        }
                    ],
                },
            )
        await app.state.router.close()
        return response

    response = asyncio.run(run())

    assert response.status_code == 200


def test_image_request_retries_next_fastest_model_after_upstream_400(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "responses-service",
                    "base_url": "http://responses-service/v1",
                    "api_key": "key",
                    "wire_api": "responses",
                    "models": [{"name": "fast"}, {"name": "fallback"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    calls = []
    app.state.router.scores["responses-service/fast"].tps = 100
    app.state.router.scores["responses-service/fallback"].tps = 50

    async def responses_handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "fast":
            return httpx.Response(
                400,
                request=request,
                json={"error": {"message": "model does not support images"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={"id": "response", "object": "response", "output": []},
        )

    app.state.router.upstreams["responses-service"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(responses_handler),
        base_url="http://responses-service/v1",
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={
                    "model": "route-fastest",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64,AA==",
                                }
                            ],
                        }
                    ],
                },
            )
            second = await client.post(
                "/v1/responses",
                json={
                    "model": "route-fastest",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64,AA==",
                                }
                            ],
                        }
                    ],
                },
            )
        await app.state.router.close()
        return response, second

    response, second = asyncio.run(run())

    assert response.status_code == 200
    assert second.status_code == 200
    assert calls == ["fast", "fallback", "fallback"]


def test_non_image_request_does_not_retry_upstream_400(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "responses-service",
                    "base_url": "http://responses-service/v1",
                    "models": [{"name": "fast"}, {"name": "fallback"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    calls = []
    app.state.router.scores["responses-service/fast"].tps = 100
    app.state.router.scores["responses-service/fallback"].tps = 50

    async def responses_handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(400, request=request, json={"error": "bad request"})

    app.state.router.upstreams["responses-service"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(responses_handler),
        base_url="http://responses-service/v1",
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "route-fastest", "input": "hello"},
            )
        await app.state.router.close()
        return response

    response = asyncio.run(run())

    assert response.status_code == 400
    assert calls == ["fast"]


def test_upstream_5xx_fails_over_and_penalizes_model(tmp_path):
    app = create_app(Config(services=[{"name": "s", "base_url": "http://s/v1", "models": [{"name": "fast"}, {"name": "fallback"}]}]), state_file=tmp_path / "state.yaml")
    calls = []

    async def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "fast":
            return httpx.Response(503, request=request, json={"error": "down"})
        return httpx.Response(200, request=request, json={"output": []})

    async def run():
        client = app.state.router.upstreams["s"]
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://s/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as c:
            response = await c.post("/v1/responses", json={"input": "hi"})
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    assert calls == ["fast", "fallback"]
    assert app.state.router.scores["s/fast"].failed_requests == 1


def test_config_reload_keeps_inflight_request_valid(tmp_path):
    app = create_app(Config(services=[{"name": "s", "base_url": "http://s/v1", "models": [{"name": "m"}]}]), state_file=tmp_path / "state.yaml")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, request=request, json={"output": [], "usage": {"output_tokens": 1}})

    async def run():
        client = app.state.router.upstreams["s"]
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://s/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as c:
            pending = asyncio.create_task(c.post("/v1/responses", json={"input": "hi"}))
            await entered.wait()
            await app.state.router.apply_config(Config(services=[]))
            release.set()
            response = await pending
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200


def test_invalid_image_error_does_not_blacklist_vision_models(tmp_path):
    app = create_app(Config(services=[{"name": "s", "base_url": "http://s/v1", "wire_api": "responses", "models": [{"name": "m", "capabilities": {"supports_vision": True}}]}]), state_file=tmp_path / "state.yaml")

    async def handler(request):
        body = json.loads(request.content)
        if "invalid" in json.dumps(body):
            return httpx.Response(400, request=request, json={"error": {"code": "invalid_image_url"}})
        return httpx.Response(200, request=request, json={"output": []})

    async def run():
        client = app.state.router.upstreams["s"]
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://s/v1")
        image = lambda url: {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": url}]}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as c:
            await c.post("/v1/responses", json=image("invalid"))
            response = await c.post("/v1/responses", json=image("valid"))
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    assert app.state.router.vision_unsupported == set()


def test_chat_sse_done_preserves_event_boundary():
    class Response:
        async def aiter_bytes(self):
            yield b"data: [DONE]\n\n"
        async def aclose(self):
            return None

    async def run():
        return b"".join([chunk async for chunk in _sse_gen(Response(), "chat", "s/m", SimpleNamespace(scores={"s/m": ModelScore("s/m")}))])

    assert asyncio.run(run()) == b"data: [DONE]\n\n"


def test_sse_forwarding_preserves_event_boundaries_for_codex():
    class Response:
        async def aiter_bytes(self):
            yield (
                b"event: response.completed\n"
                b'data: {"type":"response.completed","response":{"usage":{"output_tokens":2}}}\n'
                b"\n"
                b"data: [DONE]\n\n"
            )

        async def aclose(self):
            return None

    router = SimpleNamespace(scores={"service/model": ModelScore("service/model")})

    async def run():
        return [chunk async for chunk in _sse_gen(Response(), "responses", "service/model", router)]

    forwarded = b"".join(asyncio.run(run()))
    assert b"response.completed" in forwarded
    assert b"\n\n" in forwarded


def test_sse_forwarding_preserves_crlf_and_unterminated_partial_event():
    class Response:
        async def aiter_bytes(self):
            yield b"data: one\r\n\r\ndata: partial"
        async def aclose(self):
            return None
    async def run():
        return b"".join([chunk async for chunk in _sse_gen(Response(), "chat", "s/m", SimpleNamespace(scores={"s/m": ModelScore("s/m")}))])
    assert asyncio.run(run()) == b"data: one\r\n\r\ndata: partial"


def test_config_api_hides_keys_and_saves_validated_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "services:\n"
        "  - name: original\n"
        "    base_url: http://original/v1\n"
        "    api_key: secret-key\n"
        "    wire_api: responses\n"
        "    models: [{name: first}]\n",
        encoding="utf-8",
    )
    config = Config(
        services=[
            {
                "name": "original",
                "base_url": "http://original/v1",
                "api_key": "secret-key",
                "wire_api": "responses",
                "models": [{"name": "first"}],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml", config_file=config_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            visible = await client.get("/v1/config")
            saved = await client.put(
                "/v1/config",
                json={
                    "public_model": "route-fastest",
                    "bench_interval": 45,
                    "bench_concurrency": 2,
                    "bench_rounds": 1,
                    "bench_target_tokens": 128,
                    "timeout_seconds": 30,
                    "services": [
                        {
                            "name": "new-service",
                            "base_url": "http://new-service/v1",
                            "api_key": "new-secret",
                            "wire_api": "chat",
                            "models": [{"name": "new-model"}],
                        }
                    ],
                },
            )
        await app.state.router.close()
        return visible, saved

    visible, saved = asyncio.run(run())
    assert visible.status_code == 200
    assert visible.json()["services"][0]["api_key"] == ""
    assert visible.json()["services"][0]["api_key_set"] is True
    assert saved.status_code == 200
    assert saved.json()["services"][0]["name"] == "new-service"
    assert "new-secret" in config_path.read_text(encoding="utf-8")
    assert app.state.router.config.services[0].name == "new-service"


def test_config_api_preserves_existing_key_when_editor_leaves_it_blank(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "services:\n"
        "  - name: original\n"
        "    base_url: http://original/v1\n"
        "    api_key: secret-key\n"
        "    models: [{name: first}]\n",
        encoding="utf-8",
    )
    config = Config(
        services=[
            {
                "name": "original",
                "base_url": "http://original/v1",
                "api_key": "secret-key",
                "models": [{"name": "first"}],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml", config_file=config_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.put(
                "/v1/config",
                json={
                    "services": [
                        {
                            "name": "original",
                            "base_url": "http://changed/v1",
                            "api_key": "",
                            "wire_api": "responses",
                            "models": [{"name": "first"}],
                        }
                    ]
                },
            )
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    assert "api_key: secret-key" in config_path.read_text(encoding="utf-8")


def test_config_rename_preserves_key_and_router_state(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "services:\n  - name: original\n    base_url: http://original/v1\n"
        "    api_key: secret-key\n    models: [{name: first}]\n",
        encoding="utf-8",
    )
    config = Config.model_validate({
        "services": [{"name": "original", "base_url": "http://original/v1",
                       "api_key": "secret-key", "models": [{"name": "first"}]}]
    })
    state_path = tmp_path / "state.yaml"
    app = create_app(config, state_file=state_path, config_file=config_path)
    app.state.router.toggle_model("original", "first", False)

    async def run():
        with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                response = await client.put("/v1/config", json={
                    "services": [{"id": "original", "name": "renamed", "base_url": "http://original/v1",
                                   "api_key": "", "wire_api": "responses",
                                   "models": [{"name": "first"}]}]
                })
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    saved = config_path.read_text(encoding="utf-8")
    assert "api_key: secret-key" in saved
    assert RouterState(str(state_path)).is_disabled("original", "first")
    assert "original/first" not in app.state.router.enabled
    asyncio.run(app.state.router.close())


def test_config_api_discovers_models_for_new_service(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("services: []\n", encoding="utf-8")
    app = create_app(
        Config(services=[]), state_file=tmp_path / "state.yaml", config_file=config_path
    )

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/models"
            return httpx.Response(
                200,
                request=request,
                json={"data": [{"id": "alpha"}, {"id": "beta"}]},
            )

        class DiscoveryClient:
            def __init__(self, service, timeout_seconds):
                self.service = service

            async def list_models(self):
                request = httpx.Request("GET", "http://upstream/v1/models")
                response = await handler(request)
                response.raise_for_status()
                return [item["id"] for item in response.json()["data"]]

            async def close(self):
                return None

        monkeypatch.setattr("model_router.api.UpstreamClient", DiscoveryClient)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.put(
                "/v1/config",
                json={
                    "services": [
                        {
                            "name": "new-service",
                            "base_url": "http://upstream/v1",
                            "api_key": "secret",
                            "wire_api": "chat",
                            "models": [],
                        }
                    ]
                },
            )
        await app.state.router.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 200
    assert [m["name"] for m in response.json()["services"][0]["models"]] == ["alpha", "beta"]


def test_batch_toggle_updates_multiple_models_and_persists_once(tmp_path):
    config = Config(
        services=[
            {
                "name": "service",
                "base_url": "http://service/v1",
                "models": [{"name": "one"}, {"name": "two"}, {"name": "three"}],
            }
        ]
    )
    state_file = tmp_path / "state.yaml"
    app = create_app(config, state_file=state_file)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/models/toggle-batch",
                json={"keys": ["service/one", "service/two"], "enabled": False},
            )
            status = await client.get("/v1/status")
        await app.state.router.close()
        return response, status

    response, status = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["changed"] == 2
    enabled = {item["key"]: item["enabled"] for item in status.json()["models"]}
    assert enabled == {"service/one": False, "service/two": False, "service/three": True}
    assert set(__import__("yaml").safe_load(state_file.read_text())["disabled"]) == {
        "service/one",
        "service/two",
    }


def test_status_exposes_pricing_and_uses_speed_routing(tmp_path):
    config = Config(
        services=[
            {
                "name": "service",
                "base_url": "http://service/v1",
                "models": [
                    {
                        "name": "model",
                        "pricing": {"input_per_million": 3, "output_per_million": 15},
                    }
                ],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            status = await client.get("/v1/status")
        await app.state.router.close()
        return status

    status = asyncio.run(run())
    assert "routing_policy" not in status.json()
    assert status.json()["models"][0]["pricing"] == {
        "input_per_million": 3.0,
        "output_per_million": 15.0,
    }


def test_reorder_endpoint_updates_selection_order_without_policy_switch(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "first"}, {"name": "second"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            reordered = await client.post(
                "/v1/models/reorder",
                json={"keys": ["service/second", "service/first"]},
            )
            status = await client.get("/v1/status")
        await app.state.router.close()
        return reordered, status

    reordered, status = asyncio.run(run())
    assert reordered.status_code == 200
    priorities = {model["key"]: model["priority"] for model in status.json()["models"]}
    assert priorities == {"service/first": 2, "service/second": 1}


def test_manual_bench_reports_running_state_and_completes(tmp_path, monkeypatch):
    app = create_app(
        Config(
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "model"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    started = asyncio.Event()

    async def fake_bench(**kwargs):
        started.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(app.state.router, "run_bench_round", fake_bench)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            accepted = await client.post("/v1/bench")
            running = await client.get("/v1/status")
            await started.wait()
            await asyncio.sleep(0.08)
            finished = await client.get("/v1/status")
        await app.state.router.close()
        return accepted, running, finished

    accepted, running, finished = asyncio.run(run())
    assert accepted.status_code == 202
    assert accepted.json()["started"] is True
    assert running.json()["bench"]["running"] is True
    assert finished.json()["bench"]["running"] is False
    assert finished.json()["bench"]["last_result"] == "success"


def test_manual_bench_uses_enabled_pool_and_reports_progress(tmp_path, monkeypatch):
    app = create_app(
        Config(
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "selected"}, {"name": "disabled"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    app.state.router.toggle_model("service", "disabled", False)
    started = asyncio.Event()
    calls = []

    async def fake_bench(**kwargs):
        calls.append(kwargs)
        started.set()
        await asyncio.sleep(0.01)
        kwargs["on_progress"](
            "service/selected", SimpleNamespace(ok=True, error=""), 1, 1
        )

    monkeypatch.setattr(app.state.router, "run_bench_round", fake_bench)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            accepted = await client.post("/v1/bench")
            await started.wait()
            running = await client.get("/v1/status")
            await asyncio.sleep(0.03)
            finished = await client.get("/v1/status")
        await app.state.router.close()
        return accepted, running, finished

    accepted, running, finished = asyncio.run(run())

    assert accepted.status_code == 202
    assert accepted.json()["bench"]["scope"] == "当前已选模型"
    assert accepted.json()["bench"]["total"] == 2
    assert running.json()["bench"]["completed"] == 0
    assert finished.json()["bench"]["completed"] == 1
    assert calls[0]["model_keys"] == ["service/selected"]
    assert calls[0]["rounds"] == 1


def test_manual_bench_can_be_stopped(tmp_path, monkeypatch):
    app = create_app(
        Config(
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "model"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )
    started = asyncio.Event()

    async def fake_bench(**kwargs):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(app.state.router, "run_bench_round", fake_bench)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            await client.post("/v1/bench")
            await started.wait()
            stopped = await client.post("/v1/bench/cancel")
            status = await client.get("/v1/status")
        await app.state.router.close()
        return stopped, status

    stopped, status = asyncio.run(run())

    assert stopped.status_code == 200
    assert stopped.json()["cancelled"] is True
    assert status.json()["bench"]["running"] is False
    assert status.json()["bench"]["last_result"] == "cancelled"


def test_background_bench_uses_enabled_pool_and_fast_defaults(tmp_path, monkeypatch):
    app = create_app(
        Config(
            bench_interval=3600,
            bench_rounds=2,
            bench_target_tokens=512,
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "selected"}, {"name": "disabled"}],
                }
            ],
        ),
        state_file=tmp_path / "state.yaml",
    )
    app.state.router.toggle_model("service", "disabled", False)
    started = asyncio.Event()
    calls = []

    async def fake_bench(**kwargs):
        calls.append(kwargs)
        started.set()

    monkeypatch.setattr(app.state.router, "run_bench_round", fake_bench)

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.sleep(0)

    asyncio.run(run())

    assert calls[0]["model_keys"] == ["service/selected"]
    assert calls[0]["rounds"] == 1
    assert calls[0]["target_tokens"] == 256


def test_request_logs_and_stats_capture_proxy_usage_without_prompt_content(tmp_path):
    app = create_app(
        Config(
            services=[
                {
                    "name": "service",
                    "base_url": "http://service/v1",
                    "models": [{"name": "model"}],
                }
            ]
        ),
        state_file=tmp_path / "state.yaml",
    )

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "response",
                    "object": "response",
                    "output": [],
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                },
            )

        app.state.router.upstreams["service"]._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://service/v1"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "route-fastest", "input": "do not log this prompt"},
            )
            logs = await client.get("/v1/logs")
            stats = await client.get("/v1/stats")
        await app.state.router.close()
        return response, logs, stats

    response, logs, stats = asyncio.run(run())
    assert response.status_code == 200
    assert len(logs.json()["logs"]) == 1
    assert logs.json()["logs"][0]["model"] == "service/model"
    assert logs.json()["logs"][0]["ok"] is True
    assert "do not log this prompt" not in json.dumps(logs.json())
    assert stats.json()["total_requests"] == 1
    assert stats.json()["success_requests"] == 1
    assert stats.json()["total_input_tokens"] == 12
    assert stats.json()["total_output_tokens"] == 8
    assert stats.json()["models"][0]["requests"] == 1


def test_config_update_leaves_only_one_bench_loop(tmp_path):
    app = create_app(
        Config(bench_interval=3600, services=[{"name": "s", "base_url": "http://s/v1", "models": [{"name": "m"}]}]),
        config_file=tmp_path / "config.yaml",
    )
    started = asyncio.Event()

    async def fake_bench(**kwargs):
        started.set()
        await asyncio.Event().wait()

    app.state.router.run_bench_round = fake_bench

    async def run():
        def loops():
            return [task for task in asyncio.all_tasks() if task.get_coro().__qualname__ == "_initial_bench_then_loop"]
        with patch("model_router.api.save_config"):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(started.wait(), timeout=1)
                old_loop = loops()[0]
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                    response = await client.put(
                        "/v1/config",
                        json={
                            "bench_interval": 3600,
                            "services": [{"name": "s", "base_url": "http://s/v1", "models": [{"name": "m"}] }],
                        },
                    )
                await asyncio.sleep(0)
                return response, old_loop, len(loops())

    response, old_loop, count = asyncio.run(run())
    assert response.status_code == 200
    assert old_loop.done()
    assert count == 1


def test_status_reports_version_and_flags_replaced_app_bundle(tmp_path, monkeypatch):
    from model_router import api as api_module

    config = Config(
        services=[
            {
                "name": "svc",
                "base_url": "http://svc/v1",
                "api_key": "k",
                "models": [{"name": "gpt"}],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")

    async def status_payload() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.get("/v1/status")
        return response.json()

    fresh = asyncio.run(status_payload())
    assert fresh["version"] == api_module.CURRENT_VERSION
    assert fresh["app_build_time"] == ""
    assert fresh["app_restart_required"] is False

    # 模拟“磁盘上的 App 在进程启动后被替换”：运行中的进程仍是旧代码。
    monkeypatch.setattr(
        api_module, "_app_build_time", lambda: api_module._PROCESS_STARTED_AT + 60
    )
    stale = asyncio.run(status_payload())
    assert stale["app_restart_required"] is True
    assert stale["app_build_time"]

    asyncio.run(app.state.router.close())


def test_logs_explain_upstream_official_client_rejection(tmp_path):
    config = Config(
        services=[
            {
                "name": "relay",
                "base_url": "http://relay/v1",
                "api_key": "k",
                "models": [{"name": "gpt"}],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")
    rejection = {
        "error": {
            "message": "This account only allows Codex official clients",
            "type": "forbidden_error",
        }
    }

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json=rejection, request=request)

        app.state.router.upstreams["relay"]._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay/v1"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            anonymous = await client.post(
                "/v1/responses", json={"model": "route-fastest", "input": "hi"}
            )
            identified = await client.post(
                "/v1/responses",
                json={"model": "route-fastest", "input": "hi"},
                headers={
                    "originator": "Codex Desktop",
                    "user-agent": "Codex Desktop/0.153.4 (Mac OS; arm64)",
                },
            )
            logs = (await client.get("/v1/logs")).json()["logs"]
        await app.state.router.close()
        return anonymous, identified, logs

    anonymous, identified, logs = asyncio.run(run())

    assert anonymous.status_code == 403 and identified.status_code == 403
    # 上游响应原样透传，只在本机日志里补上可执行说明
    assert anonymous.json()["error"]["type"] == "forbidden_error"
    assert all(entry["error"] == "upstream_status_403" for entry in logs)
    hints = [entry["hint"] for entry in logs]
    assert any("没有携带 Codex 客户端标识" in hint for hint in hints)
    assert any("已带 Codex 标识仍被拒" in hint for hint in hints)
