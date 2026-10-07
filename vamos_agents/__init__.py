"""Agent packs: one subpackage per VAmoS agent, each exporting an ``AgentSpec`` as ``AGENT``.

The framework (``vamos_voice``) finds a pack by name at runtime; it never
imports one in code. An image carries exactly one pack.
"""
