import asyncio

from model_router.translation import (
    translate_chat_request,
    translate_chat_response,
    translate_responses_request,
    translate_chat_stream,
)


def test_responses_request_translates_to_chat_messages_and_tools():
    payload = {
        "model": "A/gpt",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "max_output_tokens": 32,
        "tools": [{"type": "function", "name": "lookup", "description": "find", "parameters": {"type": "object"}}],
    }
    result = translate_responses_request(payload)
    assert result["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert result["max_tokens"] == 32
    assert result["tools"][0]["function"]["name"] == "lookup"


def test_chat_request_translates_to_responses_input():
    payload = {"model": "A/gpt", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}
    result = translate_chat_request(payload)
    assert result["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert result["max_output_tokens"] == 32


def test_chat_response_translates_to_responses_output_and_usage():
    payload = {"id": "chatcmpl-1", "model": "gpt", "choices": [{"message": {"role": "assistant", "content": "hello"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
    result = translate_chat_response(payload, "A/gpt")
    assert result["output"][0]["content"][0]["text"] == "hello"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    assert result["model"] == "A/gpt"


def test_chat_stream_translates_text_deltas_and_done():
    async def source():
        yield b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"llo"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
        yield b'data: [DONE]\n\n'

    async def run():
        return [chunk async for chunk in translate_chat_stream(source(), "A/gpt")]

    output = b"".join(asyncio.run(run())).decode()
    assert 'response.output_text.delta' in output
    assert '"delta": "he"' in output
    assert 'response.completed' in output


def test_responses_response_translates_to_chat_completion():
    from model_router.translation import translate_responses_response
    result = translate_responses_response({"id": "r1", "model": "gpt", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]}, "gpt")
    assert result["choices"][0]["message"]["content"] == "hello"


def test_responses_stream_translates_text_deltas_to_chat_chunks():
    from model_router.translation import translate_responses_stream
    async def source():
        yield b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    async def run():
        return [chunk async for chunk in translate_responses_stream(source(), "gpt")]
    output = b"".join(asyncio.run(run())).decode()
    assert '"content": "hi"' in output
    assert 'data: [DONE]' in output


def test_chat_tool_call_response_maps_to_responses_function_call():
    payload = {"id": "chatcmpl-1", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "function": {"name": "lookup", "arguments": '{"q":"x"}'}}]}}]}
    result = translate_chat_response(payload, "A/gpt")
    assert result["output"][0]["type"] == "function_call"
    assert result["output"][0]["name"] == "lookup"
    assert result["output"][0]["arguments"] == '{"q":"x"}'
