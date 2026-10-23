"""A small, closed JSON-Schema subset validator (standard library only).

Supported keywords: ``type`` (object, array, string, integer, boolean, null), ``properties``,
``required``, ``additionalProperties`` (must be ``False`` on every object: schemas are closed),
``items``, ``minItems``, ``maxItems``, ``minLength``, ``maxLength``, ``pattern``, ``enum``,
``minimum``, ``maximum``, ``const``. Any other keyword is a programming error and raises, so
a schema can never silently mean less than it says.
"""

from __future__ import annotations

import re
from typing import Any

_KNOWN = frozenset({"type", "properties", "required", "additionalProperties", "items", "minItems",
                    "maxItems", "minLength", "maxLength", "pattern", "enum", "minimum", "maximum",
                    "const", "description"})


class SchemaError(ValueError):
    """Input does not match the schema. ``path`` locates the first problem."""

    def __init__(self, path: str, problem: str):
        super().__init__(f"{path or '$'}: {problem}")
        self.path = path or "$"
        self.problem = problem


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    raise TypeError(f"unsupported schema type {kind!r}")


def validate(schema: dict, value: Any, path: str = "") -> None:
    unknown = set(schema) - _KNOWN
    if unknown:
        raise TypeError(f"unsupported schema keywords {sorted(unknown)}")
    kind = schema.get("type")
    if kind is not None and not _type_ok(kind, value):
        raise SchemaError(path, f"expected {kind}")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(path, "unexpected value")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(path, "value not allowed")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise SchemaError(path, "too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(path, "too long")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaError(path, "does not match pattern")
    if kind == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(path, "below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(path, "above maximum")
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise SchemaError(path, "too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(path, "too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(schema["items"], item, f"{path}[{index}]")
    if kind == "object":
        if schema.get("additionalProperties", True) is not False:
            raise TypeError("object schemas must be closed (additionalProperties: false)")
        properties = schema.get("properties", {})
        for key in value:
            if key not in properties:
                raise SchemaError(f"{path}.{key}" if isinstance(key, str) else path, "unexpected property")
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}.{key}", "missing required property")
        for key, item in value.items():
            validate(properties[key], item, f"{path}.{key}")


def closed_object(properties: dict, required: tuple[str, ...] | list[str] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}
