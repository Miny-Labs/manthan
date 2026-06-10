"""A2A (Agent-to-Agent) surface for Manthan.

Exposes the Investigator and — per the Track 3 requirement that *any* state be
pickup-able by other agents — read access to every case artifact (cases,
briefs, findings, actions, audit trail) over the A2A protocol:

  * AgentCard at GET /.well-known/agent-card.json  (identity + skill catalog)
  * JSON-RPC 2.0 at POST /                         (message/send, tasks/get)

The protocol layer (card + dispatch) is framework-free and unit-tested with an
in-memory store; manthan-api mounts it over its Postgres-backed CaseStore.
"""

from .card import build_agent_card
from .client import A2AClientError, call_skill, get_card, skill_data
from .server import create_a2a_app, dispatch
from .store import CaseStore, InMemoryCaseStore

__all__ = [
    "build_agent_card",
    "create_a2a_app",
    "dispatch",
    "CaseStore",
    "InMemoryCaseStore",
    "call_skill",
    "get_card",
    "skill_data",
    "A2AClientError",
]
