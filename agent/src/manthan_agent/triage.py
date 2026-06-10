"""Stripe-webhook triage — turn a raw Stripe event into an investigation trigger.

CROSS-AGENT CONTRACT (do not change the signature or the returned keys —
another agent builds against this exact shape):

    trigger_from_stripe_event(event: dict) -> dict with keys
        {"text", "case_type", "customer_ref", "structured", "source_surface"}

`event` is a Stripe webhook envelope: {"type": "...", "data": {"object": {...}}}.

This module is PURE: stdlib only, no I/O, no network, never raises. Unknown
event types fall back to a generic "stripe_event" framing instead of failing —
a webhook fan-out must never lose an event to a triage crash.

Each handled type gets investigation-quality trigger text (the agent's opening
brief), tuned to what the event actually means for the business:

  charge.dispute.created            -> chargeback            (investigate + recommend)
  charge.dispute.funds_withdrawn    -> chargeback_funds_withdrawn (money already gone — urgent)
  charge.dispute.closed             -> dispute_closed        (post-mortem / learning)
  radar.early_fraud_warning.created -> fraud_warning         (pre-dispute: preemptive refund?)
  invoice.payment_failed            -> payment_failed        (dunning: involuntary vs intentional)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SOURCE_SURFACE = "stripe_webhook"

# Stripe zero-decimal currencies: `amount` is already in major units.
_ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

_SYMBOL = {"usd": "$", "eur": "€", "gbp": "£", "inr": "₹"}


def _money(amount_minor: Any, currency: Any = "usd") -> str:
    """Render Stripe minor units as human money: 840000/'usd' -> '$8,400.00'."""
    try:
        minor = int(amount_minor)
    except (TypeError, ValueError):
        return "an unknown amount"
    cur = str(currency or "usd").lower()
    if cur in _ZERO_DECIMAL:
        body = f"{minor:,.0f}"
    else:
        body = f"{minor / 100:,.2f}"
    sym = _SYMBOL.get(cur)
    return f"{sym}{body}" if sym else f"{body} {cur.upper()}"


def _customer_ref(obj: dict[str, Any]) -> str:
    """Best-effort customer reference: email first, then customer id, else ''."""
    if not isinstance(obj, dict):
        return ""
    for key in ("customer_email", "email", "billing_email"):
        v = obj.get(key)
        if isinstance(v, str) and v:
            return v
    billing = obj.get("billing_details")
    if isinstance(billing, dict):
        v = billing.get("email")
        if isinstance(v, str) and v:
            return v
    evidence = obj.get("evidence")
    if isinstance(evidence, dict):
        v = evidence.get("customer_email_address")
        if isinstance(v, str) and v:
            return v
    cust = obj.get("customer")
    if isinstance(cust, str) and cust:
        return cust
    if isinstance(cust, dict):
        for key in ("email", "id"):
            v = cust.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _due_by_clause(obj: dict[str, Any]) -> str:
    details = obj.get("evidence_details")
    due = details.get("due_by") if isinstance(details, dict) else None
    if isinstance(due, (int, float)) and due > 0:
        try:
            return f" Evidence is due by {datetime.fromtimestamp(due, tz=UTC):%Y-%m-%d}."
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


# ── Per-type text builders. Each returns (text, case_type). ─────────────────


def _dispute_created(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    amount = _money(obj.get("amount"), obj.get("currency"))
    text = (
        f"New Stripe chargeback: dispute {obj.get('id') or '(unknown dispute id)'} "
        f"for {amount} on charge {obj.get('charge') or '(unknown charge)'}, "
        f"reason '{obj.get('reason') or 'unspecified'}', "
        f"status '{obj.get('status') or 'needs_response'}'."
        f"{_due_by_clause(obj)} "
        "Investigate the customer's full history across all connected sources and "
        "recommend fight / refund / accept / escalate, with the complete set of "
        "drafted actions for the chosen path (dispute response, refund, customer "
        "email, internal notes — whatever the decision requires)."
    )
    return text, "chargeback"


def _funds_withdrawn(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    amount = _money(obj.get("amount"), obj.get("currency"))
    text = (
        f"URGENT — Stripe has withdrawn {amount} from our balance for dispute "
        f"{obj.get('id') or '(unknown dispute id)'} "
        f"(reason '{obj.get('reason') or 'unspecified'}', "
        f"charge {obj.get('charge') or '(unknown charge)'}). The money is already "
        "gone; recovering it now depends entirely on winning the dispute."
        f"{_due_by_clause(obj)} "
        "Treat this as time-critical: investigate immediately and recommend "
        "fight / refund / accept / escalate with complete drafted actions — every "
        "day without a response narrows the window to recover the funds."
    )
    return text, "chargeback_funds_withdrawn"


def _dispute_closed(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    amount = _money(obj.get("amount"), obj.get("currency"))
    text = (
        f"Stripe dispute {obj.get('id') or '(unknown dispute id)'} has closed with "
        f"status '{obj.get('status') or 'unknown'}' for {amount} "
        f"(reason '{obj.get('reason') or 'unspecified'}', "
        f"charge {obj.get('charge') or '(unknown charge)'}). "
        "This is a post-mortem, not a new fight: pull our original recommendation "
        "and findings for this dispute, record the final outcome versus what we "
        "advised, and capture what we got right or wrong so future "
        "fight/refund/accept calls improve. No new money movement is expected."
    )
    return text, "dispute_closed"


def _fraud_warning(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    actionable = obj.get("actionable")
    text = (
        f"Stripe Radar early fraud warning {obj.get('id') or '(unknown warning id)'} "
        f"for charge {obj.get('charge') or '(unknown charge)'} "
        f"(fraud type '{obj.get('fraud_type') or 'unspecified'}', "
        f"actionable={actionable if actionable is not None else 'unknown'}). "
        "No dispute has been filed yet — this is a pre-dispute fraud signal from "
        "the card network. Assess whether the charge is genuinely fraudulent and "
        "whether to preemptively refund it now to avoid a formal chargeback and "
        "its fee, or to hold because the charge looks legitimate. Recommend with "
        "complete drafted actions."
    )
    return text, "fraud_warning"


def _payment_failed(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    amount = _money(obj.get("amount_due", obj.get("amount")), obj.get("currency"))
    attempt = obj.get("attempt_count")
    attempt_clause = f", attempt {attempt}" if isinstance(attempt, int) else ""
    next_at = obj.get("next_payment_attempt")
    if isinstance(next_at, (int, float)) and next_at > 0:
        try:
            retry_clause = (
                f" Stripe will retry on {datetime.fromtimestamp(next_at, tz=UTC):%Y-%m-%d}."
            )
        except (OverflowError, OSError, ValueError):
            retry_clause = ""
    else:
        retry_clause = ""
    text = (
        f"Stripe invoice {obj.get('id') or '(unknown invoice id)'} payment failed "
        f"for {amount}{attempt_clause}.{retry_clause} "
        "Dunning triage: determine whether this is involuntary churn — an expired "
        "or declined card, insufficient funds, a stale payment method — or an "
        "intentional non-payment / cancellation signal from the customer. "
        "Recommend the right dunning path (retry schedule, card-update outreach, "
        "drafted customer email) or escalate if the account is at risk."
    )
    return text, "payment_failed"


def _generic(obj: dict[str, Any], etype: str) -> tuple[str, str]:
    text = (
        f"Stripe event '{etype}' received (no specialised playbook for this type). "
        "Review the payload, determine whether it carries billing risk or requires "
        "any action, and recommend next steps with drafted actions if warranted."
    )
    return text, "stripe_event"


_BUILDERS = {
    "charge.dispute.created": _dispute_created,
    "charge.dispute.funds_withdrawn": _funds_withdrawn,
    "charge.dispute.closed": _dispute_closed,
    "radar.early_fraud_warning.created": _fraud_warning,
    "invoice.payment_failed": _payment_failed,
}


def trigger_from_stripe_event(event: dict) -> dict:
    """Map a Stripe webhook envelope to a case-trigger dict. Pure; never raises.

    Returns exactly the cross-agent contract keys:
        text, case_type, customer_ref, structured, source_surface.
    """
    if not isinstance(event, dict):
        event = {}
    etype = str(event.get("type") or "unknown")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}

    builder = _BUILDERS.get(etype, _generic)
    try:
        text, case_type = builder(obj, etype)
    except Exception:  # noqa: BLE001 — triage must never lose an event
        text, case_type = _generic(obj, etype)

    return {
        "text": text,
        "case_type": case_type,
        "customer_ref": _customer_ref(obj),
        "structured": {"event_type": etype, "object": obj},
        "source_surface": SOURCE_SURFACE,
    }
