"""manthan-triage - event intake agent as its own A2A service.

Run it:  uvicorn manthan_api.agents.triage:app --host 0.0.0.0 --port 8080

Surface:
  GET  /.well-known/agent-card.json   triage-flavored AgentCard
                                      (identity agent_id 'manthan-triage')
  POST /webhooks/stripe               Stripe's webhook endpoint: verify the
       (also /webhooks/stripe/{org})  signature (when STRIPE_WEBHOOK_SECRET is
                                      set), map the 5 triggering event types
                                      through manthan_agent.triage.
                                      trigger_from_stripe_event, then hand the
                                      trigger to the investigator.
  POST /a2a                           JSON-RPC: the generic `route_event`
                                      skill - same fan-out for events
                                      delivered agent-to-agent instead of by
                                      Stripe.

Investigator delivery - two modes, deliberately:

  * INVESTIGATOR_A2A_URL set (cloud): the trigger crosses a real identity
    boundary via manthan_agent.a2a.client.call_skill(url,
    'investigate_dispute', trigger) - triage and investigator run as separate
    Cloud Run services under separate service accounts.

  * INVESTIGATOR_A2A_URL unset (LOCAL DEV MODE): there is no second service
    to call, so triage imports manthan_api.agents.investigator and awaits its
    in-process handle_investigate() directly. Same handler, same dedupe, same
    background run - just no HTTP hop. This keeps `uvicorn
    manthan_api.agents.triage:app` fully functional on a laptop with only
    Postgres running. The DB pool is initialised lazily by
    handle_investigate(), so triage itself never needs a DB.

Dedupe note: triage is stateless (no DB). Stripe-event idempotency lives in
the investigator, which refuses a second case for the same
structured.event_id.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from typing import Any

import stripe
from fastapi import FastAPI, HTTPException, Request, status

from manthan_agent.a2a.card import build_agent_card
from manthan_agent.triage import trigger_from_stripe_event

from manthan_api.config import get_settings
# Canonical list of Stripe event types that open a case - shared with the
# back-compat handler on the main API so the two never drift.
from manthan_api.api.webhooks import TRIGGERING_EVENTS, _short_id_from_event
from manthan_api.agents.common import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    agent_identity,
    data_message,
    extract_skill,
    parse_jsonrpc,
    public_base_url,
    rpc_err,
    rpc_ok,
)

logger = logging.getLogger("agents.triage")

AGENT_ID = "manthan-triage"

TRIAGE_SKILLS: list[dict[str, Any]] = [
    {
        "id": "route_event",
        "name": "Route a billing event",
        "description": (
            "Triage a raw billing event (a Stripe webhook envelope) into an "
            "investigation trigger and dispatch it to the Manthan "
            "Investigator over A2A. Five event types open a case: "
            "charge.dispute.created, charge.dispute.funds_withdrawn, "
            "charge.dispute.closed, radar.early_fraud_warning.created, "
            "invoice.payment_failed."
        ),
        "tags": ["triage", "routing", "stripe", "intake"],
        "examples": [
            'route_event {"event": {"type": "charge.dispute.created", "data": {...}}}',
        ],
    },
]


# ──────────────────────────────────────────────────────────────────────
# Forwarding (A2A in cloud, in-process in local dev)
# ──────────────────────────────────────────────────────────────────────


async def _forward_to_investigator(
    trigger: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    """Hand a trigger to the investigator - over A2A when
    INVESTIGATOR_A2A_URL is set, in-process otherwise (local dev mode)."""
    url = os.environ.get("INVESTIGATOR_A2A_URL")
    if url:
        # Cross-service path: real A2A hop between two agent identities.
        from manthan_agent.a2a.client import call_skill

        result = call_skill(
            url,
            "investigate_dispute",
            trigger,
            api_key=os.environ.get("INVESTIGATOR_A2A_API_KEY"),
        )
        if inspect.isawaitable(result):
            result = await result
        out: dict[str, Any] = {"delivery": "a2a"}
        if isinstance(result, dict):
            out.update(result)
        else:
            out["investigator"] = result
        return out

    # LOCAL DEV MODE: no second service - call the investigator's handler
    # in-process. Imported lazily so triage starts without pulling ADK in
    # until the first event actually arrives.
    from manthan_api.agents import investigator

    result = await investigator.handle_investigate(trigger, actor=actor)
    return {"delivery": "in_process", **result}


def _enriched_trigger(event: dict[str, Any]) -> dict[str, Any]:
    """trigger_from_stripe_event + the same structured enrichment the main
    API's webhook applies (event_id for dedupe, event_object alias)."""
    trigger = trigger_from_stripe_event(event)
    structured = dict(trigger.get("structured") or {})
    structured.setdefault("event_id", event.get("id"))
    structured.setdefault("event_type", event.get("type"))
    if "event_object" not in structured and isinstance(structured.get("object"), dict):
        structured["event_object"] = structured["object"]
    return {**trigger, "structured": structured}


# ──────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────

# No lifespan/DB pool: triage is stateless. (The local-dev fallback path
# initialises the pool itself inside investigator.handle_investigate.)
app = FastAPI(title="Manthan Triage (A2A)")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "agent": AGENT_ID}


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    identity = agent_identity(
        AGENT_ID, os.environ.get("MANTHAN_MODEL_TRIAGE") or "gemini-3.1-flash-lite"
    )
    card = build_agent_card(public_base_url(request), identity=identity)
    card["name"] = "Manthan Triage"
    card["description"] = (
        "Event-intake agent for Manthan. Receives raw billing events (Stripe "
        "webhooks or A2A route_event messages), maps them to investigation "
        "triggers, and dispatches the Manthan Investigator."
    )
    card["skills"] = TRIAGE_SKILLS
    return card


@app.post("/webhooks/stripe", status_code=status.HTTP_200_OK)
@app.post("/webhooks/stripe/{org_slug}", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request, org_slug: str | None = None) -> dict[str, Any]:
    """Stripe webhook intake - the 5-type fan-out, now owned by triage."""
    settings = get_settings()
    secret = settings.stripe_webhook_secret
    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Signature verification: same policy as the main API's handler - in dev
    # with no secret set we parse anyway (lets `stripe trigger` work locally).
    if secret:
        try:
            stripe.Webhook.construct_event(body, sig_header, secret)
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            logger.warning("stripe webhook signature invalid: %s", e)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature"
            )
    else:
        if not settings.is_dev:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="STRIPE_WEBHOOK_SECRET not configured",
            )
        logger.warning(
            "STRIPE_WEBHOOK_SECRET unset - skipping signature check (dev only)"
        )

    try:
        event: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON"
        )

    event_id = event.get("id")
    event_type = event.get("type")

    # Ack-and-ignore everything we don't open cases for, so Stripe stops
    # retrying.
    if event_type not in TRIGGERING_EVENTS:
        logger.info("ignored event type %s (event=%s)", event_type, event_id)
        return {"received": True, "ignored": True, "event_type": event_type}

    trigger = _enriched_trigger(event)
    obj = (event.get("data") or {}).get("object") or {}
    trigger["short_id_hint"] = _short_id_from_event(str(event_type), obj)
    # Per-org webhook path: carry the slug so the investigator opens the case
    # in the right tenant instead of the default A2A org.
    if org_slug:
        trigger["org_slug"] = org_slug

    try:
        forwarded = await _forward_to_investigator(trigger, actor="stripe:webhook")
    except Exception as e:  # noqa: BLE001 - never 500 a webhook on our hop
        logger.exception("forward to investigator failed: %s", e)
        return {
            "received": True,
            "forwarded": False,
            "error": f"{type(e).__name__}: {e}",
            "event_type": event_type,
        }

    logger.info(
        "routed stripe event %s (%s) -> investigator (%s)",
        event_id, event_type, forwarded.get("delivery"),
    )
    return {"received": True, "forwarded": True, "event_type": event_type, **forwarded}


@app.post("/a2a")
async def a2a_rpc(request: Request) -> dict[str, Any]:
    """JSON-RPC: route_event - same fan-out for A2A-delivered events."""
    payload = await parse_jsonrpc(request)
    if payload is None:
        return rpc_err(None, JSONRPC_PARSE_ERROR, "invalid JSON")
    req_id = payload.get("id")
    if payload.get("method") != "message/send":
        return rpc_err(
            req_id, JSONRPC_METHOD_NOT_FOUND,
            f"unknown method '{payload.get('method')}' (triage serves message/send route_event)",
        )

    skill, args = extract_skill(payload.get("params", {}) or {})
    if skill != "route_event":
        return rpc_err(req_id, JSONRPC_METHOD_NOT_FOUND, f"unknown skill '{skill}'")

    # Accept {"event": {...}} or the bare envelope as args.
    event = args.get("event") if isinstance(args.get("event"), dict) else args
    if not isinstance(event, dict) or not event.get("type"):
        return rpc_err(
            req_id, JSONRPC_INVALID_PARAMS, "route_event needs an event envelope with a 'type'"
        )

    event_type = event.get("type")
    if event_type not in TRIGGERING_EVENTS:
        return rpc_ok(
            req_id,
            data_message({"received": True, "ignored": True, "event_type": event_type}),
        )

    trigger = _enriched_trigger(event)
    try:
        forwarded = await _forward_to_investigator(trigger, actor="a2a:triage")
    except Exception as e:  # noqa: BLE001
        return rpc_err(req_id, JSONRPC_INTERNAL_ERROR, f"{type(e).__name__}: {e}")
    return rpc_ok(
        req_id,
        data_message(
            {"received": True, "forwarded": True, "event_type": event_type, **forwarded}
        ),
    )
