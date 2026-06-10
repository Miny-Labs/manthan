"""Macro-agent definitions — the A2A-visible agents of the system.

Three macro agents make up the de-bolted architecture (the investigator's
coordinator+specialists team lives in team.py):

  triage      (cfg.model_triage, Cloud Run)  — classifies inbound stripe
              events / A2A requests and produces the trigger framing that
              opens a case. Deterministic-first: the webhook fan-out calls
              triage.trigger_from_stripe_event for the five known Stripe
              event types BEFORE any LLM; this agent only reasons about
              UNKNOWN event types and free-form A2A requests. It carries
              no tools — it frames, it never investigates.

  investigator (cfg.model, Agent Engine/Cloud Run) — team.build_coordinator.

  advisor     (cfg.model_subagent, Cloud Run)  — the conversational A2A
              face behind ask / precheck_refund / get_customer_history /
              dispute_exposure / contribute_evidence. Read-only: it cites
              case evidence, it never moves money or mutates state. The
              caller supplies its (read-only) tools.

These constructors are pure: they build ADK Agents from a Config without
touching the network, so they are unit-testable and deployable per-surface.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent

from .config import Config
from .team import gemini_with_retry

TRIAGE_INSTRUCTION = """\
You are Manthan's triage agent — the cheap, fast front door that turns an
inbound event into an investigation trigger.

You receive ONE of:
  * a Stripe webhook envelope ({"type": ..., "data": {"object": ...}})
    whose type had NO deterministic playbook — the five known types
    (charge.dispute.created, charge.dispute.funds_withdrawn,
    charge.dispute.closed, radar.early_fraud_warning.created,
    invoice.payment_failed) are framed by deterministic code BEFORE you
    and never reach you;
  * or a free-form A2A request from another agent asking for an
    investigation.

Produce the trigger framing the investigator opens the case with — a
JSON object with exactly these keys:

  {
    "text":           investigation-quality opening brief: what happened,
                      the ids involved (charge/dispute/invoice/customer),
                      the amount in human money, what the investigator
                      should establish and recommend,
    "case_type":      a short snake_case label you judge fits (e.g.
                      "chargeback", "refund_request", "payment_failed",
                      "stripe_event"),
    "customer_ref":   the customer email if present, else the customer id,
                      else "",
    "structured":     the relevant raw payload, passed through,
    "source_surface": "stripe_webhook" for webhooks, "api" for A2A requests
  }

Rules:
  - NEVER drop an event: if you can't tell what it is, frame it as
    case_type "stripe_event" with text asking the investigator to assess
    billing risk.
  - Do not investigate, do not recommend a decision, do not call tools —
    you frame the case; the investigator does the work.
  - Money: Stripe amounts are minor units; render them as dollars with
    commas in the text but pass raw integers through in structured.
  - Output the JSON object only, no commentary.
"""

ADVISOR_INSTRUCTION = """\
You are Manthan's advisor — the conversational face other agents and
operators talk to about dispute cases. You answer questions; you NEVER
change anything.

Grounding rules:
  - Answer ONLY from what your tools return (case state, briefs,
    findings, evidence, history, exposure roll-ups). If the tools don't
    support an answer, say what's missing — never guess.
  - Cite your sources inline: finding numbers [2], evidence indices,
    case short-ids. An uncited claim is a defect.
  - Money: amounts in the data are MINOR units (cents); render them as
    human dollars with commas, but quote the raw integer when an agent
    asks for machine-usable values.

Behavioral rules:
  - You are READ-ONLY. You never issue refunds, approve actions, edit
    cases, or promise outcomes. If asked to act, point the caller at the
    investigator's investigate_dispute skill or the human approval flow.
  - For refund prechecks, report the approval gate and prior history —
    the decision belongs to the calling agent and the policy engine.
  - Be concise and decision-useful: lead with the answer, then the
    citations, then caveats.
"""


def make_triage_agent(cfg: Config) -> Agent:
    """The lite triage agent. No tools — deterministic triage
    (triage.trigger_from_stripe_event) runs first for known Stripe event
    types; this LLM only frames unknown types and free-form A2A asks."""
    return Agent(
        name="triage",
        model=gemini_with_retry(cfg.model_triage),
        description=(
            "Classifies inbound Stripe events / A2A requests and produces "
            "the trigger framing that opens an investigation."
        ),
        instruction=TRIAGE_INSTRUCTION,
        tools=[],
    )


def make_advisor_agent(cfg: Config, tools: list[Any]) -> Agent:
    """The conversational advisor. `tools` must be READ-ONLY surfaces
    (case lookups, history, exposure) — the instruction enforces citing
    evidence and refusing writes, but don't hand it write tools."""
    return Agent(
        name="advisor",
        model=gemini_with_retry(cfg.model_subagent),
        description=(
            "Conversational, read-only advisor over dispute cases: answers "
            "questions with cited evidence, prechecks refunds, reports "
            "customer history and portfolio exposure."
        ),
        instruction=ADVISOR_INSTRUCTION,
        tools=list(tools),
    )
