"""Pure-logic tests for services/case_store.py (extracted from the retired
investigate worker).

No Postgres: where a function touches the pool, we monkeypatch
``case_store.get_pool`` with a small fake that returns canned rows and
records executed statements.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from manthan_api.services import case_store
from manthan_api.services.case_store import (
    _build_customer_email,
    _customer_display_name,
    _dollars,
    _strip_html_tags,
)
from manthan_api.workers.actor import make_idempotency_key


# ──────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────


class FakeConn:
    """asyncpg connection stand-in. fetchrow/fetchval pop canned results
    in call order; execute records (normalized_sql, args) and can raise
    queued exceptions."""

    def __init__(
        self,
        *,
        fetchrow_results: list[Any] | None = None,
        fetchval_results: list[Any] | None = None,
        execute_errors: list[Exception | None] | None = None,
    ) -> None:
        self._fetchrow = list(fetchrow_results or [])
        self._fetchval = list(fetchval_results or [])
        self._execute_errors = list(execute_errors or [])
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        return self._fetchval.pop(0) if self._fetchval else None

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((" ".join(query.split()), args))
        if self._execute_errors:
            err = self._execute_errors.pop(0)
            if err is not None:
                raise err
        return "OK"


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _use_pool(monkeypatch: pytest.MonkeyPatch, conn: FakeConn) -> None:
    monkeypatch.setattr(case_store, "get_pool", lambda: FakePool(conn))


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make env-dependent defaults deterministic."""
    for var in (
        "NOTION_DECISION_LOG_PARENT_ID",
        "MANTHAN_SLACK_CHANNEL",
        "SLACK_DEFAULT_CHANNEL",
        "MANTHAN_FALLBACK_CUSTOMER_EMAIL",
        "MANTHAN_EMAIL_FROM",
        "RESEND_FROM_ADDRESS",
    ):
        monkeypatch.delenv(var, raising=False)


# ──────────────────────────────────────────────────────────────────────
# _dollars
# ──────────────────────────────────────────────────────────────────────


def test_dollars_none_is_dash():
    assert _dollars(None) == "-"


def test_dollars_formats_cents_and_thousands():
    assert _dollars(0) == "$0.00"
    assert _dollars(56000) == "$560.00"
    assert _dollars(123456789) == "$1,234,567.89"


# ──────────────────────────────────────────────────────────────────────
# _customer_display_name
# ──────────────────────────────────────────────────────────────────────


def test_display_name_none_and_empty():
    assert _customer_display_name(None) == "there"
    assert _customer_display_name("") == "there"


def test_display_name_role_prefix_email_is_there():
    assert _customer_display_name("billing@aperture-analytics.co") == "there"
    assert _customer_display_name("support@x.io") == "there"


def test_display_name_personal_email_titlecased():
    assert _customer_display_name("maya.chen@gmail.com") == "Maya"


def test_display_name_company_name_first_word():
    assert _customer_display_name("Aperture Analytics") == "Aperture"


# ──────────────────────────────────────────────────────────────────────
# _build_customer_email
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "decision", ["refund", "partial_credit", "partial_refund", "fight", "escalate", None, "weird"],
)
def test_email_builder_returns_3_tuple_of_strings(decision):
    out = _build_customer_email(
        decision_action=decision,
        amount_str="$560.00",
        short_id="CASE-7",
        dispute_id=None,
        customer_name="Maya",
    )
    assert isinstance(out, tuple) and len(out) == 3
    subject, html, text = out
    assert all(isinstance(part, str) and part for part in (subject, html, text))
    # No operator-facing internals leak into customer copy.
    for part in (subject, html, text):
        assert "[Finding" not in part
        assert "[Cites" not in part


def test_email_builder_duplicate_charge_framing():
    subject, html, text = _build_customer_email(
        decision_action="refund",
        amount_str="$560.00",
        short_id="CASE-7",
        dispute_id=None,
        customer_name="Maya",
        is_duplicate_charge=True,
    )
    assert subject == "We refunded the duplicate $560.00 charge"
    assert "duplicate charge" in html
    assert "Hi Maya," in text
    assert "Refund: $560.00" in text  # summary card in plaintext fallback
    assert "CASE-7" in html and "CASE-7" in text


def test_email_builder_dispute_refund_framing():
    subject, _html, text = _build_customer_email(
        decision_action="refund",
        amount_str="$120.00",
        short_id="CASE-9",
        dispute_id="du_123",
        customer_name="there",
    )
    assert subject == "Refund of $120.00 approved on your dispute"
    assert "dispute du_123" in text


def test_email_builder_escalate_has_no_amount_card():
    _subject, html, text = _build_customer_email(
        decision_action="escalate",
        amount_str="$560.00",
        short_id="CASE-7",
        dispute_id=None,
        customer_name="Maya",
    )
    # summary_label is None for escalate - the amount card is omitted.
    assert "$560.00" not in html
    assert "$560.00" not in text


def test_strip_html_tags():
    assert _strip_html_tags("a <strong>b</strong>   c") == "a b c"


# ──────────────────────────────────────────────────────────────────────
# Idempotency key (actor's, reused by record_brief)
# ──────────────────────────────────────────────────────────────────────


def test_idempotency_key_stable_and_order_insensitive():
    case_id = uuid4()
    k1 = make_idempotency_key(case_id, "stripe_refund", {"a": 1, "b": 2})
    k2 = make_idempotency_key(case_id, "stripe_refund", {"b": 2, "a": 1})
    assert k1 == k2
    assert len(k1) == 32


def test_idempotency_key_varies_with_inputs():
    case_id = uuid4()
    base = make_idempotency_key(case_id, "stripe_refund", {"a": 1})
    assert make_idempotency_key(case_id, "stripe_refund", {"a": 2}) != base
    assert make_idempotency_key(case_id, "customer_email", {"a": 1}) != base
    assert make_idempotency_key(uuid4(), "stripe_refund", {"a": 1}) != base


# ──────────────────────────────────────────────────────────────────────
# append_event retry loop
# ──────────────────────────────────────────────────────────────────────


def test_append_event_retries_on_unique_violation(monkeypatch):
    conn = FakeConn(execute_errors=[
        asyncpg.UniqueViolationError("dup"),
        asyncpg.UniqueViolationError("dup"),
        None,
    ])
    _use_pool(monkeypatch, conn)
    asyncio.run(case_store.append_event(
        uuid4(), uuid4(), "case_opened", "system", {"k": "v"},
        trace_id="ignored", span_id="ignored",  # accepted, no-op (no columns)
    ))
    assert len(conn.executed) == 3
    sql, args = conn.executed[-1]
    assert "INSERT INTO events" in sql
    assert args[2:] == ("case_opened", "system", {"k": "v"})


def test_append_event_gives_up_after_5_attempts(monkeypatch):
    conn = FakeConn(execute_errors=[asyncpg.UniqueViolationError("dup")] * 5)
    _use_pool(monkeypatch, conn)
    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(case_store.append_event(
            uuid4(), uuid4(), "case_opened", "system", {},
        ))
    assert len(conn.executed) == 5


# ──────────────────────────────────────────────────────────────────────
# record_finding_projection - citation normalization
# ──────────────────────────────────────────────────────────────────────


def test_finding_projection_prefers_citations_resolved(monkeypatch):
    conn = FakeConn(fetchval_results=[3])  # next seq
    _use_pool(monkeypatch, conn)
    asyncio.run(case_store.record_finding_projection(
        uuid4(), uuid4(),
        {
            "text": "Duplicate confirmed",
            "confidence": 0.9,
            "citations": [0, 1],  # legacy int indices - must be ignored
            "citations_resolved": [
                {"source": "coral", "table": "stripe.charges", "ref": "ch_1", "field": "id", "idx": 0},
                "not-a-dict",
            ],
        },
    ))
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "INSERT INTO findings" in sql
    org_id, case_id, seq, text, conf, cites = args
    assert seq == 3 and text == "Duplicate confirmed" and conf == 0.9
    # Non-dict entries dropped; idx not carried into the normalized shape.
    assert cites == [{"source": "coral", "table": "stripe.charges", "ref": "ch_1", "field": "id"}]


def test_finding_projection_falls_back_to_legacy_citations(monkeypatch):
    conn = FakeConn(fetchval_results=[1])
    _use_pool(monkeypatch, conn)
    asyncio.run(case_store.record_finding_projection(
        uuid4(), uuid4(),
        {
            "finding": "via legacy field",
            "confidence": "high",  # non-numeric -> stored as NULL
            "citations": [{"table": "stripe.disputes", "ref": "du_9"}],
        },
    ))
    _sql, args = conn.executed[0]
    assert args[3] == "via legacy field"
    assert args[4] is None
    assert args[5] == [{"source": "manthan", "table": "stripe.disputes", "ref": "du_9", "field": None}]


# ──────────────────────────────────────────────────────────────────────
# _synthesize_actions - payload shaping with stubbed DB rows
# ──────────────────────────────────────────────────────────────────────


def test_synthesize_refund_prefers_labeled_duplicate_charge(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=[
        {"data": {"trigger_text": (
            "Customer reports double billing. Original charge: ch_orig111. "
            "Duplicate charge: ch_dup222."
        )}},
        {"short_id": "CASE-7", "customer_ref": "maya@gmail.com"},
    ])
    _use_pool(monkeypatch, conn)
    actions = asyncio.run(case_store._synthesize_actions(
        uuid4(), uuid4(), uuid4(), "refund", 56000,
    ))
    assert [a["kind"] for a in actions] == ["stripe_refund", "customer_email"]
    refund = actions[0]["payload"]
    assert refund["charge"] == "ch_dup222"  # labeled duplicate wins over first ch_*
    assert refund["amount_minor"] == 56000
    assert refund["reason"] == "requested_by_customer"
    assert refund["metadata"] == {"manthan_case_short_id": "CASE-7"}
    email = actions[1]["payload"]
    assert email["to"] == "maya@gmail.com"  # customer_ref looks like an email
    assert email["subject"] == "Refund processed - case CASE-7"
    assert "$560.00" in email["body_text"]


def test_synthesize_fight_uses_dispute_id_and_fallback_email(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=[
        {"data": {"trigger_text": "Chargeback du_999 on ch_abc for Aperture."}},
        {"short_id": "CASE-9", "customer_ref": "Aperture Analytics"},
    ])
    _use_pool(monkeypatch, conn)
    actions = asyncio.run(case_store._synthesize_actions(
        uuid4(), uuid4(), uuid4(), "fight", 120000,
    ))
    assert actions[0]["kind"] == "stripe_dispute_response"
    assert actions[0]["payload"]["dispute"] == "du_999"
    assert actions[0]["payload"]["submit"] is False
    # customer_ref isn't an email -> fallback address
    assert actions[1]["payload"]["to"] == "ops@manthan.quest"


def test_synthesize_no_charge_yields_email_only(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=[
        {"data": {"trigger_text": "no ids here"}},
        {"short_id": "CASE-2", "customer_ref": "x@y.zz"},
    ])
    _use_pool(monkeypatch, conn)
    actions = asyncio.run(case_store._synthesize_actions(
        uuid4(), uuid4(), uuid4(), "refund", 100,
    ))
    assert [a["kind"] for a in actions] == ["customer_email"]


# ──────────────────────────────────────────────────────────────────────
# _enrich_drafted_actions - overlay shaping with stubbed DB rows
# ──────────────────────────────────────────────────────────────────────


def _enrich_rows(trigger_payload: dict | None = None, **case_overrides: Any):
    case = {
        "short_id": "CASE-9",
        "customer_ref": "billing@aperture-analytics.co",
        "trigger_payload": trigger_payload if trigger_payload is not None else {},
        "currency": "usd",
        "case_type": "duplicate_charge",
    }
    case.update(case_overrides)
    return [
        {"data": {"trigger_text": "Duplicate charge: ch_text999."}},
        case,
    ]


def test_enrich_fills_thin_payloads(monkeypatch):
    _clean_env(monkeypatch)
    case_id = uuid4()
    conn = FakeConn(fetchrow_results=_enrich_rows(
        {"charge": "ch_tp1", "customer_email": "finance@aperture.co"},
    ))
    _use_pool(monkeypatch, conn)
    drafted = [
        {"kind": "stripe_refund", "description": "Refund $560", "payload": {}},
        {"kind": "customer_email", "description": "Email customer", "payload": None},
        {"kind": "slack_brief", "description": "Post brief"},
        "garbage-skipped",
    ]
    enriched = asyncio.run(case_store._enrich_drafted_actions(
        uuid4(), uuid4(), case_id, "refund", 56000, "operator rationale [2][3]", drafted,
    ))
    assert [a["kind"] for a in enriched] == ["stripe_refund", "customer_email", "slack_brief"]

    refund = enriched[0]["payload"]
    assert refund["charge"] == "ch_tp1"  # trigger_payload beats trigger-text regex
    assert refund["amount_minor"] == 56000
    assert refund["currency"] == "usd"
    assert refund["reason"] == "requested_by_customer"
    assert refund["metadata"]["manthan_case_short_id"] == "CASE-9"
    assert refund["metadata"]["case_id"] == str(case_id)
    assert enriched[0]["reversibility"] == "reversible"  # default applied

    email = enriched[1]["payload"]
    assert email["to"] == "finance@aperture.co"
    assert email["from"] == "manthan@miny-labs.com"
    # Templated copy always wins; case_type says duplicate -> duplicate framing.
    assert email["subject"] == "We refunded the duplicate $560.00 charge"
    assert email["body_html"] == email["html"]
    assert "operator rationale" not in email["body_text"]  # rationale never reaches customer

    slack = enriched[2]["payload"]
    assert slack["channel"] == "#billing-ops"  # env-unset default
    assert "CASE-9" in slack["text"]


def test_enrich_agent_supplied_fields_win(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=_enrich_rows({"charge": "ch_tp1"}))
    _use_pool(monkeypatch, conn)
    drafted = [{
        "kind": "stripe_refund",
        "description": "agent refund",
        "reversibility": "irreversible",
        "payload": {"charge": "ch_agent", "amount_minor": 100, "currency": "eur", "reason": "fraud"},
    }]
    enriched = asyncio.run(case_store._enrich_drafted_actions(
        uuid4(), uuid4(), uuid4(), "refund", 56000, "", drafted,
    ))
    payload = enriched[0]["payload"]
    assert payload["charge"] == "ch_agent"
    assert payload["amount_minor"] == 100
    assert payload["currency"] == "eur"
    assert payload["reason"] == "fraud"
    assert enriched[0]["reversibility"] == "irreversible"


def test_enrich_charge_id_alias_accepted(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=_enrich_rows())
    _use_pool(monkeypatch, conn)
    drafted = [{"kind": "stripe_refund", "payload": {"charge_id": "ch_alias"}}]
    enriched = asyncio.run(case_store._enrich_drafted_actions(
        uuid4(), uuid4(), uuid4(), "refund", None, "", drafted,
    ))
    payload = enriched[0]["payload"]
    assert payload["charge"] == "ch_alias"
    assert "charge_id" not in payload


def test_enrich_demo_email_to_overrides_recipient(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=_enrich_rows({"demo_email_to": "operator@demo.dev"}))
    _use_pool(monkeypatch, conn)
    drafted = [{"kind": "customer_email", "payload": {"to": "real@customer.com"}}]
    enriched = asyncio.run(case_store._enrich_drafted_actions(
        uuid4(), uuid4(), uuid4(), "refund", 56000, "", drafted,
    ))
    payload = enriched[0]["payload"]
    assert payload["to"] == "operator@demo.dev"
    assert payload["bypass_demo_override"] is True


def test_enrich_dispute_response_defaults(monkeypatch):
    _clean_env(monkeypatch)
    conn = FakeConn(fetchrow_results=[
        {"data": {"trigger_text": "Dispute du_777 opened."}},
        {
            "short_id": "CASE-3",
            "customer_ref": "x@y.zz",
            "trigger_payload": {},
            "currency": "usd",
            "case_type": "chargeback",
        },
    ])
    _use_pool(monkeypatch, conn)
    drafted = [{"kind": "stripe_dispute_response", "payload": {}}]
    enriched = asyncio.run(case_store._enrich_drafted_actions(
        uuid4(), uuid4(), uuid4(), "fight", None, "the rationale", drafted,
    ))
    payload = enriched[0]["payload"]
    assert payload["dispute"] == "du_777"  # regex fallback from trigger text
    assert payload["submit"] is False
    assert payload["evidence"] == {"uncategorized_text": "the rationale"}


# ──────────────────────────────────────────────────────────────────────
# serialize
# ──────────────────────────────────────────────────────────────────────


def test_serialize_handles_nested_and_dataclass():
    import dataclasses

    @dataclasses.dataclass
    class Cite:
        ref: str

    out = case_store.serialize({"a": [Cite(ref="ch_1"), (1, 2)], "b": None})
    assert out == {"a": [{"ref": "ch_1"}, [1, 2]], "b": None}
