"""Tests for the three A2A agent services (triage / investigator / advisor).

Pure logic: NO live Postgres (stores are monkeypatched with an extended
in-memory store), NO live LLM (manthan_agent.llm.generate_text is
monkeypatched in ask tests), NO network. TestClient is used WITHOUT a
context manager so lifespans never run and the DB pool is never opened.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import manthan_agent.llm as agent_llm
from manthan_agent.a2a.store import InMemoryCaseStore

import manthan_api.agents.advisor as advisor_mod
import manthan_api.agents.investigator as investigator_mod
import manthan_api.agents.triage as triage_mod
from manthan_api.services import a2a_store


# ──────────────────────────────────────────────────────────────────────
# An InMemoryCaseStore extended with the advisor methods, reusing the
# SAME shared helpers PgCaseStore uses (precheck_recommendation, run_ask,
# OPEN_STATUSES) so the advisor logic is genuinely exercised.
# ──────────────────────────────────────────────────────────────────────


class AdvisorInMemoryStore(InMemoryCaseStore):
    async def ask(self, question: str, case_id: str | None = None) -> dict[str, Any]:
        if not case_id or case_id not in self._cases:
            return {"question": question, "answer": "unknown case", "grounded": False}
        case = await self.get_case(case_id)
        findings = await self.get_findings(case_id)
        brief = await self.get_brief(case_id)
        return await a2a_store.run_ask(
            question, findings=findings, brief=brief,
            case_label=(case or {}).get("short_id") or case_id,
        )

    async def precheck_refund(
        self, customer_ref: str, amount_minor: int | None = None
    ) -> dict[str, Any]:
        cases = [
            c for c in self._cases.values() if c.get("customer_ref") == customer_ref
        ]
        open_cases = sum(
            1 for c in cases if c.get("status") in a2a_store.OPEN_STATUSES
        )
        outcomes: dict[str, int] = {}
        for c in cases:
            action = (c.get("decision") or {}).get("action")
            if action:
                outcomes[action] = outcomes.get(action, 0) + 1
        return {
            "customer_ref": customer_ref,
            "amount_minor": amount_minor,
            "prior_disputes": len(cases),
            "open_cases": open_cases,
            "outcomes": outcomes,
            "recommendation": a2a_store.precheck_recommendation(
                len(cases), open_cases, outcomes, amount_minor=amount_minor
            ),
        }

    async def get_customer_history(
        self, customer_ref: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        out = []
        for c in self._cases.values():
            if c.get("customer_ref") == customer_ref:
                out.append(await self.get_case(c["case_id"]))
            if len(out) >= limit:
                break
        return out

    async def dispute_exposure(self) -> dict[str, Any]:
        by_status: dict[str, dict[str, int]] = {}
        for c in self._cases.values():
            s = c.get("status") or "unknown"
            amt = ((c.get("decision") or {}).get("amount_minor")) or 0
            slot = by_status.setdefault(s, {"cases": 0, "decision_amount_minor": 0})
            slot["cases"] += 1
            slot["decision_amount_minor"] += amt
        open_cases = sum(
            v["cases"] for s, v in by_status.items() if s in a2a_store.OPEN_STATUSES
        )
        open_amount = sum(
            v["decision_amount_minor"]
            for s, v in by_status.items()
            if s in a2a_store.OPEN_STATUSES
        )
        return {
            "open_cases": open_cases,
            "open_decision_amount_minor": open_amount,
            "by_status": by_status,
        }

    async def contribute_evidence(
        self, case_id: str, evidence: dict[str, Any], contributor: str = "remote"
    ) -> dict[str, Any]:
        c = self._cases.get(case_id)
        if c is None:
            return {"error": f"unknown case '{case_id}'"}
        actor = f"a2a:{contributor}"
        c.setdefault("events", []).append(
            {"type": "evidence_contributed", "actor": actor, "data": evidence}
        )
        return {
            "contributed": True,
            "case_id": case_id,
            "type": "evidence_contributed",
            "actor": actor,
        }


def _seeded_store() -> AdvisorInMemoryStore:
    store = AdvisorInMemoryStore()
    store.seed({
        "case_id": "case-001",
        "short_id": "DSP-1ABC23",
        "status": "awaiting_approval",
        "customer_ref": "maya@example.com",
        "decision": {"action": "refund", "amount_minor": 56000, "confidence": 0.97},
        "brief": {
            "tldr": "Duplicate charge confirmed - refund $560.",
            "decision": {"action": "refund", "amount_minor": 56000, "confidence": 0.97},
        },
        "findings": [
            {"seq": 1, "text": "Two identical charges 90s apart.", "confidence": 0.95},
            {"seq": 2, "text": "Webhook retry bug confirmed in Sentry.", "confidence": 0.9},
        ],
        "actions": [{"seq": 1, "kind": "stripe_refund", "payload": {}, "status": "drafted"}],
        "events": [{"type": "case_opened"}],
    })
    store.seed({
        "case_id": "case-002",
        "short_id": "DSP-2DEF45",
        "status": "resolved",
        "customer_ref": "maya@example.com",
        "decision": {"action": "refund", "amount_minor": 1200, "confidence": 0.9},
        "brief": None,
        "findings": [],
        "actions": [],
        "events": [{"type": "case_opened"}],
    })
    return store


def _rpc(client: TestClient, skill: str, args: dict, req_id: int = 1) -> dict:
    r = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "message/send",
            "params": {"skill": skill, "args": args},
        },
    )
    assert r.status_code == 200
    return r.json()


# ──────────────────────────────────────────────────────────────────────
# Cards: each service serves its own identity + skill catalog
# ──────────────────────────────────────────────────────────────────────


def test_investigator_card() -> None:
    client = TestClient(investigator_mod.app)
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["manthanIdentity"]["agentId"] == "manthan-investigator"
    skill_ids = {s["id"] for s in card["skills"]}
    assert "investigate_dispute" in skill_ids
    assert {"get_case", "get_brief", "get_findings"} <= skill_ids
    assert card["url"].endswith("/a2a")


def test_triage_card() -> None:
    client = TestClient(triage_mod.app)
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["manthanIdentity"]["agentId"] == "manthan-triage"
    assert card["name"] == "Manthan Triage"
    assert {s["id"] for s in card["skills"]} == {"route_event"}


def test_advisor_card() -> None:
    client = TestClient(advisor_mod.app)
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["manthanIdentity"]["agentId"] == "manthan-advisor"
    skill_ids = {s["id"] for s in card["skills"]}
    assert {
        "ask",
        "precheck_refund",
        "get_customer_history",
        "dispute_exposure",
        "contribute_evidence",
    } <= skill_ids
    # ...plus the 6 reads, minus the investigator's action skill.
    assert {
        "get_case", "list_cases", "get_brief",
        "get_findings", "get_actions", "get_audit_trail",
    } <= skill_ids
    assert "investigate_dispute" not in skill_ids


# ──────────────────────────────────────────────────────────────────────
# Advisor round-trips over the in-memory store
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def advisor_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(advisor_mod, "_store", _seeded_store())
    return TestClient(advisor_mod.app)


def test_advisor_list_cases_round_trip(advisor_client: TestClient) -> None:
    body = _rpc(advisor_client, "list_cases", {})
    cases = body["result"]["parts"][0]["data"]
    assert {c["case_id"] for c in cases} == {"case-001", "case-002"}


def test_advisor_precheck_refund_round_trip(advisor_client: TestClient) -> None:
    body = _rpc(
        advisor_client, "precheck_refund",
        {"customer_ref": "maya@example.com", "amount_minor": 4200},
    )
    data = body["result"]["parts"][0]["data"]
    assert data["prior_disputes"] == 2
    assert data["open_cases"] == 1  # case-001 is awaiting_approval
    assert data["outcomes"] == {"refund": 2}
    # open case for the customer -> deterministic rule says investigate first
    assert data["recommendation"] == "investigate_first"


def test_advisor_precheck_refund_clean_customer(advisor_client: TestClient) -> None:
    body = _rpc(
        advisor_client, "precheck_refund",
        {"customer_ref": "nobody@example.com", "amount_minor": 900},
    )
    data = body["result"]["parts"][0]["data"]
    assert data["prior_disputes"] == 0
    assert data["recommendation"] == "low_risk"


def test_advisor_contribute_evidence_round_trip(advisor_client: TestClient) -> None:
    body = _rpc(
        advisor_client, "contribute_evidence",
        {
            "case_id": "case-001",
            "contributor": "refund-desk",
            "evidence": {"transcript": "customer confirmed the second charge was unexpected"},
        },
    )
    data = body["result"]["parts"][0]["data"]
    assert data["contributed"] is True
    assert data["actor"] == "a2a:refund-desk"
    # The event landed on the case thread with provenance.
    trail = _rpc(advisor_client, "get_audit_trail", {"case_id": "case-001"})
    events = trail["result"]["parts"][0]["data"]
    contributed = [e for e in events if e.get("type") == "evidence_contributed"]
    assert len(contributed) == 1
    assert contributed[0]["actor"] == "a2a:refund-desk"
    assert "transcript" in contributed[0]["data"]


def test_advisor_dispute_exposure(advisor_client: TestClient) -> None:
    body = _rpc(advisor_client, "dispute_exposure", {})
    data = body["result"]["parts"][0]["data"]
    assert data["open_cases"] == 1
    assert data["open_decision_amount_minor"] == 56000
    assert data["by_status"]["resolved"]["cases"] == 1


def test_advisor_get_customer_history(advisor_client: TestClient) -> None:
    body = _rpc(
        advisor_client, "get_customer_history", {"customer_ref": "maya@example.com"}
    )
    data = body["result"]["parts"][0]["data"]
    assert len(data) == 2
    assert all(c["customer_ref"] == "maya@example.com" for c in data)


# ──────────────────────────────────────────────────────────────────────
# ask(): grounded answer + LLM-unavailable degrade (generate_text mocked)
# ──────────────────────────────────────────────────────────────────────


def test_advisor_ask_grounded(
    advisor_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_generate_text(cfg: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "Refund was right per the duplicate charge [1] and the Sentry bug [2]."

    monkeypatch.setattr(agent_llm, "generate_text", fake_generate_text)

    body = _rpc(
        advisor_client, "ask",
        {"case_id": "case-001", "question": "why was a refund recommended?"},
    )
    data = body["result"]["parts"][0]["data"]
    assert data["grounded"] is True
    assert "[1]" in data["answer"]
    assert data["findings_used"] == 2
    # The grounding prompt actually carried the findings + brief.
    assert "Two identical charges" in captured["user"]
    assert "Duplicate charge confirmed" in captured["user"]


def test_advisor_ask_llm_unavailable(
    advisor_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_not_configured(cfg: Any, **kwargs: Any) -> str:
        raise agent_llm.LLMNotConfigured("GOOGLE_API_KEY is not set")

    monkeypatch.setattr(agent_llm, "generate_text", raise_not_configured)

    body = _rpc(
        advisor_client, "ask", {"case_id": "case-001", "question": "anything?"}
    )
    data = body["result"]["parts"][0]["data"]
    assert data["grounded"] is False
    assert "LLM unavailable" in data["answer"]


# ──────────────────────────────────────────────────────────────────────
# precheck rules (pure)
# ──────────────────────────────────────────────────────────────────────


def test_precheck_recommendation_rules() -> None:
    rec = a2a_store.precheck_recommendation
    assert rec(0, 0, {}) == "low_risk"
    assert rec(0, 0, {}, amount_minor=100000) == "investigate_first"  # >= $500
    assert rec(2, 1, {}) == "investigate_first"  # open case wins
    assert rec(3, 0, {}) == "high_risk"
    assert rec(1, 0, {"fight": 2}) == "high_risk"
    assert rec(1, 0, {"refund": 1}) == "investigate_first"


# ──────────────────────────────────────────────────────────────────────
# Triage: local fallback (INVESTIGATOR_A2A_URL unset) calls the
# investigator's in-process handler
# ──────────────────────────────────────────────────────────────────────


class _StubSettings:
    stripe_webhook_secret = None
    is_dev = True


def _dispute_envelope() -> dict[str, Any]:
    return {
        "id": "evt_test_dispute_1",
        "object": "event",
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "du_1Abc234DefGhi",
                "object": "dispute",
                "amount": 840000,
                "currency": "usd",
                "reason": "product_not_received",
                "charge": "ch_1Abc234",
                "status": "needs_response",
            }
        },
    }


@pytest.fixture()
def stubbed_investigator(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict, str]]:
    """No INVESTIGATOR_A2A_URL + a stubbed in-process handler."""
    monkeypatch.delenv("INVESTIGATOR_A2A_URL", raising=False)
    monkeypatch.setattr(triage_mod, "get_settings", lambda: _StubSettings())

    calls: list[tuple[dict, str]] = []

    async def fake_handle(trigger: dict, *, actor: str = "a2a:remote") -> dict:
        calls.append((trigger, actor))
        return {
            "id": "11111111-1111-1111-1111-111111111111",
            "case_id": "11111111-1111-1111-1111-111111111111",
            "short_id": "DSP-1ABC23",
            "status": {"state": "submitted"},
            "kind": "task",
        }

    monkeypatch.setattr(investigator_mod, "handle_investigate", fake_handle)
    return calls


def test_triage_webhook_local_fallback_creates_case(
    stubbed_investigator: list[tuple[dict, str]],
) -> None:
    client = TestClient(triage_mod.app)
    r = client.post("/webhooks/stripe", json=_dispute_envelope())
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True
    assert body["forwarded"] is True
    assert body["delivery"] == "in_process"
    assert body["case_id"] == "11111111-1111-1111-1111-111111111111"

    # The investigator got a proper triage-contract trigger.
    assert len(stubbed_investigator) == 1
    trigger, actor = stubbed_investigator[0]
    assert actor == "stripe:webhook"
    assert trigger["case_type"] == "chargeback"
    assert trigger["source_surface"] == "stripe_webhook"
    assert trigger["structured"]["event_id"] == "evt_test_dispute_1"
    assert trigger["structured"]["event_object"]["id"] == "du_1Abc234DefGhi"
    assert trigger["short_id_hint"].startswith("DSP-")


def test_triage_webhook_ignores_unknown_event_types(
    stubbed_investigator: list[tuple[dict, str]],
) -> None:
    client = TestClient(triage_mod.app)
    r = client.post(
        "/webhooks/stripe",
        json={"id": "evt_x", "type": "customer.created", "data": {"object": {}}},
    )
    assert r.status_code == 200
    assert r.json()["ignored"] is True
    assert stubbed_investigator == []


def test_triage_route_event_skill(
    stubbed_investigator: list[tuple[dict, str]],
) -> None:
    client = TestClient(triage_mod.app)
    body = _rpc(client, "route_event", {"event": _dispute_envelope()})
    data = body["result"]["parts"][0]["data"]
    assert data["forwarded"] is True
    assert data["case_id"] == "11111111-1111-1111-1111-111111111111"
    assert stubbed_investigator[0][1] == "a2a:triage"


def test_triage_route_event_rejects_unknown_skill() -> None:
    client = TestClient(triage_mod.app)
    r = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0", "id": 9, "method": "message/send",
            "params": {"skill": "investigate_dispute", "args": {}},
        },
    )
    assert r.json()["error"]["code"] == -32601


# ──────────────────────────────────────────────────────────────────────
# Investigator: investigate_dispute creates a case + spawns the run
# (DB + run stubbed; the JSON-RPC shape is what other agents code against)
# ──────────────────────────────────────────────────────────────────────


def test_investigator_a2a_investigate_dispute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    org = uuid.uuid4()
    case = uuid.uuid4()
    spawned: list[tuple[Any, Any, dict]] = []

    async def fake_init_pool() -> None:
        return None

    async def fake_resolve_org(org_slug=None) -> Any:
        # Mirrors the real signature: optional slug from the caller
        # (triage's per-org webhook path), default A2A org otherwise.
        return org

    async def fake_insert(org_id: Any, trigger: dict, **kwargs: Any) -> tuple[Any, str]:
        assert org_id == org
        assert kwargs.get("actor") == "a2a:remote"
        return case, "DSP-TEST01"

    monkeypatch.setattr(investigator_mod, "init_pool", fake_init_pool)
    monkeypatch.setattr(investigator_mod, "_resolve_org_id", fake_resolve_org)
    monkeypatch.setattr(investigator_mod, "insert_case_from_trigger", fake_insert)
    monkeypatch.setattr(
        investigator_mod, "_spawn_investigation",
        lambda org_id, case_id, trigger: spawned.append((org_id, case_id, trigger)),
    )

    client = TestClient(investigator_mod.app)
    body = _rpc(
        client, "investigate_dispute",
        {"text": "investigate du_123", "case_type": "chargeback", "structured": {}},
    )
    result = body["result"]
    assert result["kind"] == "task"
    assert result["status"]["state"] == "submitted"
    assert result["case_id"] == str(case)
    assert result["short_id"] == "DSP-TEST01"
    assert len(spawned) == 1 and spawned[0][1] == case


def test_investigator_tasks_get_via_shared_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryCaseStore()
    store.seed({
        "case_id": "case-009",
        "short_id": "DSP-9",
        "status": "awaiting_approval",
        "customer_ref": "x@example.com",
        "decision": {"action": "fight", "amount_minor": 900000, "confidence": 0.8},
        "brief": None, "findings": [], "actions": [], "events": [],
    })
    monkeypatch.setattr(investigator_mod, "_store", store)
    client = TestClient(investigator_mod.app)
    r = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": "case-009"}},
    )
    body = r.json()
    assert body["result"]["status"]["state"] == "completed"
