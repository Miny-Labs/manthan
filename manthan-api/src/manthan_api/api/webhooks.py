"""Inbound webhooks - Stripe.

This is the TRIGGER half of the system: Stripe pushes real billing events
here, we verify the signature, dedupe by event id, and write a `case_opened`
row that the investigate worker picks up via LISTEN/NOTIFY.

Stripe events we fan out on (keys-only auth):
  - charge.dispute.created            → chargeback fight/refund decision (primary)
  - charge.dispute.funds_withdrawn    → dispute escalation, funds pulled
  - charge.dispute.closed             → dispute outcome reconciliation
  - radar.early_fraud_warning.created → fraud signal, refund vs. let-ride
  - invoice.payment_failed            → dunning case, retry vs. pause

Each event is mapped to a trigger dict by the cross-agent triage contract
(manthan_agent.triage.trigger_from_stripe_event) and then opened through the
shared case-creation flow (services.a2a_store.insert_case_from_trigger), the
same inserts POST /api/cases performs. Unknown event types are acked with 200
and logged so Stripe stops retrying.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request, status

from manthan_api.config import get_settings
from manthan_api.db import get_conn
from manthan_api.services.a2a_store import insert_case_from_trigger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("manthan_api.webhooks")


# Stripe event types we open a case for. Everything else is ignored
# (acknowledged with 200 so Stripe stops retrying).
TRIGGERING_EVENTS = {
    "charge.dispute.created",
    "charge.dispute.funds_withdrawn",
    "charge.dispute.closed",
    "radar.early_fraud_warning.created",
    "invoice.payment_failed",
}


# ──────────────────────────────────────────────────────────────────────
# POST /webhooks/stripe/{org_slug}
# ──────────────────────────────────────────────────────────────────────


@router.post("/stripe/{org_slug}", status_code=status.HTTP_200_OK)
async def stripe_webhook(org_slug: str, request: Request) -> dict[str, Any]:
    """Receive a Stripe webhook. Verifies signature, dedupes, opens a case."""
    settings = get_settings()
    secret = settings.stripe_webhook_secret
    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Signature verification. In dev with no secret set, we still parse the
    # payload but log a warning - this lets local `stripe trigger` work
    # without `stripe listen` configured.
    if secret:
        try:
            stripe.Webhook.construct_event(body, sig_header, secret)
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            logger.warning("stripe webhook signature invalid: %s", e)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid signature",
            )
    else:
        if not settings.is_dev:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="STRIPE_WEBHOOK_SECRET not configured",
            )
        logger.warning("STRIPE_WEBHOOK_SECRET unset - skipping signature check (dev only)")

    # The (now verified) raw envelope - what the triage contract consumes.
    try:
        event: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid JSON",
        )

    event_id = event.get("id")
    event_type = event.get("type")

    # Resolve org by slug.
    async with get_conn() as conn:
        org_row = await conn.fetchrow(
            "SELECT id FROM orgs WHERE slug = $1",
            org_slug,
        )
        if org_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"org not found: {org_slug}",
            )
        org_id = org_row["id"]

        # Idempotency: dedupe by Stripe event id. If we've already opened a
        # case for this event, ack 200 + skip. Stripe retries failures up to
        # 3 days - duplicate fires must not duplicate cases.
        already = await conn.fetchval(
            """
            SELECT 1 FROM cases
            WHERE org_id = $1
              AND trigger_surface = 'stripe_webhook'
              AND trigger_payload->>'event_id' = $2
            LIMIT 1
            """,
            org_id, event_id,
        )
        if already:
            return {"received": True, "deduplicated": True, "event_id": event_id}

    # Bail on uninteresting events with 200 (so Stripe stops retrying).
    if event_type not in TRIGGERING_EVENTS:
        logger.info("stripe webhook ignored event type %s (event=%s)", event_type, event_id)
        return {"received": True, "ignored": True, "event_type": event_type}

    # Map the envelope to a trigger via the cross-agent triage contract.
    # Imported lazily: the triage module ships with the agent-package build
    # and the API must import cleanly without it.
    from manthan_agent.triage import trigger_from_stripe_event

    trigger = trigger_from_stripe_event(event)

    # Make sure the dedupe key + event type survive in trigger_payload, and
    # alias the Stripe object under `event_object` - the investigate worker
    # reads trigger_payload->'event_object' to extract charge/dispute ids.
    structured = dict(trigger.get("structured") or {})
    structured.setdefault("event_id", event_id)
    structured.setdefault("event_type", event_type)
    if "event_object" not in structured and isinstance(structured.get("object"), dict):
        structured["event_object"] = structured["object"]
    trigger = {**trigger, "structured": structured}

    short_id = _short_id_from_event(event_type, (event.get("data") or {}).get("object") or {})

    case_id, short_id = await insert_case_from_trigger(
        org_id,
        trigger,
        actor="stripe:webhook",
        short_id=short_id,
        extra_event_data={
            "stripe_event_id": event_id,
            "stripe_event_type": event_type,
        },
    )

    logger.info(
        "stripe webhook opened case %s (event=%s type=%s)",
        short_id, event_id, event_type,
    )
    return {
        "received": True,
        "case_id": str(case_id),
        "short_id": short_id,
        "event_type": event_type,
    }


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _short_id_from_event(event_type: str, obj: dict[str, Any]) -> str:
    """Generate a short_id deterministic from the Stripe event for traceability.

    e.g. dispute du_1Abc234DefGhi → DSP-1ABC23.
    """
    prefix = {
        "charge.dispute.created": "DSP",
        "charge.dispute.funds_withdrawn": "DSP",
        "charge.dispute.closed": "DSP",
        "radar.early_fraud_warning.created": "EFW",
        "invoice.payment_failed": "INV",
    }.get(event_type, "STR")
    ext_id = obj.get("id") or ""
    # Strip the type prefix (du_, ch_, in_, etc.), then keep only alnum, take 6.
    suffix_raw = ext_id.split("_", 1)[1] if "_" in ext_id else ext_id
    suffix = "".join(c for c in suffix_raw if c.isalnum())[:6].upper()
    if not suffix:
        import secrets
        suffix = f"{secrets.randbelow(900000) + 100000}"
    return f"{prefix}-{suffix}"
