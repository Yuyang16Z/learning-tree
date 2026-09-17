"""Offline initial-context reservations for schemas loaded on later tool rounds."""

import copy

import pytest

from app.context import build_context
from app.context_budget import ContextPolicy, estimate_request_tokens, fit_request
from app.models import Message, Node


def definition(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }


def history():
    ancestors = []
    for number in range(1, 8):
        node = Node(id=number, tree_id=1, title=f"Earlier topic {number}", status="complete")
        ancestors.append(
            (
                node,
                [
                    Message(
                        id=number * 2,
                        node_id=number,
                        role="user",
                        content=f"Earlier question {number}",
                    ),
                    Message(
                        id=number * 2 + 1,
                        node_id=number,
                        role="assistant",
                        content=f"Only condition {number} permits the operation. " * 400,
                    ),
                ],
            )
        )
    return ancestors


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_initial_context_reserves_only_missing_schema_allowance(protocol):
    policy = ContextPolicy(window_tokens=16384, output_tokens=2048)
    tools = [definition("discover_tools", "Find a selected tool by purpose.")]
    loaded = [*tools, definition("read_selected_source", "Full schema description. " * 100)]
    allowance = 4200
    actual_cost = estimate_request_tokens("", [], tools, protocol) - estimate_request_tokens(
        "", [], protocol=protocol
    )
    assert 0 < actual_cost < allowance
    assert (
        estimate_request_tokens("", [], loaded, protocol)
        - estimate_request_tokens("", [], protocol=protocol)
        <= allowance
    )
    ancestors = history()
    current = Node(id=8, tree_id=1, title="Current question", seed_text="Do not lose this quote.")
    original = copy.deepcopy((ancestors, current, tools, loaded))
    diagnostics = {}
    system, messages = build_context(
        ancestors,
        current,
        [],
        "Which condition matters?",
        policy=policy,
        tool_defs=tools,
        protocol=protocol,
        tool_schema_budget=allowance,
        diagnostics=diagnostics,
    )
    reserved_room = allowance - actual_cost
    assert diagnostics["compacted"]
    assert estimate_request_tokens(system, messages, tools, protocol) <= (
        policy.input_budget - policy.tool_reserve - reserved_room
    )
    # Loading the reserved definitions must not consume the space for tool results
    # or require another lossy context pass before the first actual tool call.
    assert estimate_request_tokens(system, messages, loaded, protocol) <= (
        policy.input_budget - policy.tool_reserve
    )
    fitted = fit_request(system, messages, loaded, policy=policy, protocol=protocol)
    assert not fitted.compressed
    assert fitted.system == system and fitted.messages == messages
    assert (ancestors, current, tools, loaded) == original


def test_already_loaded_schemas_are_not_reserved_twice():
    tools = [definition("large_loaded_tool", "Full description. " * 100)]
    actual_cost = estimate_request_tokens("", [], tools) - estimate_request_tokens("", [])
    args = (history(), Node(id=8, tree_id=1, title="Current"), [], "Continue.")
    policy = ContextPolicy(window_tokens=16384, output_tokens=2048)
    normal = build_context(*args, policy=policy, tool_defs=tools)
    with_allowance = build_context(
        *args, policy=policy, tool_defs=tools, tool_schema_budget=actual_cost
    )
    assert with_allowance == normal


def test_protected_system_contains_only_constructed_current_core():
    current = Node(
        id=2,
        tree_id=1,
        title="CURRENT_TOPIC",
        seed_text="CURRENT_QUOTE",
        learning_note="CURRENT_NOTE with text that looks like a historical memory header.",
    )
    ancestor = Node(id=1, tree_id=1, title="OLD_TOPIC", learning_note="OLD_NOTE")
    diagnostics = {}
    system, _ = build_context(
        [(ancestor, [Message(id=1, node_id=1, role="user", content="OLD_QUESTION")])],
        current,
        [],
        "CURRENT_QUESTION",
        memory_note="OPTIONAL_MEMORY",
        documents=[
            {
                "id": "synthetic-document",
                "name": "OPTIONAL_DOCUMENT.txt",
                "characters": 32,
                "warnings": [],
                "sections": [{"label": "1", "text": "OPTIONAL_DOCUMENT_CONTENT"}],
            }
        ],
        diagnostics=diagnostics,
    )
    core, _ = build_context([], current, [], "CURRENT_QUESTION")
    assert diagnostics["protected_system"] == core
    for text in ("CURRENT_TOPIC", "CURRENT_QUOTE", "CURRENT_NOTE"):
        assert text in diagnostics["protected_system"]
    for text in ("OLD_TOPIC", "OLD_NOTE", "OLD_QUESTION", "OPTIONAL_MEMORY", "OPTIONAL_DOCUMENT"):
        assert text not in diagnostics["protected_system"]
    assert "OPTIONAL_MEMORY" in system and "OPTIONAL_DOCUMENT" in system
