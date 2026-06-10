"""Tests for the Stripe-webhook triage — pure logic, no LLM, no network.

trigger_from_stripe_event is a CROSS-AGENT CONTRACT: another agent builds
against the exact signature and returned keys, so these tests pin the shape
({"text", "case_type", "customer_ref", "structured", "source_surface"}) as
well as the per-type framing and minor-unit money rendering.
"""

from __future__ import annotations

from manthan_agent.triage import trigger_from_stripe_event

CONTRACT_KEYS = {"text", "case_type", "customer_ref", "structured", "source_surface"}


def _envelope(etype: str, obj: dict) -> dict:
    return {"id": "evt_1", "type": etype, "data": {"object": obj}}


def _dispute_obj(**over) -> dict:
    obj = {
        "id": "du_99",
        "object": "dispute",
        "amount": 840000,
        "currency": "usd",
        "charge": "ch_42",
        "reason": "fraudulent",
        "status": "needs_response",
        "evidence": {"customer_email_address": "jo@acme.com"},
    }
    obj.update(over)
    return obj


# ── contract shape across all five handled types ─────────────────────────


def test_all_five_types_produce_contract_keys() -> None:
    events = [
        _envelope("charge.dispute.created", _dispute_obj()),
        _envelope("charge.dispute.funds_withdrawn", _dispute_obj()),
        _envelope("charge.dispute.closed", _dispute_obj(status="lost")),
        _envelope("radar.early_fraud_warning.created",
                  {"id": "issfr_1", "charge": "ch_42", "fraud_type": "misuse_of_card",
                   "actionable": True}),
        _envelope("invoice.payment_failed",
                  {"id": "in_7", "amount_due": 129900, "currency": "usd",
                   "customer": "cus_9", "customer_email": "kay@example.io",
                   "attempt_count": 2}),
    ]
    for event in events:
        trig = trigger_from_stripe_event(event)
        assert set(trig.keys()) == CONTRACT_KEYS, event["type"]
        assert isinstance(trig["text"], str) and trig["text"]
        assert isinstance(trig["case_type"], str) and trig["case_type"]
        assert isinstance(trig["customer_ref"], str)
        assert isinstance(trig["structured"], dict)
        assert trig["source_surface"] == "stripe_webhook"
        assert trig["structured"]["event_type"] == event["type"]


# ── per-type framing + money rendering ───────────────────────────────────


def test_dispute_created_chargeback() -> None:
    trig = trigger_from_stripe_event(_envelope("charge.dispute.created", _dispute_obj()))
    assert trig["case_type"] == "chargeback"
    assert "$8,400.00" in trig["text"]          # 840000 minor units, with commas
    assert "du_99" in trig["text"]
    assert "fraudulent" in trig["text"]
    # asks for a decision with complete drafted actions
    for word in ("fight", "refund", "accept", "escalate", "drafted actions"):
        assert word in trig["text"]
    assert trig["customer_ref"] == "jo@acme.com"   # via evidence.customer_email_address
    assert trig["structured"]["object"]["id"] == "du_99"


def test_funds_withdrawn_is_urgent() -> None:
    trig = trigger_from_stripe_event(
        _envelope("charge.dispute.funds_withdrawn", _dispute_obj()))
    assert trig["case_type"] == "chargeback_funds_withdrawn"
    assert "URGENT" in trig["text"]
    assert "withdrawn" in trig["text"]
    assert "$8,400.00" in trig["text"]


def test_dispute_closed_is_post_mortem() -> None:
    trig = trigger_from_stripe_event(
        _envelope("charge.dispute.closed", _dispute_obj(status="lost")))
    assert trig["case_type"] == "dispute_closed"
    assert "'lost'" in trig["text"]
    assert "post-mortem" in trig["text"]
    # learning framing: outcome vs what we recommended
    assert "outcome" in trig["text"]
    assert "recommendation" in trig["text"]


def test_fraud_warning_is_pre_dispute() -> None:
    trig = trigger_from_stripe_event(_envelope(
        "radar.early_fraud_warning.created",
        {"id": "issfr_1", "charge": "ch_42", "fraud_type": "misuse_of_card",
         "actionable": True},
    ))
    assert trig["case_type"] == "fraud_warning"
    assert "issfr_1" in trig["text"]
    assert "misuse_of_card" in trig["text"]
    assert "No dispute has been filed yet" in trig["text"]
    assert "preemptively refund" in trig["text"]
    assert trig["customer_ref"] == ""   # EFW object carries no customer fields


def test_payment_failed_is_dunning() -> None:
    trig = trigger_from_stripe_event(_envelope(
        "invoice.payment_failed",
        {"id": "in_7", "amount_due": 129900, "currency": "usd",
         "customer": "cus_9", "customer_email": "kay@example.io",
         "attempt_count": 2},
    ))
    assert trig["case_type"] == "payment_failed"
    assert "$1,299.00" in trig["text"]
    assert "attempt 2" in trig["text"]
    assert "involuntary churn" in trig["text"]
    assert "intentional" in trig["text"]
    assert trig["customer_ref"] == "kay@example.io"   # email beats customer id


def test_customer_ref_falls_back_to_customer_id() -> None:
    trig = trigger_from_stripe_event(_envelope(
        "invoice.payment_failed",
        {"id": "in_8", "amount_due": 5000, "currency": "usd", "customer": "cus_77"},
    ))
    assert trig["customer_ref"] == "cus_77"


def test_non_usd_currency_renders_code() -> None:
    trig = trigger_from_stripe_event(_envelope(
        "charge.dispute.created", _dispute_obj(amount=1234567, currency="sek")))
    assert "12,345.67 SEK" in trig["text"]


# ── unknown / malformed input is always safe ─────────────────────────────


def test_unknown_event_type_gets_generic_framing() -> None:
    trig = trigger_from_stripe_event(_envelope(
        "customer.subscription.updated", {"id": "sub_1", "customer": "cus_5"}))
    assert set(trig.keys()) == CONTRACT_KEYS
    assert trig["case_type"] == "stripe_event"
    assert "customer.subscription.updated" in trig["text"]
    assert trig["customer_ref"] == "cus_5"


def test_never_raises_on_garbage() -> None:
    for garbage in (
        {},                                          # empty envelope
        {"type": "charge.dispute.created"},          # no data.object
        {"type": "charge.dispute.created", "data": {"object": {"amount": "weird"}}},
        {"type": None, "data": None},
        {"data": {"object": []}},                    # object isn't a dict
        "not even a dict",                           # event isn't a dict
        None,
    ):
        trig = trigger_from_stripe_event(garbage)  # type: ignore[arg-type]
        assert set(trig.keys()) == CONTRACT_KEYS
        assert trig["source_surface"] == "stripe_webhook"
        assert isinstance(trig["text"], str) and trig["text"]
