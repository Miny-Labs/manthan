"""Inbound webhooks - Stripe (back-compat surface).

The CANONICAL Stripe intake now lives on the triage agent service
(manthan_api.agents.triage, POST /webhooks/stripe) - this endpoint stays up
so existing Stripe endpoint configurations keep working. Behavior:

  * TRIAGE_A2A_URL set      -> verify + dedupe here, then FORWARD the event
                               to the triage agent over A2A (route_event);
                               triage dispatches the investigator. If the
                               forward fails we fall back to the direct path
                               so no webhook is ever dropped.
  * TRIAGE_A2A_URL unset    -> original direct path: map the event through
                               the triage contract and open the case here.
                               The investigator agent service (or its
                               in-process local-dev handler) picks cases up
                               by being CALLED - there is no NOTIFY worker
                               anymore.

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

import inspect
import json
import logging
import os
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

    # ── Forward to the triage agent when it's deployed ──────────────
    # TRIAGE_A2A_URL points at the manthan-triage Cloud Run service; the
    # event crosses an identity boundary via A2A (route_event) and triage
    # dispatches the investigator. Any failure falls through to the
    # original direct-insert path below - a webhook must never be lost
    # because our internal hop hiccuped.
    triage_url = os.environ.get("TRIAGE_A2A_URL")
    if triage_url:
        try:
            from manthan_agent.a2a.client import call_skill

            result = call_skill(triage_url, "route_event", {"event": event})
            if inspect.isawaitable(result):
                result = await result
            logger.info(
                "stripe webhook forwarded to triage (event=%s type=%s)",
                event_id, event_type,
            )
            return {
                "received": True,
                "forwarded": "triage",
                "event_type": event_type,
                "triage": result if isinstance(result, dict) else {"result": result},
            }
        except Exception as e:  # noqa: BLE001 - fall back to the direct path
            logger.warning(
                "triage forward failed (%s: %s) - using direct insert path",
                type(e).__name__, e,
            )

    # Map the envelope to a trigger via the cross-agent triage contract.
    # Imported lazily: the triage module ships with the agent-package build
    # and the API must import cleanly without it.
    from manthan_agent.triage import trigger_from_stripe_event

    trigger = trigger_from_stripe_event(event)

    # Make sure the dedupe key + event type survive in trigger_payload, and
    # alias the Stripe object under `event_object` - the action enrichment
    # (services.case_store) reads trigger_payload->'event_object' to extract
    # charge/dispute ids.
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

    # No NOTIFY worker exists anymore — a case inserted here would sit
    # uninvestigated forever. Run the investigation in-process exactly the
    # way triage's local-dev fallback does. Lazy import keeps the gateway
    # importable without the agents extras.
    try:
        from manthan_api.agents.investigator import _spawn_investigation

        _spawn_investigation(org_id, case_id, trigger)
    except Exception as e:  # noqa: BLE001 — case exists; surface, don't 500
        logger.exception(
            "in-process investigation spawn failed for case %s: %s", short_id, e
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
