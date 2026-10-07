"""Static guards shared by the framework's and every agent pack's tests."""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

WALL_CLOCK_CALLS = ("now", "today", "utcnow", "time")
SQL_CLOCKS = ("now()", "current_date", "current_timestamp")


def assert_no_wall_clock(module: ModuleType) -> None:
    """No ``datetime.now()``/``date.today()``/``time.time()`` call, and no SQL clock in any string."""
    tree = ast.parse(inspect.getsource(module))
    docstrings = {
        id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            assert not (
                isinstance(owner, ast.Name)
                and owner.id in ("datetime", "date", "time")
                and node.func.attr in WALL_CLOCK_CALLS
            ), (module.__name__, ast.unparse(node))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value.lower()
            assert not any(clock in text for clock in SQL_CLOCKS), (module.__name__, node.value)


def imported_modules(module: ModuleType) -> set[str]:
    """Every absolute module name ``module`` imports (relative imports resolved against its package)."""
    tree = ast.parse(inspect.getsource(module))
    package = module.__name__ if hasattr(module, "__path__") else module.__name__.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                names.add(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                names.add(node.module)
    return names
