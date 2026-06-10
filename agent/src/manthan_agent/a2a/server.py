"""A2A JSON-RPC dispatch + a mountable app.

`dispatch()` is framework-free and unit-tested: it implements the A2A methods
`message/send` and `tasks/get` over a CaseStore. A2A skills are invoked by
sending a Message whose data part carries {"skill": <id>, "args": {...}}.
Action skills do work: investigate_dispute starts a case and returns a Task;
contribute_evidence appends an external evidence record and returns
{ok, recorded}. Query skills (get_* plus the advisor surface: ask,
precheck_refund, get_customer_history, dispute_exposure) return a Message
with the requested artifact.

`create_a2a_app()` wraps that in a Starlette app that also serves the
AgentCard at /.well-known/agent-card.json.
"""

from __future__ import annotations

from typing import Any

from .card import ACTION_SKILL_IDS, QUERY_SKILL_IDS, build_agent_card
from .store import CaseStore

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _data_message(data: Any) -> dict[str, Any]:
    """An A2A agent Message carrying a single data part."""
    return {"role": "agent", "parts": [{"kind": "data", "data": data}]}


def _extract_skill(params: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Pull {skill, args} out of a message/send params payload.

    Accepts either an explicit data part {"kind":"data","data":{"skill","args"}}
    or a convenience top-level {"skill","args"} on params.
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


async def _run_query(skill: str, args: dict[str, Any], store: CaseStore) -> Any:
    case_id = args.get("case_id", "")
    if skill == "get_case":
        return await store.get_case(case_id)
    if skill == "list_cases":
        return await store.list_cases(args.get("status"), int(args.get("limit", 50)))
    if skill == "get_brief":
        return await store.get_brief(case_id)
    if skill == "get_findings":
        return await store.get_findings(case_id)
    if skill == "get_actions":
        return await store.get_actions(case_id)
    if skill == "get_audit_trail":
        return await store.get_audit_trail(case_id)
    # ---- advisor surface (skills v2) ----
    if skill == "ask":
        return await store.ask(args.get("question", ""), args.get("case_id") or None)
    if skill == "precheck_refund":
        return await store.precheck_refund(
            args.get("customer_ref", ""), int(args.get("amount_minor", 0) or 0)
        )
    if skill == "get_customer_history":
        return await store.get_customer_history(args.get("customer_ref", ""))
    if skill == "dispute_exposure":
        return await store.dispute_exposure()
    raise KeyError(skill)


async def dispatch(payload: dict[str, Any], store: CaseStore) -> dict[str, Any]:
    """Handle one JSON-RPC request against the store. Returns the response dict."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _err(payload.get("id") if isinstance(payload, dict) else None,
                    _INVALID_REQUEST, "expected JSON-RPC 2.0 request")
    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {}) or {}

    try:
        if method == "tasks/get":
            case_id = params.get("id") or params.get("case_id")
            case = await store.get_case(case_id) if case_id else None
            if case is None:
                return _err(req_id, _INVALID_PARAMS, f"unknown task/case '{case_id}'")
            state = "completed" if case.get("decision") else "working"
            return _ok(req_id, {"id": case_id, "status": {"state": state}, "artifacts": [case]})

        if method == "message/send":
            skill, args = _extract_skill(params)
            if skill is None:
                return _err(req_id, _INVALID_PARAMS, "message has no {skill,args} data part")

            if skill in ACTION_SKILL_IDS:
                if skill == "investigate_dispute":
                    case_id = await store.create_investigation(args)
                    return _ok(req_id, {
                        "id": case_id,
                        "status": {"state": "submitted"},
                        "kind": "task",
                    })
                if skill == "contribute_evidence":
                    result = await store.contribute_evidence(
                        args.get("case_id", ""),
                        args.get("evidence") or {},
                        args.get("contributor", "unknown"),
                    )
                    return _ok(req_id, _data_message(result))
                return _err(req_id, _METHOD_NOT_FOUND, f"unrouted action skill '{skill}'")

            if skill in QUERY_SKILL_IDS:
                result = await _run_query(skill, args, store)
                return _ok(req_id, _data_message(result))

            return _err(req_id, _METHOD_NOT_FOUND, f"unknown skill '{skill}'")

        return _err(req_id, _METHOD_NOT_FOUND, f"unknown method '{method}'")
    except Exception as exc:  # noqa: BLE001
        return _err(req_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def create_a2a_app(
    store: CaseStore,
    *,
    base_url: str = "http://localhost:8080",
    identity: dict[str, Any] | None = None,
):
    """Return a Starlette app serving the AgentCard + the JSON-RPC endpoint."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    card = build_agent_card(base_url, identity=identity)

    async def agent_card(_request: Request) -> JSONResponse:
        return JSONResponse(card)

    async def rpc(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(_err(None, _PARSE_ERROR, "invalid JSON"))
        return JSONResponse(await dispatch(payload, store))

    return Starlette(routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", rpc, methods=["POST"]),
    ])
