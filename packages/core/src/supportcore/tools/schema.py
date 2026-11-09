"""Tool input schemas: a small JSON Schema subset the model can fill in, validated on save and on every call."""

from __future__ import annotations

import re
from typing import Any

from supportcore.errors import DomainError
from supportcore.tools.ports import ToolSnapshot

MAX_PROPERTIES = 20
MAX_DESCRIPTION_LENGTH = 500
MAX_ENUM_VALUES = 50
MAX_STRING_ARGUMENT = 2000
PROPERTY_TYPES = ("string", "number", "integer", "boolean")
PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class InvalidToolSchemaError(DomainError):
    pass


class InvalidToolArgumentsError(DomainError):
    pass


def _is_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if type_name == "integer":
        return isinstance(value, int)
    return isinstance(value, (int, float))


def validate_input_schema(schema: Any) -> dict[str, Any]:
    """A flat object of at most 20 string/number/integer/boolean properties. Returns the cleaned schema."""
    if not isinstance(schema, dict):
        raise InvalidToolSchemaError("Input schema must be an object")
    unknown = set(schema) - {"type", "properties", "required"}
    if unknown:
        raise InvalidToolSchemaError(f"Unsupported schema key: {sorted(map(str, unknown))[0][:50]}")
    if schema.get("type") != "object":
        raise InvalidToolSchemaError('Input schema must have type "object"')
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise InvalidToolSchemaError("properties must be an object")
    if len(properties) > MAX_PROPERTIES:
        raise InvalidToolSchemaError(f"A tool can have at most {MAX_PROPERTIES} properties")
    cleaned: dict[str, Any] = {}
    for name, prop in properties.items():
        if not isinstance(name, str) or not PROPERTY_NAME.match(name):
            raise InvalidToolSchemaError("Property names must be letters, digits and underscores (up to 64)")
        if not isinstance(prop, dict):
            raise InvalidToolSchemaError(f"Property {name} must be an object")
        if set(prop) - {"type", "description", "enum"}:
            raise InvalidToolSchemaError(f"Property {name} only supports type, description and enum")
        type_name = prop.get("type")
        if type_name not in PROPERTY_TYPES:
            raise InvalidToolSchemaError(f"Property {name} must have type {', '.join(PROPERTY_TYPES)}")
        entry: dict[str, Any] = {"type": type_name}
        if "description" in prop:
            description = prop["description"]
            if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
                raise InvalidToolSchemaError(
                    f"Property {name} description must be text of at most {MAX_DESCRIPTION_LENGTH} characters"
                )
            entry["description"] = description
        if "enum" in prop:
            values = prop["enum"]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= MAX_ENUM_VALUES
                or not all(_is_type(v, type_name) for v in values)
            ):
                raise InvalidToolSchemaError(
                    f"Property {name} enum must list 1-{MAX_ENUM_VALUES} values of type {type_name}"
                )
            entry["enum"] = values
        cleaned[name] = entry
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or not all(isinstance(r, str) for r in required)
        or not set(required) <= set(cleaned)
        or len(set(required)) != len(required)
    ):
        raise InvalidToolSchemaError("required must list each property at most once and only existing properties")
    return {"type": "object", "properties": cleaned, "required": list(required)}


def placeholders(url_template: str) -> list[str]:
    """The `{name}` placeholders of a URL template, in order, without repeats."""
    return list(dict.fromkeys(PLACEHOLDER.findall(url_template)))


def validate_placeholders(url_template: str, schema: dict[str, Any]) -> None:
    """Every placeholder must be a property of the schema, and braces must only form placeholders."""
    names = placeholders(url_template)
    if "{" in PLACEHOLDER.sub("", url_template) or "}" in PLACEHOLDER.sub("", url_template):
        raise InvalidToolSchemaError("URL template has an unmatched brace")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for name in names:
        if name not in properties:
            raise InvalidToolSchemaError(f"URL placeholder {{{name[:50]}}} is not an input property")
        if name not in required:
            raise InvalidToolSchemaError(f"URL placeholder {{{name[:50]}}} must be a required property")


UNSAFE_PATH_VALUES = ("", ".", "..")


def validate_arguments(schema: dict[str, Any], args: Any, url_template: str | None = None) -> dict[str, Any]:
    """The arguments the model (or a tester) supplied, checked against the schema. With `url_template`,
    a value that would become a whole path segment "", "." or ".." is refused: HTTP clients resolve
    dot segments, which would redirect the call to another endpoint."""
    if not isinstance(args, dict):
        raise InvalidToolArgumentsError("Arguments must be an object")
    properties = schema.get("properties", {})
    for name in args:
        if name not in properties:
            raise InvalidToolArgumentsError(f"Unknown argument: {str(name)[:50]}")
    for name in schema.get("required", []):
        if name not in args or args[name] is None:
            raise InvalidToolArgumentsError(f"Missing required argument: {name}")
    for name, value in args.items():
        spec = properties[name]
        if not _is_type(value, spec["type"]):
            raise InvalidToolArgumentsError(f"Argument {name} must be of type {spec['type']}")
        if spec["type"] == "string" and len(value) > MAX_STRING_ARGUMENT:
            raise InvalidToolArgumentsError(f"Argument {name} is too long")
        if "enum" in spec and value not in spec["enum"]:
            raise InvalidToolArgumentsError(f"Argument {name} must be one of the allowed values")
    for name in placeholders(url_template or ""):
        if name in args and str(args[name]).strip() in UNSAFE_PATH_VALUES:
            raise InvalidToolArgumentsError(f"Argument {name} is not a valid path value")
    return dict(args)


def tool_definition(tool: ToolSnapshot) -> dict[str, Any]:
    """The function definition handed to the chat model (`bind_tools`)."""
    return {"name": tool.name, "description": tool.description, "parameters": tool.input_schema}
