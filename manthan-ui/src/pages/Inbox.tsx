/**
 * Inbox - the operator's morning view, editorial-memo direction.
 *
 * The inbox is a stack of mini-memos. Each row is a compressed version of
 * the case workspace: HeaderStrip (case_id + customer + policy + status)
 * on top, headline case-line + dollar-transform in the body, TLDR + next
 * action in the footer.
 *
 * Sort order: investigating → awaiting_approval (oldest-first within) →
 * resolved/escalated/errored (newest-first within). One stack, no tabs.
 *
 * Realtime: subscribes to /api/inbox/stream over SSE (useInboxStream).
 * Wraps each row in `<Link to=/app/case/:id>` so the workspace opens on
 * click.
 */

import { Link } from "react-router-dom";
import { motion } from "motion/react";
import { useEffect, useMemo, useState } from "react";

import {
  formatAge,
  formatAmount,
  type ApiCase,
  type CaseStatus,
} from "@/lib/api";
import { useInboxStream } from "@/lib/useInboxStream";

// ──────────────────────────────────────────────────────────────────────
// Status presentation - colors + labels for the right edge of the
// HeaderStrip. Match the WorkspaceMemo phase indicator palette.
// ──────────────────────────────────────────────────────────────────────

const STATUS_LABEL: Record<CaseStatus, string> = {
  investigating: "Investigating",
  awaiting_approval: "Awaiting nod",
  acting: "Acting",
  resolved: "Resolved",
  errored: "Errored",
  escalated: "Escalated",
};

const STATUS_COLOR: Record<CaseStatus, string> = {
  investigating: "var(--color-info)",
  awaiting_approval: "var(--color-amber)",
  acting: "var(--color-amber)",
  resolved: "var(--color-accent)",
  errored: "var(--color-danger)",
  escalated: "var(--color-danger)",
};

const ACTION_VERB: Record<string, string> = {
  refund: "Refund",
  fight: "Submit dispute evidence",
  partial_credit: "Issue partial credit",
  accept: "Accept",
  escalate: "Escalate to human",
};

// Sort priority: lower number = higher in the stack.
const STATUS_RANK: Record<CaseStatus, number> = {
  investigating: 0,
  acting: 1,
  awaiting_approval: 2,
  errored: 3,
  escalated: 4,
  resolved: 5,
};

// ──────────────────────────────────────────────────────────────────────
// Page
// ──────────────────────────────────────────────────────────────────────

export default function Inbox() {
  const { cases, isLive, error, lastUpdatedAt } = useInboxStream(60);

  // Subtle "just refreshed" pulse on the meta line. Driven off the SSE
  // timestamp so it fires once per real update, not on every render.
  const [justUpdated, setJustUpdated] = useState(false);
  useEffect(() => {
    if (lastUpdatedAt === null) return;
    setJustUpdated(true);
    const t = window.setTimeout(() => setJustUpdated(false), 900);
    return () => window.clearTimeout(t);
  }, [lastUpdatedAt]);

  const sorted = useMemo(() => {
    if (!cases) return null;
    // Inbox is the active desk only - resolved / errored / escalated
    // cases live in /app/done. Without this filter, closed cases stay
    // listed at the bottom forever and the inbox never reaches "zero",
    // which defeats the whole "Inbox Zero" framing.
    const arr = cases.filter(
      (c) =>
        c.status !== "resolved" &&
        c.status !== "errored" &&
        c.status !== "escalated",
    );
    arr.sort((a, b) => {
      const ra = STATUS_RANK[a.status] ?? 99;
      const rb = STATUS_RANK[b.status] ?? 99;
      if (ra !== rb) return ra - rb;
      // Within awaiting_approval, oldest first (most overdue at top).
      if (a.status === "awaiting_approval") {
        return (
          new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
        );
      }
      // Everything else: newest first.
      return (
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      );
    });
    return arr;
  }, [cases]);

  const counts = useMemo(() => {
    if (!sorted) return null;
    let awaiting = 0;
    let inflight = 0;
    for (const c of sorted) {
      if (c.status === "awaiting_approval") awaiting++;
      else if (c.status === "investigating" || c.status === "acting")
        inflight++;
    }
    return { total: sorted.length, awaiting, inflight };
  }, [sorted]);

  const isEmpty = sorted !== null && sorted.length === 0;

  // Empty state takes the full viewport on its own - no PageHeader above
  // (it would just duplicate the centered "Inbox zero." title). On
  // mobile the content can be taller than the viewport (sidebar drawer
  // adds 44px top, hero + cards + trailing button stack to >700px on a
  // 360px-wide phone) so we make the wrapper scrollable; on lg+ the
  // content always fits so the wrapper just behaves as a full-height
  // canvas.
  if (isEmpty) {
    return (
      <div
        className="h-full w-full overflow-y-auto"
        style={{ background: "var(--color-bg)" }}
      >
        <InboxEmptyState />
      </div>
    );
  }

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
        <PageHeader counts={counts} isLive={isLive} justUpdated={justUpdated} />

        {error && cases === null && (
          <p
            className="mt-8 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-danger)",
            }}
          >
            Couldn’t load the inbox: {error}
          </p>
        )}

        {cases === null && !error && (
          <p
            className="mt-12 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-ink-faint)",
            }}
          >
            Loading the desk…
          </p>
        )}

        {sorted && sorted.length > 0 && (
          <ol className="flex flex-col gap-4 mt-12 list-none p-0">
            {sorted.map((c, i) => (
              <motion.li
                key={c.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  duration: 0.36,
                  delay: Math.min(i * 0.04, 0.4),
                  ease: [0.22, 0.61, 0.36, 1],
                }}
              >
                <CaseRow c={c} />
              </motion.li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// PageHeader - eyebrow + Spectral italic title + italic subtitle +
// live pulse dot.
// ──────────────────────────────────────────────────────────────────────

function PageHeader({
  counts,
  isLive,
  justUpdated,
}: {
  counts: { total: number; awaiting: number; inflight: number } | null;
  isLive: boolean;
  justUpdated: boolean;
}) {
  const title =
    counts === null
      ? "The desk."
      : counts.total === 0
        ? "Inbox zero."
        : counts.total === 1
          ? "One case on your desk."
          : `${humanCount(counts.total)} cases on your desk.`;

  return (
    <header className="flex flex-col gap-5">
      <Eyebrow>Inbox</Eyebrow>

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
        {title}
      </h1>

      {counts && counts.total > 0 && (
        <p
          className="leading-[1.5] inline-flex items-baseline flex-wrap gap-2"
          style={{
            fontFamily: "Spectral, serif",
            fontStyle: "italic",
            fontSize: 15,
            color: "var(--color-ink-muted)",
            letterSpacing: "-0.003em",
          }}
        >
          {counts.awaiting > 0 ? (
            <>
              <span style={{ color: "var(--color-amber)" }}>
                {counts.awaiting} awaiting your nod
              </span>
              {counts.inflight > 0 && (
                <span style={{ color: "var(--color-rule-strong)" }}>·</span>
              )}
            </>
          ) : null}
          {counts.inflight > 0 && (
            <span style={{ color: "var(--color-info)" }}>
              {counts.inflight === 1
                ? "one in flight"
                : `${counts.inflight} in flight`}
            </span>
          )}
          {counts.awaiting === 0 && counts.inflight === 0 && (
            <span>everything’s closed.</span>
          )}
          <span
            className="ml-2 inline-block transition-opacity"
            title={isLive ? "Live" : "Reconnecting…"}
            style={{
              width: 7,
              height: 7,
              borderRadius: 999,
              background: isLive
                ? "var(--color-accent)"
                : "var(--color-ink-ghost)",
              opacity: justUpdated ? 1 : isLive ? 0.7 : 0.4,
              transform: `scale(${justUpdated ? 1.2 : 1})`,
              transitionDuration: "500ms",
            }}
          />
        </p>
      )}
    </header>
  );
}

// ──────────────────────────────────────────────────────────────────────
// CaseRow - one mini-memo. Outer chrome matches the WorkspaceMemo:
// 1px hairline, 6px radius, oklch warm dark.
// ──────────────────────────────────────────────────────────────────────

function CaseRow({ c }: { c: ApiCase }) {
  const policyName = c.policy_match?.rule_name ?? null;
  const tldr = c.card_summary?.trim() || synthesizeDescription(c);
  const next = synthesizeNextAction(c);
  const statusMeta = synthesizeStatusMeta(c);

  return (
    <Link
      to={`/app/case/${c.id}`}
      className="block outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-line)] focus-visible:rounded-md"
    >
      <article
        className="row-shell transition-colors duration-200 cursor-pointer"
        style={{
          background: "var(--color-bg)",
          border: "1px solid var(--color-rule)",
          borderRadius: 6,
          overflow: "hidden",
        }}
      >
        <HeaderStrip
          shortId={c.short_id}
          customer={c.customer_ref ?? "Unknown customer"}
          policyMatched={policyName}
          status={c.status}
          statusMeta={statusMeta}
        />
        <RowBody c={c} />
        <RowFooter tldr={tldr} next={next} status={c.status} />

        <style>{`
          a:hover > .row-shell { background: var(--color-surface); }
        `}</style>
      </article>
    </Link>
  );
}

function HeaderStrip({
  shortId,
  customer,
  policyMatched,
  status,
  statusMeta,
}: {
  shortId: string;
  customer: string;
  policyMatched: string | null;
  status: CaseStatus;
  statusMeta: string | null;
}) {
  return (
    <header
      className="flex items-center px-7 gap-3"
      style={{
        minHeight: 40,
        paddingTop: 8,
        paddingBottom: 8,
        borderBottom: "1px solid var(--color-rule-soft)",
      }}
    >
      <span
        className="font-mono text-[12px] uppercase tabular-nums shrink-0"
        style={{
          color: "var(--color-ink-muted)",
          letterSpacing: "0.14em",
        }}
      >
        CASE {shortId}
      </span>

      <span
        className="shrink-0"
        style={{ color: "var(--color-rule-strong)" }}
        aria-hidden
      >
        ·
      </span>

      <span
        className="text-[15px] shrink-0"
        style={{
          fontFamily: "Spectral, serif",
          color: "var(--color-ink)",
          letterSpacing: "0.005em",
        }}
      >
        {customer}
      </span>

      {policyMatched && (
        <>
          <span
            className="shrink-0"
            style={{ color: "var(--color-rule-strong)" }}
            aria-hidden
          >
            ·
          </span>
          <span
            className="font-mono text-[11.5px] tabular-nums inline-flex items-baseline gap-2 min-w-0"
            style={{
              color: "var(--color-ink-faint)",
              letterSpacing: "0.04em",
            }}
            title={`policy match · ${policyMatched}`}
          >
            <span
              className="uppercase shrink-0"
              style={{
                letterSpacing: "0.18em",
                color: "var(--color-ink-ghost)",
              }}
            >
              policy
            </span>
            <span
              className="truncate"
              style={{ color: "var(--color-ink-muted)" }}
            >
              {policyMatched}
            </span>
          </span>
        </>
      )}

      <span className="ml-auto inline-flex items-baseline gap-3 shrink-0">
        {statusMeta && (
          <span
            className="font-mono text-[11.5px] tabular-nums"
            style={{
              color: "var(--color-ink-faint)",
              letterSpacing: "0.04em",
            }}
          >
            {statusMeta}
          </span>
        )}
        <span
          className="text-[12.5px] uppercase"
          style={{
            color: STATUS_COLOR[status],
            letterSpacing: "0.22em",
            fontWeight: 500,
          }}
        >
          {STATUS_LABEL[status]}
        </span>
      </span>
    </header>
  );
}

function RowBody({ c }: { c: ApiCase }) {
  const caseLine = synthesizeCaseLine(c);
  return (
    <div
      className="flex items-start justify-between gap-10 px-7"
      style={{ paddingTop: 22, paddingBottom: 22, minHeight: 80 }}
    >
      <h2
        className="leading-[1.18] min-w-0 flex-1"
        style={{
          fontFamily: "Spectral, serif",
          fontSize: 22,
          color: "var(--color-ink-strong)",
          letterSpacing: "-0.010em",
          fontWeight: 400,
        }}
      >
        <em
          style={{
            fontStyle: "italic",
            color: "var(--color-ink)",
          }}
        >
          {caseLine}
        </em>
      </h2>

      <DollarTransform c={c} />
    </div>
  );
}

function DollarTransform({ c }: { c: ApiCase }) {
  const investigating =
    c.status === "investigating" || c.status === "acting";
  const dispute = formatAmount(c.amount_minor, c.currency ?? "usd");
  const recommended =
    c.decision_amount_minor != null
      ? formatAmount(c.decision_amount_minor, c.currency ?? "usd")
      : null;

  // Tint the recommended slot by what was decided. Refund full = neutral
  // (the company concedes); refund partial = accent (the win); fight = accent.
  const recommendKind = classifyDecision(c);
  const recommendedColor =
    recommendKind === "credit" || recommendKind === "fight"
      ? "var(--color-accent)"
      : recommendKind === "refund"
        ? "var(--color-ink)"
        : "var(--color-ink-faint)";

  return (
    <div className="flex items-baseline gap-4 shrink-0">
      <span
        className="font-mono tabular-nums"
        style={{
          color: "var(--color-ink)",
          fontSize: 18,
          letterSpacing: "-0.005em",
        }}
      >
        {dispute}
      </span>
      <span
        style={{
          color: "var(--color-ink-ghost)",
          fontSize: 16,
          transform: "translateY(-1px)",
        }}
        aria-hidden
      >
        →
      </span>
      <span
        className="tabular-nums whitespace-nowrap"
        style={{
          fontFamily: "Spectral, serif",
          fontStyle: "italic",
          fontSize: 26,
          color: recommendedColor,
          letterSpacing: "-0.008em",
          lineHeight: 1,
        }}
        aria-label={investigating ? "recommendation pending" : undefined}
      >
        {recommended ?? "…"}
      </span>
    </div>
  );
}

function RowFooter({
  tldr,
  next,
  status,
}: {
  tldr: string;
  next: string;
  status: CaseStatus;
}) {
  return (
    <div
      className="flex items-center justify-between gap-8 px-7"
      style={{
        minHeight: 44,
        paddingTop: 10,
        paddingBottom: 10,
        borderTop: "1px solid var(--color-rule-soft)",
      }}
    >
      <p
        className="min-w-0 truncate"
        style={{
          fontFamily: "Spectral, serif",
          fontStyle: "italic",
          fontSize: 14,
          color: "var(--color-ink-muted)",
          letterSpacing: "-0.003em",
          lineHeight: 1.5,
        }}
      >
        {tldr}
      </p>

      <div className="shrink-0 inline-flex items-baseline gap-2.5">
        <span
          className="text-[10.5px] uppercase"
          style={{
            color: "var(--color-ink-faint)",
            letterSpacing: "0.20em",
            fontWeight: 500,
          }}
        >
          Next
        </span>
        <span
          className="font-mono text-[13px] tabular-nums"
          style={{
            color:
              status === "resolved"
                ? "var(--color-ink-faint)"
                : "var(--color-ink)",
            letterSpacing: "0.005em",
          }}
        >
          {next}
        </span>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Synthesizers - derive editorial copy from the raw API case row.
// ──────────────────────────────────────────────────────────────────────

function synthesizeCaseLine(c: ApiCase): string {
  const amt = formatAmount(c.amount_minor, c.currency ?? "usd");
  const kind = c.case_type?.replace(/_/g, " ") ?? "case";
  if (kind === "chargeback") {
    return `vs. an ${amt} chargeback`;
  }
  if (kind === "refund request") {
    return `over an ${amt} refund request`;
  }
  if (kind === "duplicate charge") {
    return `over an alleged ${amt} duplicate charge`;
  }
  return `vs. an ${amt} ${kind}`;
}

function synthesizeDescription(c: ApiCase): string {
  if (c.status === "investigating") {
    return "Manthan is mid-investigation - reading across the connected sources to write the brief.";
  }
  if (c.status === "acting") {
    return "Drafted actions are firing in sequence. Live receipts in the workspace.";
  }
  if (c.status === "awaiting_approval" && c.decision_action) {
    return `Recommends ${humanizeAction(c.decision_action)}. Waiting on your nod to fire the drafted actions.`;
  }
  if (c.status === "resolved") {
    return c.decision_action
      ? `Resolved - ${humanizeAction(c.decision_action)} fired and the customer was notified.`
      : "Resolved.";
  }
  if (c.status === "escalated") {
    return "Escalated to a human - beyond Manthan’s policy envelope.";
  }
  if (c.status === "errored") {
    return "Run errored mid-investigation; check the trace for the failure point.";
  }
  return `${c.case_type?.replace(/_/g, " ") ?? "Case"} from ${c.customer_ref ?? "this customer"}.`;
}

function synthesizeNextAction(c: ApiCase): string {
  if (c.status === "resolved") return "Closed · all actions fired";
  if (c.status === "escalated") return "Awaiting human review";
  if (c.status === "errored") return "Retry from the workspace";
  if (c.status === "investigating") return "Brief in flight";
  if (c.status === "acting") return "Actions firing now";
  // awaiting_approval
  if (c.decision_action) {
    return humanizeAction(c.decision_action);
  }
  return "Awaiting your nod";
}

function synthesizeStatusMeta(c: ApiCase): string | null {
  if (c.status === "investigating") return null;
  if (c.status === "awaiting_approval") return null;
  if (c.status === "resolved" && c.resolved_at) {
    return `${formatAge(c.resolved_at)} ago`;
  }
  return `${formatAge(c.created_at)} ago`;
}

function classifyDecision(c: ApiCase): "credit" | "fight" | "refund" | null {
  if (c.decision_action == null) return null;
  if (c.decision_action === "fight") return "fight";
  if (c.decision_action === "partial_credit") return "credit";
  if (
    c.decision_action === "refund" &&
    c.decision_amount_minor != null &&
    c.amount_minor != null &&
    c.decision_amount_minor < c.amount_minor
  ) {
    return "credit";
  }
  if (c.decision_action === "refund") return "refund";
  return null;
}

function humanizeAction(action: string): string {
  return ACTION_VERB[action] ?? action.replace(/_/g, " ");
}

function humanCount(n: number): string {
  const words = [
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
  ];
  if (n <= 12) return words[n];
  return String(n);
}

// ──────────────────────────────────────────────────────────────────────
// Empty state - the morning-quiet hero. No CTA panel: the desk sits
// empty, the Manthan mark is the only sign of life, and one mono line
// says what's true - cases arrive when a real trigger (Stripe webhook
// or an A2A investigate_dispute call) fires.
// ──────────────────────────────────────────────────────────────────────

function InboxEmptyState() {
  return (
    <div
      // min-h-full + items-center + justify-center keeps the hero
      // centered; on short viewports the parent's overflow-y-auto
      // takes over so nothing clips.
      className="min-h-full w-full flex flex-col items-center justify-center px-4 sm:px-6 lg:px-8 py-10 select-none"
      style={{
        maxWidth: 1240,
        margin: "0 auto",
        gap: "clamp(32px, 6vh, 96px)",
      }}
    >
      <div className="w-full flex flex-col items-center text-center gap-7">
        <Insignia />

        <h2
          className="leading-[0.95]"
          style={{
            fontFamily: "Spectral, serif",
            fontSize: "clamp(56px, 11vw, 128px)",
            color: "var(--color-ink-strong)",
            letterSpacing: "-0.028em",
            fontWeight: 400,
          }}
        >
          Inbox Zero
        </h2>

        <p
          className="leading-[1.2]"
          style={{
            fontFamily: "Geist Mono, ui-monospace, monospace",
            fontSize: 13,
            color: "var(--color-ink-faint)",
            letterSpacing: "0.24em",
            textTransform: "uppercase",
          }}
        >
          New cases land here when a trigger fires
        </p>
      </div>

      <style>{`
        @keyframes pulse-soft {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(0.92); }
        }
        @keyframes radar-pulse {
          0%, 100% { opacity: 0.95; }
          50% { opacity: 0.55; }
        }
      `}</style>
    </div>
  );
}

/**
 * Insignia - the Manthan mark drawn LARGE. Three concentric radar arcs
 * emitting from an emerald core, with a slow halo pulse behind so the
 * empty state breathes instead of sitting frozen.
 */
function Insignia() {
  return (
    <div
      className="relative inline-flex items-center justify-center"
      style={{ width: 124, height: 124 }}
    >
      {/* Halo wash - a soft radial behind the mark, brand-emerald tint. */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(circle at center, var(--color-accent-soft) 0%, rgba(86,207,131,0.00) 70%)",
          animation: "pulse-soft 4s ease-in-out infinite",
        }}
      />

      <svg
        width={116}
        height={116}
        viewBox="0 0 32 32"
        fill="none"
        aria-hidden
        style={{ animation: "radar-pulse 6s ease-in-out infinite" }}
      >
        {/* Outermost arc */}
        <path
          d="M 16 2 A 14 14 0 0 1 16 30"
          stroke="var(--color-ink-ghost)"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
        {/* Middle arc */}
        <path
          d="M 16 6 A 10 10 0 0 1 16 26"
          stroke="var(--color-ink)"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        {/* Inner arc */}
        <path
          d="M 16 11 A 5 5 0 0 1 16 21"
          stroke="var(--color-ink-strong)"
          strokeWidth="2.2"
          strokeLinecap="round"
        />
        {/* Emerald core - the steady center the radar emits from. */}
        <circle
          cx="16"
          cy="16"
          r="2.2"
          fill="var(--color-accent)"
          style={{
            filter: "drop-shadow(0 0 8px var(--color-accent-line))",
          }}
        />
      </svg>
    </div>
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
