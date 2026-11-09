import pytest

from supportcore.tools.ports import ToolSnapshot
from supportcore.tools.schema import (
    InvalidToolArgumentsError,
    InvalidToolSchemaError,
    tool_definition,
    validate_arguments,
    validate_input_schema,
    validate_placeholders,
)

SIMPLE = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "The order"},
        "count": {"type": "integer"},
        "kind": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["order_id"],
}


def test_validate_schema_accepts_simple_object():
    cleaned = validate_input_schema(SIMPLE)
    assert cleaned["properties"]["kind"] == {"type": "string", "enum": ["a", "b"]}
    assert cleaned["required"] == ["order_id"]
    assert validate_input_schema({"type": "object"}) == {"type": "object", "properties": {}, "required": []}


def test_rejects_nested_object():
    nested = {"type": "object", "properties": {"a": {"type": "object", "properties": {}}}}
    with pytest.raises(InvalidToolSchemaError):
        validate_input_schema(nested)
    with pytest.raises(InvalidToolSchemaError):
        validate_input_schema({"type": "object", "properties": {"a": {"type": "array"}}})


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {"type": "array"},
        {"type": "object", "extra": 1},
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}},
        {"type": "object", "properties": {"bad name": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
        {"type": "object", "properties": {"a": {"type": "string", "enum": [1]}}},
        {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(21)}},
    ],
)
def test_rejects_invalid_schemas(bad):
    with pytest.raises(InvalidToolSchemaError):
        validate_input_schema(bad)


def test_rejects_placeholder_not_in_properties():
    schema = validate_input_schema(SIMPLE)
    validate_placeholders("https://api.x.com/orders/{order_id}", schema)
    with pytest.raises(InvalidToolSchemaError):
        validate_placeholders("https://api.x.com/orders/{missing}", schema)
    with pytest.raises(InvalidToolSchemaError):
        validate_placeholders("https://api.x.com/orders/{order_id", schema)


def test_validate_arguments_types_and_required():
    schema = validate_input_schema(SIMPLE)
    assert validate_arguments(schema, {"order_id": "A1", "count": 2}) == {"order_id": "A1", "count": 2}
    for bad in (
        {},
        {"order_id": 5},
        {"order_id": "A1", "count": True},
        {"order_id": "A1", "count": 1.5},
        {"order_id": "A1", "kind": "c"},
        {"order_id": "A1", "other": 1},
        "nope",
    ):
        with pytest.raises(InvalidToolArgumentsError):
            validate_arguments(schema, bad)


def test_tool_definition_shape():
    schema = validate_input_schema(SIMPLE)
    tool = ToolSnapshot(
        id=__import__("uuid").uuid4(), name="get_order", description="Look up", method="GET",
        url_template="https://x.com/{order_id}", input_schema=schema, read_only=True,
    )
    assert tool_definition(tool) == {"name": "get_order", "description": "Look up", "parameters": schema}


def test_dot_segment_values_refused_for_placeholders_only():
    schema = validate_input_schema({**SIMPLE, "required": ["order_id"]})
    url = "https://x.com/v1/{order_id}/status"
    for bad in ("", ".", ".."):
        with pytest.raises(InvalidToolArgumentsError):
            validate_arguments(schema, {"order_id": bad}, url)
    assert validate_arguments(schema, {"order_id": "..a"}, url) == {"order_id": "..a"}
    assert validate_arguments(schema, {"order_id": ".."}) == {"order_id": ".."}  # no URL: not a path value


def test_placeholder_must_be_required_property():
    optional = validate_input_schema({"type": "object", "properties": {"a": {"type": "string"}}})
    with pytest.raises(InvalidToolSchemaError, match="required"):
        validate_placeholders("https://x.com/{a}", optional)
