"""Tool declarations: ``ToolRegistry`` and its pieces (see ``vamos_voice.tools.registry``)."""

from .registry import Handler, Tool, ToolError, ToolRegistry, ToolSpec, json_schema

__all__ = ["Handler", "Tool", "ToolError", "ToolRegistry", "ToolSpec", "json_schema"]
