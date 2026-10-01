from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator

from .conversion import (
    ToolRegistry,
    UpstreamProtocolError,
    custom_tool_input,
    finish_response,
    response_shell,
    usage_to_openai,
)
from .errors import CredentialFilter, scrub_credentials


async def read_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """Parse SSE boundaries, including comments, CRLF and multiline data."""
    data: list[str] = []
    size = 0

    def parse() -> dict:
        try:
            payload = json.loads("\n".join(data))
        except ValueError:
            raise UpstreamProtocolError("Invalid upstream SSE JSON") from None
        if not isinstance(payload, dict):
            raise UpstreamProtocolError("Upstream SSE data must be an object")
        return payload

    async for line in lines:
        if not line:
            if data:
                yield parse()
                data, size = [], 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            data.append(value)
            size += len(value)
            if size > 1_048_576:
                raise UpstreamProtocolError("Upstream SSE event exceeded size limit")
        # event/id/retry and :comments don't affect the JSON semantic event.
    if data:
        yield parse()


class StreamTranslator:
    def __init__(
        self,
        model: str,
        response_id: str,
        secrets: tuple[str, ...] = (),
        registry: ToolRegistry | None = None,
    ):
        self.response = response_shell(model, response_id)
        self.sequence = 0
        self.blocks: dict[int, dict | None] = {}
        self.usage: dict = {}
        self.stop_reason: str | None = None
        self.started = False
        self.terminal = False
        self.secrets = secrets
        self.registry = registry or ToolRegistry()

    def event(self, kind: str, **fields) -> dict:
        value = {**copy.deepcopy(fields), "type": kind, "sequence_number": self.sequence}
        self.sequence += 1
        return value

    def begin(self) -> list[dict]:
        return [
            self.event("response.created", response=self.response),
            self.event("response.in_progress", response=self.response),
        ]

    def item_fields(self, state: dict) -> dict:
        return {
            "response_id": self.response["id"],
            "item_id": state["item"]["id"],
            "output_index": state["output_index"],
        }

    def delta(self, state: dict, value: str, final: bool = False) -> list[dict]:
        if state["item"]["type"] == "custom_tool_call":
            # Anthropic streams JSON, while Responses custom tools stream raw
            # text. Decode only the complete wrapper, never emit JSON escapes or
            # an unfinished escape sequence as executable custom tool input.
            state["raw_arguments"] += value
            return []
        value = state["credential_filter"].push(value, final)
        if not value:
            return []
        item = state["item"]
        if item["type"] == "message":
            item["content"][0]["text"] += value
            return [
                self.event(
                    "response.output_text.delta",
                    **self.item_fields(state),
                    content_index=0,
                    delta=value,
                    logprobs=[],
                )
            ]
        item["arguments"] += value
        return [
            self.event(
                "response.function_call_arguments.delta", **self.item_fields(state), delta=value
            )
        ]

    def finish_item(self, state: dict) -> list[dict]:
        if state["done"]:
            raise UpstreamProtocolError("Duplicate content block stop")
        item = state["item"]
        fields = self.item_fields(state)
        events = self.delta(state, "", final=True)
        if item["type"] == "message":
            part = item["content"][0]
            events.append(
                self.event(
                    "response.output_text.done",
                    **fields,
                    content_index=0,
                    text=part["text"],
                    logprobs=[],
                )
            )
            events.append(
                self.event("response.content_part.done", **fields, content_index=0, part=part)
            )
        elif item["type"] == "custom_tool_call":
            try:
                arguments = json.loads(state["raw_arguments"])
            except ValueError:
                raise UpstreamProtocolError("Invalid streamed custom tool arguments") from None
            if not isinstance(arguments, dict):
                raise UpstreamProtocolError("Custom tool arguments must be an object")
            item["input"] = scrub_credentials(custom_tool_input(arguments), self.secrets)
            if item["input"]:
                events.append(
                    self.event(
                        "response.custom_tool_call_input.delta", **fields, delta=item["input"]
                    )
                )
            events.append(
                self.event("response.custom_tool_call_input.done", **fields, input=item["input"])
            )
        else:
            if not item["arguments"]:
                events.extend(self.delta(state, "{}"))
            try:
                arguments = json.loads(item["arguments"])
            except ValueError:
                raise UpstreamProtocolError("Invalid streamed function arguments") from None
            if not isinstance(arguments, dict):
                raise UpstreamProtocolError("Streamed function arguments must be an object")
            events.append(
                self.event(
                    "response.function_call_arguments.done",
                    **fields,
                    arguments=item["arguments"],
                    name=item["name"],
                )
            )
        item["status"] = "completed"
        state["done"] = True
        events.append(
            self.event(
                "response.output_item.done",
                response_id=self.response["id"],
                output_index=state["output_index"],
                item=item,
            )
        )
        return events

    def feed(self, payload: dict) -> list[dict]:
        if self.terminal:
            raise UpstreamProtocolError("Event after terminal response")
        kind = payload.get("type")
        if kind == "ping":
            return []
        if kind == "message_start":
            if self.started or not isinstance(payload.get("message"), dict):
                raise UpstreamProtocolError("Invalid message_start")
            self.started = True
            usage = payload["message"].get("usage") or {}
            if not isinstance(usage, dict):
                raise UpstreamProtocolError("Invalid stream usage")
            self.usage.update(usage)
            return []
        if not self.started:
            raise UpstreamProtocolError("Missing message_start")
        if kind == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block")
            if not isinstance(index, int) or index in self.blocks or not isinstance(block, dict):
                raise UpstreamProtocolError("Invalid content_block_start")
            block_kind = block.get("type")
            if block_kind in {"thinking", "redacted_thinking"}:
                self.blocks[index] = None
                return []
            if block_kind == "text":
                item = {
                    "id": "msg_" + uuid.uuid4().hex,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
            elif block_kind == "tool_use":
                if not isinstance(block.get("name"), str) or not isinstance(block.get("id"), str):
                    raise UpstreamProtocolError("Invalid streamed tool_use")
                item = self.registry.output_item(block["name"], block["id"])
            else:
                raise UpstreamProtocolError("Unsupported upstream stream block")
            item = scrub_credentials(item, self.secrets)
            state = {
                "item": item,
                "output_index": len(self.response["output"]),
                "done": False,
                "credential_filter": CredentialFilter(self.secrets),
                "raw_arguments": "",
            }
            self.blocks[index] = state
            self.response["output"].append(item)
            events = [
                self.event(
                    "response.output_item.added",
                    response_id=self.response["id"],
                    output_index=state["output_index"],
                    item=item,
                )
            ]
            if block_kind == "text":
                part = {"type": "output_text", "text": "", "annotations": []}
                item["content"].append(part)
                events.append(
                    self.event(
                        "response.content_part.added",
                        **self.item_fields(state),
                        content_index=0,
                        part=part,
                    )
                )
                initial = block.get("text", "")
                if not isinstance(initial, str):
                    raise UpstreamProtocolError("Invalid initial text")
                if initial:
                    events.extend(self.delta(state, initial))
            elif block.get("input"):
                if not isinstance(block["input"], dict):
                    raise UpstreamProtocolError("Invalid initial function arguments")
                events.extend(
                    self.delta(
                        state, json.dumps(block["input"], ensure_ascii=False, separators=(",", ":"))
                    )
                )
            return events
        if kind in {"content_block_delta", "content_block_stop"}:
            index = payload.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index not in self.blocks:
                raise UpstreamProtocolError("Unknown content block index")
            state = self.blocks[index]
            if state is None:
                return []
            if kind == "content_block_stop":
                return self.finish_item(state)
            if state["done"]:
                raise UpstreamProtocolError("Delta after content block stop")
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                raise UpstreamProtocolError("Invalid content block delta")
            text_item = state["item"]["type"] == "message"
            expected = "text_delta" if text_item else "input_json_delta"
            value = delta.get("text" if text_item else "partial_json")
            if delta.get("type") != expected or not isinstance(value, str):
                raise UpstreamProtocolError("Unexpected content block delta")
            return self.delta(state, value)
        if kind == "message_delta":
            delta, usage = payload.get("delta") or {}, payload.get("usage") or {}
            if not isinstance(delta, dict) or not isinstance(usage, dict):
                raise UpstreamProtocolError("Invalid message_delta")
            self.stop_reason = delta.get("stop_reason", self.stop_reason)
            self.usage.update(usage)
            return []
        if kind == "message_stop":
            events = []
            for state in self.blocks.values():
                if state is not None and not state["done"]:
                    events.extend(self.finish_item(state))
            finish_response(self.response, self.stop_reason, self.usage)
            self.terminal = True
            event = (
                "response.incomplete"
                if self.response["status"] == "incomplete"
                else "response.completed"
            )
            events.append(self.event(event, response=self.response))
            return events
        raise UpstreamProtocolError("Unknown upstream SSE event")

    def fail(self, payload: dict) -> list[dict]:
        if self.terminal:
            return []
        self.response["status"] = "failed"
        self.response["error"] = payload["error"]
        self.response["usage"] = usage_to_openai(self.usage)
        self.terminal = True
        error = payload["error"]
        return [
            self.event(
                "error",
                code=error.get("code", "upstream_error"),
                message=error.get("message", "Upstream stream failed"),
                param=error.get("param"),
                request_id=payload["request_id"],
                upstream_request_id=payload.get("upstream_request_id"),
            ),
            self.event("response.failed", response=self.response),
        ]


def encode_event(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode(
        "utf-8"
    )
