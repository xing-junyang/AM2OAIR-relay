"""Adapt root schema composition to Anthropic's object-only tool envelope."""

from __future__ import annotations

import copy

ROOT_COMBINATORS = ("oneOf", "allOf", "anyOf")
ARGUMENTS_FIELD = "arguments"


class SchemaAdaptationError(Exception):
    """A fixed, content-free explanation of an unsupported schema construct."""


def adapt_tool_schema(schema: dict) -> tuple[dict, bool]:
    if not any(key in schema for key in ROOT_COMBINATORS):
        return schema, False
    original = copy.deepcopy(schema)
    prefix = f"#/properties/{ARGUMENTS_FIELD}"
    draft4 = "draft-04" in str(schema.get("$schema", ""))

    def relocate(node, embedded_resource=False):
        if not isinstance(node, dict):
            return
        # A nested $id establishes its own resource root; its local references
        # must keep pointing to that resource rather than to our outer envelope.
        embedded_resource = (
            embedded_resource or bool(node.get("$id")) or (draft4 and bool(node.get("id")))
        )
        if not embedded_resource:
            if "$recursiveRef" in node:
                # Draft 2019-09 recursiveRef requires an empty fragment and a
                # resource-root anchor; changing it into a pointer is invalid.
                raise SchemaAdaptationError(
                    "Composed tool schemas with $recursiveRef require an explicit $id"
                )
            for keyword in ("$ref", "$dynamicRef"):
                reference = node.get(keyword)
                if reference == "#":
                    node[keyword] = prefix
                elif isinstance(reference, str) and reference.startswith("#/"):
                    node[keyword] = prefix + reference[1:]
        # Only traverse schema-bearing keywords. Examples/default/const/enum
        # can contain literal "$ref" values, which must not be rewritten.
        for keyword in (
            "properties",
            "patternProperties",
            "$defs",
            "definitions",
            "dependentSchemas",
            "dependencies",
        ):
            values = node.get(keyword)
            if isinstance(values, dict):
                for value in values.values():
                    relocate(value, embedded_resource)
        for keyword in (
            "items",
            "additionalItems",
            "additionalProperties",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
            "contentSchema",
            "allOf",
            "anyOf",
            "oneOf",
            "prefixItems",
        ):
            value = node.get(keyword)
            if isinstance(value, list):
                for child in value:
                    relocate(child, embedded_resource)
            else:
                relocate(value, embedded_resource)

    relocate(original)
    wrapper = {
        "type": "object",
        "properties": {ARGUMENTS_FIELD: original},
        "required": [ARGUMENTS_FIELD],
        "additionalProperties": False,
    }
    if "$schema" in schema:
        wrapper["$schema"] = schema["$schema"]
    return wrapper, True
