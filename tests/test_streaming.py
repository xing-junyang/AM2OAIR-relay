import json

import pytest

from relay.conversion import UpstreamProtocolError
from relay.streaming import StreamTranslator, encode_event, read_sse


def test_text_stream(text_events):
    converter = StreamTranslator("claude-opus-5-5", "resp_test")
    events = converter.begin()
    for payload in text_events:
        events.extend(converter.feed(payload))
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[2]["item"]["content"] == []
    assert events[3]["part"]["text"] == ""
    assert events[-1]["response"]["output_text"] == "Hello 世界"
    assert events[-1]["response"]["usage"]["input_tokens"] == 13
    assert events[-1]["response"]["usage"]["total_tokens"] == 18
    assert encode_event(events[-1]).startswith(b"event: response.completed\ndata: ")


def test_streamed_tool_arguments():
    converter = StreamTranslator("model", "resp_test")
    events = converter.begin()
    payloads = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "not exposed"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "call_1",
                "name": "get_weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '"Shanghai"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]
    for payload in payloads:
        events.extend(converter.feed(payload))
    assert events[2]["output_index"] == 0
    assert events[2]["item"]["arguments"] == ""
    deltas = [e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert json.loads("".join(deltas)) == {"city": "Shanghai"}
    done = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    assert done["name"] == "get_weather"
    assert done["item_id"] == events[2]["item"]["id"]
    assert events[-1]["response"]["output"][0]["call_id"] == "call_1"
    assert "not exposed" not in json.dumps(events)


def test_stream_failure():
    converter = StreamTranslator("model", "resp_test")
    events = converter.begin() + converter.fail(
        {
            "error": {"message": "Unavailable", "code": "overloaded", "param": None},
            "request_id": "req_test",
        }
    )
    assert events[-2]["type"] == "error"
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "failed"
    assert [e["sequence_number"] for e in events] == list(range(len(events)))


async def test_multiline_sse_parser():
    async def lines():
        for line in [
            ":keepalive",
            "event: ping",
            'data: {"type":',
            'data: "ping"}',
            "",
            "id: ignored",
            'data: {"type":"message_stop"}',
        ]:
            yield line

    assert [payload async for payload in read_sse(lines())] == [
        {"type": "ping"},
        {"type": "message_stop"},
    ]


async def test_bad_sse_json():
    async def lines():
        yield "data: malformed"
        yield ""

    with pytest.raises(UpstreamProtocolError):
        _ = [payload async for payload in read_sse(lines())]


def test_stream_incomplete(text_events):
    text_events[-2]["delta"]["stop_reason"] = "max_tokens"
    converter = StreamTranslator("model", "resp_test")
    events = converter.begin()
    for payload in text_events:
        events.extend(converter.feed(payload))
    assert events[-1]["type"] == "response.incomplete"
