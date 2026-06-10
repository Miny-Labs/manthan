"""Shared plumbing for the three A2A agent services.

Tiny JSON-RPC helpers (mirroring manthan_agent.a2a.server's private ones,
re-declared here so the services don't reach into another package's
underscore namespace) + the per-agent identity builder used by every card.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def rpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def data_message(data: Any) -> dict[str, Any]:
    """An A2A agent Message carrying a single data part."""
    return {"role": "agent", "parts": [{"kind": "data", "data": data}]}


def extract_skill(params: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Pull {skill, args} out of a message/send params payload.

    Accepts either an explicit data part {"kind":"data","data":{"skill","args"}}
    or a convenience top-level {"skill","args"} on params - the same two
    shapes manthan_agent.a2a.server.dispatch accepts.
    """
    if "skill" in params:
        return params.get("skill"), params.get("args", {}) or {}
    message = params.get("message", {}) or {}
    for part in message.get("parts", []) or []:
        if isinstance(part, dict) and part.get("kind") == "data":
            data = part.get("data", {}) or {}
            if "skill" in data:
                return data.get("skill"), data.get("args", {}) or {}
    return None, {}


async def parse_jsonrpc(request: Request) -> dict[str, Any] | None:
    """Best-effort JSON body parse; None signals a protocol-level parse error."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def agent_identity(agent_id: str, default_model: str) -> dict[str, Any]:
    """Agent-Identity fields stamped on this service's card.

    Each Cloud Run service runs as its OWN service account (deploy.sh wires
    A2A_SERVICE_ACCOUNT per service) - that per-agent identity is what the
    Track 3 roster UI surfaces.
    """
    return {
        "agent_id": agent_id,
        "model": os.environ.get("A2A_MODEL") or default_model,
        "service_account": (
            os.environ.get("A2A_SERVICE_ACCOUNT")
            or f"{agent_id}@manthan.iam.gserviceaccount.com"
        ),
        "signing_key_fingerprint": os.environ.get("A2A_SIGNING_FINGERPRINT") or "unset",
        "runtime": os.environ.get("A2A_RUNTIME") or "cloud-run",
    }


def public_base_url(request: Request) -> str:
    """The base URL stamped into the card (A2A_PUBLIC_URL on Cloud Run)."""
    return os.environ.get("A2A_PUBLIC_URL") or str(request.base_url)
