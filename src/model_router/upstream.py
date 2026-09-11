from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from .config import ModelSpec, ServiceSpec

BENCH_PROMPT = (
    "请列举中国神话中最重要的 20 位神祇，每位用两句话说明其身份与主要事迹。"
    "内容要具体、详实、有细节，不要重复。"
)


@dataclass
class BenchResult:
    service: str
    model: str
    output_tokens: int
    elapsed: float
    tokens_per_sec: float
    ok: bool
    error: str = ""
    time_to_first_token: float = 0.0
    sample_count: int = 1
    phase: str = "precise"
    measured_at: float = 0.0


def _build_bench_payload(wire_api: str, target_tokens: int) -> dict:
    if wire_api == "responses":
        return {
            "model": "PLACEHOLDER",
            "input": BENCH_PROMPT,
            "max_output_tokens": target_tokens,
            "stream": False,
        }
    return {
        "model": "PLACEHOLDER",
        "messages": [{"role": "user", "content": BENCH_PROMPT}],
        "max_tokens": target_tokens,
        "stream_options": {"include_usage": True},
        "stream": False,
    }


def _count_tokens(resp_json: dict, wire_api: str) -> int:
    if wire_api == "responses":
        usage = resp_json.get("usage") or {}
        return int(usage.get("output_tokens") or 0)
    usage = resp_json.get("usage") or {}
    return int(usage.get("completion_tokens") or 0)


class UpstreamClient:
    """单个上游服务：负责发起探测请求。"""

    def __init__(self, service: ServiceSpec, timeout_seconds: int):
        self.service = service
        self.timeout = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=service.base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {service.api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[dict]:
        """Discover model identifiers from the OpenAI-compatible models endpoint."""
        response = await self._client.get(
            "/models", headers={"Authorization": f"Bearer {self.service.api_key}"}
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("上游 /models 返回缺少 data 数组")
        models = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name")
                pricing = _parse_pricing(item)
            elif isinstance(item, str):
                name = item
                pricing = None
            else:
                name = None
                pricing = None
            if isinstance(name, str) and name and not any(model["name"] == name for model in models):
                model = {"name": name}
                if pricing is not None:
                    model["pricing"] = pricing
                models.append(model)
        if not models:
            raise ValueError("上游 /models 没有返回可用模型")
        return models

    async def bench(self, model: ModelSpec, target_tokens: int, phase: str = "precise") -> BenchResult:
        """完整读取流式探针，以最终 usage 计算长文输出吞吐。"""
        payload = _build_bench_payload(self.service.wire_api, target_tokens)
        payload["model"] = model.name
        payload["stream"] = True
        path = "/responses" if self.service.wire_api == "responses" else "/chat/completions"
        start = time.monotonic()
        first_token_at = None
        resp = None
        try:
            resp = await self._send_probe(path, payload)
            if (
                resp.status_code == 400
                and self.service.wire_api == "chat"
                and "stream_options" in payload
            ):
                await resp.aclose()
                fallback_payload = dict(payload)
                fallback_payload.pop("stream_options")
                resp = await self._send_probe(path, fallback_payload)
            resp.raise_for_status()
            buffer = b""
            out = 0
            completed = False
            failed = False
            async for chunk in resp.aiter_bytes():
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload_bytes = line[5:].lstrip()
                    if payload_bytes == b"[DONE]":
                        completed = True
                        break
                    try:
                        event = json.loads(payload_bytes)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    has_content_delta = event_type == "response.output_text.delta" or any(
                        isinstance(choice, dict) and isinstance(choice.get("delta"), dict)
                        and choice["delta"].get("content")
                        for choice in (event.get("choices") or [])
                    )
                    if first_token_at is None and has_content_delta:
                        first_token_at = time.monotonic()
                    if self.service.wire_api == "chat" and isinstance(event.get("error"), dict):
                        failed = True
                    if event_type == "response.failed":
                        failed = True
                    if event_type == "response.completed":
                        completed = True
                    if event_type == "response.incomplete":
                        failed = True
                        completed = True
                    out = max(out, _stream_usage(event, self.service.wire_api))
                if completed:
                    break
            if failed:
                return BenchResult(self.service.name, model.name, 0, time.monotonic() - start, 0.0, False, "upstream response failed")
            if not completed:
                return BenchResult(self.service.name, model.name, 0, time.monotonic() - start, 0.0, False, "incomplete stream")
            elapsed = time.monotonic() - start
            tps = (out / elapsed) if out > 0 else 0.0
            return BenchResult(
                service=self.service.name,
                model=model.name,
                output_tokens=out,
                elapsed=elapsed,
                tokens_per_sec=tps,
                ok=True,
                time_to_first_token=(first_token_at - start) if first_token_at else 0.0,
                phase=phase,
                measured_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - start
            is_timeout = isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError))
            return BenchResult(
                service=self.service.name,
                model=model.name,
                output_tokens=0,
                elapsed=elapsed,
                tokens_per_sec=0.0,
                ok=False,
                error=("timeout" if is_timeout else str(e)[:200]),
                phase=phase,
            )
        finally:
            if resp is not None:
                await resp.aclose()

    async def _send_probe(self, path: str, payload: dict) -> httpx.Response:
        req = self._client.build_request("POST", path, json=payload)
        return await self._client.send(req, stream=True)


def _stream_usage(event: dict, wire_api: str) -> int:
    usage = event.get("usage") or {}
    if wire_api == "responses":
        usage = (event.get("response") or {}).get("usage") or usage
        return int(usage.get("output_tokens") or 0)
    return int(usage.get("completion_tokens") or 0)


def _parse_pricing(item: dict) -> dict | None:
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        pricing = item

    input_per_million = _number(pricing.get("input_per_million"))
    output_per_million = _number(pricing.get("output_per_million"))
    if input_per_million is None:
        input_per_million = _token_price(pricing, "input", "prompt")
    if output_per_million is None:
        output_per_million = _token_price(pricing, "output", "completion")

    parsed = {}
    if input_per_million is not None:
        parsed["input_per_million"] = input_per_million
    if output_per_million is not None:
        parsed["output_per_million"] = output_per_million
    return parsed or None


def _token_price(pricing: dict, primary: str, alias: str) -> float | None:
    value = pricing.get(primary)
    if value is None:
        value = pricing.get(alias)
    number = _number(value)
    return number * 1_000_000 if number is not None else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
