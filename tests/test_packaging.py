"""An image installs the core plus exactly one agent extra and one vendor extra.

So every third-party module an agent pack imports must come from the core
dependencies or that agent's own extra. The suite installs every extra at once
and would never notice a module that only another extra happens to bring
(Remi's Fineract client imported httpx, which only some vendor extras
declared, and the images without it crashed at start).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"vamos_agents", "vamos_voice"}


def _requirement_names(requirements: list[str]) -> set[str]:
    return {re.split(r"[\[<>=~!; ]", r, maxsplit=1)[0].lower().replace("_", "-") for r in requirements}


def _top_level_imports(package: Path) -> set[str]:
    found: set[str] = set()
    for path in package.rglob("*.py"):
        if "tests" in path.relative_to(package).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found - LOCAL - set(sys.stdlib_module_names)


PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
AGENTS = sorted(p.name for p in (ROOT / "vamos_agents").iterdir() if (p / "__init__.py").exists())


@pytest.mark.parametrize("agent", AGENTS)
def test_an_agent_pack_imports_only_what_its_extra_and_the_core_declare(agent: str) -> None:
    declared = _requirement_names(PROJECT["dependencies"] + PROJECT["optional-dependencies"].get(agent, []))
    distributions = packages_distributions()
    missing = {
        module: distributions.get(module, [module])
        for module in _top_level_imports(ROOT / "vamos_agents" / agent)
        if not {d.lower().replace("_", "-") for d in distributions.get(module, [module])} & declared
    }
    assert not missing, f"{agent} imports modules its extra does not declare: {missing}"
