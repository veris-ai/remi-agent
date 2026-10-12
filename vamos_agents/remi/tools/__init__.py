"""Remi's tools: one schema and one dispatch path shared by every vendor.

Importing this package registers every tool into ``REGISTRY`` in a fixed
order (connect reads, identity, flags, the creditor's payment options, payments, wrap-up). Vendors never see
handlers; they get ``REGISTRY.specs()`` converted by their ``ToolFormat`` and
send every call back through the framework's ``CallSession.dispatch``, which
checks ``vamos_agents.remi.gates.GATES`` and hands the handler the call's ``RemiCall``.
"""

from . import connect, flags, identity, money, options, wrapup  # noqa: F401 - registration side effects
from .catalog import REGISTRY

__all__ = ["REGISTRY"]
