# Track 3 winning strategy — derived from the kickoff deck + Devpost brief

Source: "AI Agents Challenge: Kickoff" (43-slide deck, Google for Startups) +
Devpost Track 3 additional details. This supersedes the architecture sections
of PLAN.md where they conflict.

## The two readings

**What they say (hard mandates):** B2B problem · Cloud-native runtime
(Cloud Run/GKE) · Gemini-powered reasoning · A2A interoperability. We satisfy
all four today.

**What they also say, in "Key Considerations" (reads as judging criteria):**
- "Focus on the design and orchestration of interactions between **multiple
  agents** using ADK"
- "Focus on the deployment of your multi-agent system on **Agent Engine**"
- "Strategically employ **Grounding** (Vertex AI Search, Google Search, Maps,
  private data) and **RAG** (Vertex AI Search or custom implementations)"
- "Clearly demonstrate how the **collaboration between agents** … leads to a
  more powerful solution **than a single agent could achieve**"

**What they don't say (deck subtext):** the entire deck is a Gemini Enterprise
Agent Platform launch (Build/Scale/Govern/Optimize; Agent Registry, Gateway,
Identity GA, Memory Bank GA, Agent Evaluation/Simulation/Observability/
Optimizer all "New"). Judges are the people shipping these products. A winning
entry produces console screenshots of *our* agent inside *their* registry with
populated Traces / Identity / Sessions / Memory / Evaluation tabs — and is
published to Gemini Enterprise (the Agents CLI flow ends at "Publish").
AP2/UCP (agent payments) appear in the Build row — for a disputes product this
makes A2A *product-native*: when buyers are agents, disputes are
agent-to-agent conversations.

## Status update — the native spine is in (macro split shipped)

The de-bolting landed: **agents are the system.** Three macro A2A agents,
each its own FastAPI app / Cloud Run service / service account
(`manthan-api/src/manthan_api/agents/`):

- **triage** (flash-lite) — Stripe webhook intake + `route_event`; dispatches
  the investigator over A2A (`INVESTIGATOR_A2A_URL`), with a documented
  in-process fallback for local dev.
- **investigator** (pro) — `investigate_dispute` creates the case and runs
  the ADK investigation **in-process**, writing its own events + projections
  through `services/case_store.py`. The NOTIFY-mirror investigate worker is
  **deleted** (chat_loop with it); `workers/main.py` runs only the
  deterministic actor + prettifier.
- **advisor** (flash) — the conversational A2A face: `ask` (grounded, cited),
  `precheck_refund`, `get_customer_history`, `dispute_exposure`,
  `contribute_evidence` + the 6 reads, over the same Postgres projections the
  merchant UI reads (zero UI changes).

`deploy/gcp/deploy.sh` deploys all three from the same image with per-agent
service accounts (Agent Identity); the README documents Agent Engine
(`adk deploy agent_engine`) as the preferred investigator runtime with Cloud
Run as the working fallback. Remaining inside the investigator: the
in-process parallel specialist split (team.py/agents.py — in flight).

## Gap analysis (current repo @ 8df95c0)

| Criterion | Status | Fix |
|---|---|---|
| B2B / Gemini / Cloud Run / A2A baseline | DONE | — |
| Multi-agent macro split (triage → investigator → case store; advisor face) | **DONE** (see status update above) | — |
| Multi-agent ADK orchestration (in-process specialists) | IN FLIGHT (team.py/agents.py) | P0 split below |
| Agent Engine deployment | DOCUMENTED (runbook path; Cloud Run fallback deployed) | P1 runtime profile |
| Grounding/RAG articulation | WEAK (Coral never framed as grounding; zero Google grounding surfaces) | Frame Coral = live-system grounding + RAG over merchant SOPs (Notion retrieval); add Google Search grounding via the Network-Rules Analyst |
| Collaboration > single agent, demonstrated | MISSING | Eval chart: solo vs multi config on same 10-case set |
| Gemini Enterprise registration / Agent Registry | MISSING | agents-cli publish |
| Identity / Memory Bank / Eval / Simulation / Optimizer | PARTIAL (per-agent SAs + cards shipped; rest scaffolds) | One real, screenshotted loop each |
| A2A richness | **DONE for v2 asks**: ask / precheck_refund / get_customer_history / dispute_exposure / contribute_evidence + investigate + 6 reads | request_approval + subscribe_case still open |

## Target architecture

```
Stripe webhook / A2A ─▶ Triage Agent (gemini-3.1-flash-lite)
                              ▼
                Investigator Coordinator (gemini-3.1-pro)
                owns case · pacer callbacks · synthesizes brief
        ┌──────────┬──────────┼──────────┬───────────────┐
        ▼          ▼          ▼          ▼               ▼
   Payments    Customer   Reliability  Policy        Network-Rules
   Analyst     Context    Analyst      Analyst       Analyst
   stripe.*    crm +      datadog/     notion SOP    google_search
   (flash)     intercom   sentry/pd/   retrieval     grounding: Visa
               (flash)    posthog      = RAG (flash) CE3.0, MC rules
        └──────────┴──────────┴──────────┴───────────────┘
                              ▼
                Resolution Agent (gemini-3.5-flash)
                complete action set → HITL policy gates → actor
```

Notes:
- Specialists are ADK agents wrapped as AgentTools, fanned out in parallel;
  each has a restricted Coral schema scope and focused prompt. Coordinator
  keeps RunState/Evidence/citations + pacer.
- Network-Rules Analyst MUST be its own agent: ADK built-in google_search
  cannot mix with function tools on one agent — multi-agent justified by a
  real constraint, and it hits "Grounding (Google Search)" verbatim.
- The run_case() Event-stream seam is preserved; sub-agent events stream
  through the same translator (worker untouched, again).

## A2A skill catalog v2 (any state pickup-able → any workflow joinable)

Ask & advise (SHIPPED on the advisor agent):
- `ask` — NL question over a case → cited answer (one grounded Gemini call
  over the case's findings+brief; v2: Coral tool-loop for live re-checks).
- `precheck_refund` — CS/refund-desk agent asks before granting a refund:
  history + usage + risk + recommended path in seconds. Deflects friendly
  fraud pre-dispute. THE inter-agent demo. (SHIPPED — deterministic rules.)
- `get_customer_history` — episodic memory by customer_ref. (SHIPPED)
- `dispute_exposure` — aggregate open exposure for CFO agents. (SHIPPED;
  win-rates still open.)

Collaborate (bidirectional):
- `contribute_evidence` — external agent pushes evidence into an open case
  (e.g. CS chat transcript), provenance tagged with the caller's Agent
  Identity; investigator incorporates next round. (SHIPPED — event lands on
  the thread with `a2a:<contributor>` actor.)
- `request_approval` — A2A-native HITL: an approval agent with a delegated
  mandate (AP2-style) approves a drafted action within limits, signed.
- `subscribe_case` — push state changes to peers (streaming/webhook).

Keep: `investigate_dispute` + the 6 read skills. Card adds securitySchemes
(API key now, OIDC on GCP) + extended card.

## Platform residency moves

| Product | Move |
|---|---|
| Agent Engine | `adk deploy agent_engine` profile for the investigator system; flag-flip to Vertex backend (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`); Coral becomes a Cloud Run sidecar over **streamable-HTTP MCP** (Coral 0.4.2 native) instead of stdio subprocess |
| Agent Registry + Gemini Enterprise | register + publish via agents-cli; console screenshot = top demo asset |
| Agent Identity | per-agent service accounts; ID-token-authenticated A2A between our agents; real fingerprints in the card + Roster UI |
| Memory Bank | back per-customer episodic memory with Agent Engine Memory Bank |
| Agent Evaluation | 10-case dispute eval via agents-cli eval + console; report solo-vs-multi comparison (trajectory score, evidence coverage, wall-clock) |
| Agent Simulation / Optimizer | one documented loop each (dispute-storm stress test; observe-failure → refine-instruction → verify) |
| Agent Gateway / Model Armor | Gateway fronts the A2A endpoint; Model Armor noted on webhook→LLM path |

## Narrative

"Stop using disconnected chatbots; start orchestrating autonomous agents with
full business context" — their words; Manthan is that for the moment money is
disputed. Today: a webhook wakes a coordinator that commands four specialist
agents across nine live business systems and returns a cited, policy-gated
brief in minutes. Tomorrow: buyers are agents (AP2), disputes are
agent-to-agent conversations, and the merchant's side of that conversation is
Manthan — discoverable by any enterprise agent via A2A, governed by Agent
Identity, measured by Agent Evaluation, listed on Marketplace + Gemini
Enterprise.

## Build order

- **P0** multi-agent split + A2A skills v2 + Coral streamable-HTTP client.
- **P1** Agent Engine profile · Registry + GE publish · per-agent identities ·
  Memory Bank · eval set with solo-vs-multi chart.
- **P2** RefundDesk companion agent (50-line ADK, discovers card, calls
  precheck_refund/ask on camera) · Simulation + Optimizer loops · Producer
  Portal submission · demo video · Trust Center finish.
