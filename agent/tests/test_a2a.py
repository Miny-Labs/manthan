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
