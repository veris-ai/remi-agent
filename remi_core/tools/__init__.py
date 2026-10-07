"""Remi's tools: one schema and one dispatch path shared by every vendor.

Importing this package registers every tool into ``REGISTRY`` in a fixed
order (connect reads, identity, flags, payments, wrap-up). Vendors never see
handlers; they get ``REGISTRY.specs()`` converted by their ``ToolFormat`` and
send every call back through ``CallSession.dispatch``.
"""

from ..tool_registry import Gate, Tool, ToolError, ToolRegistry, ToolSpec
from . import connect, flags, identity, money, wrapup  # noqa: F401 - registration side effects
from .catalog import REGISTRY

__all__ = ["REGISTRY", "Gate", "Tool", "ToolError", "ToolRegistry", "ToolSpec"]
