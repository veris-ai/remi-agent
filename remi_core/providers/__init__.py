"""Provider discovery: a vendor is a folder under ``vendors/`` exporting ``PROVIDER``.

Adding a vendor touches nothing in ``remi_core``: create ``vendors/<name>/``,
subclass ``VoiceProvider``, export it as ``PROVIDER``, and add a ``<name>``
extra to pyproject.toml for its dependencies.
"""

from __future__ import annotations

import importlib
import pkgutil

import vendors

from .base import CallRuntime, SessionSetup, ToolFormat, TurnTaking, VoiceProvider

__all__ = [
    "CallRuntime",
    "SessionSetup",
    "ToolFormat",
    "TurnTaking",
    "VoiceProvider",
    "load_provider",
    "vendor_names",
]


class UnknownVendor(LookupError):
    """REMI_VENDOR names no folder under vendors/."""


def vendor_names() -> list[str]:
    """Every vendor folder, installed or not."""
    return sorted(m.name for m in pkgutil.iter_modules(vendors.__path__) if m.ispkg)


def load_provider(name: str) -> type[VoiceProvider]:
    """Import ``vendors.<name>`` and return its ``PROVIDER``. Import errors propagate:
    a vendor whose extra is not installed must fail loudly, not vanish."""
    if name not in vendor_names():
        raise UnknownVendor(f"no vendor {name!r}; known: {', '.join(vendor_names())}")
    module = importlib.import_module(f"vendors.{name}")
    provider = getattr(module, "PROVIDER", None)
    if not (isinstance(provider, type) and issubclass(provider, VoiceProvider)):
        raise UnknownVendor(f"vendors.{name} does not export a VoiceProvider as PROVIDER")
    if provider.name != name:
        raise UnknownVendor(f"vendors.{name}.PROVIDER.name is {provider.name!r}; it must match the folder")
    return provider
