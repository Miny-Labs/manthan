"""Manthan Advisor on Vertex AI Agent Engine.

The advisor's brain hosted on Google's managed agent runtime. It holds no
credentials to Manthan's database: every tool below is a live A2A skill
call into the Cloud Run mesh, so this deployment consumes exactly the
surface any external agent would — the advisor card's ten read/advisory
skills, none of the write actions.

Deploy (from the repo root):

    adk deploy agent_engine \
        --project nifty-edge-494703-u6 --region us-central1 \
        --display_name "Manthan Advisor (Agent Engine)" \
        deploy/gcp/agent-engine/manthan_advisor
"""

from __future__ import annotations

import os
import uuid
from functools import cached_property
from typing import Any

import httpx
from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.genai import Client, types

ADVISOR_A2A_URL = os.environ.get(
    "MANTHAN_ADVISOR_A2A_URL", "https://manthan-advisor-dzv6bwbpba-uc.a.run.app"
)

MODEL = os.environ.get("MANTHAN_ENGINE_MODEL", "gemini-3.5-flash")


def _call_skill(skill: str, args: dict[str, Any]) -> Any:
    """One A2A message/send against the live advisor; returns the data part."""
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {"skill": skill, "args": args},
    }
    resp = httpx.post(ADVISOR_A2A_URL.rstrip("/") + "/a2a", json=payload, timeout=60.0)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"] or {}
        return {"error": f"A2A {err.get('code')}: {err.get('message')}"}
    result = body.get("result") if isinstance(body, dict) else body
    if isinstance(result, dict):
        for part in result.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "data":
                return part.get("data")
    return result


def list_cases(status: str = "", limit: int = 10) -> Any:
    """List dispute cases, optionally filtered by status (e.g. awaiting_approval)."""
    args: dict[str, Any] = {"limit": limit}
    if status:
        args["status"] = status
    return _call_skill("list_cases", args)


def get_case(case_id: str) -> Any:
    """Return one case's summary: status, customer, decision."""
    return _call_skill("get_case", {"case_id": case_id})


def get_brief(case_id: str) -> Any:
    """Return the case's brief: TL;DR, decision, drafted actions."""
    return _call_skill("get_brief", {"case_id": case_id})


def get_findings(case_id: str) -> Any:
    """Return the case's cited findings, each with evidence provenance."""
    return _call_skill("get_findings", {"case_id": case_id})


def get_actions(case_id: str) -> Any:
    """Return the drafted/approved/executed actions for a case."""
    return _call_skill("get_actions", {"case_id": case_id})


def get_audit_trail(case_id: str) -> Any:
    """Return the ordered, signed event log for a case."""
    return _call_skill("get_audit_trail", {"case_id": case_id})


def ask(question: str, case_id: str = "") -> Any:
    """Ask the live advisor a grounded question about one case (pass case_id)."""
    args: dict[str, Any] = {"question": question}
    if case_id:
        args["case_id"] = case_id
    return _call_skill("ask", args)


def precheck_refund(customer_ref: str, amount_minor: int) -> Any:
    """Approval gate (auto / one-click / two-person) for a refund amount in minor units."""
    return _call_skill(
        "precheck_refund", {"customer_ref": customer_ref, "amount_minor": amount_minor}
    )


def get_customer_history(customer_ref: str) -> Any:
    """Every case on file for a customer reference (email or customer id)."""
    return _call_skill("get_customer_history", {"customer_ref": customer_ref})


def dispute_exposure() -> Any:
    """Portfolio roll-up: open case count, disputed amount at risk, per-status breakdown."""
    return _call_skill("dispute_exposure", {})


class GlobalEndpointGemini(Gemini):
    """Preview Gemini models are served from the global endpoint only."""

    @cached_property
    def api_client(self) -> Client:
        return Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location="global",
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=4,
                    initial_delay=1.0,
                    max_delay=20.0,
                    exp_base=2.0,
                    http_status_codes=[429, 500, 502, 503, 504],
                )
            ),
        )


INSTRUCTION = """\
You are the Manthan Advisor: the conversational front door to Manthan's
billing-dispute investigations. You answer questions about cases, customers,
refund policy gates, and portfolio exposure.

Rules:
- Ground every claim in a tool result. Never invent case state. If a tool
  errors or returns nothing, say so plainly.
- Always identify cases by short_id (and case_id when useful).
- Monetary amounts from tools are in minor units (cents); state them in
  dollars when answering.
- You are read-only: you advise, you never execute. If asked to refund,
  fight, or modify a case, run precheck_refund where relevant and explain
  the human approval gate instead.
- Be concise: answer first, evidence after.
"""

root_agent = Agent(
    name="manthan_advisor",
    model=GlobalEndpointGemini(model=MODEL),
    description=(
        "Advisory agent over Manthan's billing-dispute mesh: grounded case "
        "Q&A, refund prechecks, customer history, and dispute exposure, all "
        "via live A2A skill calls into the Cloud Run agents."
    ),
    instruction=INSTRUCTION,
    tools=[
        list_cases,
        get_case,
        get_brief,
        get_findings,
        get_actions,
        get_audit_trail,
        ask,
        precheck_refund,
        get_customer_history,
        dispute_exposure,
    ],
)
