"""A2A surface - AgentCard + JSON-RPC endpoint, mounted at the app ROOT.

The card MUST live at /.well-known/agent-card.json (no /api prefix) per the
A2A discovery convention, so this router is registered without a prefix.

The protocol layer (build_agent_card / dispatch) lives in the agent package
(manthan_agent.a2a); this module only wires it to FastAPI and the
Postgres-backed CaseStore.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request

from manthan_agent.a2a.card import build_agent_card
from manthan_agent.a2a.server import dispatch

from manthan_api.services.a2a_store import PgCaseStore

router = APIRouter(tags=["a2a"])

# Lazily constructed so importing the app never touches the DB. Tests swap
# this for an InMemoryCaseStore via monkeypatch.
_store: Any = None


def get_store() -> Any:
    global _store
    if _store is None:
        _store = PgCaseStore()
    return _store


def _identity() -> dict[str, Any]:
    """Agent-Identity fields surfaced on the card (Track 3 roster UI)."""
    return {
        "model": os.environ.get("MANTHAN_MODEL") or "gemini-3.1-pro-preview",
        "service_account": (
            os.environ.get("A2A_SERVICE_ACCOUNT")
            or "investigator@manthan.iam.gserviceaccount.com"
        ),
        "signing_key_fingerprint": os.environ.get("A2A_SIGNING_FINGERPRINT") or "unset",
    }


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    """The Manthan AgentCard - identity + skill catalog for A2A discovery."""
    base_url = os.environ.get("A2A_PUBLIC_URL") or str(request.base_url)
    return build_agent_card(base_url, identity=_identity())


@router.post("/a2a")
async def a2a_rpc(request: Request) -> dict[str, Any]:
    """JSON-RPC 2.0 endpoint: message/send (skills) + tasks/get."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a protocol-level error
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "invalid JSON"},
        }
    return await dispatch(payload, get_store())
