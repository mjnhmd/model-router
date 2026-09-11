import asyncio
from types import SimpleNamespace

from model_router.updates import check_for_update, normalize_version


def test_normalize_version_accepts_release_tag():
    assert normalize_version("v0.2.0") == (0, 2, 0)
    assert normalize_version("0.1.3") == (0, 1, 3)


def test_check_for_update_reports_latest_release(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return [{
                "tag_name": "v0.2.0",
                "name": "Model Router 0.2.0",
                "web_url": "https://github.com/mjnhmd/model-router/releases/v0.2.0",
                "_links": {"self": "https://api.github.com/repos/mjnhmd/model-router/releases/1"},
            }]

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("model_router.updates.httpx.AsyncClient", lambda **kwargs: FakeClient())
    result = asyncio.run(check_for_update("0.1.3"))
    assert result["latest_version"] == "0.2.0"
    assert result["update_available"] is True
    assert result["release_url"] == "https://github.com/mjnhmd/model-router/releases/v0.2.0"


def test_check_for_update_returns_non_fatal_error(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args, **kwargs):
            raise TimeoutError("offline")

    monkeypatch.setattr("model_router.updates.httpx.AsyncClient", lambda **kwargs: FakeClient())
    result = asyncio.run(check_for_update("0.1.3"))
    assert result["update_available"] is False
    assert result["error"] == "检查更新失败，请稍后重试"


def test_update_endpoint_returns_checker_result(tmp_path, monkeypatch):
    from model_router.api import create_app
    import httpx
    from model_router.config import Config

    async def fake_check(version):
        assert version
        return {"current_version": version, "latest_version": "0.2.0", "update_available": True}

    monkeypatch.setattr("model_router.api.check_for_update", fake_check)
    app = create_app(Config(services=[]), state_file=tmp_path / "state.yaml")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.get("/v1/update")
        await app.state.router.close()
        return response

    assert asyncio.run(run()).json()["update_available"] is True
