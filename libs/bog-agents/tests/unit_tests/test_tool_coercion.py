"""Unit tests for `bog_agents.tools.coercion` (semantic argument coercion)."""

from __future__ import annotations

import pytest

from bog_agents.tools.coercion import (
    SemanticBool,
    SemanticNumber,
    semantic_bool,
    semantic_number,
)


class TestSemanticBool:
    @pytest.mark.parametrize("value", [True])
    def test_booleans_pass_through(self, value: bool) -> None:
        assert semantic_bool(value) is True

    @pytest.mark.parametrize("value", [False])
    def test_false_boolean_passes_through(self, value: bool) -> None:
        assert semantic_bool(value) is False

    @pytest.mark.parametrize("value", [1, 2, -1, 0.5])
    def test_nonzero_numbers_are_true(self, value: float) -> None:
        assert semantic_bool(value) is True

    @pytest.mark.parametrize("value", [0, 0.0])
    def test_zero_numbers_are_false(self, value: float) -> None:
        assert semantic_bool(value) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "y", "t", "enabled", "enable", "allow", "allowed"])
    def test_truthy_strings(self, value: str) -> None:
        assert semantic_bool(value) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "n", "f", "disabled", "disable", "deny", "denied"])
    def test_falsy_strings(self, value: str) -> None:
        assert semantic_bool(value) is False

    def test_case_and_whitespace_insensitive(self) -> None:
        assert semantic_bool("  TRUE ") is True
        assert semantic_bool("Yes") is True
        assert semantic_bool("  off  ") is False

    def test_empty_string_is_false(self) -> None:
        assert semantic_bool("") is False

    @pytest.mark.parametrize("value", ["maybe", "sometimes", "2", "yes please"])
    def test_unrecognized_strings_raise(self, value: str) -> None:
        with pytest.raises(ValueError):
            semantic_bool(value)

    @pytest.mark.parametrize("value", [None, [], object()])
    def test_non_scalar_values_raise(self, value: object) -> None:
        with pytest.raises(TypeError):
            semantic_bool(value)


class TestSemanticNumber:
    @pytest.mark.parametrize("value", [0, 7, -42])
    def test_ints_pass_through(self, value: int) -> None:
        assert semantic_number(value) == value

    @pytest.mark.parametrize("value", [0.0, 3.14, -2.5])
    def test_floats_pass_through(self, value: float) -> None:
        assert semantic_number(value) == value

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("42", 42),
            ("-7", -7),
            ("+3", 3),
            ("3.14", 3.14),
            (".5", 0.5),
            ("1,000", 1000),
            ("1,000,000", 1000000),
            ("$42", 42),
            ("$42.50", 42.5),
            ("10%", 10),
            ("15 percent", 15),
            ("1k", 1000),
            ("1.5k", 1500),
            ("2m", 2000000),
            ("2B", 2000000000),
            ("forty two", 42),
            ("forty-two", 42),
            ("three thousand", 3000),
            ("one million", 1000000),
            ("two hundred", 200),
            ("one hundred and five", 105),
        ],
    )
    def test_parses_loose_string_forms(self, value: str, expected: float) -> None:
        assert semantic_number(value) == expected

    def test_integral_results_are_ints(self) -> None:
        assert isinstance(semantic_number("1,000"), int)
        assert isinstance(semantic_number("10%"), int)
        assert isinstance(semantic_number("1k"), int)
        assert isinstance(semantic_number("forty two"), int)

    def test_fractional_results_are_floats(self) -> None:
        assert isinstance(semantic_number("3.14"), float)
        assert isinstance(semantic_number("$42.50"), float)
        assert isinstance(semantic_number("2.5"), float)

    @pytest.mark.parametrize("value", ["", "abc", "1..2", "12k34", "one zillion", "a few"])
    def test_unrecognized_strings_raise(self, value: str) -> None:
        with pytest.raises(ValueError):
            semantic_number(value)

    @pytest.mark.parametrize("value", [None, [], object()])
    def test_non_numeric_values_raise(self, value: object) -> None:
        with pytest.raises(TypeError):
            semantic_number(value)

    def test_boolean_rejected_explicitly(self) -> None:
        with pytest.raises(TypeError, match="boolean"):
            semantic_number(True)


class TestSemanticAliasesInTools:
    """The Annotated aliases must coerce through a real StructuredTool."""

    def test_semantic_bool_coerces_through_tool(self) -> None:
        from langchain_core.tools import StructuredTool

        def deploy(*, staged: SemanticBool = False) -> str:
            """Deploy with an optional staging flag."""
            return f"staged={staged}"

        tool = StructuredTool.from_function(func=deploy)
        assert tool.invoke({"staged": "yes"}) == "staged=True"
        assert tool.invoke({"staged": "off"}) == "staged=False"

        # The JSON schema must stay a plain boolean.
        schema = tool.tool_call_schema.model_json_schema()
        assert schema["properties"]["staged"]["type"] == "boolean"

    def test_semantic_number_coerces_through_tool(self) -> None:
        from langchain_core.tools import StructuredTool

        def scale(*, replicas: SemanticNumber = 1) -> str:
            """Scale out to the requested replica count."""
            return f"replicas={replicas}"

        tool = StructuredTool.from_function(func=scale)
        assert tool.invoke({"replicas": "1k"}) == "replicas=1000"
        assert tool.invoke({"replicas": "five"}) == "replicas=5"

        schema = tool.tool_call_schema.model_json_schema()
        property_schema = schema["properties"]["replicas"]
        assert property_schema.get("type") in {"number", "integer"} or property_schema.get("anyOf")

    def test_aliases_are_exported_from_tools_package(self) -> None:
        from bog_agents.tools import SemanticBool, SemanticNumber, semantic_bool, semantic_number

        assert callable(semantic_bool)
        assert callable(semantic_number)
        assert str(SemanticBool).startswith("typing.Annotated")
        assert str(SemanticNumber).startswith("typing.Annotated")
