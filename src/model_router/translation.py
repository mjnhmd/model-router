"""Small, explicit adapters between OpenAI Responses and Chat Completions."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


def _chat_content(parts: Any) -> Any:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return parts
    result = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": str(part.get("text", ""))})
        elif kind in ("input_image", "image_url"):
            url = part.get("image_url")
            if isinstance(url, dict):
                result.append({"type": "image_url", "image_url": url})
            elif url:
                result.append({"type": "image_url", "image_url": {"url": url}})
    return result


def translate_responses_request(payload: dict) -> dict:
    result = {k: v for k, v in payload.items() if k not in {"input", "max_output_tokens", "tools"}}
    value = payload.get("input", "")
    messages = value if isinstance(value, list) else [{"role": "user", "content": value}]
    result["messages"] = [
        {"role": item.get("role", "user"), "content": _chat_content(item.get("content", ""))}
        if isinstance(item, dict) else {"role": "user", "content": str(item)}
        for item in messages
    ]
    if "max_output_tokens" in payload:
        result["max_tokens"] = payload["max_output_tokens"]
    if isinstance(payload.get("tools"), list):
        tools = []
        for tool in payload["tools"]:
            if isinstance(tool, dict) and tool.get("type") == "function":
                function = {k: tool[k] for k in ("name", "description", "parameters") if k in tool}
                tools.append({"type": "function", "function": function})
        if tools:
            result["tools"] = tools
    return result


def _responses_content(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content or "")}]
    result = []
    for part in content:
        if isinstance(part, str):
            result.append({"type": "input_text", "text": part})
        elif isinstance(part, dict):
            if part.get("type") in ("text", "input_text"):
                result.append({"type": "input_text", "text": str(part.get("text", ""))})
            elif part.get("type") == "image_url":
                image = part.get("image_url") or {}
                result.append({"type": "input_image", "image_url": image.get("url", image) if isinstance(image, dict) else image})
    return result


def translate_chat_request(payload: dict) -> dict:
    result = {k: v for k, v in payload.items() if k not in {"messages", "max_tokens", "tools"}}
    result["input"] = [
        {"role": item.get("role", "user"), "content": _responses_content(item.get("content", ""))}
        for item in payload.get("messages", []) if isinstance(item, dict)
    ]
    if "max_tokens" in payload:
        result["max_output_tokens"] = payload["max_tokens"]
    if isinstance(payload.get("tools"), list):
        result["tools"] = [
            {"type": "function", **(tool.get("function") or {})}
            for tool in payload["tools"] if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]
    return result


def translate_chat_response(payload: dict, requested_model: str) -> dict:
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    message = message if isinstance(message, dict) else {}
    content = message.get("content") or ""
    output = []
    if content:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            continue
        function = call["function"]
        output.append({
            "type": "function_call",
            "call_id": call.get("id", ""),
            "name": function.get("name", ""),
            "arguments": function.get("arguments", "{}"),
        })
    usage = payload.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    result = {"id": payload.get("id", "response"), "object": "response", "model": requested_model, "output": output}
    if input_tokens or output_tokens:
        result["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
    return result


def translate_responses_response(payload: dict, requested_model: str) -> dict:
    text_parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text_parts.append(str(part.get("text", "")))
    usage = payload.get("usage") or {}
    result = {
        "id": payload.get("id", "chatcmpl-response"),
        "object": "chat.completion",
        "model": requested_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(text_parts)}, "finish_reason": "stop"}],
    }
    if usage:
        result["usage"] = {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        }
    return result


async def translate_chat_stream(chunks: AsyncIterator[bytes], requested_model: str) -> AsyncIterator[bytes]:
    usage: dict[str, Any] = {}
    async for chunk in chunks:
        for raw in chunk.splitlines():
            if not raw.startswith(b"data:"):
                continue
            data = raw.split(b":", 1)[1].strip()
            if data == b"[DONE]":
                event = {"type": "response.completed", "response": {"object": "response", "model": requested_model}}
                if usage:
                    event["response"]["usage"] = {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
                continue
            try:
                item = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage.update(item.get("usage") or {})
            choices = item.get("choices") or []
            delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else {}
            text = delta.get("content") if isinstance(delta, dict) else None
            if text:
                event = {"type": "response.output_text.delta", "delta": text}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


async def translate_responses_stream(chunks: AsyncIterator[bytes], requested_model: str) -> AsyncIterator[bytes]:
    async for chunk in chunks:
        for raw in chunk.splitlines():
            if not raw.startswith(b"data:"):
                continue
            data = raw.split(b":", 1)[1].strip()
            if data == b"[DONE]":
                yield b"data: [DONE]\n\n"
                continue
            try:
                item = json.loads(data)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "response.output_text.delta":
                event = {"id": "chatcmpl-response", "object": "chat.completion.chunk", "model": requested_model, "choices": [{"index": 0, "delta": {"content": item.get("delta", "")}, "finish_reason": None}]}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
            elif item.get("type") == "response.completed":
                usage = (item.get("response") or {}).get("usage") or {}
                event = {"id": "chatcmpl-response", "object": "chat.completion.chunk", "model": requested_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0), "completion_tokens": int(usage.get("output_tokens") or 0), "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)}}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
                yield b"data: [DONE]\n\n"
