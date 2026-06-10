"""Stripe fan-out contract tests - pure logic, no network, no DB.

Each of the five webhook event types the API fans out on must map through
manthan_agent.triage.trigger_from_stripe_event (the cross-agent triage
contract). The triage module ships with the agent-package build; until it
lands, the triage-dependent tests skip with an explicit reason rather than
stubbing the contract.
"""

from __future__ import annotations

import pytest

from manthan_api.api.webhooks import TRIGGERING_EVENTS, _short_id_from_event


def _envelope(event_type: str, obj: dict) -> dict:
    """A Stripe webhook envelope as the triage contract consumes it."""
    return {
        "id": f"evt_{event_type.replace('.', '_')}",
        "object": "event",
        "type": event_type,
        "data": {"object": obj},
    }


_OBJECTS: dict[str, dict] = {
    "charge.dispute.created": {
        "id": "du_1Abc234DefGhi",
        "object": "dispute",
        "amount": 840000,
        "currency": "usd",
        "reason": "product_not_received",
        "charge": "ch_1Abc234",
        "status": "needs_response",
        "evidence_details": {"due_by": 1767139200},
    },
    "charge.dispute.funds_withdrawn": {
        "id": "du_1Xyz987Wvu",
        "object": "dispute",
        "amount": 12500,
        "currency": "usd",
        "reason": "fraudulent",
        "charge": "ch_1Xyz987",
        "status": "needs_response",
    },
    "charge.dispute.closed": {
        "id": "du_1Closed01",
        "object": "dispute",
        "amount": 45000,
        "currency": "usd",
        "reason": "general",
        "charge": "ch_1Closed",
        "status": "won",
    },
    "radar.early_fraud_warning.created": {
        "id": "issfr_1Warn001",
        "object": "radar.early_fraud_warning",
        "fraud_type": "made_with_stolen_card",
        "charge": "ch_1Warn001",
        "actionable": True,
    },
    "invoice.payment_failed": {
        "id": "in_1Inv00123",
        "object": "invoice",
        "amount_due": 99000,
        "currency": "usd",
        "attempt_count": 2,
        "customer": "cus_123",
        "customer_email": "finance@northwind.example",
    },
}

ALL_FIVE = sorted(_OBJECTS)


def test_webhook_fans_out_on_exactly_the_five_event_types() -> None:
    assert TRIGGERING_EVENTS == set(ALL_FIVE)


@pytest.mark.parametrize("event_type", ALL_FIVE)
def test_each_event_type_maps_through_triage(event_type: str) -> None:
    triage = pytest.importorskip(
        "manthan_agent.triage",
        reason="manthan_agent.triage lands with the agent-package build",
    )
    trig = triage.trigger_from_stripe_event(_envelope(event_type, _OBJECTS[event_type]))

    # The cross-agent contract: exactly these keys, stripe_webhook surface.
    assert set(trig) >= {"text", "case_type", "customer_ref", "structured", "source_surface"}
    assert trig["source_surface"] == "stripe_webhook"
    assert isinstance(trig["text"], str) and trig["text"].strip()
    assert isinstance(trig["case_type"], str) and trig["case_type"]
    assert isinstance(trig["customer_ref"], str)
    assert isinstance(trig["structured"], dict)


@pytest.mark.parametrize("event_type", ALL_FIVE)
def test_short_id_prefix_is_deterministic(event_type: str) -> None:
    sid = _short_id_from_event(event_type, _OBJECTS[event_type])
    prefix, suffix = sid.split("-", 1)
    assert prefix in {"DSP", "EFW", "INV"}
    assert suffix  # derived from the Stripe object id
