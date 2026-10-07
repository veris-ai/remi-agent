"""Remi: the debt-collection voice agent for the VAmoS Debt Collection benchmark (outbound)."""

from .agent import RemiAgent

AGENT = RemiAgent

__all__ = ["AGENT", "RemiAgent"]
