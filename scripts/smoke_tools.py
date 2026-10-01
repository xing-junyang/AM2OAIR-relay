#!/usr/bin/env python3
"""Live function-call SSE and tool-result roundtrip; metadata-only output."""

from __future__ import annotations

import argparse
import json
import sys

from smoke_docker import fetch


class SmokeCheckError(Exception):
    """Fixed metadata-only assertion labels, safe for console output."""


def check(condition, label):
    if not condition:
        raise SmokeCheckError(label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        choices=("function", "custom", "namespace", "oneOf", "allOf", "anyOf"),
        default="function",
    )
    kind = parser.parse_args().kind
    prompt = {
        "role": "user",
        "content": "Call echo with value OK. After receiving its result, reply with OK.",
    }
    tools = [
        {
            "type": "function",
            "name": "echo",
            "description": "Echo a value supplied by the caller",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }
    ]
    call_type, result_type = "function_call", "function_call_output"
    delta_type = "response.function_call_arguments.delta"
    done_type = "response.function_call_arguments.done"
    if kind == "custom":
        tools = [
            {
                "type": "custom",
                "name": "echo",
                "description": "Echo the raw text input exactly",
                "format": {"type": "text"},
            }
        ]
        prompt["content"] = (
            "Call echo with the raw text OK as its input. After receiving its result, reply with OK."
        )
        call_type, result_type = "custom_tool_call", "custom_tool_call_output"
        delta_type, done_type = (
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
        )
    elif kind == "namespace":
        tools = [
            {
                "type": "namespace",
                "name": "smoke",
                "description": "Local echo tools",
                "tools": tools,
            }
        ]
        prompt["content"] = (
            "Call smoke.echo with value OK. After receiving its result, reply with OK."
        )
    elif kind in ("oneOf", "anyOf", "allOf"):
        original = tools[0]["parameters"]
        if kind == "allOf":
            tools[0]["parameters"] = {
                **original,
                "allOf": [
                    {"properties": {"value": {"minLength": 1}}},
                    {"properties": {"value": {"const": "OK"}}},
                ],
            }
        else:
            tools[0]["parameters"] = {
                kind: [
                    original,
                    {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                ]
            }
    body, _ = fetch(
        "/v1/responses",
        {
            "input": [prompt],
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "max_output_tokens": 128,
        },
    )
    events = [
        json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")
    ]
    check(
        [event["sequence_number"] for event in events] == list(range(len(events))),
        "SSE sequence numbers",
    )
    check(events and events[-1]["type"] == "response.completed", "SSE completion")
    check(any(event["type"] == delta_type for event in events), "Tool argument delta")
    check(any(event["type"] == done_type for event in events), "Tool argument done")
    first = events[-1]["response"]
    calls = [item for item in first["output"] if item["type"] == call_type]
    check(calls and all(call["name"] == "echo" for call in calls), "Tool name roundtrip")
    history = [prompt, *first["output"]]
    for call in calls:
        if kind == "custom":
            value = call["input"]
        else:
            arguments = json.loads(call["arguments"])
            value = arguments.get("value")
        check(isinstance(value, str), "Original string argument roundtrip")
        if kind == "namespace":
            check(call["namespace"] == "smoke", "Namespace roundtrip")
        history.append(
            {
                "type": result_type,
                "call_id": call["call_id"],
                "output": value,
            }
        )
    body, _ = fetch(
        "/v1/responses",
        {
            "input": history,
            "tools": tools,
            "tool_choice": "none",
            "stream": False,
            "max_output_tokens": 512,
        },
    )
    final = json.loads(body)
    check(final["status"] == "completed", "Tool result response completion")
    check(bool(final["output_text"]), "Tool result response text")
    print(
        json.dumps(
            {
                "kind": kind,
                "tool_call_sse": "passed",
                "tool_result_roundtrip": "passed",
                "calls": len(calls),
                "new_requests": 2,
                "total_tokens": first["usage"]["total_tokens"] + final["usage"]["total_tokens"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        label = f": {exc}" if isinstance(exc, SmokeCheckError) else ""
        print(
            f"Tool smoke failed ({type(exc).__name__}{label}); no sensitive body printed",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
