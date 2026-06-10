"""The Manthan AgentCard — A2A identity + skill catalog.

Served at GET /.well-known/agent-card.json. Carries the agent's identity (the
Track 3 "Agent Identity" surface: provider, version, service account, model,
signing-key fingerprint) and advertises both the investigate ACTION and the
state-read QUERY skills so other agents can pick up any case artifact.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = "0.3.0"
AGENT_VERSION = "1.0.0"

# Action skills (do work) --------------------------------------------------
_ACTION_SKILLS = [
    {
        "id": "investigate_dispute",
        "name": "Investigate a billing dispute",
        "description": (
            "Run a full autonomous investigation of a Stripe dispute / chargeback: "
            "query the customer's data across every connected source via Coral, "
            "record cited findings, apply policy, and produce a brief with a "
            "recommended decision and a drafted action set."
        ),
        "tags": ["action", "disputes", "chargebacks", "investigation"],
        "examples": [
            'Investigate dispute du_1Tch1O… ($8,400, product_not_received)',
            "Triage this charge.dispute.created webhook and recommend fight/refund/accept/escalate",
        ],
    },
    {
        "id": "contribute_evidence",
        "name": "Contribute evidence to a case",
        "description": (
            "Attach an external evidence record to a case from another agent or "
            "system (e.g. a delivery confirmation, a CRM note, a fraud signal). "
            "The contribution is appended to the case's audit trail with the "
            "contributor's identity and becomes available to the investigation."
        ),
        "tags": ["action", "evidence", "collaboration", "write"],
        "examples": [
            'contribute_evidence case_id=case-000123 contributor=logistics-agent '
            'evidence={"kind": "delivery_confirmation", "tracking": "1Z..."}',
        ],
    },
]

# Query skills (read state) — the "any state is A2A-pickup-able" surface ---
_QUERY_SKILLS = [
    {
        "id": "get_case",
        "name": "Get a case",
        "description": "Return one case's summary: status, customer, decision.",
        "tags": ["query", "state", "read", "case"],
        "examples": ["get_case for case_id case-000123"],
    },
    {
        "id": "list_cases",
        "name": "List cases",
        "description": "List cases, optionally filtered by status, with pagination.",
        "tags": ["query", "state", "read", "queue"],
        "examples": ["list_cases status=awaiting_approval limit=20"],
    },
    {
        "id": "get_brief",
        "name": "Get a case brief",
        "description": "Return the signed brief artifact: TL;DR, decision, drafted actions.",
        "tags": ["query", "state", "read", "brief"],
        "examples": ["get_brief for case-000123"],
    },
    {
        "id": "get_findings",
        "name": "Get case findings",
        "description": "Return the cited findings (each with evidence provenance).",
        "tags": ["query", "state", "read", "findings", "citations"],
        "examples": ["get_findings for case-000123"],
    },
    {
        "id": "get_actions",
        "name": "Get drafted/executed actions",
        "description": "Return the drafted/approved/executed actions for a case.",
        "tags": ["query", "state", "read", "actions"],
        "examples": ["get_actions for case-000123"],
    },
    {
        "id": "get_audit_trail",
        "name": "Get the audit trail",
        "description": "Return the ordered, signed event log for a case.",
        "tags": ["query", "state", "read", "audit", "compliance"],
        "examples": ["get_audit_trail for case-000123"],
    },
    {
        "id": "ask",
        "name": "Ask about a case or the portfolio",
        "description": (
            "Conversational Q&A grounded in case state: ask about one case "
            "(pass case_id) or the whole dispute portfolio. Answers cite the "
            "underlying findings/evidence — the advisor agent's front door."
        ),
        "tags": ["query", "advisor", "conversation", "read"],
        "examples": [
            "ask question='why did we refund this?' case_id=case-000123",
            "ask question='what is our biggest open dispute right now?'",
        ],
    },
    {
        "id": "precheck_refund",
        "name": "Pre-check a refund",
        "description": (
            "Before another agent issues a refund: return the approval gate "
            "(auto / one-click / two-person) for the amount, plus the "
            "customer's prior case and refund history."
        ),
        "tags": ["query", "advisor", "refunds", "policy", "read"],
        "examples": [
            "precheck_refund customer_ref=billing@acme.co amount_minor=4200",
        ],
    },
    {
        "id": "get_customer_history",
        "name": "Get a customer's case history",
        "description": (
            "Return every case on file for a customer reference (email or "
            "customer id): status and decision per case."
        ),
        "tags": ["query", "advisor", "customer", "read"],
        "examples": ["get_customer_history customer_ref=billing@acme.co"],
    },
    {
        "id": "dispute_exposure",
        "name": "Get open dispute exposure",
        "description": (
            "Portfolio roll-up: open case count, total disputed amount at "
            "risk (minor units), and a per-status breakdown."
        ),
        "tags": ["query", "advisor", "portfolio", "finance", "read"],
        "examples": ["dispute_exposure"],
    },
]


def build_agent_card(
    base_url: str,
    *,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the AgentCard dict for the given public base URL.

    `identity` carries the Agent-Identity fields surfaced in the roster UI:
    agent_id, service_account, model, signing_key_fingerprint.
    """
    identity = identity or {}
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Manthan Investigator",
        "description": (
            "Autonomous investigator for B2B SaaS billing disputes. Triggered by "
            "Stripe dispute events, it triangulates across 10+ business systems "
            "(via Coral), grounds every claim in a citation, and produces an "
            "auditable brief with a human-gated action set."
        ),
        "url": base_url.rstrip("/") + "/a2a",
        "preferredTransport": "JSONRPC",
        "version": AGENT_VERSION,
        "provider": {
            "organization": identity.get("provider_org", "Miny Labs"),
            "url": identity.get("provider_url", "https://manthan.quest"),
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        # Caller auth: a shared API key in the X-Manthan-A2A-Key header.
        # (Verification happens at the deployment edge / mounting app.)
        "securitySchemes": {
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Manthan-A2A-Key",
            },
        },
        "security": [{"apiKey": []}],
        "skills": _ACTION_SKILLS + _QUERY_SKILLS,
        # Track 3 "Agent Identity" — cryptographic + deployment identity.
        "manthanIdentity": {
            "agentId": identity.get("agent_id", "manthan-investigator"),
            "serviceAccount": identity.get("service_account", "investigator@manthan.iam.gserviceaccount.com"),
            "model": identity.get("model", "gemini-3.1-pro-preview"),
            "signingKeyFingerprint": identity.get("signing_key_fingerprint", "unset"),
            "runtime": identity.get("runtime", "cloud-run"),
        },
    }


# The set of query skill ids the dispatcher will accept, for validation.
QUERY_SKILL_IDS = {s["id"] for s in _QUERY_SKILLS}
ACTION_SKILL_IDS = {s["id"] for s in _ACTION_SKILLS}
