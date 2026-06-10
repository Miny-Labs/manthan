"""Email dispatcher - single send path for Manthan-branded customer email.

Used by:
  - adapters/resend.py          → render_branded_html for agent-drafted
                                   customer_email actions (the actor's
                                   outbound rail)
  - api/clerk_webhook.py        → send_welcome_email on user.created

Why a dispatcher and not just calling Resend directly:
  - One place owns the From/Reply-To choices (the
    "manthan@demo.manthan.quest" outbound display address vs. the
    plain reply mailbox).
  - One place renders the branded templates so every outbound surface
    shares the same look.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from manthan_api.db import get_pool
from manthan_api.services.email_templates import (
    render_ack_email,
    render_action_email,
    render_plain_text_fallback,
    render_resolution_email,
    render_welcome_email,
)

logger = logging.getLogger("services.email_dispatcher")


# ──────────────────────────────────────────────────────────────────────
# From / Reply-To choices.
#
# Outbound display = "Manthan <manthan@demo.manthan.quest>" so the brand
# domain is what the customer sees in their inbox. Reply-To points at
# the plain monitored mailbox.
# ──────────────────────────────────────────────────────────────────────

DEFAULT_FROM_DISPLAY = os.environ.get(
    "MANTHAN_EMAIL_FROM",
    "Manthan <manthan@demo.manthan.quest>",
)
DEFAULT_REPLY_TO = os.environ.get(
    "MANTHAN_EMAIL_REPLY_TO",
    os.environ.get("RESEND_FROM_ADDRESS", "manthan@miny-labs.com"),
)


# ──────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────


async def send_welcome_email(
    *,
    clerk_user_id: str,
    email: str,
    first_name: str | None,
    last_name: str | None,
    demo_url: str | None = None,
) -> bool:
    """Send the MVP welcome email and persist the dedup row.

    Idempotent: if `auth_signups.welcome_sent_at` is already set for this
    Clerk user, we no-op. Otherwise we render, send, and mark sent.
    Returns True if an email was actually sent this call.
    """
    # 1. Dedup check - INSERT ... ON CONFLICT means we claim the slot
    #    atomically. If we lose the race we return False without sending.
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO auth_signups
                (clerk_user_id, email, first_name, last_name, source)
            VALUES ($1, $2, $3, $4, 'clerk')
            ON CONFLICT (clerk_user_id) DO UPDATE
              SET clerk_user_id = EXCLUDED.clerk_user_id
            RETURNING welcome_sent_at
            """,
            clerk_user_id, email, first_name, last_name,
        )
    if row and row["welcome_sent_at"] is not None:
        logger.info("welcome email already sent (clerk_user_id=%s)", clerk_user_id)
        return False

    # 2. Render + send.
    demo = demo_url or os.environ.get(
        "WEB_APP_ORIGIN", "https://demo.manthan.quest"
    ).rstrip("/") + "/app"
    subj, html_body = render_welcome_email(
        first_name=first_name,
        email=email,
        demo_url=demo,
    )
    text_body = render_plain_text_fallback(html_body)
    external_ref = await _send_via_resend(
        to=email,
        subject=subj,
        html_body=html_body,
        text_body=text_body,
        tag="welcome_mvp",
    )

    # 3. Mark sent (only when Resend gave us a message id back).
    if external_ref:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_signups
                SET welcome_sent_at = now(),
                    welcome_email_id = $2
                WHERE clerk_user_id = $1
                """,
                clerk_user_id, external_ref,
            )
        logger.info(
            "welcome email sent (clerk_user_id=%s email=%s msg=%s)",
            clerk_user_id, email, external_ref,
        )
        return True
    return False


async def render_branded_html(
    *,
    template: str,
    case_short_id: str,
    customer_email: str,
    customer_name: str | None,
    payload: dict[str, Any],
) -> tuple[str, str, str]:
    """Render one of the three branded templates from a payload-shape
    dict. Used by the Resend adapter when the agent's drafted action
    includes `template=resolution|action_item|ack`.

    Returns (subject, html_body, text_body). The caller (the adapter)
    threads this through Resend with the From/Reply-To shared in this
    file.
    """
    if template == "resolution":
        subj, html_body = render_resolution_email(
            customer_name=customer_name,
            customer_email=customer_email,
            headline=str(payload.get("headline") or ""),
            body_paragraphs=_split_body(payload),
            case_short_id=case_short_id,
            stripe_dispute_url=payload.get("stripe_dispute_url"),
            signed_by=payload.get("signed_by"),
            subject_override=payload.get("subject"),
        )
    elif template == "action_item" or template == "action":
        subj, html_body = render_action_email(
            customer_name=customer_name,
            customer_email=customer_email,
            purpose=str(payload.get("purpose") or "Update"),
            headline=str(payload.get("headline") or ""),
            body_paragraphs=_split_body(payload),
            case_short_id=case_short_id,
            call_to_action=payload.get("call_to_action"),
            stripe_dispute_url=payload.get("stripe_dispute_url"),
            signed_by=payload.get("signed_by"),
            subject_override=payload.get("subject"),
        )
    elif template == "ack":
        subj, html_body = render_ack_email(
            customer_name=customer_name,
            customer_email=customer_email,
            subject_received=str(payload.get("subject_received") or ""),
            case_short_id=case_short_id,
            stripe_dispute_id=payload.get("stripe_dispute_id"),
        )
    else:
        raise ValueError(f"unknown email template: {template!r}")

    text_body = render_plain_text_fallback(html_body)
    return subj, html_body, text_body


# ──────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────


def _split_body(payload: dict[str, Any]) -> list[str]:
    """Accept either body_paragraphs (preferred) or body_text (legacy).
    Splits body_text on blank lines into paragraphs."""
    paras = payload.get("body_paragraphs")
    if isinstance(paras, list) and paras:
        return [str(p) for p in paras if str(p).strip()]
    text = str(payload.get("body_text") or payload.get("body") or "")
    if not text.strip():
        return []
    return [p.strip() for p in text.split("\n\n") if p.strip()]


async def _send_via_resend(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    tag: str,
) -> str | None:
    """Direct Resend call. Returns the email id on success, None on
    failure (logged, not raised - callers proceed with case open even
    if the ack fails to send)."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY missing - email send skipped (%s)", tag)
        return None
    try:
        import resend  # local import keeps webhook startup light
        resend.api_key = api_key
        params: dict[str, Any] = {
            "from": DEFAULT_FROM_DISPLAY,
            "to": [to],
            "subject": subject,
            "html": html_body,
            "text": text_body,
            "reply_to": DEFAULT_REPLY_TO,
            "tags": [{"name": "manthan_kind", "value": tag}],
        }
        r = resend.Emails.send(params)
        eid = r.get("id") if isinstance(r, dict) else getattr(r, "id", None)
        if eid:
            logger.info("resend send ok (kind=%s id=%s)", tag, eid)
        return str(eid) if eid else None
    except Exception as e:  # noqa: BLE001
        logger.warning("resend send failed (kind=%s): %s", tag, e)
        return None

