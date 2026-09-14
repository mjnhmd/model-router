import asyncio
import json

import httpx
import pytest

from model_router.api import create_app
from model_router.config import Config


@pytest.mark.parametrize("stream", [False, True])
def test_switching_services_keeps_client_identity_and_uses_target_credentials(tmp_path, stream):
    config = Config(
        codex={"enabled": True, "mode": "mapped", "models": ["a/gpt", "b/gpt"]},
        services=[
            {"id": sid, "name": sid.upper(), "base_url": f"http://{sid}/v1",
             "api_key": f"key-{sid}", "models": [{"name": "gpt"}]}
            for sid in ("a", "b")
        ],
    )
    app = create_app(config, state_file=tmp_path / "state.yaml")
    calls = []
    identity = {"originator": "Codex Desktop", "user-agent": "Codex Desktop/0.149.0 (codex_exec)",
                "session-id": "session-1", "thread-id": "thread-1",
                "x-client-request-id": "request-1", "x-codex-window-id": "window-1",
                "x-codex-turn-metadata": '{"turn_id":"turn-1","thread_id":"thread-1"}',
                "x-codex-beta-features": "remote_compaction_v2"}
    history = [{"role": "user", "content": "same conversation"}]

    async def upstream(request):
        sid = request.url.host
        body = json.loads(request.content)
        calls.append(sid)
        if any(request.headers.get(name) != value for name, value in identity.items()):
            return httpx.Response(403, json={"error": {"type": "forbidden_error",
                "message": "This account only allows Codex official clients"}})
        assert request.headers["authorization"] == f"Bearer key-{sid}"
        assert request.headers["host"] == sid
        assert request.headers["session_id"] == "same-session"
        assert "chatgpt-account-id" not in request.headers
        assert "x-openai-actor-authorization" not in request.headers
        assert "cookie" not in request.headers
        assert body["model"] == "gpt"
        assert body["input"] == history
        result = {"id": f"resp-{sid}", "object": "response", "status": "completed",
                  "output": [], "model": "gpt", "usage": {"input_tokens": 1, "output_tokens": 1}}
        if stream:
            event = json.dumps({"type": "response.completed", "response": result})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                 content=f"event: response.completed\ndata: {event}\n\n")
        return httpx.Response(200, json=result)

    async def run():
        try:
            for client in app.state.router.upstreams.values():
                defaults = client._client.headers
                await client.close()
                client._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), headers=defaults)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client:
                for name in ("A/gpt", "B/gpt", "A/gpt"):
                    response = await client.post("/v1/responses",
                        headers={**identity, "authorization": "Bearer local-key",
                                 "session_id": "same-session", "cookie": "session=secret",
                                 "chatgpt-account-id": "account-a", "x-openai-actor-authorization": "secret"},
                        json={"model": name, "input": history, "stream": stream})
                    assert response.status_code == 200, response.text
                    if stream:
                        assert "event: response.completed\n" in response.text
                        assert response.text.endswith("\n\n")
                    else:
                        assert response.json()["id"] == f"resp-{name[0].lower()}"
                # Shared upstream clients must not retain the previous caller's identity.
                plain = await client.post("/v1/responses", json={"model": "B/gpt", "input": history})
                assert plain.status_code == 403
                assert plain.json()["error"]["type"] == "forbidden_error"
            assert calls == ["a", "b", "a", "b"]
        finally:
            await app.state.router.close()

    asyncio.run(run())
