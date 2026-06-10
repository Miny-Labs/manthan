"""worker.prettifier - turns raw events into one-line human summaries.

Polls events with NULL summary in (tool_call, tool_result, finding_recorded,
reflexion, brief_drafted), batches up to N, calls a fast/cheap Gemini model
(cfg.model_triage, AI Studio via manthan_agent.llm) with a tight prompt, and
writes the summary back. The SSE stream + the UI surface these short
sentences; expanding a step shows the raw event.

This is the Haiku-pretty-trace pattern from Claude Code: the orchestrator
uses the pro model, the trace rendering uses a tiny model for cost/latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

# Load .env BEFORE reading MODEL - otherwise the module-level constant
# locks in the in-code default and the MANTHAN_PRETTIFIER_MODEL override
# in manthan-api/.env never takes effect. Tried this once before, missed
# it because the main() block also calls load_dotenv but only AFTER
# module import.
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

from manthan_agent import config as agent_config  # noqa: E402
from manthan_agent import llm as agent_llm  # noqa: E402

from manthan_api.db import close_pool, get_pool, init_pool  # noqa: E402

logger = logging.getLogger("worker.prettifier")

# Optional model override; empty/unset falls back to cfg.model_triage
# (gemini-3.1-flash-lite on AI Studio).
MODEL = os.environ.get("MANTHAN_PRETTIFIER_MODEL") or None
BATCH_SIZE = 8
POLL_INTERVAL = 2.0


SYSTEM_PROMPT = """You translate one step of a behind-the-scenes billing-dispute investigation into ONE plain-English sentence for a FINANCE PERSON (a Director of Revenue Accounting). They do NOT know what schemas, tables, queries, or APIs are. They DO know about customers, charges, refunds, support tickets, sales contacts, billing records, etc.

For each raw event you'll be given a TYPE and DATA. Write ONE sentence (max 14 words) that names what the agent is trying to learn about the CUSTOMER OR CASE - in the language a CFO would use over coffee. No quotes. No emoji. No trailing period unless natural.

FORBIDDEN words (rewrite around them):
  schema, table, column, query, SQL, API, endpoint, payload, JSON,
  fetch, describe, list, catalog, projection, filter, join, row(s)

REWRITE into:
  "describing the stripe subscriptions table"  → "Checking what plan the customer is on"
  "listing intercom conversations columns"     → "Looking through the customer's support chats"
  "describing the zendesk tickets schema"      → "Pulling open support tickets for this account"
  "listing tables across multiple schemas"     → "Surveying which systems hold customer history"
  "describing stripe.active_entitlements"      → "Checking what the customer currently has access to"
  "reading hubspot.companies"                  → "Looking up the company record in our CRM"
  "running SELECT FROM stripe.disputes"        → "Pulling the dispute details from Stripe"

Good full-sentence examples:
  "Looking up Northwind in Stripe by their finance email."
  "Checking whether the customer cancelled in writing anywhere."
  "Counting prior chargebacks on this customer in the last year."
  "Pulling their support history to confirm what was promised."
  "Drafted the recommendation - fight this dispute, submit evidence."
  "Found 6 conversations in support, none mentioning cancellation."
  "Couldn't find a match - empty result from our records."
  "Confirming the customer is still using the product after disputing."

If the raw event is a finding the agent recorded, translate the finding directly into one line a CFO would understand.

If the raw event is a brief or decision, name the decision and one key reason.

Reply with ONLY the sentence, nothing else."""


PRETTIFIABLE_TYPES = {
    "tool_call",
    "tool_result",
    "finding_recorded",
    "reflexion",
    "brief_drafted",
    "agent_thought",
    "case_closed",
    "error",
}


class PrettifierWorker:
    def __init__(self, poll_interval: float = POLL_INTERVAL) -> None:
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._cfg: agent_config.Config | None = None
        self._model: str = ""

    async def run(self) -> None:
        cfg = agent_config.load()
        if not cfg.google_api_key:
            logger.error("GOOGLE_API_KEY missing - prettifier idle")
            return
        self._cfg = cfg
        self._model = MODEL or cfg.model_triage
        logger.info(
            "worker.prettifier starting (model=%s, batch=%d)", self._model, BATCH_SIZE
        )

        while not self._stop.is_set():
            try:
                claimed = await self._drain_once()
            except Exception as e:  # noqa: BLE001
                logger.exception("prettifier loop error: %s", e)
                claimed = 0
            if claimed == 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass

        logger.info("worker.prettifier stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _drain_once(self) -> int:
        # Pull a batch of pending rows. We don't FOR UPDATE because a single
        # prettifier instance is fine; multiple instances would need locking.
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, org_id, thread_id, seq, type, data
                FROM events
                WHERE summary IS NULL
                  AND type = ANY($1::text[])
                ORDER BY id ASC
                LIMIT $2
                """,
                list(PRETTIFIABLE_TYPES),
                BATCH_SIZE,
            )
        if not rows:
            return 0

        # Run prettifier calls in parallel (cheap, IO-bound).
        results = await asyncio.gather(
            *[self._prettify_one(row) for row in rows],
            return_exceptions=True,
        )

        # Write back. Skip ones that errored.
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                for row, summary in zip(rows, results, strict=True):
                    if isinstance(summary, BaseException):
                        logger.warning("event %s prettify failed: %s", row["id"], summary)
                        # Write a placeholder so we don't retry forever; can be
                        # blanked out manually if a fix lands.
                        summary = _fallback_summary(row["type"], row["data"])
                    await conn.execute(
                        "UPDATE events SET summary = $1 WHERE id = $2",
                        summary,
                        row["id"],
                    )
        return len(rows)

    async def _prettify_one(self, row: Any) -> str:
        data = row["data"] if isinstance(row["data"], dict) else {}
        user_msg = (
            f"TYPE: {row['type']}\n"
            f"DATA (json):\n{_clip(json.dumps(data, default=str), 1800)}"
        )
        assert self._cfg is not None
        text = await agent_llm.agenerate_text(
            self._cfg,
            user=user_msg,
            system=SYSTEM_PROMPT,
            model=self._model,
            temperature=0.2,
            max_output_tokens=64,
        )
        if not text:
            text = _fallback_summary(row["type"], data)
        return _normalize(text)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _normalize(s: str) -> str:
    s = s.strip().strip('"').strip("'").strip()
    # Collapse newlines + drop leading "Summary:" / "Step N:" boilerplate.
    s = " ".join(s.split())
    for prefix in ("Summary:", "Step:", "Description:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    # Cap to 200 chars for safety.
    return s[:200]


def _fallback_summary(type_: str, data: Any) -> str:
    """If the LLM call fails, write something sane - but better than
    "Calling coral_sql". Extract the SQL's source tables when we can
    so multi-source joins read as joins, not as a generic tool name."""
    if not isinstance(data, dict):
        data = {}
    if type_ == "tool_call":
        name = str(data.get("name") or "tool")
        if name in ("coral_sql", "coral_describe_table"):
            return _describe_coral_call(name, data)
        if name == "record_finding":
            args = data.get("arguments") or {}
            text = str((args.get("text") if isinstance(args, dict) else "") or "")
            return f"Logging a finding: {text[:120]}" if text else "Logging a finding"
        if name == "conclude":
            return "Concluding with a recommended decision"
        if name == "ask_human":
            return "Pausing to ask the operator"
        return f"Calling {name}"
    if type_ == "tool_result":
        return f"Result from {data.get('name', 'tool')}"
    if type_ == "finding_recorded":
        text = str(data.get("text", ""))
        return text[:140] if text else "Recorded a finding"
    if type_ == "reflexion":
        return "Reflexion checkpoint"
    if type_ == "brief_drafted":
        dec = data.get("decision") or {}
        return f"Drafted brief - decision: {dec.get('action', 'unknown')}"
    if type_ == "case_closed":
        return f"Case closed ({data.get('reason', 'concluded')})"
    if type_ == "error":
        return f"Error: {data.get('reason', 'unknown')}"
    return type_


# Mirror of the frontend KNOWN_SOURCES set - kept in sync by hand
# because there's no cross-language registry. Sources we recognize in
# a `<source>.<table>` token; anything else is treated as a catalog
# table (coral.tables, etc.).
_KNOWN_SOURCES = {
    "stripe", "hubspot", "intercom", "zendesk", "salesforce",
    "notion", "slack", "datadog", "pagerduty", "sentry", "posthog",
    "gmail", "resend", "github", "linear", "mixpanel",
}


def _describe_coral_call(tool: str, data: dict[str, Any]) -> str:
    """Source-aware fallback summary for coral_* calls."""
    args = data.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    if tool == "coral_describe_table":
        qn = str(args.get("qualified_name") or "")
        if "." in qn:
            src, tbl = qn.split(".", 1)
            return f"Describing the {src} {tbl.replace('_', ' ')} table"
        return "Describing a source table"
    # coral_sql
    query = str(args.get("query") or "")
    if not query:
        return "Running a coral query"
    sources = _extract_sources(query)
    if len(sources) >= 2:
        primary, *rest = sources
        return f"Cross-checking {primary} with {_join_natural(rest)}"
    if len(sources) == 1:
        return f"Reading {sources[0]} records"
    return "Surveying which systems hold customer history"


def _extract_sources(sql: str) -> list[str]:
    """All distinct known sources in `<source>.<table>` form, in
    textual order. Mirrors the frontend extractSources logic."""
    import re

    # Strip quoted string literals before the regex sees them so
    # 'thatspacebiker@gmail.com' doesn't surface "gmail" as a source.
    q = _strip_sql_literals(sql.lower())
    seen: set[str] = set()
    out: list[str] = []
    # Prefer top-level FROM as the primary.
    primary: str | None = None
    for m in re.finditer(r"\bfrom\s+([a-z_][a-z0-9_]*)\s*\.\s*[a-z_][a-z0-9_]*\b", q):
        src = m.group(1)
        if src not in _KNOWN_SOURCES:
            continue
        before = q[: m.start()]
        if before.count("(") == before.count(")"):
            primary = src
    if primary:
        out.append(primary)
        seen.add(primary)
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s*\.\s*[a-z_][a-z0-9_]*\b", q):
        src = m.group(1)
        if src not in _KNOWN_SOURCES or src in seen:
            continue
        seen.add(src)
        out.append(src)
    return out


def _strip_sql_literals(sql: str) -> str:
    """Replace contents of single- and double-quoted strings with spaces
    so the source-extraction regex doesn't false-match domains inside
    email/URL literals. Preserves length so any positional logic
    downstream still works approximately."""
    import re

    sql = re.sub(r"'(?:[^'\\]|\\.)*'", lambda m: " " * len(m.group(0)), sql)
    sql = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group(0)), sql)
    return sql


def _join_natural(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) <= 5:
        return f"{', '.join(parts[:-1])} and {parts[-1]}"
    return f"{', '.join(parts[:4])} and {len(parts) - 4} more"


async def main() -> None:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await init_pool()
    worker = PrettifierWorker()
    try:
        await worker.run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
