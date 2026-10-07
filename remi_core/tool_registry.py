"""One tool registry: each tool's argument model, handler, gate and redactions.

A tool is declared once, with ``@registry.tool(...)`` on an async handler whose
second parameter is a pydantic model. The model is the single source of the
tool's JSON schema (every vendor's tool list is derived from it by a
``ToolFormat``) and of argument validation (vendors hand back raw JSON).

Metadata the session enforces lives on the declaration, not in the prompt:

- ``requires``: how far the call-opening gate must have advanced (``Gate``).
- ``redact``: argument fields that never reach a log line (card data).
- ``blocked_by_stop``: refused once something on the call stopped collection.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from .session import CallSession


class Gate(enum.IntEnum):
    """RULES.md's opening order, as the least a tool needs before it may run."""

    NONE = 0  # the connect-time reads themselves
    ASSESSED = 1  # all three reads done; the call may proceed or must close
    PROCEEDING = 2  # assessed, and the call is not one that should not have been placed
    VERIFIED = 3  # the right party (consumer or verified estate rep) confirmed
    DEBT_UNLOCKED = 4  # recording disclosure, mini-Miranda and the rest given; debt details read


Handler = Callable[["CallSession", Any], Awaitable[dict[str, Any]]]


class ToolError(Exception):
    """A refusal the model can act on: becomes ``{"error": message}``, never a crash."""


@dataclass(frozen=True)
class ToolSpec:
    """A vendor-neutral tool definition: what every ToolFormat converts."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    requires: Gate
    redact: frozenset[str] = field(default_factory=frozenset)
    blocked_by_stop: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, json_schema(self.args_model))

    def parse(self, raw: dict[str, Any]) -> BaseModel:
        try:
            return self.args_model.model_validate(raw)
        except ValidationError as err:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in err.errors()
            )
            raise ToolError(f"invalid arguments for {self.name}: {problems}") from None


class ToolRegistry:
    """The tools in declaration order."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self,
        args: type[BaseModel],
        *,
        requires: Gate,
        redact: tuple[str, ...] = (),
        blocked_by_stop: bool = False,
    ) -> Callable[[Handler], Handler]:
        """Register the decorated handler; its docstring is the model-facing description."""

        def register(handler: Handler) -> Handler:
            name = handler.__name__
            if name in self._tools:
                raise ValueError(f"tool {name} registered twice")
            unknown = set(redact) - set(args.model_fields)
            if unknown:
                raise ValueError(f"{name} redacts unknown fields {sorted(unknown)}")
            description = " ".join((handler.__doc__ or "").split())
            if not description:
                raise ValueError(f"tool {name} needs a docstring: it is the model's description")
            self._tools[name] = Tool(
                name, description, args, handler, requires, frozenset(redact), blocked_by_stop
            )
            return handler

        return register

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema reduced to the subset every vendor accepts.

    Pydantic spells ``X | None`` as ``anyOf [X, null]`` and nests enums under
    ``$defs``; Gemini rejects both. Optional fields become plain ``X`` left out
    of ``required``, references are inlined, and titles are dropped.
    """
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def simplify(node: Any) -> Any:
        if isinstance(node, list):
            return [simplify(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return simplify(defs[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            branches = [b for b in node["anyOf"] if b.get("type") != "null"]
            if len(branches) == 1:
                merged = {**branches[0], **{k: v for k, v in node.items() if k not in ("anyOf", "default")}}
                return simplify(merged)
        return {k: simplify(v) for k, v in node.items() if k not in ("title", "$defs", "default")}

    schema = simplify(raw)
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    schema["type"] = "object"
    return schema
