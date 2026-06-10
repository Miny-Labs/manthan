/**
 * Agent Controls - the controls surface at /app/controls.
 *
 * Three sections, all honest about where they're enforced:
 *
 *  (a) HITL thresholds - the dollar gates locked in the worker (auto
 *      under $50, one-click $50-$500, two-person at $500+ with the ARR
 *      override), plus the live policy rules from /api/policy that
 *      route decisions on top of those gates. Rules edit in /app/policy.
 *
 *  (b) Model pinning - which Gemini models the agent runs per tier and
 *      the env vars that pin them. Read at boot; changing them is a
 *      redeploy, so we show the names as copyable code, not a toggle.
 *
 *  (c) Kill switch + budget - deploy-level levers as copyable gcloud /
 *      env commands. No in-app toggle: a switch that doesn't reach the
 *      deployment would be theatre.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Copy, Lock } from "lucide-react";

import { listPolicyRules, type ApiPolicyRule } from "@/lib/api";
import { fetchAgentCard, OFFLINE_AGENT_CARD } from "@/lib/agentCard";

// ──────────────────────────────────────────────────────────────────────
// The locked gates. These mirror the worker's enforcement; the UI
// renders them read-only on purpose - they are not org-editable knobs.
// ──────────────────────────────────────────────────────────────────────

const LOCKED_GATES = [
  {
    range: "Under $50",
    label: "Auto-execute",
    tone: "var(--color-accent)",
    detail:
      "Refunds and credits below $50 fire without a human in the loop. Every action still lands in the audit trail with its citations.",
  },
  {
    range: "$50 – $500",
    label: "One-click approval",
    tone: "var(--color-amber)",
    detail:
      "The agent drafts the full action set, then waits. A single operator nod from the inbox or the case workspace releases it.",
  },
  {
    range: "$500 and above",
    label: "Two-person rule",
    tone: "var(--color-danger)",
    detail:
      "Two distinct approvers are required before anything fires. High-ARR accounts override into this gate regardless of the disputed amount.",
  },
] as const;

// Default models per tier - same fallbacks the agent's config.py bakes
// in when the env vars are unset. Overridden by the live agent card's
// pinned investigator model when the card is reachable.
const MODEL_TIERS = [
  {
    role: "Investigator",
    envVar: "MANTHAN_MODEL",
    fallback: "gemini-3.1-pro-preview",
    detail: "The orchestrator that runs the investigation loop.",
  },
  {
    role: "Sub-agents",
    envVar: "MANTHAN_MODEL_SUBAGENT",
    fallback: "gemini-3.5-flash",
    detail: "Action drafting and helper agents.",
  },
  {
    role: "Triage router",
    envVar: "MANTHAN_MODEL_TRIAGE",
    fallback: "gemini-3.1-flash-lite",
    detail: "Cheap event router for inbound webhooks.",
  },
] as const;

const DEPLOY_COMMANDS = [
  {
    label: "Kill switch - scale the worker to zero",
    detail:
      "Stops every new investigation immediately. In-flight Cloud Run instances drain and exit; nothing else can start.",
    command:
      "gcloud run services update manthan-worker --region us-central1 --min-instances 0 --max-instances 0",
  },
  {
    label: "Resume the worker",
    detail: "Brings the worker back at single-instance scale.",
    command:
      "gcloud run services update manthan-worker --region us-central1 --min-instances 0 --max-instances 1",
  },
  {
    label: "Cap parallel spend",
    detail:
      "One instance, one case at a time - an upper bound on concurrent Gemini usage when you want the agent slow and cheap.",
    command:
      "gcloud run services update manthan-worker --region us-central1 --max-instances 1 --concurrency 1",
  },
  {
    label: "Budget mode - pin cheaper models",
    detail:
      "Re-pins every tier to the flash family. Cuts per-case token cost; the worker reads these env vars once at boot.",
    command:
      "gcloud run services update manthan-worker --region us-central1 --update-env-vars MANTHAN_MODEL=gemini-3.5-flash,MANTHAN_MODEL_SUBAGENT=gemini-3.1-flash-lite",
  },
] as const;

// ──────────────────────────────────────────────────────────────────────
// Page
// ──────────────────────────────────────────────────────────────────────

export default function AgentControls() {
  const [rules, setRules] = useState<ApiPolicyRule[] | null>(null);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [pinnedModel, setPinnedModel] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listPolicyRules()
      .then((r) => {
        if (!cancelled) setRules(r);
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setRulesError(e instanceof Error ? e.message : String(e));
      });
    // The live card carries the investigator's actually-pinned model.
    // Unreachable card -> fall back to the documented defaults quietly.
    fetchAgentCard()
      .then((card) => {
        if (cancelled) return;
        setPinnedModel(
          card.manthanIdentity?.model ??
            OFFLINE_AGENT_CARD.manthanIdentity?.model ??
            null,
        );
      })
      .catch(() => {
        if (!cancelled)
          setPinnedModel(OFFLINE_AGENT_CARD.manthanIdentity?.model ?? null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div
      className="h-full w-full overflow-y-auto"
      style={{ background: "var(--color-bg)" }}
    >
      <div
        className="mx-auto flex flex-col px-4 sm:px-6 py-8 sm:py-10"
        style={{
          maxWidth: 1100,
          paddingBottom: 64,
          color: "var(--color-ink-strong)",
        }}
      >
        <header className="flex flex-col gap-5">
          <Eyebrow>Controls</Eyebrow>
          <h1
            className="leading-[1.06]"
            style={{
              fontFamily: "Spectral, serif",
              fontSize: "clamp(30px, 3vw, 38px)",
              color: "var(--color-ink-strong)",
              letterSpacing: "-0.014em",
              fontStyle: "italic",
              fontWeight: 400,
            }}
          >
            The levers, and where they live.
          </h1>
          <p
            className="leading-[1.5] max-w-[66ch]"
            style={{
              fontFamily: "Spectral, serif",
              fontStyle: "italic",
              fontSize: 15,
              color: "var(--color-ink-muted)",
              letterSpacing: "-0.003em",
            }}
          >
            What the agent may do alone, which models it runs, and how to
            stop it. Every control on this page says where it’s enforced -
            there are no toggles here that don’t reach the deployment.
          </p>
        </header>

        <GatesSection rules={rules} rulesError={rulesError} />
        <ModelsSection pinnedModel={pinnedModel} />
        <DeploySection />
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// (a) HITL thresholds - locked gates + live policy rules.
// ──────────────────────────────────────────────────────────────────────

function GatesSection({
  rules,
  rulesError,
}: {
  rules: ApiPolicyRule[] | null;
  rulesError: string | null;
}) {
  return (
    <section className="mt-12">
      <SectionRule
        eyebrow="HITL thresholds"
        note="enforced in the worker before any action fires"
      />

      <div
        style={{
          border: "1px solid var(--color-rule)",
          borderRadius: 6,
          overflow: "hidden",
        }}
      >
        {LOCKED_GATES.map((g, i) => (
          <div
            key={g.range}
            className="grid items-baseline gap-x-6 gap-y-1 px-7 py-4"
            style={{
              gridTemplateColumns: "150px 190px minmax(0, 1fr) auto",
              borderTop: i === 0 ? "none" : "1px solid var(--color-rule-soft)",
            }}
          >
            <span
              className="font-mono text-[12.5px] tabular-nums"
              style={{ color: "var(--color-ink)", letterSpacing: "0.02em" }}
            >
              {g.range}
            </span>
            <span
              className="text-[12px] uppercase"
              style={{ color: g.tone, letterSpacing: "0.18em", fontWeight: 500 }}
            >
              {g.label}
            </span>
            <span
              className="text-[13px] leading-[1.5] min-w-0"
              style={{
                fontFamily: "Spectral, serif",
                fontStyle: "italic",
                color: "var(--color-ink-muted)",
              }}
            >
              {g.detail}
            </span>
            <span
              className="inline-flex items-center gap-1.5 font-mono text-[9.5px] uppercase shrink-0"
              style={{ color: "var(--color-ink-ghost)", letterSpacing: "0.18em" }}
              title="Locked in the worker - not org-editable"
            >
              <Lock size={10} strokeWidth={1.8} />
              Locked
            </span>
          </div>
        ))}
      </div>

      {/* Live policy rules - the org-editable layer on top of the gates */}
      <div className="mt-8 flex items-baseline justify-between flex-wrap gap-2">
        <span className="inline-flex items-baseline gap-3">
          <Eyebrow>Live policy rules</Eyebrow>
          <span
            className="text-[13px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-ink-faint)",
            }}
          >
            from /api/policy · routed on top of the locked gates
          </span>
        </span>
        <Link
          to="/app/policy"
          className="text-[12px] uppercase hover:underline"
          style={{
            color: "var(--color-accent)",
            letterSpacing: "0.16em",
            fontWeight: 500,
            textUnderlineOffset: 4,
          }}
        >
          Edit in Policies →
        </Link>
      </div>

      {rulesError && (
        <p
          className="mt-4 text-[13.5px] italic"
          style={{ fontFamily: "Spectral, serif", color: "var(--color-danger)" }}
        >
          Couldn’t load policy rules: {rulesError}
        </p>
      )}
      {rules === null && !rulesError && (
        <p
          className="mt-4 text-[13.5px] italic"
          style={{
            fontFamily: "Spectral, serif",
            color: "var(--color-ink-faint)",
          }}
        >
          Loading rules…
        </p>
      )}
      {rules !== null && rules.length === 0 && (
        <p
          className="mt-4 text-[13.5px] italic"
          style={{
            fontFamily: "Spectral, serif",
            color: "var(--color-ink-faint)",
          }}
        >
          No rules yet - the agent escalates every case until one exists.
        </p>
      )}

      {rules !== null && rules.length > 0 && (
        <ol className="mt-3 flex flex-col">
          {rules.map((r, i) => (
            <RuleRow key={r.id} rule={r} first={i === 0} />
          ))}
        </ol>
      )}
    </section>
  );
}

const MODE_COLOR: Record<string, string> = {
  auto: "var(--color-accent)",
  recommend: "var(--color-amber)",
  suggest: "var(--color-amber)",
  hitl: "var(--color-info)",
  escalate: "var(--color-danger)",
};

function RuleRow({ rule, first }: { rule: ApiPolicyRule; first: boolean }) {
  const mode = String(
    (rule.decision as { mode?: string }).mode ?? "recommend",
  );
  return (
    <li
      className="grid items-baseline gap-x-5 gap-y-1 py-2.5 px-1"
      style={{
        gridTemplateColumns: "64px 110px minmax(0, 1fr) auto",
        borderTop: first ? "none" : "1px solid var(--color-rule-soft)",
        opacity: rule.enabled ? 1 : 0.5,
      }}
    >
      <span
        className="font-mono text-[11.5px] uppercase tabular-nums"
        style={{ color: "var(--color-ink-faint)", letterSpacing: "0.08em" }}
      >
        RULE {rule.priority.toString().padStart(2, "0")}
      </span>
      <span
        className="text-[10.5px] uppercase"
        style={{
          color: MODE_COLOR[mode] ?? "var(--color-ink-muted)",
          letterSpacing: "0.18em",
          fontWeight: 500,
        }}
      >
        {mode === "suggest" ? "recommend" : mode}
      </span>
      <span
        className="text-[13.5px] leading-[1.4] truncate min-w-0"
        style={{
          fontFamily: "Spectral, serif",
          fontStyle: "italic",
          color: "var(--color-ink)",
        }}
        title={rule.name}
      >
        {rule.name}
      </span>
      <span
        className="font-mono text-[11px] tabular-nums shrink-0"
        style={{
          color: rule.enabled ? "var(--color-accent)" : "var(--color-ink-faint)",
          letterSpacing: "0.06em",
        }}
      >
        {rule.enabled ? "enabled" : "disabled"} ·{" "}
        {rule.match_count_90d.toLocaleString()} matches/90d
      </span>
    </li>
  );
}

// ──────────────────────────────────────────────────────────────────────
// (b) Model pinning.
// ──────────────────────────────────────────────────────────────────────

function ModelsSection({ pinnedModel }: { pinnedModel: string | null }) {
  return (
    <section className="mt-14">
      <SectionRule
        eyebrow="Model pinning"
        note="read once at boot - changing a pin is a redeploy, not a toggle"
      />
      <div
        style={{
          border: "1px solid var(--color-rule)",
          borderRadius: 6,
          overflow: "hidden",
        }}
      >
        {MODEL_TIERS.map((tier, i) => {
          // The live agent card pins the investigator tier; the other
          // tiers show the documented defaults.
          const model =
            tier.envVar === "MANTHAN_MODEL" && pinnedModel
              ? pinnedModel
              : tier.fallback;
          return (
            <div
              key={tier.envVar}
              className="grid items-center gap-x-6 gap-y-2 px-7 py-4"
              style={{
                gridTemplateColumns: "150px minmax(0, 1fr) minmax(0, auto)",
                borderTop:
                  i === 0 ? "none" : "1px solid var(--color-rule-soft)",
              }}
            >
              <span className="min-w-0">
                <span
                  className="block text-[14px]"
                  style={{
                    fontFamily: "Spectral, serif",
                    fontStyle: "italic",
                    color: "var(--color-ink-strong)",
                  }}
                >
                  {tier.role}
                </span>
                <span
                  className="block mt-0.5 text-[11.5px] leading-[1.4]"
                  style={{ color: "var(--color-ink-faint)" }}
                >
                  {tier.detail}
                </span>
              </span>
              <span
                className="font-mono text-[13px] tabular-nums truncate"
                style={{ color: "var(--color-ink)", letterSpacing: "0.01em" }}
                title={model}
              >
                {model}
              </span>
              <CopyCode text={`${tier.envVar}=${model}`} />
            </div>
          );
        })}
      </div>
      <p
        className="mt-3 text-[12.5px] italic"
        style={{
          fontFamily: "Spectral, serif",
          color: "var(--color-ink-faint)",
        }}
      >
        The investigator pin reflects the live agent card when reachable;
        sub-agent and triage pins show the worker’s documented defaults.
      </p>
    </section>
  );
}

// ──────────────────────────────────────────────────────────────────────
// (c) Kill switch + budget - deploy-level commands.
// ──────────────────────────────────────────────────────────────────────

function DeploySection() {
  return (
    <section className="mt-14">
      <SectionRule
        eyebrow="Kill switch · budget"
        note="applied at deployment - copy, run, done"
      />

      <p
        className="mb-6 text-[14px] leading-[1.55] max-w-[66ch]"
        style={{
          fontFamily: "Spectral, serif",
          fontStyle: "italic",
          color: "var(--color-ink-muted)",
        }}
      >
        These levers act on the Cloud Run worker itself, so they hold even
        if the app is down. There is deliberately no in-page switch:
        per-run tool budgets are already capped in-code by the
        investigation pacer, and anything stronger has to reach the
        deployment to be real.
      </p>

      <div className="flex flex-col gap-5">
        {DEPLOY_COMMANDS.map((c) => (
          <div key={c.label}>
            <div className="flex items-baseline justify-between gap-4 flex-wrap">
              <span
                className="text-[14px]"
                style={{
                  fontFamily: "Spectral, serif",
                  fontStyle: "italic",
                  color: "var(--color-ink-strong)",
                }}
              >
                {c.label}
              </span>
              <span
                className="font-mono text-[9.5px] uppercase"
                style={{
                  color: "var(--color-ink-ghost)",
                  letterSpacing: "0.18em",
                }}
              >
                applied at deployment
              </span>
            </div>
            <p
              className="mt-1 text-[12.5px] leading-[1.5] max-w-[70ch]"
              style={{ color: "var(--color-ink-faint)" }}
            >
              {c.detail}
            </p>
            <CommandBlock command={c.command} />
          </div>
        ))}
      </div>
    </section>
  );
}

function CommandBlock({ command }: { command: string }) {
  return (
    <div
      className="mt-2 flex items-start gap-3 px-4 py-3"
      style={{
        background: "var(--color-surface)",
        border: "1px solid var(--color-rule-soft)",
        borderRadius: 4,
      }}
    >
      <code
        className="font-mono text-[12px] leading-[1.6] min-w-0 break-all flex-1"
        style={{ color: "var(--color-ink)", letterSpacing: "0.01em" }}
      >
        {command}
      </code>
      <span className="shrink-0">
        <CopyCode text={command} bare />
      </span>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// CopyCode - mono code chip with a copy affordance. `bare` renders just
// the button (for use inside an existing code block).
// ──────────────────────────────────────────────────────────────────────

function CopyCode({ text, bare }: { text: string; bare?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      })
      .catch(() => {});
  };

  if (bare) {
    return (
      <button
        type="button"
        onClick={copy}
        className="inline-flex items-center gap-1 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-line)]"
        style={{
          fontFamily: "Geist Mono, ui-monospace, monospace",
          fontSize: 10,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: copied ? "var(--color-accent)" : "var(--color-ink-muted)",
          background: "transparent",
          border: "none",
          padding: "2px 0",
          cursor: "pointer",
        }}
        aria-label="Copy command"
        title="Copy command"
      >
        {copied ? (
          <Check size={12} strokeWidth={2} />
        ) : (
          <Copy size={12} strokeWidth={1.8} />
        )}
        {copied ? "Copied" : "Copy"}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={copy}
      className="inline-flex items-center gap-2 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-line)]"
      style={{
        fontFamily: "Geist Mono, ui-monospace, monospace",
        fontSize: 11.5,
        color: copied ? "var(--color-accent)" : "var(--color-ink-muted)",
        background: "var(--color-surface)",
        border: "1px solid var(--color-rule-soft)",
        borderRadius: 4,
        padding: "6px 11px",
        cursor: "pointer",
        letterSpacing: "0.01em",
      }}
      aria-label={`Copy ${text}`}
      title={`Copy ${text}`}
    >
      <span className="truncate" style={{ maxWidth: 320 }}>
        {text}
      </span>
      {copied ? (
        <Check size={12} strokeWidth={2} className="shrink-0" />
      ) : (
        <Copy size={12} strokeWidth={1.8} className="shrink-0" />
      )}
    </button>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Section rule + eyebrow primitives.
// ──────────────────────────────────────────────────────────────────────

function SectionRule({ eyebrow, note }: { eyebrow: string; note: string }) {
  return (
    <div
      className="flex items-baseline gap-3 pb-2.5 mb-6 border-b flex-wrap"
      style={{ borderColor: "var(--color-rule-soft)" }}
    >
      <Eyebrow>{eyebrow}</Eyebrow>
      <span
        className="text-[13px] italic"
        style={{
          fontFamily: "Spectral, serif",
          color: "var(--color-ink-faint)",
        }}
      >
        {note}
      </span>
    </div>
  );
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="text-[12.5px] uppercase"
      style={{
        color: "var(--color-ink-muted)",
        letterSpacing: "0.20em",
        fontWeight: 500,
      }}
    >
      {children}
    </span>
  );
}
