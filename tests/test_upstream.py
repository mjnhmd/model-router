import asyncio

import httpx

from model_router.config import ModelSpec, ServiceSpec
from model_router.upstream import UpstreamClient, _build_bench_payload


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _TrackingResponseStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        if False:
            yield b""

    async def aclose(self):
        self.closed = True


def test_chat_bench_requests_usage_for_full_stream():
    payload = _build_bench_payload("chat", 512)

    assert payload["max_tokens"] == 512
    assert payload["stream"] is False
    assert payload["stream_options"] == {"include_usage": True}


def test_chat_bench_retries_without_stream_options_when_upstream_rejects_it():
    calls = []
    chunks = [
        b'data: {"choices":[],"usage":{"completion_tokens":32}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(400, request=request, json={"error": {"message": "unsupported"}})
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                stream=_ChunkStream(chunks),
            )

        service = ServiceSpec(
            name="test",
            base_url="http://test/v1",
            api_key="key",
            wire_api="chat",
            models=[{"name": "model"}],
        )
        client = UpstreamClient(service, timeout_seconds=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=service.base_url,
        )
        try:
            return await client.bench(ModelSpec(name="model"), target_tokens=32)
        finally:
            await client.close()

    result = asyncio.run(run())
    assert result.ok
    assert result.output_tokens == 32
    assert len(calls) == 2


def test_bench_closes_error_response_to_prevent_connection_pool_leaks():
    stream = _TrackingResponseStream()

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, request=request, stream=stream)

        service = ServiceSpec(
            name="test",
            base_url="http://test/v1",
            api_key="key",
            wire_api="responses",
            models=[{"name": "model"}],
        )
        client = UpstreamClient(service, timeout_seconds=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=service.base_url
        )
        try:
            return await client.bench(ModelSpec(name="model"), target_tokens=32)
        finally:
            await client.close()

    result = asyncio.run(run())
    assert not result.ok
    assert stream.closed


def test_bench_measures_full_stream_usage():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"part one"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"part two"}}]}\n\n',
        b'data: {"choices":[],"usage":{"completion_tokens":64}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                stream=_ChunkStream(chunks),
            )

        service = ServiceSpec(
            name="test",
            base_url="http://test/v1",
            api_key="key",
            wire_api="chat",
            models=[{"name": "model"}],
        )
        client = UpstreamClient(service, timeout_seconds=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=service.base_url,
        )
        try:
            return await client.bench(ModelSpec(name="model"), target_tokens=64)
        finally:
            await client.close()

    result = asyncio.run(run())
    assert result.ok
    assert result.output_tokens == 64
    assert result.tokens_per_sec > 0


def test_bench_rejects_failed_partial_and_empty_streams():
    cases = [
        [b'data: {"type":"response.failed","response":{}}\n\n'],
        [b'data: {"type":"response.incomplete","response":{}}\n\n'],
        [b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'],
        [],
    ]

    async def run():
        service = ServiceSpec(name="test", base_url="http://test/v1", wire_api="responses", models=[{"name": "model"}])
        client = UpstreamClient(service, timeout_seconds=5)
        results = []
        for chunks in cases:
            await client._client.aclose()
            async def handler(request, chunks=chunks):
                return httpx.Response(200, request=request, stream=_ChunkStream(chunks))
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=service.base_url)
            results.append(await client.bench(ModelSpec(name="model"), target_tokens=32))
        await client.close()
        return results

    assert all(not result.ok for result in asyncio.run(run()))


def test_chat_bench_rejects_error_event_even_with_done():
    async def run():
        service = ServiceSpec(name="test", base_url="http://test/v1", wire_api="chat", models=[{"name": "model"}])
        client = UpstreamClient(service, timeout_seconds=5)
        async def handler(request):
            return httpx.Response(200, request=request, stream=_ChunkStream([
                b'data: {"error":{"code":"server_error"}}\n\n', b'data: [DONE]\n\n'
            ]))
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=service.base_url)
        result = await client.bench(ModelSpec(name="model"), target_tokens=32)
        await client.close()
        return result
    assert not asyncio.run(run()).ok


def test_list_models_reads_openai_models_endpoint():
    async def run():
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/models"
            assert request.headers["authorization"] == "Bearer key"
            return httpx.Response(
                200,
                request=request,
                json={
                    "object": "list",
                    "data": [
                        {"id": "model-a", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
                        {"id": "model-b"},
                    ],
                },
            )

        service = ServiceSpec(
            name="test",
            base_url="http://test/v1",
            api_key="key",
            wire_api="chat",
            models=[{"name": "existing"}],
        )
        client = UpstreamClient(service, timeout_seconds=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=service.base_url
        )
        try:
            return await client.list_models()
        finally:
            await client.close()

    assert asyncio.run(run()) == [
        {"name": "model-a", "pricing": {"input_per_million": 3.0, "output_per_million": 15.0}},
        {"name": "model-b"},
    ]
