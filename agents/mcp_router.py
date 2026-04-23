#!/usr/bin/env python3
"""Central MCP router for TEA tools."""

from typing import Any, Callable, Dict

from agents.mcp_tools.dom_tools import find_element, get_dom_snapshot
from agents.mcp_tools.jira_tools import add_comment, create_bug, get_issue
from agents.mcp_tools.qdrant_tools import vector_search, vector_upsert


class MCPRouter:
    """Routes stateless tool calls to concrete implementations."""

    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Dict[str, Any]]] = {
            "jira.create_bug": create_bug,
            "jira.add_comment": add_comment,
            "jira.get_issue": get_issue,
            "qdrant.vector_search": vector_search,
            "qdrant.vector_upsert": vector_upsert,
            "dom.find_element": find_element,
            "dom.get_dom_snapshot": get_dom_snapshot,
        }

    def list_tools(self) -> Dict[str, Callable[..., Dict[str, Any]]]:
        return dict(self._tools)

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"ok": False, "error": f"Unknown tool: {name}", "data": None}
        try:
            return tool(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments for {name}: {exc}", "data": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "data": None}


_router = MCPRouter()


def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Module-level router entry point."""
    return _router.execute_tool(name, args)

