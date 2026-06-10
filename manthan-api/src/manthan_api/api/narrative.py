"""Live investigation narrative.

GET /api/cases/{case_id}/narrative

Pulls the last N events from the case's thread, hands the whole window
to a fast model in ONE call, and returns:

  - `narrative`: a 2-paragraph plain-English story of what the agent
    has done and is currently doing, written for a Director of Revenue
    Accounting.
  - `findings`: 3-5 interim facts the agent has surfaced so far,
    derived from tool_results. These are NOT formally committed
    `record_finding` events - they're a live read of what's emerging,
    shown next to the placeholder so the operator sees signal early.

Why one big LLM call instead of per-event prettifier rows: the prose
investigation view needs a *story*, not a list. A single
window-over-everything call writes a paragraph that connects 18 steps
into "Manthan started by surveying the catalog, then pulled the
dispute, then joined HubSpot and Intercom looking for matching
records." The per-event prettifier still runs alongside for the Coral
trace surface.

Model: gemini-3.1-flash-lite via AI Studio (GOOGLE_API_KEY) by default;
override with MANTHAN_NARRATIVE_MODEL. Same `manthan_agent.llm` helper
the prettifier uses - this endpoint sends 25-event windows and needs a
cheap model that starts writing immediately.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from manthan_api.db import get_conn
from manthan_api.middleware.tenant import TenantCtx, get_ctx

logger = logging.getLogger("manthan_api.narrative")

router = APIRouter(prefix="/api/cases", tags=["narrative"])

# Cheap, fast tier (matches cfg.model_triage / the prettifier's pick).
MODEL = os.environ.get("MANTHAN_NARRATIVE_MODEL", "gemini-3.1-flash-lite")
WINDOW = 25  # events back from the latest

# In-memory cache so repeated polls within 5 seconds for the same
# case don't re-call the LLM. Keyed by (case_id, max_seq).
_cache: dict[tuple[str, int], dict[str, Any]] = {}


SYSTEM_PROMPT = """You write LIVE narratives of a billing-ops AI agent's investigation, for a Director of Revenue Accounting.

You are given the last N steps of the agent's activity - `tool_call`, `tool_result`, `reflexion`, `case_opened`, `investigation_started`. Write TWO outputs and return them as STRICT JSON:

{
  "narrative": "<2 short paragraphs, max ~60 words total, plain English, no engineering jargon - no schemas/tables/queries/SQL/SELECT/JOIN/joins/columns. Describe what the agent did first, what it's doing now. Past then present.>",
  "findings": [
    "<one concrete fact the agent has surfaced from a tool_result - customer name, charge amount, dispute reason, related record, etc.>",
    "<another fact>",
    ...
  ]
}

Findings rules:
- 3 to 5 entries
- Each ≤ 18 words
- Specific (use real ids and numbers when present)
- Don't repeat
- Don't include the case opening event ("Stripe chargeback opened…") - that's already shown elsewhere
- Don't speculate - only state what tool_results clearly show

Narrative rules:
- Two paragraphs, separated by \\n\\n
- First paragraph: what's been done so far (past tense)
- Second paragraph: what's happening right now (present tense)
- Plain English. The reader does not know what a schema or a table is.
- Never quote the agent's tool names. Translate.

Reply with the JSON object only. No markdown fences."""


class NarrativeResponse(BaseModel):
    narrative: str
    findings: list[str]
    events_processed: int
    max_seq: int
    cached: bool = False


@router.get("/{case_id}/narrative", response_model=NarrativeResponse)
async def get_narrative(
    case_id: UUID,
    ctx: TenantCtx = Depends(get_ctx),
) -> NarrativeResponse:
    # Lazy import keeps module import light; module-attribute access keeps
    # monkeypatching of manthan_agent.llm effective in tests.
    from manthan_agent import config as agent_config
    from manthan_agent import llm as agent_llm

    cfg = agent_config.load()
    if not cfg.google_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_API_KEY not configured",
        )

    # Pull events from the thread.
    async with get_conn() as conn:
        thread_row = await conn.fetchrow(
            "SELECT thread_id FROM cases WHERE org_id=$1 AND id=$2",
            ctx.org_id, case_id,
        )
        if thread_row is None:
            raise HTTPException(status_code=404, detail="case not found")
        events = await conn.fetch(
            """
            SELECT seq, type, data
            FROM events
            WHERE org_id=$1 AND thread_id=$2
            ORDER BY seq DESC
            LIMIT $3
            """,
            ctx.org_id, thread_row["thread_id"], WINDOW,
        )

    if not events:
        return NarrativeResponse(
            narrative="Listening for the first event…",
            findings=[],
            events_processed=0,
            max_seq=0,
        )

    max_seq = events[0]["seq"]
    cache_key = (str(case_id), max_seq)

    # Cache hit - same window, return what we already generated.
    if cache_key in _cache:
        cached = _cache[cache_key]
        return NarrativeResponse(
            narrative=cached["narrative"],
            findings=cached["findings"],
            events_processed=cached["events_processed"],
            max_seq=max_seq,
            cached=True,
        )

    # Build the compact event log in chronological order.
    lines: list[str] = []
    for e in reversed(events):
        d = e["data"] if isinstance(e["data"], dict) else {}
        seq = e["seq"]
        t = e["type"]
        if t == "tool_call":
            name = d.get("name") or "tool"
            args = d.get("arguments") or {}
            query = (args.get("query") or "").strip()
            qname = args.get("qualified_name") or ""
            extra = ""
            if query:
                extra = " " + query[:240].replace("\n", " ")
            elif qname:
                extra = f" {qname}"
            lines.append(f"#{seq} CALL {name}{extra}")
        elif t == "tool_result":
            res = d.get("result") or d.get("rows") or d
            res_str = json.dumps(res, default=str)[:280].replace("\n", " ")
            lines.append(f"#{seq} RESULT {res_str}")
        elif t == "case_opened":
            txt = d.get("text") or json.dumps(d, default=str)[:200]
            lines.append(f"#{seq} CASE_OPENED {txt[:200]}")
        elif t == "investigation_started":
            lines.append(f"#{seq} INVESTIGATION_STARTED {json.dumps(d, default=str)[:120]}")
        elif t == "reflexion":
            txt = d.get("text") or json.dumps(d, default=str)[:160]
            lines.append(f"#{seq} REFLEXION {txt[:200]}")
        elif t == "agent_thought":
            txt = d.get("text") or json.dumps(d, default=str)[:160]
            lines.append(f"#{seq} THOUGHT {txt[:200]}")

    if not lines:
        return NarrativeResponse(
            narrative="The agent is starting up.",
            findings=[],
            events_processed=0,
            max_seq=max_seq,
        )

    user_msg = "\n".join(lines)

    try:
        content = await agent_llm.agenerate_text(
            cfg,
            user=user_msg,
            system=SYSTEM_PROMPT,
            model=MODEL,
            temperature=0.3,
            max_output_tokens=400,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("narrative LLM call failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"LLM call failed: {type(e).__name__}",
        )

    # Models sometimes wrap JSON in markdown fences; strip them if so.
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()

    narrative = ""
    findings: list[str] = []
    try:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            parsed = json.loads(content[start:end + 1])
            narrative = str(parsed.get("narrative") or "").strip()
            raw_findings = parsed.get("findings") or []
            if isinstance(raw_findings, list):
                findings = [str(f).strip() for f in raw_findings if f][:5]
    except json.JSONDecodeError:
        # Fallback: treat the whole content as the narrative.
        narrative = content

    if not narrative:
        narrative = "The agent is mid-step - checking sources right now."

    result = {
        "narrative": narrative,
        "findings": findings,
        "events_processed": len(lines),
    }
    # Cache (small bounded LRU - keep only most recent 32 entries).
    if len(_cache) > 32:
        # Drop the oldest 8 entries.
        for k in list(_cache.keys())[:8]:
            _cache.pop(k, None)
    _cache[cache_key] = result

    return NarrativeResponse(
        narrative=narrative,
        findings=findings,
        events_processed=len(lines),
        max_seq=max_seq,
        cached=False,
    )
