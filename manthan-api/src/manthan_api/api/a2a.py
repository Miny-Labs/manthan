"""A2A surface - AgentCard + JSON-RPC endpoint, mounted at the app ROOT.

The card MUST live at /.well-known/agent-card.json (no /api prefix) per the
A2A discovery convention, so this router is registered without a prefix.

This is now the AGGREGATOR/GATEWAY surface: the dedicated agent services
(manthan_api.agents.{triage,investigator,advisor}) each serve their own card,
but this endpoint stays up for back-compat and one-URL discovery. It
dispatches through the advisor's dispatcher (which adds the ask /
precheck_refund / get_customer_history / dispute_exposure /
contribute_evidence skills on the shared PgCaseStore and routes
investigate_dispute to the investigator), and when ADVISOR_A2A_URL is set the
card points callers at the advisor service - the conversational face.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request

from manthan_agent.a2a.card import build_agent_card

from manthan_api.agents.advisor import ADVISOR_SKILLS, dispatch_advisor
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
    card = build_agent_card(base_url, identity=_identity())
    # Gateway card advertises the advisor skills too - this endpoint serves
    # them via dispatch_advisor over the shared PgCaseStore.
    card["skills"] = ADVISOR_SKILLS + card["skills"]
    # When the dedicated advisor service exists, point callers at it (the
    # gateway keeps proxying /a2a unchanged for anyone already wired here).
    advisor_url = os.environ.get("ADVISOR_A2A_URL")
    if advisor_url:
        u = advisor_url.rstrip("/")
        card["url"] = u if u.endswith("/a2a") else u + "/a2a"
    return card


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
    return await dispatch_advisor(payload, get_store())
