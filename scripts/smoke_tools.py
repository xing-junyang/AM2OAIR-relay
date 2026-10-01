#!/usr/bin/env python3
"""Live function-call SSE and tool-result roundtrip; metadata-only output."""

from __future__ import annotations

import argparse
import json
import sys

from smoke_docker import fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("function", "custom", "namespace"), default="function")
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
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert events[-1]["type"] == "response.completed"
    assert any(event["type"] == delta_type for event in events)
    assert any(event["type"] == done_type for event in events)
    first = events[-1]["response"]
    calls = [item for item in first["output"] if item["type"] == call_type]
    assert calls and all(call["name"] == "echo" for call in calls)
    history = [prompt, *first["output"]]
    for call in calls:
        if kind == "custom":
            value = call["input"]
        else:
            arguments = json.loads(call["arguments"])
            value = arguments.get("value")
        assert isinstance(value, str)
        if kind == "namespace":
            assert call["namespace"] == "smoke"
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
            "max_output_tokens": 64,
        },
    )
    final = json.loads(body)
    assert final["status"] == "completed" and final["output_text"]
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
        print(
            f"Tool smoke failed ({type(exc).__name__}); no sensitive body printed", file=sys.stderr
        )
        raise SystemExit(1) from None
