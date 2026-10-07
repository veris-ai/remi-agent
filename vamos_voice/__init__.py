"""vamos_voice: the shared voice-agent framework for every VAmoS agent.

Server, audio plumbing, call-start policies, the clock, the tool registry and
gate engine, and the vendor providers. It never imports an agent: agents are
found by name at runtime (``vamos_voice.registry``) and plug in through
``vamos_voice.agent.AgentSpec``.
"""
