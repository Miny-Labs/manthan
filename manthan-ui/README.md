# manthan-ui

The React surface that renders Manthan's investigations, approvals,
and audit trail. Editorial-magazine layout, not a SaaS dashboard:
Spectral serif for the prose, Geist Mono for the data, brand-colored
source chips for citations, and a full-screen cinematic when the
operator hits Approve.

## Stack

- **React 19** + **TypeScript** + **Vite**
- **Tailwind CSS 4** with a hand-tuned token system (`var(--color-bg)`, `var(--color-ink)`, `var(--color-accent)` etc.)
- **motion/react** for the per-page fade and the approval cinematic
- **react-router-dom 7** for routing
- **Clerk** for auth (per-Clerk-user workspace isolation; every signed-in user gets their own org slug)
- **Spectral** serif + **Geist Mono** typography
- **simple-icons** + **lucide-react** for source and UI glyphs

## Routes

| Path | What renders |
|---|---|
| `/` | Landing page (marketing surface) |
| `/login` | Clerk-hosted sign-in |
| `/app` | Inbox; the empty state is the "Inbox Zero" insignia hero - cases arrive when a real trigger (Stripe webhook or A2A `investigate_dispute`) fires |
| `/app/case/:id` | The case workspace - InvestigationMemo while the agent runs, WorkspaceMemo for the brief and approve flow, the actions cinematic on approve, the Closed Brief on resolve |
| `/app/done` | Resolved case history |
| `/app/policies` | Policy rules (auto-fire conditions) |
| `/app/sources` | Connected source list (Coral catalog) |
| `/app/agents` | Agent roster - per-agent identity, model, A2A card |
| `/app/traces` | Live traces - span tree per case |
| `/app/controls` | Agent controls - HITL thresholds, model pin, kill switch |
| `/app/audit` | Per-case audit log |
| `/app/settings` | Workspace settings |
| `/blog/:slug` | Editorial posts (Captain's Log style) |
| `/changelog` | Release notes |

## Key components

| Component | What it renders |
|---|---|
| `InvestigationMemo` | Renders the live agent run - tool calls coming in over SSE, prettified into a rolling narrative ("Manthan is asking Stripe…"), with the raw Coral SQL feed available in the right-rail toggle. |
| `WorkspaceMemo` | The settled-brief surface: TL;DR, decision recommendation, suggested actions with the Approve · Execute / Hold / Deny / Escalate verdicts, citation chips wired to each source. |
| `ApprovalCinematic` | The full-screen takeover after Approve. One action at a time, MIN_DWELL_MS per action, real status from SSE. |
| `CitationChip` | The brand-colored pill that links a brief claim back to its source record (Stripe dashboard, Notion page, etc.). |
| `SourceIcon` / `getSource` | Glyph + brand color for every connected source. |

## How a case actually renders

```
/app/case/:id loads
        │
        ▼
  Workspace.tsx fetches `/api/cases` (list) and `/api/cases/:id` (detail)
        │  populates rawCaseById + workspaceActions
        ▼
  case.status === 'investigating'
        ▼
  <InvestigationMemo />  subscribes to `/api/cases/:id/events` SSE
                          renders each tool_call + finding as it lands
        │
        │  brief_drafted event arrives
        ▼
  case.status flips to 'awaiting_approval'
        ▼
  <WorkspaceMemo />       shows brief + Approve · Execute button
        │
        │  operator clicks Approve
        ▼
  state = "firing"
        ▼
  <ApprovalCinematic />   plays each action with the full dwell time
        │
        │  cinematic completes, case.status = 'resolved'
        ▼
  state = "fired"
        ▼
  <WorkspaceMemo />       now in Closed Brief mode (executed actions
                          with external_ref deep-links)
```

## Development

```bash
npm install
npm run dev          # Dev server at localhost:5173 (proxies /api to :8000)
npm run build        # Production build to dist/
npm run typecheck    # tsc --noEmit
```

Production deploy is GCP-native: `deploy/gcp/Dockerfile.ui` runs
`npm run build` with the API origin baked into the bundle via the
`VITE_MANTHAN_API_URL` build arg, then serves `dist/` with Caddy
(static files only - no /api proxy). See
[`../deploy/gcp/README.md`](../deploy/gcp/README.md) for the full path.
