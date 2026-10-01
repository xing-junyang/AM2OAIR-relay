from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .schemas import ARGUMENTS_FIELD, SchemaAdaptationError, adapt_tool_schema


class InvalidRequest(Exception):
    def __init__(self, message: str, param: str = "input"):
        super().__init__(message)
        self.param = param


class UpstreamProtocolError(Exception):
    """Never put an upstream body into the exception or its log message."""


def require_string(value: Any, param: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequest(f"{param} must be a nonempty string", param)
    return value


@dataclass(frozen=True)
class ToolSpec:
    name: str
    namespace: str | None
    kind: str
    upstream_name: str
    wrapped_arguments: bool = False

    def output_item(self, call_id: str, status: str = "in_progress") -> dict:
        custom = self.kind == "custom"
        item = {
            "id": ("ctc_" if custom else "fc_") + uuid.uuid4().hex,
            "type": "custom_tool_call" if custom else "function_call",
            "status": status,
            "call_id": call_id,
            "name": self.name,
            "input" if custom else "arguments": "",
        }
        if self.namespace is not None:
            item["namespace"] = self.namespace
        return item


class ToolRegistry:
    """Request-local, reversible names and custom string input wrappers.

    Aliases depend only on tool identity, so stateless history remains valid if
    the client reorders or removes definitions between turns. Never persist this
    registry: definitions and tool inputs can contain private information.
    """

    def __init__(self, raw_tools: Any = None, history: Any = None):
        self.by_identity: dict[tuple[str | None, str], ToolSpec] = {}
        self.by_upstream: dict[str, ToolSpec] = {}
        self.tools: list[dict] = []
        raw_tools = [] if raw_tools is None else raw_tools
        if not isinstance(raw_tools, list):
            raise InvalidRequest("tools must be an array", "tools")
        for tool in raw_tools:
            if not isinstance(tool, dict):
                raise InvalidRequest("Tools must be objects", "tools")
            if tool.get("type") == "namespace":
                namespace = require_string(tool.get("name"), "tools.namespace")
                children = tool.get("tools")
                if not isinstance(children, list):
                    raise InvalidRequest("Namespace tools must be an array", "tools")
                for child in children:
                    self.add_definition(child, namespace, tool.get("description"))
            else:
                self.add_definition(tool)
        # A tool can disappear from the current tools list while its calls are
        # still in the conversation. Keep the same alias for those history items.
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict) and item.get("type") in (
                    "function_call",
                    "custom_tool_call",
                ):
                    self.resolve_history(item)

    def register(
        self, name: str, namespace: str | None, kind: str, wrapped_arguments: bool = False
    ) -> ToolSpec:
        identity = (namespace, name)
        if identity in self.by_identity:
            existing = self.by_identity[identity]
            if existing.kind != kind:
                raise InvalidRequest("Tool call type differs from its definition", "tools")
            return existing
        if (
            namespace is None
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            and not name.startswith("am2oair_")
        ):
            alias = name
        else:
            # Reserve this prefix for aliases, including unusually named flat
            # functions. A short hash avoids namespace/underscore collisions.
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[
                :16
            ]
            readable = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:32]
            alias = f"am2oair_{readable}_{digest}"
        spec = ToolSpec(name, namespace, kind, alias, wrapped_arguments)
        if alias in self.by_upstream:
            raise InvalidRequest("Conflicting tool names", "tools")
        self.by_identity[identity] = spec
        self.by_upstream[alias] = spec
        return spec

    def add_definition(self, tool: Any, namespace: str | None = None, description: Any = None):
        if not isinstance(tool, dict) or tool.get("type") not in ("function", "custom"):
            raise InvalidRequest("Supported tool types are function, custom and namespace", "tools")
        kind = tool["type"]
        fn = tool.get("function", tool) if kind == "function" else tool
        if not isinstance(fn, dict):
            raise InvalidRequest("Function tool must be an object", "tools")
        name = require_string(fn.get("name"), "tools.name")
        if (namespace, name) in self.by_identity:
            raise InvalidRequest("Duplicate tool definition", "tools")
        descriptions = []
        wrapped_arguments = False
        if namespace:
            descriptions.append(f"Tool {namespace}.{name}.")
            if isinstance(description, str):
                descriptions.append(description)
        if isinstance(fn.get("description"), str):
            descriptions.append(fn["description"])
        if kind == "custom":
            schema = {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "The complete raw text input for the tool.",
                    }
                },
                "required": ["input"],
                "additionalProperties": False,
            }
            descriptions.append(
                "Provide the tool's raw text in the input string. Do not wrap that string in another JSON object or a Markdown code fence."
            )
            format_ = tool.get("format")
            if isinstance(format_, dict) and format_.get("type") == "grammar":
                grammar = format_.get("definition")
                if isinstance(grammar, str):
                    descriptions.append(
                        f"Follow this {format_.get('syntax', 'custom')} grammar for input:\n{grammar}"
                    )
        else:
            schema = fn.get("parameters") or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                raise InvalidRequest("Tool parameters must be a JSON Schema object", "tools")
            try:
                schema, wrapped_arguments = adapt_tool_schema(schema)
            except SchemaAdaptationError as exc:
                raise InvalidRequest(str(exc), "tools.parameters") from None
            except RecursionError:
                raise InvalidRequest(
                    "Tool schema exceeds the nesting limit", "tools.parameters"
                ) from None
            if wrapped_arguments:
                descriptions.append(
                    "Place the complete original function argument object in the arguments field of the tool input. The relay removes this outer envelope before executing the function."
                )
        spec = self.register(name, namespace, kind, wrapped_arguments)
        target = {"name": spec.upstream_name, "input_schema": schema}
        if descriptions:
            target["description"] = "\n\n".join(descriptions)
        self.tools.append(target)

    def resolve_history(self, item: dict) -> ToolSpec:
        name = require_string(item.get("name"), "name")
        namespace = item.get("namespace")
        if namespace is not None:
            namespace = require_string(namespace, "namespace")
        kind = "custom" if item.get("type") == "custom_tool_call" else "function"
        return self.register(name, namespace, kind)

    def selected_name(self, choice: dict) -> str:
        fn = choice.get("function")
        name = choice.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
        name = require_string(name, "tool_choice.name")
        namespace = choice.get("namespace")
        if namespace is not None:
            namespace = require_string(namespace, "tool_choice.namespace")
        spec = self.by_identity.get((namespace, name))
        active = {t["name"] for t in self.tools}
        if spec is None or spec.upstream_name not in active or spec.kind != choice["type"]:
            raise InvalidRequest("tool_choice must name a supplied tool", "tool_choice")
        return spec.upstream_name

    def output_item(self, name: str, call_id: str, status: str = "in_progress") -> dict:
        spec = self.by_upstream.get(name)
        if spec is None:
            if self.by_upstream:
                raise UpstreamProtocolError("Upstream called an unknown tool")
            spec = ToolSpec(name, None, "function", name)
        return spec.output_item(call_id, status)

    def wraps_arguments(self, name: str) -> bool:
        spec = self.by_upstream.get(name)
        return spec is not None and spec.wrapped_arguments

    def decode_function_arguments(self, name: str, arguments: dict) -> dict:
        if self.wraps_arguments(name):
            arguments = arguments.get(ARGUMENTS_FIELD)
        if not isinstance(arguments, dict):
            raise UpstreamProtocolError("Function arguments must be a JSON object")
        return arguments


def custom_tool_input(arguments: dict) -> str:
    if not isinstance(arguments.get("input"), str):
        raise UpstreamProtocolError("Custom tool input must be a string")
    return arguments["input"]


def text_blocks(content: Any, param: str = "input") -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise InvalidRequest("Text content must be a string or array", param)
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            raise InvalidRequest("Content parts must be objects", param)
        kind = part.get("type")
        if not isinstance(kind, str):
            raise InvalidRequest("Content part type must be a string", param)
        if kind in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if not isinstance(text, str):
                raise InvalidRequest("Text parts require a string text field", param)
            blocks.append({"type": "text", "text": text})
        elif kind in {"reasoning", "thinking", "redacted_thinking"}:
            continue
        else:
            raise InvalidRequest("MVP supports only text content", param)
    return blocks


def tool_result(item: dict) -> dict:
    call_id = require_string(item.get("call_id") or item.get("tool_use_id"), "call_id")
    output = item.get("output", item.get("content", ""))
    if isinstance(output, list):
        content: Any = text_blocks(output)
    elif isinstance(output, str):
        content = output
    else:
        content = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if item.get("is_error") is True:
        block["is_error"] = True
    return block


def tool_use(item: dict, registry: ToolRegistry) -> dict:
    call_id = require_string(item.get("call_id") or item.get("id"), "call_id")
    spec = None
    if item.get("type") == "tool_use":
        name = require_string(item.get("name"), "name")
    else:
        spec = registry.resolve_history(item)
        name = spec.upstream_name
    if item.get("type") == "custom_tool_call":
        raw_input = item.get("input")
        if not isinstance(raw_input, str):
            raise InvalidRequest("Custom tool input must be a string", "input")
        return {"type": "tool_use", "id": call_id, "name": name, "input": {"input": raw_input}}
    arguments = item.get("arguments", item.get("input", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            raise InvalidRequest("Function arguments must be valid JSON", "arguments") from None
    if not isinstance(arguments, dict):
        raise InvalidRequest("Function arguments must be a JSON object", "arguments")
    if spec is not None and spec.wrapped_arguments:
        arguments = {ARGUMENTS_FIELD: arguments}
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def to_anthropic(body: dict, settings: Settings, registry: ToolRegistry | None = None) -> dict:
    registry = registry or ToolRegistry(body.get("tools"), body.get("input"))
    # Stateless: silently dropping this would lose the conversation history.
    if body.get("previous_response_id") or body.get("conversation"):
        raise InvalidRequest(
            "Send full input history; server-side conversations are unsupported",
            "previous_response_id",
        )
    if body.get("background") is True:
        raise InvalidRequest("Background responses are unsupported", "background")
    instructions = body.get("instructions")
    system: list[str] = []
    if instructions is not None:
        if not isinstance(instructions, str):
            raise InvalidRequest("instructions must be a string", "instructions")
        if instructions:
            system.append(instructions)
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        items = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        items = raw_input
    else:
        raise InvalidRequest("input must be a string or array")
    messages: list[dict] = []

    def append(role: str, blocks: list[dict]):
        if not blocks:
            return
        # Anthropic requires grouped consecutive tool calls/results. Merge roles.
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for item in items:
        if not isinstance(item, dict):
            raise InvalidRequest("Input items must be objects")
        kind = item.get("type", "message")
        if not isinstance(kind, str):
            raise InvalidRequest("Input item type must be a string")
        if kind == "reasoning":
            continue
        if kind in {"function_call", "custom_tool_call", "tool_use"}:
            append("assistant", [tool_use(item, registry)])
        elif kind in {"function_call_output", "custom_tool_call_output", "tool_result"}:
            append("user", [tool_result(item)])
        elif kind == "message":
            role = item.get("role")
            if not isinstance(role, str):
                raise InvalidRequest("Message role must be a string")
            content = item.get("content", "")
            if role in {"system", "developer"}:
                system.extend(block["text"] for block in text_blocks(content))
            elif role in {"user", "assistant"}:
                if isinstance(content, list):
                    blocks = []
                    for block in content:
                        if not isinstance(block, dict):
                            raise InvalidRequest("Content parts must be objects")
                        if block.get("type") == "tool_result" and role == "user":
                            blocks.append(tool_result(block))
                        elif block.get("type") == "tool_use" and role == "assistant":
                            blocks.append(tool_use(block, registry))
                        else:
                            blocks.extend(text_blocks([block]))
                    append(role, blocks)
                else:
                    append(role, text_blocks(content))
            else:
                raise InvalidRequest("Unsupported message role")
        else:
            raise InvalidRequest("Unsupported input item type")
    if not messages:
        raise InvalidRequest("input must contain at least one message")
    max_tokens = body.get("max_output_tokens")
    if max_tokens is None:
        max_tokens = settings.default_max_tokens
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise InvalidRequest("max_output_tokens must be a positive integer", "max_output_tokens")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean", "stream")
    result = {
        "model": settings.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if system:
        result["system"] = "\n\n".join(system)
    tools = registry.tools
    if tools:
        result["tools"] = tools
        choice = body.get("tool_choice")
        if choice in (None, "auto"):
            result["tool_choice"] = {"type": "auto"}
        elif choice == "required":
            result["tool_choice"] = {"type": "any"}
        elif choice == "none":
            result.pop("tools")
        elif isinstance(choice, dict) and choice.get("type") in ("function", "custom"):
            name = registry.selected_name(choice)
            result["tool_choice"] = {"type": "tool", "name": name}
        else:
            raise InvalidRequest("Unsupported tool_choice", "tool_choice")
    for key in ("temperature", "top_p"):
        value = body.get(key)
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise InvalidRequest(f"{key} must be between 0 and 1", key)
            result[key] = value
    # reasoning/include/store/parallel_tool_calls/prompt_cache_key/text.verbosity,
    # metadata and other optional Responses-only fields are deliberately not copied.
    return result


def token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def usage_to_openai(usage: dict | None) -> dict:
    usage = usage or {}
    cached = token_count(usage.get("cache_read_input_tokens"))
    created = token_count(usage.get("cache_creation_input_tokens"))
    # Anthropic reports uncached input separately; OpenAI input includes cache tokens.
    inputs = token_count(usage.get("input_tokens")) + cached + created
    outputs = token_count(usage.get("output_tokens"))
    return {
        "input_tokens": inputs,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": outputs,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": inputs + outputs,
    }


def response_shell(
    model: str, response_id: str | None = None, created_at: int | None = None
) -> dict:
    return {
        "id": response_id or "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [],
        "output_text": "",
        "usage": None,
        "parallel_tool_calls": True,
        "store": False,
    }


def finish_response(response: dict, stop_reason: str | None, usage: dict):
    response["usage"] = usage_to_openai(usage)
    response["output_text"] = "".join(
        part["text"]
        for item in response["output"]
        if item["type"] == "message"
        for part in item["content"]
    )
    if stop_reason == "max_tokens":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    else:
        response["status"] = "completed"


def to_openai(
    message: dict, model: str, response_id: str | None = None, registry: ToolRegistry | None = None
) -> dict:
    registry = registry or ToolRegistry()
    if (
        not isinstance(message, dict)
        or message.get("type") != "message"
        or not isinstance(message.get("content"), list)
    ):
        raise UpstreamProtocolError("Invalid Anthropic message")
    response = response_shell(model, response_id)
    for block in message["content"]:
        if not isinstance(block, dict):
            raise UpstreamProtocolError("Invalid content block")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise UpstreamProtocolError("Invalid text block")
            response["output"].append(
                {
                    "id": "msg_" + uuid.uuid4().hex,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": block["text"], "annotations": []}],
                }
            )
        elif block.get("type") == "tool_use":
            if (
                not isinstance(block.get("input"), dict)
                or not isinstance(block.get("name"), str)
                or not isinstance(block.get("id"), str)
            ):
                raise UpstreamProtocolError("Invalid tool block")
            item = registry.output_item(block["name"], block["id"], "completed")
            if item["type"] == "custom_tool_call":
                item["input"] = custom_tool_input(block["input"])
            else:
                item["arguments"] = json.dumps(
                    registry.decode_function_arguments(block["name"], block["input"]),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            response["output"].append(item)
        elif block.get("type") not in {"thinking", "redacted_thinking"}:
            raise UpstreamProtocolError("Unsupported upstream content block")
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        raise UpstreamProtocolError("Invalid upstream usage")
    finish_response(response, message.get("stop_reason"), usage)
    return response
