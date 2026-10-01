import json

import pytest

from relay.conversion import (
    InvalidRequest,
    UpstreamProtocolError,
    to_anthropic,
    to_openai,
    usage_to_openai,
)


def test_plain_text(settings):
    result = to_anthropic(
        {"input": "hello", "instructions": "Be concise", "max_output_tokens": 100}, settings
    )
    assert result == {
        "model": "claude-opus-5-5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "system": "Be concise",
        "max_tokens": 100,
        "stream": False,
    }


def test_roles_and_history(settings):
    result = to_anthropic(
        {
            "instructions": "One",
            "input": [
                {"role": "system", "content": "Two"},
                {"role": "developer", "content": [{"type": "input_text", "text": "Three"}]},
                {"role": "user", "content": "question"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                    "phase": "final_answer",
                },
                {"role": "user", "content": "follow up"},
            ],
        },
        settings,
    )
    assert result["system"] == "One\n\nTwo\n\nThree"
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "user"]


def test_tools_calls_and_results(settings):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    result = to_anthropic(
        {
            "input": [
                {"role": "user", "content": "weather"},
                {
                    "type": "function_call",
                    "call_id": "toolu_1",
                    "name": "weather",
                    "arguments": '{"city":"Shanghai"}',
                },
                {
                    "type": "function_call",
                    "call_id": "toolu_2",
                    "name": "weather",
                    "arguments": '{"city":"Beijing"}',
                },
                {"type": "function_call_output", "call_id": "toolu_1", "output": "sunny"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": [{"type": "text", "text": "rain"}],
                    "is_error": True,
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "weather",
                    "description": "Weather",
                    "parameters": schema,
                    "strict": True,
                }
            ],
            "tool_choice": "required",
        },
        settings,
    )
    assert result["tools"] == [
        {"name": "weather", "description": "Weather", "input_schema": schema}
    ]
    assert result["tool_choice"] == {"type": "any"}
    assert len(result["messages"]) == 3
    assert result["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "weather",
        "input": {"city": "Shanghai"},
    }
    assert result["messages"][2]["content"][1]["is_error"] is True
    assert result["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


def test_nested_anthropic_tool_blocks(settings):
    result = to_anthropic(
        {
            "input": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call1", "name": "f", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "done"}],
                },
            ]
        },
        settings,
    )
    assert result["messages"][1]["content"][0]["content"] == "done"


def test_ignored_optional_fields(settings):
    result = to_anthropic(
        {
            "input": [
                {"type": "reasoning", "encrypted_content": "opaque"},
                {"role": "user", "content": "hi"},
            ],
            "model": "client-alias",
            "reasoning": {"effort": "high"},
            "include": ["reasoning.encrypted_content"],
            "store": True,
            "parallel_tool_calls": False,
            "prompt_cache_key": "some-key",
            "text": {"verbosity": "high", "format": {"type": "json_schema"}},
        },
        settings,
    )
    assert set(result) == {"model", "messages", "max_tokens", "stream"}
    assert result["model"] == settings.model


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ({"type": "function", "name": "f"}, {"type": "tool", "name": "f"}),
    ],
)
def test_tool_choice(settings, choice, expected):
    result = to_anthropic(
        {"input": "hi", "tools": [{"type": "function", "name": "f"}], "tool_choice": choice},
        settings,
    )
    assert result["tool_choice"] == expected


def test_no_tools_choice(settings):
    result = to_anthropic(
        {"input": "hi", "tools": [{"type": "function", "name": "f"}], "tool_choice": "none"},
        settings,
    )
    assert "tools" not in result


@pytest.mark.parametrize(
    "body",
    [
        {"input": None},
        {"input": "hi", "max_output_tokens": -1},
        {"input": "hi", "stream": "yes"},
        {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
        {"input": "hi", "tools": [{"type": "web_search"}]},
        {"input": "hi", "previous_response_id": "resp_prior"},
        {
            "input": [
                {"type": "function_call", "call_id": "c", "name": "f", "arguments": "not JSON"}
            ]
        },
    ],
)
def test_unsupported_or_invalid_requests_are_explicit(settings, body):
    with pytest.raises(InvalidRequest):
        to_anthropic(body, settings)


def test_nonstream_response(message, settings):
    result = to_openai(message, settings.model, "resp_test")
    assert result["id"] == "resp_test"
    assert result["status"] == "completed"
    assert result["output_text"] == "Hello"
    assert result["output"][0]["content"][0] == {
        "type": "output_text",
        "text": "Hello",
        "annotations": [],
    }
    assert result["usage"]["total_tokens"] == 15


def test_nonstream_function_call(message, settings):
    message["content"].append(
        {"type": "tool_use", "id": "toolu_abc", "name": "f", "input": {"path": "file"}}
    )
    message["stop_reason"] = "tool_use"
    result = to_openai(message, settings.model)
    call = result["output"][1]
    assert call["type"] == "function_call"
    assert call["call_id"] == "toolu_abc"
    assert json.loads(call["arguments"]) == {"path": "file"}
    assert result["status"] == "completed"


def test_max_tokens_incomplete(message, settings):
    message["stop_reason"] = "max_tokens"
    result = to_openai(message, settings.model)
    assert result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": "max_output_tokens"}


def test_cache_usage_accounting():
    result = usage_to_openai(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 5,
        }
    )
    assert result["input_tokens"] == 60
    assert result["input_tokens_details"]["cached_tokens"] == 30
    assert result["total_tokens"] == 65


def test_invalid_upstream_response(settings):
    with pytest.raises(UpstreamProtocolError):
        to_openai({"type": "message", "content": [{"type": "text", "text": None}]}, settings.model)
