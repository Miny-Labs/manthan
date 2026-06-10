"""A2A router tests - agent card discovery + JSON-RPC round-trips.

Runs against the real FastAPI app with the PgCaseStore swapped for the agent
package's InMemoryCaseStore, so no Postgres is needed. TestClient is used
WITHOUT a context manager, which skips lifespan and therefore never opens
the DB pool.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from manthan_agent.a2a import InMemoryCaseStore

import manthan_api.api.a2a as a2a_api
from manthan_api.main import app


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    store = InMemoryCaseStore()
    store.seed(
        {
            "case_id": "case-001",
            "short_id": "DSP-1ABC23",
            "status": "awaiting_approval",
            "customer_ref": "billing@aperture-analytics.co",
            "decision": {"action": "refund", "amount_minor": 56000, "confidence": 0.97},
            "brief": {"tldr": "Refund $560 pro-rata."},
            "findings": [{"seq": 1, "text": "48-hour SLA breach.", "confidence": 0.95}],
            "actions": [{"seq": 1, "kind": "stripe_refund", "payload": {}, "status": "drafted"}],
            "events": [{"type": "case_opened"}],
        }
    )
    monkeypatch.setattr(a2a_api, "_store", store)
    return TestClient(app)


def _rpc(client: TestClient, method: str, params: dict, req_id: int = 1) -> dict:
    r = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
    )
    assert r.status_code == 200
    return r.json()


def test_agent_card_served_at_root_well_known(client: TestClient) -> None:
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    # JSON-RPC endpoint advertised relative to the request base URL.
    assert card["url"].endswith("/a2a")
    skill_ids = {s["id"] for s in card["skills"]}
    assert "investigate_dispute" in skill_ids
    assert {
        "get_case",
        "list_cases",
        "get_brief",
        "get_findings",
        "get_actions",
        "get_audit_trail",
    } <= skill_ids
    ident = card["manthanIdentity"]
    assert ident["model"]
    assert ident["serviceAccount"]
    assert "signingKeyFingerprint" in ident


def test_a2a_list_cases_round_trips(client: TestClient) -> None:
    body = _rpc(client, "message/send", {"skill": "list_cases", "args": {}})
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    cases = body["result"]["parts"][0]["data"]
    assert [c["case_id"] for c in cases] == ["case-001"]
    assert cases[0]["status"] == "awaiting_approval"
    assert cases[0]["short_id"] == "DSP-1ABC23"


def test_a2a_get_case_round_trips(client: TestClient) -> None:
    body = _rpc(
        client, "message/send", {"skill": "get_case", "args": {"case_id": "case-001"}}
    )
    data = body["result"]["parts"][0]["data"]
    assert data["short_id"] == "DSP-1ABC23"
    assert data["decision"]["action"] == "refund"


def test_a2a_invalid_json_returns_parse_error(client: TestClient) -> None:
    r = client.post(
        "/a2a", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32700
