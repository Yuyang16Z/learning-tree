"""Discovery only loads complete schemas from the user's selected tool registry."""

import copy
import json

import pytest

from app.context_budget import ContextBudgetExceeded, estimate_request_tokens
from app.tool_catalog import (
    SEARCH_TOOL_DEF,
    SEARCH_TOOL_NAME,
    ToolCatalog,
    needs_catalog,
    tool_definition_cost,
)


def tool(name, description="", padding=0):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "x" * padding,
                        "enum": ["one", "two"],
                    }
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def names(catalog):
    return [definition["function"]["name"] for definition in catalog.definitions]


def test_large_catalog_remains_bounded_with_pinned_readers_and_compact_builtins():
    definitions = [tool("web_search"), tool("fetch"), tool("read_learning_source")]
    definitions += [tool(f"mcp_{index}", f"Remote operation {index}", 600) for index in range(120)]
    catalog = ToolCatalog(definitions, budget=2200, pinned_names=("read_learning_source",))
    assert needs_catalog(definitions, 2200)
    assert set(names(catalog)) >= {SEARCH_TOOL_NAME, "read_learning_source", "web_search", "fetch"}
    assert tool_definition_cost(catalog.definitions) <= 2200
    assert len(catalog.definitions) < len(definitions)
    expected = estimate_request_tokens("", [], catalog.definitions) - estimate_request_tokens(
        "", []
    )
    assert tool_definition_cost(catalog.definitions) == expected


def test_exact_search_loads_complete_schema_for_the_next_round_without_changing_it():
    selected = tool("mcp_files_read", "Read a local document", 500)
    catalog = ToolCatalog([selected], budget=3000)
    before = catalog.definitions
    assert names(catalog) == [SEARCH_TOOL_NAME]
    result = json.loads(catalog.search({"query": "mcp_files_read"}))
    assert result["loaded"] == ["mcp_files_read"]
    assert result["tools"][0]["status"] == "loaded"
    assert catalog.definitions[1] == selected
    assert before == [SEARCH_TOOL_DEF]
    assert "parameters" not in result["tools"][0]


def test_queries_rank_english_words_and_chinese_bigrams_from_name_and_description():
    definitions = [
        tool("mcp_file_read", "Read local files"),
        tool("mcp_browse", "读取网页和搜索结果"),
        tool("mcp_clock", "Current clock time"),
    ]
    catalog = ToolCatalog(definitions, budget=2400, query="local files")
    assert "mcp_file_read" in names(catalog)
    result = json.loads(catalog.search({"query": "网页搜索"}))
    assert result["loaded"] == ["mcp_browse"]
    assert json.loads(catalog.search({"query": "CLOCK"}))["loaded"] == ["mcp_clock"]


def test_exact_name_preserves_case_sensitive_tool_identity():
    catalog = ToolCatalog([tool("Foo"), tool("foo")], budget=2400)
    assert json.loads(catalog.search({"query": "Foo"}))["loaded"] == ["Foo"]
    assert json.loads(catalog.search({"query": "foo"}))["loaded"] == ["foo"]
    assert [entry["name"] for entry in json.loads(catalog.search({"query": "FOO"}))["tools"]] == [
        "Foo",
        "foo",
    ]


def test_latest_loaded_tool_evicts_older_schemas_but_never_pinned_readers():
    reader, one, two = (
        tool("read_learning_source"),
        tool("first", padding=700),
        tool("second", padding=700),
    )
    budget = tool_definition_cost([SEARCH_TOOL_DEF, reader, one]) + 10
    catalog = ToolCatalog([reader, one, two], budget=budget, pinned_names=("read_learning_source",))
    assert json.loads(catalog.search({"query": "first"}))["loaded"] == ["first"]
    assert json.loads(catalog.search({"query": "second"}))["loaded"] == ["second"]
    assert names(catalog) == [SEARCH_TOOL_NAME, "read_learning_source", "second"]
    assert tool_definition_cost(catalog.definitions) <= budget


def test_oversized_individual_schema_is_reported_explicitly_and_never_truncated():
    oversized = tool("oversized", "A tool with many required arguments", 50_000)
    catalog = ToolCatalog([tool("small"), oversized], budget=2000)
    catalog.search({"query": "small"})
    before = catalog.definitions
    result = json.loads(catalog.search({"query": "oversized"}))
    assert result["loaded"] == []
    assert result["tools"][0]["status"] == "tool_too_large"
    assert "tool_too_large" in result
    assert catalog.definitions == before
    assert len(oversized["function"]["parameters"]["properties"]["value"]["description"]) == 50_000


def test_too_many_matches_give_exact_name_hint_without_exceeding_budget():
    first, second = tool("option_one", padding=800), tool("option_two", padding=800)
    budget = tool_definition_cost([SEARCH_TOOL_DEF, first]) + 10
    catalog = ToolCatalog([first, second], budget=budget)
    result = json.loads(catalog.search({"query": "option", "limit": 4}))
    assert [entry["status"] for entry in result["tools"]] == ["loaded", "budget_limit"]
    assert "budget_limit" in result
    assert tool_definition_cost(catalog.definitions) <= budget
    assert json.loads(catalog.search({"query": "option_two", "limit": 1}))["loaded"] == [
        "option_two"
    ]


def test_empty_query_paginates_every_selected_name_and_no_external_registry():
    selected = [tool(f"selected_{index}") for index in range(11)]
    catalog = ToolCatalog(selected, budget=2400)
    seen, offset = [], 0
    while offset is not None:
        result = json.loads(catalog.search({"query": "", "limit": 4, "offset": offset}))
        seen.extend(entry["name"] for entry in result["tools"])
        offset = result["next_offset"]
        assert tool_definition_cost(catalog.definitions) <= 2400
    assert seen == [definition["function"]["name"] for definition in selected]
    assert json.loads(catalog.search({"query": "unavailable_foobar"}))["tools"] == []


def test_caller_cannot_mutate_registry_or_mandatory_discovery_and_duplicates_are_stable():
    first, duplicate = tool("same", "First description"), tool("same", "Second description")
    definitions = [first, duplicate, tool(SEARCH_TOOL_NAME, "Malicious replacement")]
    original = copy.deepcopy(definitions)
    catalog = ToolCatalog(definitions, budget=2400)
    assert definitions == original
    definitions[0]["function"]["parameters"]["required"].append("later")
    result = json.loads(catalog.search({"query": "same"}))
    assert result["tools"][0]["description"] == "First description"
    snapshot = catalog.definitions
    assert snapshot[0] == SEARCH_TOOL_DEF
    assert snapshot[1] == original[0]
    snapshot[1]["function"]["parameters"]["required"].clear()
    snapshot[0]["function"]["name"] = "changed"
    assert catalog.definitions == [SEARCH_TOOL_DEF, original[0]]
    assert original[1] == duplicate


def test_search_arguments_are_bounded_and_invalid_query_is_safe():
    catalog = ToolCatalog([tool(f"operation_{index}") for index in range(8)], budget=2400)
    assert len(json.loads(catalog.search({"query": "", "limit": 99}))["tools"]) == 4
    assert len(json.loads(catalog.search({"query": "", "limit": -3}))["tools"]) == 1
    assert len(json.loads(catalog.search({"query": "", "limit": True}))["tools"]) == 3
    assert json.loads(catalog.search({"query": "", "offset": 10**20}))["tools"] == []
    assert (
        json.loads(catalog.search({"query": "", "offset": -1, "limit": 1}))["tools"][0]["name"]
        == "operation_0"
    )
    assert json.loads(catalog.search({"query": ["operation_0"]}))["error"] == "invalid_arguments"
    assert json.loads(catalog.search(None))["error"] == "invalid_arguments"
    # The appended exact tool name is outside the query bound and must not be loaded.
    assert json.loads(catalog.search({"query": "z" * 300 + " operation_0"}))["tools"] == []


def test_insufficient_discovery_or_pinned_budget_fails_with_safe_context_error():
    with pytest.raises(ContextBudgetExceeded):
        ToolCatalog([], budget=tool_definition_cost([SEARCH_TOOL_DEF]) - 1)
    reader = tool("read_learning_source", padding=5000)
    with pytest.raises(ContextBudgetExceeded):
        ToolCatalog([reader], budget=1000, pinned_names=("read_learning_source",))
    catalog = ToolCatalog([], budget=tool_definition_cost([SEARCH_TOOL_DEF]))
    assert catalog.definitions == [SEARCH_TOOL_DEF]
    assert not needs_catalog([], 0)
