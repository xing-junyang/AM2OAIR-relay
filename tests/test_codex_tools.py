import json
import re
from pathlib import Path

import httpx
import pytest
from test_app import client_for, parse_events, stream_response

from relay.conversion import (
    InvalidRequest,
    ToolRegistry,
    UpstreamProtocolError,
    to_anthropic,
    to_openai,
)
from relay.database import Database
from relay.streaming import StreamTranslator

PATCH = '*** Begin Patch\n*** Add File: hello.txt\n+hello 世界 "quoted" \\ path\n*** End Patch'
TOOLS = [
    {
        "type": "namespace",
        "name": "functions",
        "description": "Local workspace tools",
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
            {
                "type": "custom",
                "name": "apply_patch",
                "format": {
                    "type": "grammar",
                    "syntax": "lark",
                    "definition": 'start: "*** Begin Patch"',
                },
            },
        ],
    }
]


def payloads(name, arguments):
    encoded = json.dumps(arguments, ensure_ascii=True)
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": name, "id": "call_patch", "input": {}},
        },
    ]
    # Split inside JSON escape sequences, including unicode, to ensure custom
    # events contain decoded text rather than fragments of the JSON wrapper.
    for index in range(0, len(encoded), 3):
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": encoded[index : index + 3]},
            }
        )
    return events + [
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]


def test_actual_codex_cli_tools_are_accepted(settings):
    tools = json.loads((Path(__file__).parent / "fixtures/codex_0_159_2_tools.json").read_text())
    converted = to_anthropic({"input": "hi", "tools": tools}, settings)
    assert len(converted["tools"]) == 12
    assert any("spawn_agent" in tool["name"] for tool in converted["tools"])
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"]) for tool in converted["tools"])


def test_custom_and_namespace_history_roundtrip(settings):
    registry = ToolRegistry(TOOLS)
    alias = registry.by_identity[("functions", "apply_patch")].upstream_name
    message = {
        "type": "message",
        "content": [
            {"type": "tool_use", "name": alias, "id": "call_patch", "input": {"input": PATCH}}
        ],
        "usage": {"input_tokens": 12, "output_tokens": 9},
        "stop_reason": "tool_use",
    }
    response = to_openai(message, settings.model, registry=registry)
    item = response["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["namespace"] == "functions" and item["name"] == "apply_patch"
    assert item["input"] == PATCH and "arguments" not in item
    body = {
        "input": [
            {"role": "user", "content": "change file"},
            item,
            {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "Success"},
        ],
        "tools": TOOLS,
    }
    converted = to_anthropic(body, settings)
    assert converted["messages"][1]["content"] == [
        {"type": "tool_use", "id": "call_patch", "name": alias, "input": {"input": PATCH}}
    ]
    assert converted["messages"][2]["content"][0]["tool_use_id"] == "call_patch"
    assert converted["messages"][2]["content"][0]["content"] == "Success"
    # Removing definitions from a later turn does not change history aliases.
    body.pop("tools")
    assert to_anthropic(body, settings)["messages"] == converted["messages"]
    target = registry.tools[1]
    assert target["input_schema"]["required"] == ["input"]
    assert 'start: "*** Begin Patch"' in target["description"]


def test_namespace_function_response_and_choice(settings):
    registry = ToolRegistry(TOOLS)
    alias = registry.by_identity[("functions", "exec_command")].upstream_name
    body = {
        "input": "run",
        "tools": TOOLS,
        "tool_choice": {"type": "function", "namespace": "functions", "name": "exec_command"},
    }
    assert to_anthropic(body, settings)["tool_choice"] == {"type": "tool", "name": alias}
    events = StreamTranslator(settings.model, "resp_ns", registry=registry)
    result = events.begin()
    for payload in payloads(alias, {"cmd": "pwd"}):
        result.extend(events.feed(payload))
    item = result[-1]["response"]["output"][0]
    assert item["namespace"] == "functions" and item["name"] == "exec_command"
    assert json.loads(item["arguments"]) == {"cmd": "pwd"}
    history = to_anthropic(
        {
            "input": [
                item,
                {"type": "function_call_output", "call_id": item["call_id"], "output": "/tmp"},
            ],
            "tools": TOOLS,
        },
        settings,
    )
    assert history["messages"][0]["content"][0]["name"] == alias


def test_tool_aliases_are_stable_and_collision_safe():
    tools = [{"type": "function", "name": "x"}, {"type": "function", "name": "am2oair_x_reserved"}]
    for namespace in ("one", "two", "one__two"):
        tools.append(
            {"type": "namespace", "name": namespace, "tools": [{"type": "function", "name": "x"}]}
        )
    left, right = ToolRegistry(tools), ToolRegistry(list(reversed(tools)))
    assert len(left.by_upstream) == 5
    assert {key: value.upstream_name for key, value in left.by_identity.items()} == {
        key: value.upstream_name for key, value in right.by_identity.items()
    }


def test_requests_do_not_share_tool_types(settings):
    def handler(request):
        tool = json.loads(request.content)["tools"][0]
        custom = "input" in tool["input_schema"]["properties"]
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "c",
                        "name": tool["name"],
                        "input": {"input": "OK"} if custom else {},
                    }
                ],
                "usage": {},
                "stop_reason": "tool_use",
            },
        )

    with client_for(settings, handler) as client:
        for kind in ("custom", "function", "custom"):
            result = client.post(
                "/v1/responses", json={"input": "echo", "tools": [{"type": kind, "name": "echo"}]}
            ).json()
            item = result["output"][0]
            assert item["type"] == ("custom_tool_call" if kind == "custom" else "function_call")
            assert item.get("input") == ("OK" if kind == "custom" else None)


def test_initial_custom_arguments_and_redaction(settings):
    registry = ToolRegistry([{"type": "custom", "name": "patch"}])
    translator = StreamTranslator(settings.model, "resp_initial", (settings.api_key,), registry)
    events = translator.begin()
    for payload in [
        {"type": "message_start", "message": {}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "name": "patch",
                "id": "c",
                "input": {"input": 'line\n"' + settings.api_key},
            },
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]:
        events.extend(translator.feed(payload))
    assert events[-1]["response"]["output"][0]["input"] == 'line\n"[REDACTED]'
    assert settings.api_key not in json.dumps(events)


@pytest.mark.parametrize("namespace", [None, "functions"])
@pytest.mark.parametrize("raw_input", [PATCH, ""])
def test_custom_sse_uses_custom_events(settings, namespace, raw_input):
    tools = [{"type": "custom", "name": "apply_patch"}]
    if namespace:
        tools = [{"type": "namespace", "name": namespace, "tools": tools}]
    registry = ToolRegistry(tools)
    alias = registry.by_identity[(namespace, "apply_patch")].upstream_name
    translator = StreamTranslator(settings.model, "resp_custom", registry=registry)
    events = translator.begin()
    for payload in payloads(alias, {"input": raw_input}):
        events.extend(translator.feed(payload))
    types = [event["type"] for event in events]
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert "response.function_call_arguments.delta" not in types
    assert "response.function_call_arguments.done" not in types
    assert "response.custom_tool_call_input.done" in types
    assert (
        "".join(e["delta"] for e in events if e["type"] == "response.custom_tool_call_input.delta")
        == raw_input
    )
    added = next(e["item"] for e in events if e["type"] == "response.output_item.added")
    assert added["type"] == "custom_tool_call" and added["input"] == ""
    assert events[-1]["response"]["output"][0]["input"] == raw_input
    assert events[-1]["response"]["status"] == "completed"


@pytest.mark.parametrize("stream", [False, True])
def test_custom_endpoint_history_and_privacy(settings, stream, caplog):
    prompt = "private-custom-prompt"
    output = 'private-patch-output 世界 "quote"'
    authorization, cookie = "private-client-auth", "private-cookie"
    raw_input = " ".join((output, settings.api_key, authorization, cookie))
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if calls == 2:
            assert body["messages"][1]["content"][0]["input"]["input"].startswith(output)
            assert body["messages"][2]["content"][0]["content"] == "private-tool-result"
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "stop_reason": "end_turn",
                },
            )
        alias = body["tools"][1]["name"]
        if stream:
            return stream_response(payloads(alias, {"input": raw_input}))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_patch",
                        "name": alias,
                        "input": {"input": raw_input},
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 9},
                "stop_reason": "tool_use",
            },
        )

    with client_for(settings, handler) as client:
        response = client.post(
            "/v1/responses",
            headers={"authorization": "Bearer " + authorization, "cookie": cookie},
            json={"input": prompt, "tools": TOOLS, "stream": stream},
        )
        assert response.status_code == 200
        result = parse_events(response)[-1]["response"] if stream else response.json()
        item = result["output"][0]
        assert item["type"] == "custom_tool_call" and item["namespace"] == "functions"
        assert "[REDACTED]" in item["input"]
        history = [
            {"role": "user", "content": prompt},
            item,
            {
                "type": "custom_tool_call_output",
                "call_id": item["call_id"],
                "output": "private-tool-result",
            },
        ]
        assert (
            client.post("/v1/responses", json={"input": history, "tools": TOOLS}).status_code == 200
        )
        admin = json.dumps(
            [client.get("/api/admin/" + path).json() for path in ("status", "logs", "stats")]
        )
        db = Database(settings.database_url)
        with db.connect() as connection:
            dump = "\n".join(connection.iterdump())
        for secret in (
            prompt,
            output,
            "private-tool-result",
            settings.api_key,
            authorization,
            cookie,
        ):
            assert secret not in admin + dump + caplog.text
        for secret in (settings.api_key, authorization, cookie):
            assert secret not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"input": "hi", "tools": [{"type": "namespace", "name": "f", "tools": {}}]},
        {
            "input": "hi",
            "tools": [{"type": "namespace", "name": "f", "tools": [{"type": "web_search"}]}],
        },
        {"input": "hi", "tools": [{"type": ["function"], "name": "f"}]},
        {"input": [{"type": ["function_call"]}]},
        {
            "input": [
                {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c", "input": {}}
            ]
        },
        {
            "input": "hi",
            "tools": [{"type": "function", "name": "f"}],
            "tool_choice": {"type": "function", "function": "invalid"},
        },
    ],
)
def test_invalid_tool_shapes_are_400(settings, body):
    with pytest.raises(InvalidRequest):
        to_anthropic(body, settings)
    with client_for(
        settings, lambda request: pytest.fail("Invalid request must not reach upstream")
    ) as client:
        assert client.post("/v1/responses", json=body).status_code == 400


@pytest.mark.parametrize("arguments", [{}, {"input": 1}, {"input": None}])
def test_invalid_custom_upstream_input_is_protocol_error(settings, arguments):
    registry = ToolRegistry([{"type": "custom", "name": "patch"}])
    message = {
        "type": "message",
        "content": [{"type": "tool_use", "id": "c", "name": "patch", "input": arguments}],
    }
    with pytest.raises(UpstreamProtocolError):
        to_openai(message, settings.model, registry=registry)
    translator = StreamTranslator(settings.model, "resp_invalid", registry=registry)
    with pytest.raises(UpstreamProtocolError):
        for payload in payloads("patch", arguments):
            translator.feed(payload)
