"""Bound selected tool schemas without changing their arguments or executing tools."""

from __future__ import annotations

import copy
import json
import re

from .context_budget import ContextBudgetExceeded, estimate_request_tokens

SEARCH_TOOL_NAME = "search_available_tools"
SEARCH_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL_NAME,
        "description": (
            "Find selected tools by name or description. Matching tools' complete schemas "
            "become available in the next round; this does not execute them. "
            "Use an empty query and offset to browse."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 300},
                "limit": {"type": "integer", "minimum": 1, "maximum": 4},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def tool_definition_cost(definitions: list[dict]) -> int:
    """Use the same serialized schema estimate as provider request fitting."""
    return estimate_request_tokens("", [], definitions) - estimate_request_tokens("", [])


def needs_catalog(definitions: list[dict], budget: int) -> bool:
    return tool_definition_cost(definitions) > budget


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
        if len(phrase) == 1:
            words.add(phrase)
    return words


def _bounded_integer(value: object, default: int, minimum: int, maximum: int) -> int:
    # Ignore arrays, booleans, fractional values, and strings instead of coercing them.
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(minimum, min(maximum, value))


class ToolCatalog:
    """A request-local registry; only explicitly selected tools can be discovered.

    Required readers and discovery are pinned. Search results displace older
    unpinned definitions when needed, while complete schemas remain unchanged.
    The caller snapshots ``definitions`` before each provider request.
    """

    def __init__(
        self,
        definitions: list[dict],
        *,
        budget: int,
        pinned_names: tuple[str, ...] = (),
        query: str = "",
    ):
        self.budget = budget
        self._registry: dict[str, dict] = {}
        for definition in definitions:
            if not isinstance(definition, dict) or definition.get("type") != "function":
                continue
            function = definition.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name or name == SEARCH_TOOL_NAME:
                continue
            # First definition wins, matching the stable order of selected groups.
            if name not in self._registry:
                self._registry[name] = copy.deepcopy(definition)
        self._pinned = list(dict.fromkeys(name for name in pinned_names if name in self._registry))
        self._search_definition = copy.deepcopy(SEARCH_TOOL_DEF)
        if self._cost([]) > self.budget:
            raise ContextBudgetExceeded()
        self._active: list[str] = []
        initial = [name for name in ("web_search", "fetch") if name in self._registry]
        if isinstance(query, str) and query.strip():
            initial.extend(self._matches(query.strip()[:300])[:3])
        for name in dict.fromkeys(initial):
            if name not in self._pinned and self._cost([*self._active, name]) <= self.budget:
                self._active.append(name)

    def _native_definitions(self, active: list[str]) -> list[dict]:
        return [
            self._search_definition,
            *(self._registry[name] for name in self._pinned),
            *(self._registry[name] for name in active if name not in self._pinned),
        ]

    def _cost(self, active: list[str]) -> int:
        return tool_definition_cost(self._native_definitions(active))

    @property
    def definitions(self) -> list[dict]:
        """Return an isolated native snapshot; callers cannot mutate the registry."""
        return copy.deepcopy(self._native_definitions(self._active))

    def _matches(self, query: str) -> list[str]:
        if not query:
            return list(self._registry)
        if query in self._registry:
            return [query]
        folded = query.casefold()
        exact = [name for name in self._registry if name.casefold() == folded]
        if exact:
            return exact
        query_terms = _terms(query)
        ranked = []
        for position, (name, definition) in enumerate(self._registry.items()):
            description = str(definition["function"].get("description", ""))
            name_terms, description_terms = _terms(name), _terms(description)
            score = 5 * len(query_terms & name_terms) + len(query_terms & description_terms)
            if folded in name.casefold():
                score += 12
            if folded in description.casefold():
                score += 4
            if score:
                ranked.append((-score, position, name))
        return [name for _, _, name in sorted(ranked)]

    def search(self, args: dict) -> str:
        """Discover/load a bounded page without calling any registered function."""
        if not isinstance(args, dict) or not isinstance(args.get("query", ""), str):
            return json.dumps({"error": "invalid_arguments", "message": "query must be a string"})
        query = args.get("query", "").strip()[:300]
        limit = _bounded_integer(args.get("limit"), 3, 1, 4)
        matches = self._matches(query)
        offset = _bounded_integer(args.get("offset"), 0, 0, len(matches))
        page = matches[offset : offset + limit]
        desired: list[str] = []
        tools = []
        loaded = []
        for name in page:
            definition = self._registry[name]
            status = "loaded"
            if name not in self._pinned:
                if self._cost([name]) > self.budget:
                    status = "tool_too_large"
                elif self._cost([*desired, name]) > self.budget:
                    status = "budget_limit"
                else:
                    desired.append(name)
            if status == "loaded":
                loaded.append(name)
            tools.append(
                {
                    "name": name,
                    "description": str(definition["function"].get("description", ""))[:180],
                    "status": status,
                }
            )
        # The latest desired tools take priority. Preserve older definitions only
        # when the complete set still fits; never shrink an argument schema.
        if loaded:
            for name in self._active:
                if name not in desired and self._cost([*desired, name]) <= self.budget:
                    desired.append(name)
            self._active = desired
        result = {
            "tools": tools,
            "loaded": loaded,
            "total_matches": len(matches),
            "next_offset": offset + len(page) if offset + len(page) < len(matches) else None,
        }
        if loaded:
            result["message"] = (
                "Loaded schemas are available in the next round; no tool was executed."
            )
        if any(tool["status"] == "tool_too_large" for tool in tools):
            result["tool_too_large"] = (
                "The unchanged schema cannot fit. Use another tool or increase the context window."
            )
        if any(tool["status"] == "budget_limit" for tool in tools):
            result["budget_limit"] = "Search this tool's exact name with limit 1 to load it alone."
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
