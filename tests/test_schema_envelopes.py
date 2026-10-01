import copy
import json

import httpx
import pytest
from test_app import client_for, parse_events, stream_response
from test_codex_tools import payloads

from relay.conversion import (
    InvalidRequest,
    ToolRegistry,
    UpstreamProtocolError,
    to_anthropic,
    to_openai,
)
from relay.database import Database
from relay.schemas import ROOT_COMBINATORS, adapt_tool_schema
from relay.streaming import StreamTranslator


def schema_for(combinator):
    branch = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    return {combinator: [branch]}


@pytest.mark.parametrize("combinator", ROOT_COMBINATORS)
@pytest.mark.parametrize("namespace", [None, "mcp_tools"])
def test_root_composition_preserved_under_envelope(settings, combinator, namespace):
    schema = schema_for(combinator)
    original = copy.deepcopy(schema)
    tools = [{"type": "function", "name": "echo", "parameters": schema}]
    if namespace:
        tools = [{"type": "namespace", "name": namespace, "tools": tools}]
    registry = ToolRegistry(tools)
    converted = to_anthropic({"input": "echo", "tools": tools}, settings, registry)
    target = converted["tools"][0]
    assert not set(ROOT_COMBINATORS).intersection(target["input_schema"])
    assert target["input_schema"]["type"] == "object"
    assert target["input_schema"]["required"] == ["arguments"]
    assert target["input_schema"]["additionalProperties"] is False
    assert target["input_schema"]["properties"]["arguments"] == original
    assert schema == original
    alias = target["name"]
    message = {
        "type": "message",
        "content": [
            {"type": "tool_use", "id": "c", "name": alias, "input": {"arguments": {"value": "OK"}}}
        ],
        "usage": {},
        "stop_reason": "tool_use",
    }
    response = to_openai(message, settings.model, registry=registry)
    item = response["output"][0]
    assert item["name"] == "echo" and item.get("namespace") == namespace
    assert json.loads(item["arguments"]) == {"value": "OK"}
    history = [item, {"type": "function_call_output", "call_id": "c", "output": "OK"}]
    replay = to_anthropic({"input": history, "tools": tools}, settings)
    assert replay["messages"][0]["content"][0]["input"] == {"arguments": {"value": "OK"}}
    assert replay["messages"][1]["content"][0]["tool_use_id"] == "c"


def test_local_refs_relocated_without_modifying_data():
    schema = {
        "$defs": {"Value": {"type": "string"}},
        "oneOf": [
            {
                "type": "object",
                "properties": {"value": {"$ref": "#/$defs/Value"}, "child": {"$ref": "#"}},
                "required": ["value"],
            }
        ],
        "default": {"$ref": "#/literal-data"},
        "examples": [{"$ref": "#/literal-example"}],
        "const": {"$ref": "#/literal-const"},
    }
    original = copy.deepcopy(schema)
    target, wrapped = adapt_tool_schema(schema)
    assert wrapped
    nested = target["properties"]["arguments"]
    properties = nested["oneOf"][0]["properties"]
    assert properties["value"]["$ref"] == "#/properties/arguments/$defs/Value"
    assert properties["child"]["$ref"] == "#/properties/arguments"
    assert nested["default"] == original["default"]
    assert nested["examples"] == original["examples"]
    assert nested["const"] == original["const"]
    assert schema == original


def test_embedded_resource_and_external_refs_keep_their_scope():
    schema = {
        "allOf": [
            {
                "$id": "https://schema.test/inner",
                "$defs": {"Value": {"type": "string"}},
                "type": "object",
                "properties": {"value": {"$ref": "#/$defs/Value"}},
            }
        ],
        "properties": {"remote": {"$ref": "https://schema.test/remote#/$defs/Value"}},
    }
    target, wrapped = adapt_tool_schema(schema)
    assert wrapped
    nested = target["properties"]["arguments"]
    assert nested["allOf"][0]["properties"]["value"]["$ref"] == "#/$defs/Value"
    assert nested["properties"]["remote"] == schema["properties"]["remote"]
    schema["$id"] = "https://schema.test/original"
    schema["$ref"] = "#/properties/remote"
    target, _ = adapt_tool_schema(schema)
    assert target["properties"]["arguments"] == schema


def test_ordinary_and_nested_composition_schemas_are_unchanged():
    schema = {
        "type": "object",
        "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["value"],
    }
    assert adapt_tool_schema(schema) == (schema, False)
    registry = ToolRegistry([{"type": "function", "name": "echo", "parameters": schema}])
    assert not registry.wraps_arguments("echo")
    assert registry.tools[0]["input_schema"] == schema


def test_schema_dialect_and_draft4_resource_scope():
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "id": "https://schema.test/root",
        "definitions": {"Value": {"type": "string"}},
        "allOf": [{"properties": {"value": {"$ref": "#/definitions/Value"}}}],
    }
    target, wrapped = adapt_tool_schema(schema)
    assert wrapped and target["$schema"] == schema["$schema"]
    assert target["properties"]["arguments"] == schema


def test_recursive_schema_without_resource_id_returns_400(settings):
    schema = {
        "$schema": "https://json-schema.org/draft/2019-09/schema",
        "$recursiveAnchor": True,
        "anyOf": [{"properties": {"next": {"$recursiveRef": "#"}}}],
    }
    body = {"input": "hi", "tools": [{"type": "function", "name": "f", "parameters": schema}]}
    with pytest.raises(InvalidRequest, match="explicit"):
        to_anthropic(body, settings)
    with client_for(
        settings, lambda request: pytest.fail("Invalid schema must not reach upstream")
    ) as client:
        assert client.post("/v1/responses", json=body).status_code == 400
    schema["$id"] = "https://schema.test/recursive"
    wrapped, _ = adapt_tool_schema(schema)
    assert wrapped["properties"]["arguments"] == schema


@pytest.mark.parametrize("combinator", ROOT_COMBINATORS)
def test_streamed_function_arguments_are_unwrapped(combinator):
    registry = ToolRegistry(
        [{"type": "function", "name": "echo", "parameters": schema_for(combinator)}]
    )
    translator = StreamTranslator("model", "resp_schema", ("private-key",), registry)
    events = translator.begin()
    for payload in payloads(
        "echo",
        {"arguments": {"value": '世界 "quoted"\nprivate-key', "arguments": "original property"}},
    ):
        events.extend(translator.feed(payload))
    arguments = {"value": '世界 "quoted"\n[REDACTED]', "arguments": "original property"}
    deltas = [e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert json.loads("".join(deltas)) == arguments
    done = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    assert json.loads(done["arguments"]) == arguments
    assert events[2]["item"]["arguments"] == ""
    assert json.loads(events[-1]["response"]["output"][0]["arguments"]) == arguments
    assert events[-1]["type"] == "response.completed"
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert "private-key" not in json.dumps(events)


@pytest.mark.parametrize("stream", [False, True])
def test_index_16_tool_roundtrip_and_privacy(settings, stream, caplog):
    schema = schema_for("oneOf")
    tools = [{"type": "function", "name": f"unused_{index}"} for index in range(16)]
    tools.append({"type": "function", "name": "custom", "parameters": schema})
    prompt, output = "private-composition-prompt", "private-composition-output"
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        for tool in body["tools"]:
            assert not set(ROOT_COMBINATORS).intersection(tool["input_schema"])
        if count == 2:
            assert body["messages"][1]["content"][0]["input"] == {"arguments": {"value": output}}
            assert body["messages"][2]["content"][0]["content"] == "private-tool-result"
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {},
                    "stop_reason": "end_turn",
                },
            )
        if stream:
            return stream_response(payloads("custom", {"arguments": {"value": output}}))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "custom",
                        "id": "call_patch",
                        "input": {"arguments": {"value": output}},
                    }
                ],
                "usage": {},
                "stop_reason": "tool_use",
            },
        )

    with client_for(settings, handler) as client:
        response = client.post(
            "/v1/responses", json={"input": prompt, "tools": tools, "stream": stream}
        )
        assert response.status_code == 200
        result = parse_events(response)[-1]["response"] if stream else response.json()
        call = result["output"][0]
        assert json.loads(call["arguments"]) == {"value": output}
        replay = client.post(
            "/v1/responses",
            json={
                "input": [
                    {"role": "user", "content": prompt},
                    call,
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": "private-tool-result",
                    },
                ],
                "tools": tools,
            },
        )
        assert replay.status_code == 200
        admin = json.dumps(
            [client.get("/api/admin/" + path).json() for path in ("logs", "status", "stats")]
        )
        db = Database(settings.database_url)
        with db.connect() as connection:
            dump = "\n".join(connection.iterdump())
        for marker in (prompt, output, "private-tool-result", settings.api_key):
            assert marker not in admin + dump + caplog.text


@pytest.mark.parametrize(
    "envelope", [{}, {"arguments": "bad"}, {"arguments": None}, {"arguments": []}]
)
def test_invalid_envelopes_fail_as_protocol_errors(settings, envelope):
    registry = ToolRegistry(
        [{"type": "function", "name": "echo", "parameters": schema_for("anyOf")}]
    )
    message = {
        "type": "message",
        "content": [{"type": "tool_use", "name": "echo", "id": "c", "input": envelope}],
    }
    with pytest.raises(UpstreamProtocolError):
        to_openai(message, settings.model, registry=registry)
    translator = StreamTranslator(settings.model, "resp_invalid", registry=registry)
    with pytest.raises(UpstreamProtocolError):
        for payload in payloads("echo", envelope):
            translator.feed(payload)


def test_initial_function_envelope_and_mixed_tool_blocks():
    registry = ToolRegistry(
        [
            {"type": "function", "name": "echo", "parameters": schema_for("allOf")},
            {"type": "custom", "name": "patch"},
        ]
    )
    translator = StreamTranslator("model", "resp_mixed", registry=registry)
    events = translator.begin()
    sequence = [
        {"type": "message_start", "message": {}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "name": "echo",
                "id": "c1",
                "input": {"arguments": {}},
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "name": "patch",
                "id": "c2",
                "input": {"input": "raw patch"},
            },
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    for payload in sequence:
        events.extend(translator.feed(payload))
    output = events[-1]["response"]["output"]
    assert json.loads(output[0]["arguments"]) == {}
    assert output[1]["type"] == "custom_tool_call" and output[1]["input"] == "raw patch"


def test_credentials_in_argument_keys_are_redacted():
    secret = 'private-key-"\\-世界'
    registry = ToolRegistry(
        [{"type": "function", "name": "echo", "parameters": schema_for("oneOf")}]
    )
    translator = StreamTranslator("model", "resp_keys", (secret,), registry)
    events = translator.begin()
    for payload in payloads("echo", {"arguments": {secret: [{secret: secret}]}}):
        events.extend(translator.feed(payload))
    item = events[-1]["response"]["output"][0]
    assert json.loads(item["arguments"]) == {"[REDACTED]": [{"[REDACTED]": "[REDACTED]"}]}
    for event in events:
        if event["type"] == "response.function_call_arguments.delta":
            assert secret not in event["delta"]
