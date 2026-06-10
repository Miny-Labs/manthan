/**
 * Agent Roster - the "Agent Identity" surface at /app/agents.
 *
 * One agent is on the desk today: the Manthan Investigator. This page
 * renders its A2A AgentCard - who it is (provider, version, service
 * account, model, signing key), where to reach it (the JSON-RPC
 * endpoint), and everything another agent can ask of it (the skill
 * catalog, split into the one ACTION it performs and the QUERY skills
 * that make every piece of case state A2A-pickup-able).
 *
 * Editorial-memo direction: identity as a mini-memo card (HeaderStrip +
 * definition list), skills as hairline-separated ledger rows. When the
 * card endpoint is unreachable we show the pinned offline placeholder,
 * clearly labeled - never a blank page.
 */

import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

import {
  AGENT_CARD_PATH,
  OFFLINE_AGENT_CARD,
  fetchAgentCard,
  splitSkills,
  type AgentCard,
  type AgentSkill,
} from "@/lib/agentCard";

// ──────────────────────────────────────────────────────────────────────
// Page
// ──────────────────────────────────────────────────────────────────────

type CardState =
  | { phase: "loading" }
  | { phase: "live"; card: AgentCard }
  | { phase: "offline"; card: AgentCard; reason: string };

export default function AgentRoster() {
  const [state, setState] = useState<CardState>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchAgentCard()
      .then((card) => {
        if (!cancelled) setState({ phase: "live", card });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState({
          phase: "offline",
          card: OFFLINE_AGENT_CARD,
          reason: e instanceof Error ? e.message : String(e),
        });
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
          <Eyebrow>Agents</Eyebrow>
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
            One agent on the roster.
          </h1>
          <p
            className="leading-[1.5] max-w-[64ch]"
            style={{
              fontFamily: "Spectral, serif",
              fontStyle: "italic",
              fontSize: 15,
              color: "var(--color-ink-muted)",
              letterSpacing: "-0.003em",
            }}
          >
            The Manthan Investigator publishes its identity and skill
            catalog as an A2A agent card - every piece of case state it
            holds is pickup-able by another agent over JSON-RPC.
          </p>
        </header>

        {state.phase === "loading" && (
          <p
            className="mt-12 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-ink-faint)",
            }}
          >
            Reading the agent card…
          </p>
        )}

        {state.phase === "offline" && (
          <p
            className="mt-8 text-[13.5px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-amber)",
            }}
          >
            Couldn’t reach{" "}
            <span className="font-mono not-italic text-[12px]">
              {AGENT_CARD_PATH}
            </span>{" "}
            ({state.reason}). Showing the pinned offline placeholder - same
            fields, not live.
          </p>
        )}

        {state.phase !== "loading" && (
          <IdentityMemo card={state.card} offline={state.phase === "offline"} />
        )}

        {state.phase !== "loading" && <SkillCatalog card={state.card} />}

        {state.phase !== "loading" && (
          <div
            className="mt-12 pt-5 flex items-center justify-between flex-wrap gap-4"
            style={{ borderTop: "1px solid var(--color-rule-soft)" }}
          >
            <span
              className="font-mono text-[12px] uppercase tabular-nums"
              style={{
                color: "var(--color-ink-faint)",
                letterSpacing: "0.18em",
              }}
            >
              {(state.card.skills ?? []).length} skills advertised
            </span>
            <span
              className="text-[13px]"
              style={{
                fontFamily: "Spectral, serif",
                fontStyle: "italic",
                color: "var(--color-ink-faint)",
              }}
            >
              From{" "}
              <span className="font-mono not-italic">{AGENT_CARD_PATH}</span>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// IdentityMemo - the agent as a mini-memo: HeaderStrip on top, the
// description + provider on the left, the manthanIdentity definition
// list on the right, the A2A endpoint as a footer strip.
// ──────────────────────────────────────────────────────────────────────

function IdentityMemo({ card, offline }: { card: AgentCard; offline: boolean }) {
  const idy = card.manthanIdentity ?? {};
  return (
    <article
      className="mt-12"
      style={{
        background: "var(--color-bg)",
        border: "1px solid var(--color-rule)",
        borderRadius: 6,
        overflow: "hidden",
      }}
    >
      {/* HeaderStrip */}
      <header
        className="flex items-center px-7 gap-3 flex-wrap"
        style={{
          minHeight: 48,
          paddingTop: 10,
          paddingBottom: 10,
          borderBottom: "1px solid var(--color-rule-soft)",
        }}
      >
        <span
          className="font-mono text-[12px] uppercase tabular-nums shrink-0"
          style={{ color: "var(--color-ink-muted)", letterSpacing: "0.14em" }}
        >
          AGENT
        </span>
        <span
          className="shrink-0"
          style={{ color: "var(--color-rule-strong)" }}
          aria-hidden
        >
          ·
        </span>
        <span
          className="text-[16px] shrink-0"
          style={{
            fontFamily: "Spectral, serif",
            color: "var(--color-ink-strong)",
            letterSpacing: "-0.006em",
          }}
        >
          {card.name ?? "Manthan Investigator"}
        </span>
        <span
          className="font-mono text-[11.5px] tabular-nums shrink-0"
          style={{ color: "var(--color-ink-faint)", letterSpacing: "0.04em" }}
        >
          v{card.version ?? "?"} · A2A {card.protocolVersion ?? "?"}
        </span>

        <span className="ml-auto inline-flex items-baseline gap-3 shrink-0">
          <span
            className="text-[12px] uppercase"
            style={{
              color: offline ? "var(--color-amber)" : "var(--color-accent)",
              letterSpacing: "0.22em",
              fontWeight: 500,
            }}
          >
            {offline ? "Offline placeholder" : "Live card"}
          </span>
        </span>
      </header>

      {/* Body: description + provider | identity dl */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <div className="px-7 py-7 flex flex-col gap-5">
          <div>
            <Eyebrow>What it does</Eyebrow>
            <p
              className="mt-3 text-[14px] leading-[1.55] max-w-[58ch]"
              style={{
                fontFamily: "Spectral, serif",
                fontStyle: "italic",
                color: "var(--color-ink-muted)",
                letterSpacing: "-0.003em",
              }}
            >
              {card.description ?? "-"}
            </p>
          </div>
          <div>
            <Eyebrow>Provider</Eyebrow>
            <dl className="mt-3 flex flex-col gap-2">
              <IdRow label="Organization" value={card.provider?.organization ?? "-"} />
              <IdRow label="URL" value={card.provider?.url ?? "-"} mono />
              <IdRow
                label="Transport"
                value={card.preferredTransport ?? "JSONRPC"}
                mono
              />
            </dl>
          </div>
        </div>

        <div
          className="px-7 py-7"
          style={{ borderLeft: "1px solid var(--color-rule-soft)" }}
        >
          <Eyebrow>Identity</Eyebrow>
          <dl className="mt-3 flex flex-col gap-2">
            <IdRow label="Agent ID" value={idy.agentId ?? "-"} mono />
            <IdRow label="Service account" value={idy.serviceAccount ?? "-"} mono />
            <IdRow label="Model" value={idy.model ?? "-"} mono />
            <IdRow
              label="Signing key"
              value={idy.signingKeyFingerprint ?? "-"}
              mono
              mute={(idy.signingKeyFingerprint ?? "unset") === "unset"}
            />
            <IdRow label="Runtime" value={idy.runtime ?? "-"} mono />
          </dl>
        </div>
      </div>

      {/* Footer strip: the A2A endpoint + copy */}
      <footer
        className="flex items-center gap-4 px-7 flex-wrap"
        style={{
          minHeight: 44,
          paddingTop: 10,
          paddingBottom: 10,
          borderTop: "1px solid var(--color-rule-soft)",
        }}
      >
        <span
          className="text-[10.5px] uppercase shrink-0"
          style={{
            color: "var(--color-ink-faint)",
            letterSpacing: "0.20em",
            fontWeight: 500,
          }}
        >
          A2A endpoint
        </span>
        <span
          className="font-mono text-[12.5px] tabular-nums min-w-0 truncate"
          style={{ color: "var(--color-ink)", letterSpacing: "0.01em" }}
        >
          {resolveEndpoint(card.url)}
        </span>
        <span className="ml-auto shrink-0">
          <CopyButton text={resolveEndpoint(card.url)} label="Copy URL" />
        </span>
      </footer>
    </article>
  );
}

/** The card may carry a relative endpoint (offline placeholder) - make
 *  it copy-pasteable by anchoring it to this origin. */
function resolveEndpoint(url: string | undefined): string {
  if (!url) return "-";
  if (/^https?:\/\//.test(url)) return url;
  return `${window.location.origin}${url.startsWith("/") ? "" : "/"}${url}`;
}

function IdRow({
  label,
  value,
  mono,
  mute,
}: {
  label: string;
  value: string;
  mono?: boolean;
  mute?: boolean;
}) {
  return (
    <div
      className="grid items-baseline"
      style={{
        gridTemplateColumns: "minmax(0, 140px) minmax(0, 1fr)",
        columnGap: 14,
      }}
    >
      <dt
        className="text-[13.5px]"
        style={{
          color: "var(--color-ink)",
          fontWeight: 500,
          letterSpacing: "-0.003em",
        }}
      >
        {label}
      </dt>
      <dd
        className={`text-[13px] min-w-0 truncate ${mono ? "font-mono tabular-nums" : ""}`}
        style={{
          color: mute ? "var(--color-ink-faint)" : "var(--color-ink-muted)",
          letterSpacing: mono ? "0.01em" : "-0.002em",
        }}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// SkillCatalog - the action skill on top (the one thing it DOES), then
// the query skills (every state read another agent can pick up).
// ──────────────────────────────────────────────────────────────────────

function SkillCatalog({ card }: { card: AgentCard }) {
  const { action, query } = splitSkills(card);
  return (
    <>
      <SkillGroup
        eyebrow="Action skills"
        note="does work - kicks off a full investigation"
        skills={action}
      />
      <SkillGroup
        eyebrow="Query skills"
        note="reads state - any case artifact is A2A-pickup-able"
        skills={query}
      />
    </>
  );
}

function SkillGroup({
  eyebrow,
  note,
  skills,
}: {
  eyebrow: string;
  note: string;
  skills: AgentSkill[];
}) {
  return (
    <section className="mt-12">
      <div
        className="flex items-baseline justify-between pb-2.5 mb-1 border-b flex-wrap gap-2"
        style={{ borderColor: "var(--color-rule-soft)" }}
      >
        <span className="inline-flex items-baseline gap-3">
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
        </span>
        <span
          className="text-[10px] uppercase tracking-[0.14em] tabular-nums"
          style={{ color: "var(--color-ink-ghost)" }}
        >
          {skills.length} {skills.length === 1 ? "skill" : "skills"}
        </span>
      </div>

      {skills.length === 0 ? (
        <p
          className="py-4 text-[14px] italic"
          style={{
            fontFamily: "Spectral, serif",
            color: "var(--color-ink-faint)",
          }}
        >
          None advertised.
        </p>
      ) : (
        <ol className="flex flex-col">
          {skills.map((s, i) => (
            <SkillRow key={s.id} skill={s} first={i === 0} />
          ))}
        </ol>
      )}
    </section>
  );
}

function SkillRow({ skill, first }: { skill: AgentSkill; first: boolean }) {
  return (
    <li
      className="grid items-baseline gap-x-5 gap-y-1 py-3.5 px-1"
      style={{
        gridTemplateColumns: "170px minmax(0, 1fr) minmax(0, auto)",
        borderTop: first ? "none" : "1px solid var(--color-rule-soft)",
      }}
    >
      <span
        className="font-mono text-[12px] tabular-nums truncate"
        style={{ color: "var(--color-ink)", letterSpacing: "0.02em" }}
        title={skill.id}
      >
        {skill.id}
      </span>

      <span className="min-w-0">
        <span
          className="text-[15px]"
          style={{
            fontFamily: "Spectral, serif",
            fontStyle: "italic",
            color: "var(--color-ink-strong)",
            letterSpacing: "-0.004em",
          }}
        >
          {skill.name}
        </span>
        {skill.description && (
          <span
            className="block mt-1 text-[13px] leading-[1.5]"
            style={{ color: "var(--color-ink-muted)" }}
          >
            {skill.description}
          </span>
        )}
      </span>

      <span className="inline-flex items-baseline gap-2 flex-wrap justify-end">
        {(skill.tags ?? []).map((t) => (
          <span
            key={t}
            className="font-mono text-[10px] uppercase"
            style={{
              color: "var(--color-ink-faint)",
              letterSpacing: "0.14em",
              padding: "2px 7px",
              border: "1px solid var(--color-rule-soft)",
              borderRadius: 999,
            }}
          >
            {t}
          </span>
        ))}
      </span>
    </li>
  );
}

// ──────────────────────────────────────────────────────────────────────
// CopyButton - quiet mono affordance, flips to a check for a beat.
// ──────────────────────────────────────────────────────────────────────

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard
          .writeText(text)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1400);
          })
          .catch(() => {});
      }}
      className="inline-flex items-center gap-1.5 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-line)]"
      style={{
        fontFamily: "Geist Mono, ui-monospace, monospace",
        fontSize: 11,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        color: copied ? "var(--color-accent)" : "var(--color-ink-muted)",
        background: "transparent",
        border: "1px solid var(--color-rule)",
        borderRadius: 4,
        padding: "5px 10px",
        cursor: "pointer",
      }}
      aria-label={label}
      title={label}
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

// ──────────────────────────────────────────────────────────────────────
// Eyebrow primitive.
// ──────────────────────────────────────────────────────────────────────

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
