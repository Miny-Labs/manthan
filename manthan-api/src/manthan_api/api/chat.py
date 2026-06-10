"""Cross-case agent chat.

The "talk to the agent across all the cases" surface - answers questions like
"which customers had refunds delayed past 5 days?" or "MRR down 4%, why?"

v1 is a stateless single-shot LLM call seeded with recent cases. It now rides
the SAME grounded-answer implementation the advisor agent's ask() skill uses
(services.a2a_store.grounded_answer -> manthan_agent.llm.generate_text via AI
Studio) instead of the retired OpenRouter client - one Q&A engine for the
operator UI and the A2A surface. Per-case follow-ups go through the advisor's
`ask` skill; the old per-case chat_loop worker is deleted.

Endpoint: POST /api/chat  body: { message: str }
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from manthan_api.db import get_conn
from manthan_api.middleware.tenant import TenantCtx, get_ctx
from manthan_api.services.a2a_store import grounded_answer

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger("manthan_api.chat")

# Optional model pin (AI Studio model id); default resolves inside
# grounded_answer to the cheap triage tier (gemini-3.1-flash-lite).
MODEL = os.environ.get("MANTHAN_CHAT_MODEL") or None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    cases_seen: int


SYSTEM = """\
You are Manthan, a billing operations AI working alongside a Director of Revenue Accounting (or similar). You speak in plain English. No engineering jargon.

You're answering a question about the operator's billing cases. The CONTEXT block below has the last 20 cases this workspace has seen with their status, customer, decision, and amount. Use it to answer.

Rules:
- If the question can be answered from the context, answer it directly and cite the case short_ids inline (e.g. "QLO-198835 was a $9k chargeback against Quill Logistics - fight recommended").
- If the question cannot be answered from the context, say so honestly. Don't make up data.
- Keep replies 2-5 sentences. The operator skims.
- No markdown beyond inline `code` for ids. No bullet points unless answering a list question.
"""


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    ctx: TenantCtx = Depends(get_ctx),
) -> ChatResponse:
    # Pull recent cases for grounding.
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT short_id, status, case_type, trigger_surface, customer_ref,
                   amount_minor, decision_action, decision_amount_minor,
                   decision_confidence, created_at
            FROM cases
            WHERE org_id=$1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            ctx.org_id,
        )

    if not rows:
        return ChatResponse(
            reply=(
                "No cases yet in this workspace, so I don't have anything "
                "to reason over. Fire a demo scenario from the TopBar and "
                "ask me again."
            ),
            cases_seen=0,
        )

    context_lines = []
    for r in rows:
        amt = f"${(r['amount_minor'] or 0) / 100:,.0f}" if r["amount_minor"] else "-"
        dec = r["decision_action"] or "pending"
        dec_amt = (
            f"${(r['decision_amount_minor']) / 100:,.0f}"
            if r["decision_amount_minor"] else ""
        )
        context_lines.append(
            f"{r['short_id']} · {r['customer_ref'] or 'unknown'} · "
            f"{r['case_type'] or 'case'} · {amt} · {r['trigger_surface']} · "
            f"status={r['status']} · decision={dec} {dec_amt}".strip()
        )
    context_block = (
        "Last 20 cases for this workspace:\n" + "\n".join(context_lines)
    )

    # Same engine as the advisor's ask(): one grounded Gemini call with a
    # graceful degrade when the key is missing or the call fails.
    answer, grounded = await grounded_answer(
        body.message, context_block=context_block, system=SYSTEM, model=MODEL
    )
    if not grounded:
        logger.warning("chat LLM unavailable: %s", answer)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=answer,
        )

    return ChatResponse(reply=answer.strip(), cases_seen=len(rows))
