"""Tiny remote A2A client — what one macro agent uses to call another.

Two functions over httpx, mirroring the server contract exactly:

    get_card(base_url)                  -> the AgentCard dict
    call_skill(base_url, skill, args)   -> the JSON-RPC `result` of a
                                           message/send carrying {skill, args}

Auth is the card's declared apiKey scheme: pass api_key and it is sent as
the X-Manthan-A2A-Key header.

Pure + unit-testable: pass `http=` any httpx.Client-compatible object (e.g.
a starlette TestClient over create_a2a_app, or an httpx.Client built on
ASGITransport) and no network is touched; omit it and a real httpx.Client
is created per call.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

API_KEY_HEADER = "X-Manthan-A2A-Key"
CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a"


class A2AClientError(RuntimeError):
    """A JSON-RPC level error returned by the remote agent."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"A2A error {code}: {message}")
        self.code = code
        self.message = message


def get_card(
    base_url: str,
    *,
    http: Any | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetch the remote agent's AgentCard."""
    url = base_url.rstrip("/") + CARD_PATH
    if http is not None:
        resp = http.get(url)
    else:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
    resp.raise_for_status()
    return resp.json()


def call_skill(
    base_url: str,
    skill: str,
    args: dict[str, Any] | None = None,
    api_key: str | None = None,
    *,
    http: Any | None = None,
    timeout: float = 30.0,
) -> Any:
    """Invoke one A2A skill via JSON-RPC message/send; return the result.

    The result is whatever the remote returns for that skill: a Task dict
    for investigate_dispute, an agent Message (parts[0].data carries the
    artifact) for query/contribute skills. Raises A2AClientError on a
    JSON-RPC error and httpx.HTTPStatusError on transport-level failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {"skill": skill, "args": dict(args or {})},
    }
    headers = {API_KEY_HEADER: api_key} if api_key else {}
    url = base_url.rstrip("/") + RPC_PATH
    if http is not None:
        resp = http.post(url, json=payload, headers=headers)
    else:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"] or {}
        raise A2AClientError(int(err.get("code", -32603)), str(err.get("message", "")))
    return body.get("result") if isinstance(body, dict) else body


def skill_data(result: Any) -> Any:
    """Unwrap the data part from a skill result that is an agent Message."""
    if isinstance(result, dict):
        parts = result.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("kind") == "data":
                    return part.get("data")
    return result
