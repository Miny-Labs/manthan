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

# Action skill (does work) ------------------------------------------------
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
        "tags": ["disputes", "chargebacks", "action", "investigation"],
        "examples": [
            'Investigate dispute du_1Tch1O… ($8,400, product_not_received)',
            "Triage this charge.dispute.created webhook and recommend fight/refund/accept/escalate",
        ],
    },
]

# Query skills (read state) — the "any state is A2A-pickup-able" surface ---
_QUERY_SKILLS = [
    {
        "id": "get_case",
        "name": "Get a case",
        "description": "Return one case's summary: status, customer, decision.",
        "tags": ["state", "read", "case"],
        "examples": ["get_case for case_id case-000123"],
    },
    {
        "id": "list_cases",
        "name": "List cases",
        "description": "List cases, optionally filtered by status, with pagination.",
        "tags": ["state", "read", "queue"],
        "examples": ["list_cases status=awaiting_approval limit=20"],
    },
    {
        "id": "get_brief",
        "name": "Get a case brief",
        "description": "Return the signed brief artifact: TL;DR, decision, drafted actions.",
        "tags": ["state", "read", "brief"],
        "examples": ["get_brief for case-000123"],
    },
    {
        "id": "get_findings",
        "name": "Get case findings",
        "description": "Return the cited findings (each with evidence provenance).",
        "tags": ["state", "read", "findings", "citations"],
        "examples": ["get_findings for case-000123"],
    },
    {
        "id": "get_actions",
        "name": "Get drafted/executed actions",
        "description": "Return the drafted/approved/executed actions for a case.",
        "tags": ["state", "read", "actions"],
        "examples": ["get_actions for case-000123"],
    },
    {
        "id": "get_audit_trail",
        "name": "Get the audit trail",
        "description": "Return the ordered, signed event log for a case.",
        "tags": ["state", "read", "audit", "compliance"],
        "examples": ["get_audit_trail for case-000123"],
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
