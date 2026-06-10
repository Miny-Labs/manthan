"""A2A surface tests — card identity, skill catalog, JSON-RPC dispatch.

No LLM calls: exercises the protocol layer against an InMemoryCaseStore.
"""

from __future__ import annotations

import asyncio

from manthan_agent.a2a import InMemoryCaseStore, build_agent_card, create_a2a_app, dispatch
from manthan_agent.a2a.card import ACTION_SKILL_IDS, QUERY_SKILL_IDS


def _seeded_store() -> InMemoryCaseStore:
    store = InMemoryCaseStore()
    store.seed({
        "case_id": "case-001",
        "short_id": "C-0001",
        "status": "awaiting_approval",
        "customer_ref": "billing@aperture-analytics.co",
        "decision": {"action": "refund", "amount_minor": 56000, "currency": "usd", "confidence": 0.98},
        "brief": {"tldr": "Refund $560 pro-rata.", "decision": {"action": "refund", "amount_minor": 56000}},
        "findings": [{"text": "48-hour SLA breach.", "citations": [3], "confidence": 0.95}],
        "actions": [{"kind": "stripe_refund", "payload": {"amount_minor": 56000}}],
        "events": [{"type": "case_opened"}, {"type": "brief_drafted"}, {"type": "case_closed"}],
    })
    return store


def _rpc(method: str, params: dict, store: InMemoryCaseStore) -> dict:
    return asyncio.run(dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, store))


def test_card_has_identity_and_all_skills():
    card = build_agent_card("https://investigator.run.app", identity={"model": "gemini-3.1-pro-preview"})
    assert card["protocolVersion"]
    assert card["url"].endswith("/a2a")
    assert card["manthanIdentity"]["agentId"]
    assert card["manthanIdentity"]["model"] == "gemini-3.1-pro-preview"
    ids = {s["id"] for s in card["skills"]}
    assert ACTION_SKILL_IDS <= ids
    assert QUERY_SKILL_IDS <= ids  # every state read advertised over A2A


def test_get_case_via_message_send():
    r = _rpc("message/send", {"skill": "get_case", "args": {"case_id": "case-001"}}, _seeded_store())
    data = r["result"]["parts"][0]["data"]
    assert data["status"] == "awaiting_approval"
    assert data["decision"]["action"] == "refund"


def test_every_query_skill_returns_state():
    store = _seeded_store()
    for skill in QUERY_SKILL_IDS:
        r = _rpc("message/send", {"skill": skill, "args": {"case_id": "case-001"}}, store)
        assert "error" not in r, (skill, r)
        assert "result" in r


def test_investigate_creates_and_is_retrievable():
    store = _seeded_store()
    r = _rpc("message/send", {"skill": "investigate_dispute",
                              "args": {"case_id": "case-xyz", "customer_ref": "x@y.co"}}, store)
    assert r["result"]["status"]["state"] == "submitted"
    assert r["result"]["id"] == "case-xyz"
    r2 = _rpc("tasks/get", {"id": "case-xyz"}, store)
    assert r2["result"]["id"] == "case-xyz"


def test_unknown_skill_and_method_are_errors():
    store = _seeded_store()
    assert _rpc("message/send", {"skill": "nope", "args": {}}, store)["error"]["code"] == -32601
    assert _rpc("frobnicate", {}, store)["error"]["code"] == -32601


def test_malformed_request_rejected():
    r = asyncio.run(dispatch({"id": 1, "method": "x"}, _seeded_store()))
    assert r["error"]["code"] == -32600


def test_http_card_and_rpc_roundtrip():
    from starlette.testclient import TestClient

    app = create_a2a_app(_seeded_store(), base_url="https://x.run.app")
    client = TestClient(app)
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Manthan Investigator"
    resp = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 9, "method": "message/send",
        "params": {"skill": "list_cases", "args": {}},
    }).json()
    assert resp["id"] == 9
    assert isinstance(resp["result"]["parts"][0]["data"], list)
    assert resp["result"]["parts"][0]["data"][0]["case_id"] == "case-001"


# ──────────────────────────────────────────────────────────────────────
# Skills v2 — advisor queries + contribute_evidence + card security
# ──────────────────────────────────────────────────────────────────────

_V2_QUERY_SKILLS = {"ask", "precheck_refund", "get_customer_history", "dispute_exposure"}


def test_card_v2_skills_security_scheme_and_tag_split():
    card = build_agent_card("https://x.run.app")
    ids = {s["id"] for s in card["skills"]}
    assert _V2_QUERY_SKILLS <= ids
    assert "contribute_evidence" in ids
    # apiKey security scheme on the canonical header
    scheme = card["securitySchemes"]["apiKey"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-Manthan-A2A-Key"}
    assert card["security"] == [{"apiKey": []}]
    # every skill is tagged action XOR query
    assert "contribute_evidence" in ACTION_SKILL_IDS
    assert _V2_QUERY_SKILLS <= QUERY_SKILL_IDS
    for s in card["skills"]:
        if s["id"] in ACTION_SKILL_IDS:
            assert "action" in s["tags"] and "query" not in s["tags"], s["id"]
        else:
            assert "query" in s["tags"] and "action" not in s["tags"], s["id"]


def test_ask_grounded_in_one_case():
    r = _rpc("message/send",
             {"skill": "ask", "args": {"question": "why refund?", "case_id": "case-001"}},
             _seeded_store())
    data = r["result"]["parts"][0]["data"]
    assert data["grounded"] is True
    assert "C-0001" in data["answer"] and "refund" in data["answer"]
    assert data["findings_count"] == 1
    assert data["evidence_cited"] == [3]


def test_ask_unknown_case_and_portfolio():
    store = _seeded_store()
    r = _rpc("message/send", {"skill": "ask", "args": {"question": "?", "case_id": "nope"}}, store)
    assert r["result"]["parts"][0]["data"]["grounded"] is False
    r2 = _rpc("message/send", {"skill": "ask", "args": {"question": "open cases?"}}, store)
    data = r2["result"]["parts"][0]["data"]
    assert data["grounded"] is True
    assert data["open_case_ids"] == ["case-001"]


def test_precheck_refund_gate_bands():
    store = _seeded_store()

    def gate(amount: int, customer: str = "someone@new.co") -> dict:
        r = _rpc("message/send", {"skill": "precheck_refund",
                                  "args": {"customer_ref": customer, "amount_minor": amount}}, store)
        return r["result"]["parts"][0]["data"]

    assert gate(4_999)["gate"] == "auto"
    assert gate(4_999)["auto_approvable"] is True       # no history, tiny amount
    assert gate(5_000)["gate"] == "one-click"
    assert gate(50_000)["gate"] == "two-person"
    # the seeded customer has an OPEN case + a prior refund decision
    seeded = gate(1_000, customer="billing@aperture-analytics.co")
    assert seeded["gate"] == "auto"
    assert seeded["auto_approvable"] is False           # open case blocks auto
    assert seeded["prior_cases"] == 1
    assert seeded["open_cases"] == 1
    assert seeded["prior_refunded_minor"] == 56000


def test_get_customer_history():
    r = _rpc("message/send", {"skill": "get_customer_history",
                              "args": {"customer_ref": "billing@aperture-analytics.co"}},
             _seeded_store())
    history = r["result"]["parts"][0]["data"]
    assert len(history) == 1
    assert history[0]["case_id"] == "case-001"
    assert history[0]["decision"]["action"] == "refund"
    r2 = _rpc("message/send", {"skill": "get_customer_history",
                               "args": {"customer_ref": "ghost@nowhere.io"}}, _seeded_store())
    assert r2["result"]["parts"][0]["data"] == []


def test_dispute_exposure_rollup():
    r = _rpc("message/send", {"skill": "dispute_exposure", "args": {}}, _seeded_store())
    data = r["result"]["parts"][0]["data"]
    assert data["open_cases"] == 1
    assert data["total_exposure_minor"] == 56000
    assert data["by_status"] == {"awaiting_approval": 1}
    assert data["open_case_ids"] == ["case-001"]


def test_contribute_evidence_records_and_audits():
    store = _seeded_store()
    r = _rpc("message/send", {"skill": "contribute_evidence", "args": {
        "case_id": "case-001",
        "evidence": {"kind": "delivery_confirmation", "tracking": "1Z999"},
        "contributor": "logistics-agent",
    }}, store)
    data = r["result"]["parts"][0]["data"]
    assert data["ok"] is True and data["recorded"] is True
    assert data["evidence_count"] == 1
    # the contribution landed on the audit trail with the contributor identity
    trail = asyncio.run(store.get_audit_trail("case-001"))
    contributed = [e for e in trail if e.get("type") == "evidence_contributed"]
    assert len(contributed) == 1
    assert contributed[0]["actor"] == "external:logistics-agent"
    assert contributed[0]["data"]["evidence"]["tracking"] == "1Z999"
    # unknown case -> ok False, never an exception
    r2 = _rpc("message/send", {"skill": "contribute_evidence",
                               "args": {"case_id": "nope", "evidence": {}, "contributor": "x"}}, store)
    assert r2["result"]["parts"][0]["data"]["ok"] is False


# ──────────────────────────────────────────────────────────────────────
# Remote A2A client (a2a/client.py) over the in-process app — no network
# ──────────────────────────────────────────────────────────────────────


def _client_and_headers(store: InMemoryCaseStore):
    """A TestClient over create_a2a_app that also captures request headers."""
    from starlette.testclient import TestClient

    inner = create_a2a_app(store, base_url="https://x.run.app")
    captured: dict = {}

    async def app(scope, receive, send):
        if scope["type"] == "http":
            captured["headers"] = {k.decode(): v.decode() for k, v in scope["headers"]}
        await inner(scope, receive, send)

    return TestClient(app), captured


def test_client_get_card_and_call_skill_roundtrip():
    from manthan_agent.a2a.client import call_skill, get_card, skill_data

    http, _ = _client_and_headers(_seeded_store())
    card = get_card("https://x.run.app", http=http)
    assert card["name"] == "Manthan Investigator"

    result = call_skill("https://x.run.app", "get_case",
                        {"case_id": "case-001"}, http=http)
    assert skill_data(result)["status"] == "awaiting_approval"

    task = call_skill("https://x.run.app", "investigate_dispute",
                      {"case_id": "case-new", "customer_ref": "x@y.co"}, http=http)
    assert task["status"]["state"] == "submitted"
    assert task["id"] == "case-new"


def test_client_sends_api_key_header():
    from manthan_agent.a2a.client import call_skill

    http, captured = _client_and_headers(_seeded_store())
    call_skill("https://x.run.app", "dispute_exposure", {},
               api_key="sekret-123", http=http)
    assert captured["headers"]["x-manthan-a2a-key"] == "sekret-123"


def test_client_raises_on_jsonrpc_error():
    import pytest

    from manthan_agent.a2a.client import A2AClientError, call_skill

    http, _ = _client_and_headers(_seeded_store())
    with pytest.raises(A2AClientError) as exc:
        call_skill("https://x.run.app", "not_a_skill", {}, http=http)
    assert exc.value.code == -32601
