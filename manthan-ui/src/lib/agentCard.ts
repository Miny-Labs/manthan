/**
 * Agent card client - fetches the A2A AgentCard the agent serves at
 * /.well-known/agent-card.json (same origin; the deploy proxies it to
 * the agent service alongside /api/*).
 *
 * Both /app/agents (identity surface) and /app/controls (model pinning)
 * read this. When the card is unreachable we fall back to
 * OFFLINE_AGENT_CARD - the same fields the agent's card builder
 * defaults to - so the surfaces stay legible, clearly labeled as a
 * placeholder by the caller.
 */

export interface AgentSkill {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  examples?: string[];
}

export interface AgentIdentity {
  agentId?: string;
  serviceAccount?: string;
  model?: string;
  signingKeyFingerprint?: string;
  runtime?: string;
}

export interface AgentCard {
  protocolVersion?: string;
  name?: string;
  description?: string;
  /** A2A JSON-RPC endpoint URL. */
  url?: string;
  preferredTransport?: string;
  version?: string;
  provider?: { organization?: string; url?: string };
  capabilities?: Record<string, unknown>;
  skills?: AgentSkill[];
  manthanIdentity?: AgentIdentity;
}

export const AGENT_CARD_PATH = "/.well-known/agent-card.json";

/** Mirror of the agent-side defaults (agent/src/manthan_agent/a2a/card.py),
 *  shown - clearly labeled - when the live card can't be fetched. */
export const OFFLINE_AGENT_CARD: AgentCard = {
  protocolVersion: "0.3.0",
  name: "Manthan Investigator",
  description:
    "Autonomous investigator for B2B SaaS billing disputes. Triggered by " +
    "Stripe dispute events, it triangulates across 10+ business systems " +
    "(via Coral), grounds every claim in a citation, and produces an " +
    "auditable brief with a human-gated action set.",
  url: "/a2a",
  preferredTransport: "JSONRPC",
  version: "1.0.0",
  provider: { organization: "Miny Labs", url: "https://manthan.quest" },
  skills: [
    {
      id: "investigate_dispute",
      name: "Investigate a billing dispute",
      description:
        "Run a full autonomous investigation of a Stripe dispute / chargeback: " +
        "query the customer's data across every connected source via Coral, " +
        "record cited findings, apply policy, and produce a brief with a " +
        "recommended decision and a drafted action set.",
      tags: ["disputes", "chargebacks", "action", "investigation"],
    },
    {
      id: "get_case",
      name: "Get a case",
      description: "Return one case's summary: status, customer, decision.",
      tags: ["state", "read", "case"],
    },
    {
      id: "list_cases",
      name: "List cases",
      description: "List cases, optionally filtered by status, with pagination.",
      tags: ["state", "read", "queue"],
    },
    {
      id: "get_brief",
      name: "Get a case brief",
      description: "Return the signed brief artifact: TL;DR, decision, drafted actions.",
      tags: ["state", "read", "brief"],
    },
    {
      id: "get_findings",
      name: "Get case findings",
      description: "Return the cited findings (each with evidence provenance).",
      tags: ["state", "read", "findings", "citations"],
    },
    {
      id: "get_actions",
      name: "Get drafted/executed actions",
      description: "Return the drafted/approved/executed actions for a case.",
      tags: ["state", "read", "actions"],
    },
    {
      id: "get_audit_trail",
      name: "Get the audit trail",
      description: "Return the ordered, signed event log for a case.",
      tags: ["state", "read", "audit", "compliance"],
    },
  ],
  manthanIdentity: {
    agentId: "manthan-investigator",
    serviceAccount: "investigator@manthan.iam.gserviceaccount.com",
    model: "gemini-3.1-pro-preview",
    signingKeyFingerprint: "unset",
    runtime: "cloud-run",
  },
};

/** Fetch the live agent card. Throws on network / HTTP / parse failure -
 *  callers decide how to degrade (usually to OFFLINE_AGENT_CARD). */
export async function fetchAgentCard(): Promise<AgentCard> {
  const res = await fetch(AGENT_CARD_PATH, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`agent card: HTTP ${res.status}`);
  }
  const body = (await res.json()) as AgentCard;
  if (!body || typeof body !== "object") {
    throw new Error("agent card: malformed response");
  }
  return body;
}

/** Split a card's skills into action vs query skills. The card tags
 *  action skills with "action"; everything else reads state. */
export function splitSkills(card: AgentCard): {
  action: AgentSkill[];
  query: AgentSkill[];
} {
  const skills = card.skills ?? [];
  const action = skills.filter((s) => (s.tags ?? []).includes("action"));
  const query = skills.filter((s) => !(s.tags ?? []).includes("action"));
  return { action, query };
}
