"""Regression tests for the adversarial-review defects in a2a_store helpers.

Pure functions — no Postgres needed.
"""

from __future__ import annotations

from manthan_api.services.a2a_store import _amount_from_structured, _case_projection


class _Row(dict):
    """asyncpg.Record stand-in (mapping access)."""


def _row(**kw):
    base = {
        "id": "5c0c5fc6-0000-0000-0000-000000000001",
        "short_id": "CASE-1234",
        "status": "investigating",
        "customer_ref": "billing@aperture-analytics.co",
        "decision_action": None,
        "decision_amount_minor": None,
        "decision_confidence": None,
    }
    base.update(kw)
    return _Row(base)


def test_undecided_case_has_none_decision():
    # DEFECT 3: a dict of Nones is truthy -> A2A tasks/get said "completed"
    # for cases the agent hadn't decided yet.
    proj = _case_projection(_row())
    assert proj["decision"] is None


def test_decided_case_has_decision_dict():
    proj = _case_projection(_row(
        decision_action="refund", decision_amount_minor=56000, decision_confidence=0.98,
    ))
    assert proj["decision"] == {"action": "refund", "amount_minor": 56000, "confidence": 0.98}


def test_amount_found_under_triage_nested_object():
    # DEFECT 2: triage nests the raw Stripe object under structured["object"]
    # — missing it made webhook cases lose amount_minor and broke the policy
    # engine's amount thresholds.
    structured = {
        "event_type": "charge.dispute.created",
        "object": {"id": "du_x", "amount": 840000, "currency": "USD"},
    }
    amount, currency = _amount_from_structured(structured)
    assert amount == 840000
    assert currency == "usd"


def test_amount_found_under_event_object_alias():
    structured = {"event_object": {"amount_due": 12300, "currency": "eur"}}
    assert _amount_from_structured(structured) == (12300, "eur")


def test_amount_top_level_still_wins():
    structured = {"amount_minor": 500, "currency": "usd", "object": {"amount": 999}}
    assert _amount_from_structured(structured) == (500, "usd")


def test_no_amount_is_none_not_crash():
    assert _amount_from_structured({"object": {"id": "fw_1"}}) == (None, None)
