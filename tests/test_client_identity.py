import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from model_router.api import create_app
from model_router.client_identity import client_headers
from model_router.config import Config, ModelSpec, ServiceSpec
from model_router.upstream import UpstreamClient


def test_forwarded_headers_keep_codex_identity_and_drop_credentials():
    inbound = {
        "Authorization": "Bearer local-router-key",
        "Content-Length": "12",
        "Host": "127.0.0.1:8765",
        "Originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.44.0 (Mac OS 15.6.0; arm64) vscode",
        "X-Codex-Installation-Id": "11111111-2222-3333-4444-555555555555",
        "Session_id": "sess-1",
    }

    out = client_headers(inbound)

    assert out["originator"] == "codex_cli_rs"
    assert out["user-agent"].startswith("codex_cli_rs/")
    assert out["x-codex-installation-id"] == "11111111-2222-3333-4444-555555555555"
    assert out["session_id"] == "sess-1"
    assert "authorization" not in out
    assert "host" not in out
    assert "content-length" not in out


def test_client_without_codex_identity_keeps_its_identity():
    out = client_headers({"User-Agent": "python-httpx/0.27", "Content-Type": "application/json"})

    assert out == {"user-agent": "python-httpx/0.27"}


def test_empty_inbound_headers_do_not_invent_client_identity():
    out = client_headers(None)

    assert out == {}


def test_desktop_identity_and_empty_feature_header_are_preserved():
    inbound = {
        "User-Agent": "codex_desktop/0.153.4 (Mac OS; arm64)",
        "Originator": "codex_desktop",
        "X-Codex-Beta-Features": "",
        "Version": "0.153.4",
    }
    assert client_headers(inbound) == {
        "user-agent": "codex_desktop/0.153.4 (Mac OS; arm64)",
        "originator": "codex_desktop", "x-codex-beta-features": "", "version": "0.153.4",
    }


def test_whitespace_does_not_create_client_identity():
    assert client_headers({"Originator": "  ", "X-Codex-Installation-Id": "\t"}) == {}


def test_account_credentials_and_backend_state_are_not_sent_across_services():
    assert client_headers({
        "Authorization": "Bearer local-key", "Cookie": "session=secret",
        "ChatGPT-Account-Id": "account-a", "X-OpenAI-Actor-Authorization": "secret",
        "X-OAI-Attestation": "secret", "X-Codex-Turn-State": "upstream-a-state",
        "X-Codex-Routing-Hint": "upstream-a-route", "X-Codex-Unknown-Token": "secret",
    }) == {}


def test_connection_nominated_headers_are_not_forwarded():
    assert client_headers({
        "Connection": "keep-alive, Originator, X-Codex-Beta-Features",
        "Originator": "codex_desktop", "X-Codex-Beta-Features": "feature",
        "User-Agent": "codex_desktop/0.153.4",
    }) == {"user-agent": "codex_desktop/0.153.4"}


def test_proxy_forwards_codex_identity_to_upstream_without_client_credentials(tmp_path):
    config = Config(
        services=[
            {
                "id": "relay",
                "name": "relay",
                "base_url": "http://relay/v1",
                "api_key": "service-key",
                "wire_api": "responses",
                "models": [{"name": "gpt"}],
            }
        ]
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")
    seen: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, request=request, json={"id": "ok", "output": []})

    app.state.router.upstreams["relay"]._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay/v1"
    )

    async def run():
        with patch("model_router.api._initial_bench_then_loop", new=AsyncMock()):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            ) as client:
                response = await client.post(
                    "/v1/responses",
                    json={"model": "route-fastest", "input": "hi"},
                    headers={
                        "Authorization": "Bearer local-router-key",
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/0.44.0 (Mac OS 15.6.0; arm64)",
                        "x-codex-installation-id": "11111111-2222-3333-4444-555555555555",
                    },
                )
        await app.state.router.close()
        return response

    response = asyncio.run(run())

    assert response.status_code == 200
    assert seen[0]["originator"] == "codex_cli_rs"
    assert seen[0]["user-agent"].startswith("codex_cli_rs/")
    assert seen[0]["x-codex-installation-id"].startswith("11111111")
    assert seen[0]["authorization"] == "Bearer service-key"


def test_discovery_and_bench_do_not_impersonate_codex():
    seen: list[httpx.Headers] = []
    chunks = [b'data: {"type":"response.completed","response":{"usage":{"output_tokens":8}}}\n\n']

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gpt"}]})
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                stream=_Stream(),
            )

        service = ServiceSpec(
            name="relay", base_url="http://relay/v1", api_key="service-key", wire_api="responses"
        )
        client = UpstreamClient(service, timeout_seconds=5)
        default_headers = client._client.headers
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://relay/v1",
            headers=default_headers,
        )
        model = ModelSpec(name='gpt')
        assert await client.list_models() == [{"name": "gpt"}]
        result = await client.bench(model, 16)
        await client.close()
        return result

    assert asyncio.run(run()).ok

    assert len(seen) == 2
    for headers in seen:
        assert "originator" not in headers
        assert not headers["user-agent"].startswith("codex_")
        assert not any(name.startswith("x-codex-") for name in headers)
