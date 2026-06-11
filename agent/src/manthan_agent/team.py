"""The investigator TEAM — a pro-model coordinator + parallel flash specialists.

This is the multi-agent core of the de-bolted architecture: the investigator
is no longer a single agent with six tools but a coordinator that fans out
in-process specialists (ADK AgentTools) which share ONE Evidence working
memory (the run's RunState). A2A stays the boundary BETWEEN macro agents
(triage -> investigator, advisor); inside the investigator everything is
in-process so evidence indices stay coherent and citation chips keep working.

Layout:
  build_specialists(...)  - the five sub-agents, wrapped as AgentTools:
      payments_analyst       stripe.* via the shared coral closures
      customer_context       CRM + support (salesforce/hubspot, intercom, zendesk)
      reliability_analyst    datadog / sentry / pagerduty / posthog
      policy_analyst         notion SOP retrieval — the RAG surface
      network_rules_analyst  google_search ONLY (ADK built-in tools cannot be
                             mixed with function tools, so it's its own agent)
  build_coordinator(...)  - the pro-model Agent: the six adk_tools closures
      + the specialist AgentTools + the pacer callbacks + the SYSTEM prompt
      extended with the "YOUR TEAM" fan-out section.

Specialists GATHER (coral_sql -> shared evidence list) and RETURN a concise
structured summary naming the evidence indices they added. They never call
record_finding / ask_human / conclude — those tools exist only on the
coordinator, which synthesizes findings WITH citations and concludes.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from .adk_pacer import build_pacer_callbacks
from .adk_tools import RunState, build_tools
from .config import Config
from .prompts import SYSTEM

# google_search is a built-in (server-side) tool; in ADK 2.2.0 it lives at
# google.adk.tools.google_search. If a future ADK moves/removes it we fall
# back to a documented stub so the team still builds and the coordinator
# learns grounding is unavailable instead of crashing.
try:  # pragma: no cover - exercised implicitly by import
    from google.adk.tools import google_search as _google_search

    GROUNDING_AVAILABLE = True
except ImportError:  # pragma: no cover - only on ADK versions without it
    _google_search = None
    GROUNDING_AVAILABLE = False


def gemini_with_retry(model_name: str) -> Gemini:
    """A Gemini model wrapped with retry/backoff for transient endpoint failures.

    The budget is sized for Vertex Dynamic Shared Quota storms on preview
    models (sustained 429s for a minute or more), not just one-off 503s:
    ~4.5 minutes of backoff per call before giving up.
    """
    return Gemini(
        model=model_name,
        retry_options=types.HttpRetryOptions(
            attempts=10,
            initial_delay=1.0,
            max_delay=60.0,
            exp_base=2.0,
            http_status_codes=[429, 500, 502, 503, 504],
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Specialist instructions
# ──────────────────────────────────────────────────────────────────────

_SPECIALIST_CONTRACT = """

================================================================
YOUR CONTRACT (same for every specialist on this team)
================================================================

Work ONLY through your tools. Every coral_sql result is appended to a
SHARED case evidence list and returns `evidence_indices` — integer
indices the coordinator will cite. Report them faithfully.

When done, RETURN a concise structured summary (plain text):

  SUMMARY:
    - <one factual bullet per thing you established, each ending with
       the evidence indices in brackets, e.g. [2] or [3,4]>
  EVIDENCE_INDICES: <all integer indices your queries added>
  GAPS: <what you could not retrieve, and why (after >=2 query shapes)>

You do NOT record findings, do NOT decide the case, do NOT draft
actions, do NOT contact humans — the coordinator does all of that.
Gather, then summarize. Keep it under ~15 lines.
"""

_PAYMENTS_INSTRUCTION = """\
You are the payments analyst on a billing-dispute investigation team.

SCOPE: the `stripe` schema ONLY (stripe.disputes, stripe.charges,
stripe.customers, stripe.subscriptions, stripe.invoices, stripe.refunds).
Ignore every other schema — teammates cover them.

Start from the ids in the request (du_*, ch_*, in_*) with ONE
within-Stripe JOIN keyed off the trigger id:

    SELECT d.id AS dispute_id, d.amount, d.reason, d.created,
           ch.id AS charge_id, ch.amount, ch.created, ch.description,
           c.id AS customer_id, c.email AS customer_email, c.name,
           s.id AS subscription_id, s.status AS subscription_status,
           s.current_period_start, s.current_period_end,
           s.canceled_at, s.cancel_at_period_end
    FROM stripe.disputes d
    LEFT JOIN stripe.charges ch ON ch.id = d.charge
    LEFT JOIN stripe.customers c ON c.id = ch.customer
    LEFT JOIN stripe.subscriptions s ON s.customer = c.id
    WHERE d.id = '<du_xxx from the request>'

Then a focused follow-up for refund history (stripe.refunds WHERE
charge = ch_xxx) and, when relevant, invoice state. ALWAYS surface the
customer's email prominently in your SUMMARY — it is the join key the
rest of the team matches contacts on. Amounts are MINOR units (cents).
""" + _SPECIALIST_CONTRACT

_CUSTOMER_CONTEXT_INSTRUCTION = """\
You are the customer-context analyst on a billing-dispute investigation
team.

SCOPE: CRM + support schemas — `salesforce` or `hubspot` (whichever the
catalog shows for this case), plus `intercom` (and `zendesk` when
present). Ignore stripe/observability/notion — teammates cover them.

Establish: who the customer is (account, owner, lifecycle, revenue),
their support history around the disputed window (cancellation or
credit requests? promises made by an agent?), and engagement recency
(intercom.contacts.last_seen_at / last_replied_at are epoch ints).

Quirks that waste turns if you forget them:
  - intercom.conversations.source_subject is often NULL even when the
    conversation exists — ALWAYS select source_subject AND source_body.
  - hubspot.companies often has duplicate rows per logical company —
    pick ONE (most-populated, latest updated_at) and say you deduped.
  - zendesk.tickets keys on requester_id — JOIN zendesk.users to
    filter by the customer email from the request.

Key contact matches off the customer EMAIL given in the request (the
customer of record, not the operator). Distinguish an informal "we're
considering cancelling" from a formal "please cancel effective <date>"
— absence of a formal request is itself worth reporting.
""" + _SPECIALIST_CONTRACT

_RELIABILITY_INSTRUCTION = """\
You are the reliability analyst on a billing-dispute investigation team.

SCOPE: observability + incident + usage schemas — `datadog`, `sentry`,
`pagerduty`, `posthog`. Ignore stripe/CRM/notion — teammates cover them.

Answer: was there a REAL platform incident or degradation during the
disputed window, and does product usage corroborate or contradict the
customer's claim?

Quirks:
  - datadog.incidents is often empty; the narrative usually lives in
    datadog.monitors.message and .tags — match by service name,
    customer name, or incident-id substring with ILIKE, then
    ORDER BY modified DESC.
  - posthog.events REQUIRES WHERE environment_id = '<id>'; get one via
    posthog.organizations / posthog.projects (project id often works).
    If unresolvable after one try, report the gap and move on —
    PostHog is rarely the deciding source.
  - pagerduty.incidents: filter by the disputed window +/- 7 days
    before declaring "no incidents".

Report severity, duration, affected services, and whether usage
dropped (supports the claim) or stayed flat (undermines it).
""" + _SPECIALIST_CONTRACT

_POLICY_INSTRUCTION = """\
You are the policy analyst on a billing-dispute investigation team —
the team's retrieval (RAG) surface over the merchant's knowledge base.

SCOPE: the docs/knowledge schemas, wherever this merchant keeps policy.
Check the catalog for whichever are connected — `notion`, `confluence`,
`google_docs`, or any wiki-like schema — and search those ONLY. Ignore
payments/CRM/observability — teammates cover them.

Your job: find THE authoritative SOP page that governs the fact
pattern in the request, and QUOTE THE FORMULA (or threshold /
required corroborations) verbatim so the coordinator can apply it.

If the docs source is notion, its quirks:
  - notion.search is a per-call table function: ONE phrase per call,
    `WHERE query = '<phrase>' LIMIT 10`. Boolean OR does NOT work.
    Run 2-3 separate searches with distinct short phrases drawn from
    the case ("pro-rata", "SLA credit", "refund policy", ...).
  - Then fetch the body: SELECT body FROM notion.pages
    WHERE page_id = '<uuid>'. notion.pages cannot be scanned.
Other docs sources follow the same shape: a search/list entry point
first, then a per-page body fetch — discover the exact tables with
coral_describe_table before querying.

Prefer the CURRENT, AUTHORITATIVE page (status='current', tags include
'authoritative') over drafts and duplicates — if two policies
conflict, report BOTH and say they conflict; never pick silently. In
your SUMMARY quote the operative sentence/formula exactly, in quotes,
with its page title.
""" + _SPECIALIST_CONTRACT

_NETWORK_RULES_INSTRUCTION = """\
You are the card-network rules analyst on a billing-dispute
investigation team. Your ONLY tool is Google Search.

Retrieve the CURRENT card-network evidence requirements relevant to
the dispute reason in the request — e.g. Visa Compelling Evidence 3.0
(CE3.0) qualification criteria for friendly-fraud / "fraudulent"
reason codes, Mastercard first-party-misuse rules, network deadlines
for representment, what evidence types the issuer must accept.

Return your answer as concise bullets — one requirement per bullet —
each with the source URL it came from. Prefer official network /
processor documentation (visa.com, mastercard.com, stripe.com docs)
over blog posts. If search cannot ground a requirement, say so
explicitly rather than guessing. You do not query company data, do
not record findings, and do not decide the case.
"""

_GROUNDING_STUB_RESULT = {
    "status": "unavailable",
    "detail": (
        "grounding unavailable locally: google_search is not importable "
        "from this ADK build, so live card-network requirements cannot "
        "be retrieved. Proceed using policy_analyst + first principles."
    ),
}


def _grounding_unavailable(query: str) -> dict:
    """Report that web grounding is unavailable in this deployment.

    Args:
        query: the web search that would have been run.
    """
    return {**_GROUNDING_STUB_RESULT, "query": query}


# ──────────────────────────────────────────────────────────────────────
# Specialists
# ──────────────────────────────────────────────────────────────────────


def build_specialists(
    coral_session: Any,
    catalog_tool: str,
    state: RunState,
    cfg: Config,
) -> list[AgentTool]:
    """The five specialists, wrapped as AgentTools for the coordinator.

    All run on cfg.model_subagent. The four data specialists get ONLY the
    three Coral closures (coral_sql / coral_list_catalog /
    coral_describe_table) from build_tools, bound to the SAME RunState as
    the coordinator — so the evidence indices they report are citable by
    the coordinator's record_finding. record_finding / ask_human /
    conclude are deliberately NOT given to specialists.

    network_rules_analyst gets ONLY the google_search built-in tool: ADK
    built-in tools cannot be mixed with function tools in one agent,
    which is exactly why it is its own agent.
    """

    def _coral_subset() -> list[Any]:
        # Fresh closures per specialist (cheap), all bound to the shared
        # `state` so evidence accumulates in one list. Select by name so a
        # reorder in build_tools can never hand a specialist conclude().
        by_name = {t.__name__: t for t in build_tools(coral_session, catalog_tool, state)}
        return [by_name["coral_sql"], by_name["coral_list_catalog"],
                by_name["coral_describe_table"]]

    def _agent(name: str, description: str, instruction: str, tools: list[Any]) -> Agent:
        return Agent(
            name=name,
            model=gemini_with_retry(cfg.model_subagent),
            description=description,
            instruction=instruction,
            tools=tools,
        )

    payments = _agent(
        "payments_analyst",
        "Pulls the dispute/charge/customer/subscription/refund picture from "
        "stripe.* and surfaces the customer email (the team's join key).",
        _PAYMENTS_INSTRUCTION,
        _coral_subset(),
    )
    customer = _agent(
        "customer_context",
        "Pulls CRM + support context (salesforce/hubspot, intercom, zendesk): "
        "who the customer is, support history, cancellation/credit requests, "
        "engagement recency.",
        _CUSTOMER_CONTEXT_INSTRUCTION,
        _coral_subset(),
    )
    reliability = _agent(
        "reliability_analyst",
        "Checks datadog/sentry/pagerduty/posthog for real incidents and usage "
        "signal during the disputed window.",
        _RELIABILITY_INSTRUCTION,
        _coral_subset(),
    )
    policy = _agent(
        "policy_analyst",
        "Retrieves THE authoritative policy/SOP for the fact pattern from "
        "wherever the merchant keeps docs (notion, confluence, ...) and "
        "quotes its formula/threshold verbatim (the team's RAG surface).",
        _POLICY_INSTRUCTION,
        _coral_subset(),
    )

    if GROUNDING_AVAILABLE:
        network_tools: list[Any] = [_google_search]
    else:  # documented no-op stub — team still builds, grounding degraded
        network_tools = [_grounding_unavailable]
    network = _agent(
        "network_rules_analyst",
        "Retrieves current card-network evidence requirements (e.g. Visa "
        "Compelling Evidence 3.0) for the dispute reason, with source URLs, "
        "via Google Search.",
        _NETWORK_RULES_INSTRUCTION,
        network_tools,
    )

    return [
        ResilientAgentTool(payments),
        ResilientAgentTool(customer),
        ResilientAgentTool(reliability),
        ResilientAgentTool(policy),
        ResilientAgentTool(network),
    ]


import asyncio as _asyncio

# Hard wall-clock cap per specialist invocation. Two failure modes need it:
# (a) AI Studio 503 storms exhaust the retry budget and raise — caught below;
# (b) a stuck connection HANGS (genai's client retries have no per-attempt
#     timeout), observed live: five dispatched specialists, zero returns,
#     whole case frozen. wait_for turns a hang into a routable error result.
SPECIALIST_TIMEOUT_S = 180.0


class ResilientAgentTool(AgentTool):
    """AgentTool whose failures (and hangs) degrade instead of aborting.

    A missing specialist is survivable — the coordinator can fill gaps with
    its own coral_sql — so a specialist failure returns an error RESULT the
    model can read and route around, never an exception.
    """

    async def run_async(self, *, args: Any, tool_context: Any) -> Any:
        try:
            return await _asyncio.wait_for(
                super().run_async(args=args, tool_context=tool_context),
                timeout=SPECIALIST_TIMEOUT_S,
            )
        except TimeoutError:
            return {
                "status": "error",
                "specialist": self.agent.name,
                "detail": f"timed out after {int(SPECIALIST_TIMEOUT_S)}s",
                "hint": (
                    "This specialist hung (likely a stuck upstream connection). "
                    "Proceed: gather this angle yourself with coral_sql, or "
                    "record an absence finding and continue."
                ),
            }
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            return {
                "status": "error",
                "specialist": self.agent.name,
                "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
                "hint": (
                    "This specialist is unavailable (likely a transient model "
                    "503). Proceed: gather this angle yourself with coral_sql, "
                    "or record an absence finding and continue."
                ),
            }


# ──────────────────────────────────────────────────────────────────────
# Coordinator
# ──────────────────────────────────────────────────────────────────────

TEAM_SECTION = """

================================================================
YOUR TEAM — fan out in parallel, then synthesize
================================================================

You coordinate five specialists, each exposed as a tool that takes one
`request` string (give it the case ids, the customer email once you
have it, the disputed window, and exactly what you want back):

  payments_analyst       stripe.* — dispute/charge/customer/subscription/
                         refunds; returns the customer email (join key).
  customer_context       CRM + support — salesforce/hubspot, intercom,
                         zendesk; account context, support history,
                         cancel/credit requests, engagement recency.
  reliability_analyst    datadog/sentry/pagerduty/posthog — real incident?
                         usage drop during the disputed window?
  policy_analyst         notion — THE authoritative SOP, formula quoted
                         verbatim (your retrieval/RAG surface).
  network_rules_analyst  Google Search — current card-network evidence
                         requirements (e.g. Visa CE3.0) for the dispute
                         reason, bullets with source URLs.

Working pattern:
  1. Skim coral_list_catalog() ONCE to see which sources this case has.
  2. FAN OUT EARLY, IN PARALLEL: in your first or second turn, emit
     MULTIPLE specialist tool calls in the SAME response — at minimum
     payments_analyst + customer_context + policy_analyst on any
     dispute; add reliability_analyst when service quality is claimed
     and network_rules_analyst on any chargeback you might fight.
     Do not serialize what can run concurrently.
  3. Specialists write into YOUR shared evidence list and report the
     evidence_indices they added — those indices are valid citations
     for record_finding. Run your own follow-up coral_sql for anything
     thin, contradictory, or suspiciously absent.
  4. Synthesize: record findings with citations (>=5 on a chargeback —
     including absence findings), then conclude() with the complete
     drafted action set per the rules above.

record_finding / ask_human / conclude are YOURS ALONE — specialists
gather and summarize; you decide. If two specialists disagree, surface
the contradiction in a finding instead of picking the convenient one.
"""


def build_coordinator(
    cfg: Config,
    coral_session: Any,
    catalog_tool: str,
    state: RunState,
    trigger_text: str,
) -> Agent:
    """The pro-model investigator: full toolkit + specialists + pacer.

    Tools = the six adk_tools closures (coral_sql, coral_list_catalog,
    coral_describe_table, record_finding, ask_human, conclude) PLUS the
    five specialist AgentTools, all sharing `state`. The pacer callbacks
    attach here (and only here): rounds are coordinator turns.
    """
    tools = build_tools(coral_session, catalog_tool, state)
    specialists = build_specialists(coral_session, catalog_tool, state, cfg)
    before_model, before_tool = build_pacer_callbacks(state, trigger_text)

    return Agent(
        name="investigator",
        model=gemini_with_retry(cfg.model),
        instruction=SYSTEM + TEAM_SECTION,
        tools=[*tools, *specialists],
        before_model_callback=before_model,
        before_tool_callback=before_tool,
    )
