"""Manthan investigation agent.

A Google ADK multi-agent team (Gemini via AI Studio) that drives a SQL
data plane (Coral, via MCP stdio) to investigate billing-operations
cases, draft replies, and propose actions. A pro-model coordinator fans
out five parallel flash specialists that share one Evidence set; every
step is cited and audited. `run_case()` in loop.py is the entry point.
"""

__version__ = "0.1.0"
