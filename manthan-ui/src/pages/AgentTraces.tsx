/**
 * Agent Traces - the observability surface at /app/traces.
 *
 * Left rail: recent cases (same /api/cases list the inbox reads).
 * Right panel: the selected case's event timeline rendered as a span
 * tree - each tool_call paired with its tool_result (matched on
 * data.tool_call_id), with a wall-clock duration and an ok/error/pending
 * status; finding_recorded rows highlighted as the committed evidence;
 * brief_drafted as the terminal node of the run.
 *
 * When an event carries a trace_id (the agent stamps OTel ids when an
 * exporter is configured), the row links out to Cloud Trace. Rows
 * without one simply omit the link - no fake telemetry.
 */

import { useEffect, useMemo, useState } from "react";
import { ExternalLink } from "lucide-react";

import {
  formatAge,
  listCaseEvents,
  listCases,
  type ApiCase,
  type ApiCaseEvent,
  type CaseStatus,
} from "@/lib/api";

// Match the inbox status palette so the rail reads the same.
const STATUS_COLOR: Record<CaseStatus, string> = {
  investigating: "var(--color-info)",
  awaiting_approval: "var(--color-amber)",
  acting: "var(--color-amber)",
  resolved: "var(--color-accent)",
  errored: "var(--color-danger)",
  escalated: "var(--color-danger)",
};

// ──────────────────────────────────────────────────────────────────────
// Page
// ──────────────────────────────────────────────────────────────────────

export default function AgentTraces() {
  const [cases, setCases] = useState<ApiCase[] | null>(null);
  const [casesError, setCasesError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listCases({ limit: 25 })
      .then((r) => {
        if (cancelled) return;
        const sorted = [...r.cases].sort(
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
        setCases(sorted);
        // Auto-select the most recent case so the panel isn't blank.
        if (sorted.length > 0) {
          setSelectedId((prev) => prev ?? sorted[0].id);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setCasesError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selected = useMemo(
    () => cases?.find((c) => c.id === selectedId) ?? null,
    [cases, selectedId],
  );

  return (
    <div
      className="h-full w-full overflow-y-auto"
      style={{ background: "var(--color-bg)" }}
    >
      <div
        className="mx-auto flex flex-col px-4 sm:px-6 py-8 sm:py-10"
        style={{
          maxWidth: 1180,
          paddingBottom: 64,
          color: "var(--color-ink-strong)",
        }}
      >
        <header className="flex flex-col gap-5">
          <Eyebrow>Traces</Eyebrow>
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
            Every run, span by span.
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
            Pick a case to replay its investigation: each tool call paired
            with its result, the findings it committed, and the brief it
            ended on. Rows stamped with a trace id link out to Cloud Trace.
          </p>
        </header>

        {casesError && (
          <p
            className="mt-8 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-danger)",
            }}
          >
            Couldn’t load cases: {casesError}
          </p>
        )}

        {cases === null && !casesError && (
          <p
            className="mt-12 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-ink-faint)",
            }}
          >
            Loading recent cases…
          </p>
        )}

        {cases !== null && cases.length === 0 && (
          <p
            className="mt-12 text-[14px] italic"
            style={{
              fontFamily: "Spectral, serif",
              color: "var(--color-ink-faint)",
            }}
          >
            No cases yet - traces appear once the agent runs its first
            investigation.
          </p>
        )}

        {cases !== null && cases.length > 0 && (
          <div
            className="mt-12 grid grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)]"
            style={{
              border: "1px solid var(--color-rule)",
              borderRadius: 6,
              overflow: "hidden",
            }}
          >
            <CaseRail
              cases={cases}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            <SpanPanel selected={selected} />
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// CaseRail - hairline rows of recent cases. Selected row carries a
// 2px accent left rule, same grammar as the sidebar's active mark.
// ──────────────────────────────────────────────────────────────────────

function CaseRail({
  cases,
  selectedId,
  onSelect,
}: {
  cases: ApiCase[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div
      className="lg:border-r border-b lg:border-b-0 max-h-[300px] lg:max-h-none overflow-y-auto"
      style={{ borderColor: "var(--color-rule-soft)" }}
    >
      <div
        className="px-4 py-2.5 border-b sticky top-0"
        style={{
          borderColor: "var(--color-rule-soft)",
          background: "var(--color-bg)",
        }}
      >
        <span
          className="font-mono text-[10.5px] uppercase tabular-nums"
          style={{ color: "var(--color-ink-ghost)", letterSpacing: "0.18em" }}
        >
          Recent cases · {cases.length}
        </span>
      </div>
      <ol className="flex flex-col">
        {cases.map((c, i) => {
          const active = c.id === selectedId;
          return (
            <li
              key={c.id}
              style={{
                borderTop: i === 0 ? "none" : "1px solid var(--color-rule-soft)",
              }}
            >
              <button
                type="button"
                onClick={() => onSelect(c.id)}
                className="w-full text-left px-4 py-3 transition-colors hover:bg-[var(--color-surface)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-line)]"
                style={{
                  background: active ? "var(--color-surface-2)" : "transparent",
                  boxShadow: active
                    ? "inset 2px 0 0 var(--color-accent)"
                    : "none",
                }}
              >
                <span className="flex items-baseline justify-between gap-3">
                  <span
                    className="font-mono text-[11.5px] uppercase tabular-nums"
                    style={{
                      color: active
                        ? "var(--color-ink-strong)"
                        : "var(--color-ink-muted)",
                      letterSpacing: "0.10em",
                    }}
                  >
                    {c.short_id}
                  </span>
                  <span
                    className="font-mono text-[10.5px] tabular-nums shrink-0"
                    style={{ color: "var(--color-ink-ghost)" }}
                  >
                    {formatAge(c.created_at)} ago
                  </span>
                </span>
                <span className="mt-1 flex items-baseline justify-between gap-3">
                  <span
                    className="text-[13.5px] truncate min-w-0"
                    style={{
                      fontFamily: "Spectral, serif",
                      color: "var(--color-ink)",
                      letterSpacing: "0.003em",
                    }}
                  >
                    {c.customer_ref ?? "Unknown customer"}
                  </span>
                  <span
                    className="text-[9.5px] uppercase shrink-0"
                    style={{
                      color: STATUS_COLOR[c.status],
                      letterSpacing: "0.18em",
                      fontWeight: 500,
                    }}
                  >
                    {c.status.replace(/_/g, " ")}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// SpanPanel - fetch the selected case's events, fold them into spans.
// ──────────────────────────────────────────────────────────────────────

function SpanPanel({ selected }: { selected: ApiCase | null }) {
  const [events, setEvents] = useState<ApiCaseEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setEvents(null);
    setError(null);
    listCaseEvents(selected.id)
      .then((r) => {
        if (!cancelled) setEvents(r.events);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const spans = useMemo(() => (events ? buildSpans(events) : null), [events]);

  if (!selected) {
    return (
      <PanelNote>Select a case on the left to replay its run.</PanelNote>
    );
  }

  return (
    <div className="min-w-0">
      <div
        className="flex items-baseline gap-3 px-6 py-2.5 border-b flex-wrap"
        style={{ borderColor: "var(--color-rule-soft)" }}
      >
        <span
          className="font-mono text-[11px] uppercase tabular-nums"
          style={{ color: "var(--color-ink-muted)", letterSpacing: "0.14em" }}
        >
          CASE {selected.short_id}
        </span>
        <span style={{ color: "var(--color-rule-strong)" }} aria-hidden>
          ·
        </span>
        <span
          className="text-[13.5px]"
          style={{
            fontFamily: "Spectral, serif",
            color: "var(--color-ink)",
          }}
        >
          {selected.customer_ref ?? "Unknown customer"}
        </span>
        {spans && (
          <span
            className="ml-auto font-mono text-[10.5px] uppercase tabular-nums"
            style={{ color: "var(--color-ink-ghost)", letterSpacing: "0.16em" }}
          >
            {spans.filter((s) => s.kind === "tool").length} tool spans ·{" "}
            {spans.filter((s) => s.kind === "finding").length} findings
          </span>
        )}
      </div>

      {error && (
        <PanelNote danger>Couldn’t load the timeline: {error}</PanelNote>
      )}
      {events === null && !error && <PanelNote>Replaying the run…</PanelNote>}
      {spans !== null && spans.length === 0 && (
        <PanelNote>No events recorded for this case yet.</PanelNote>
      )}

      {spans !== null && spans.length > 0 && (
        <ol className="flex flex-col px-6 py-5">
          {spans.map((s, i) => (
            <SpanRow key={`${s.seq}-${i}`} span={s} last={i === spans.length - 1} />
          ))}
        </ol>
      )}
    </div>
  );
}

function PanelNote({
  children,
  danger,
}: {
  children: React.ReactNode;
  danger?: boolean;
}) {
  return (
    <p
      className="px-6 py-10 text-[14px] italic"
      style={{
        fontFamily: "Spectral, serif",
        color: danger ? "var(--color-danger)" : "var(--color-ink-faint)",
      }}
    >
      {children}
    </p>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Span model - fold the raw event stream into renderable spans.
// ──────────────────────────────────────────────────────────────────────

type SpanKind = "root" | "tool" | "finding" | "brief" | "close" | "fault";

interface Span {
  kind: SpanKind;
  seq: number;
  title: string;
  detail: string | null;
  status: "ok" | "error" | "pending" | null;
  durationMs: number | null;
  traceId: string | null;
  at: string;
}

function buildSpans(events: ApiCaseEvent[]): Span[] {
  const sorted = [...events].sort((a, b) => a.seq - b.seq);

  // Index tool_results by the call id they answer.
  const resultByCall = new Map<string, ApiCaseEvent>();
  for (const e of sorted) {
    if (e.type !== "tool_result") continue;
    const callId = asString(e.data["tool_call_id"]);
    if (callId) resultByCall.set(callId, e);
  }

  const out: Span[] = [];
  for (const e of sorted) {
    const traceId = readTraceId(e);
    switch (e.type) {
      case "case_opened":
      case "investigation_started": {
        out.push({
          kind: "root",
          seq: e.seq,
          title: e.type,
          detail:
            e.summary ??
            truncate(asString(e.data["text"]) ?? "", 160) ??
            null,
          status: null,
          durationMs: null,
          traceId,
          at: e.created_at,
        });
        break;
      }
      case "tool_call": {
        const callId = asString(e.data["id"]);
        const result = callId ? resultByCall.get(callId) : undefined;
        const status =
          result === undefined
            ? ("pending" as const)
            : resultIsError(result)
              ? ("error" as const)
              : ("ok" as const);
        out.push({
          kind: "tool",
          seq: e.seq,
          title: asString(e.data["name"]) ?? "tool",
          detail: e.summary ?? argsPreview(e.data["arguments"]),
          status,
          durationMs: result
            ? Math.max(
                0,
                new Date(result.created_at).getTime() -
                  new Date(e.created_at).getTime(),
              )
            : null,
          traceId: traceId ?? (result ? readTraceId(result) : null),
          at: e.created_at,
        });
        break;
      }
      case "finding_recorded": {
        out.push({
          kind: "finding",
          seq: e.seq,
          title: "finding_recorded",
          detail: asString(e.data["text"]) ?? e.summary,
          status: "ok",
          durationMs: null,
          traceId,
          at: e.created_at,
        });
        break;
      }
      case "brief_drafted": {
        out.push({
          kind: "brief",
          seq: e.seq,
          title: "brief_drafted",
          detail:
            e.summary ??
            "Brief drafted - the terminal artifact of the investigation.",
          status: "ok",
          durationMs: null,
          traceId,
          at: e.created_at,
        });
        break;
      }
      case "case_closed": {
        out.push({
          kind: "close",
          seq: e.seq,
          title: "case_closed",
          detail: e.summary ?? asString(e.data["reason"]),
          status: null,
          durationMs: null,
          traceId,
          at: e.created_at,
        });
        break;
      }
      case "error": {
        out.push({
          kind: "fault",
          seq: e.seq,
          title: "error",
          detail: e.summary ?? asString(e.data["reason"]),
          status: "error",
          durationMs: null,
          traceId,
          at: e.created_at,
        });
        break;
      }
      default:
        // tool_result rows merge into their tool_call span; thoughts /
        // reflexions stay in the workspace narrative, not the trace.
        break;
    }
  }
  return out;
}

/** trace_id lands either in the event's data payload or as a top-level
 *  column, depending on the writer's version. Check both; null if absent. */
function readTraceId(e: ApiCaseEvent): string | null {
  const fromData = asString(e.data["trace_id"]);
  if (fromData) return fromData;
  const top = (e as unknown as Record<string, unknown>)["trace_id"];
  return asString(top);
}

function resultIsError(e: ApiCaseEvent): boolean {
  const result = e.data["result"];
  if (result && typeof result === "object" && !Array.isArray(result)) {
    const r = result as Record<string, unknown>;
    if (r["status"] === "error") return true;
    if (typeof r["error"] === "string" && r["error"].length > 0) return true;
  }
  return false;
}

function argsPreview(args: unknown): string | null {
  if (!args || typeof args !== "object") return null;
  const obj = args as Record<string, unknown>;
  const q = asString(obj["query"]) ?? asString(obj["text"]);
  if (q) return truncate(q, 160);
  try {
    const s = JSON.stringify(obj);
    return s === "{}" ? null : truncate(s, 160);
  } catch {
    return null;
  }
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

// ──────────────────────────────────────────────────────────────────────
// SpanRow - one node of the tree. Root + terminals sit on the spine;
// tool spans and findings indent one level under it. Findings carry
// the accent rule, the brief gets the terminal treatment.
// ──────────────────────────────────────────────────────────────────────

const STATUS_TONE: Record<NonNullable<Span["status"]>, string> = {
  ok: "var(--color-accent)",
  error: "var(--color-danger)",
  pending: "var(--color-amber)",
};

function SpanRow({ span, last }: { span: Span; last: boolean }) {
  const indented = span.kind === "tool" || span.kind === "finding";
  const isFinding = span.kind === "finding";
  const isBrief = span.kind === "brief";
  const isFault = span.kind === "fault";

  return (
    <li className="relative flex gap-4" style={{ paddingLeft: indented ? 26 : 0 }}>
      {/* Tree spine - a hairline that runs the full column; the last
          row stops it so the tree visibly terminates. */}
      <span
        aria-hidden
        className="absolute"
        style={{
          left: 7,
          top: 0,
          bottom: last ? "50%" : 0,
          width: 1,
          background: "var(--color-rule-soft)",
        }}
      />
      {/* Node dot */}
      <span
        aria-hidden
        className="absolute"
        style={{
          left: indented ? 26 - 3 : 4,
          top: 17,
          width: indented ? 7 : 9,
          height: indented ? 7 : 9,
          borderRadius: 999,
          background: isFinding
            ? "var(--color-accent)"
            : isBrief
              ? "var(--color-ink-strong)"
              : isFault
                ? "var(--color-danger)"
                : "var(--color-bg)",
          border: `1px solid ${
            isFinding
              ? "var(--color-accent)"
              : isBrief
                ? "var(--color-ink-strong)"
                : isFault
                  ? "var(--color-danger)"
                  : "var(--color-rule-strong)"
          }`,
        }}
      />
      {/* Elbow connecting an indented node back to the spine */}
      {indented && (
        <span
          aria-hidden
          className="absolute"
          style={{
            left: 8,
            top: 20,
            width: 14,
            height: 1,
            background: "var(--color-rule-soft)",
          }}
        />
      )}

      <div
        className="flex-1 min-w-0 py-2.5"
        style={{
          paddingLeft: 16,
          background: isFinding
            ? "color-mix(in oklch, var(--color-accent) 5%, transparent)"
            : "transparent",
          borderRadius: isFinding ? 4 : 0,
        }}
      >
        <div className="flex items-baseline gap-3 flex-wrap">
          <span
            className="font-mono text-[10.5px] tabular-nums shrink-0"
            style={{ color: "var(--color-ink-ghost)" }}
          >
            #{String(span.seq).padStart(3, "0")}
          </span>

          {isBrief || span.kind === "root" || span.kind === "close" ? (
            <span
              className="text-[15px]"
              style={{
                fontFamily: "Spectral, serif",
                fontStyle: "italic",
                color: isBrief
                  ? "var(--color-ink-strong)"
                  : "var(--color-ink)",
                letterSpacing: "-0.004em",
                fontWeight: isBrief ? 500 : 400,
              }}
            >
              {span.title}
            </span>
          ) : (
            <span
              className="font-mono text-[12.5px] tabular-nums"
              style={{
                color: isFinding
                  ? "var(--color-accent)"
                  : isFault
                    ? "var(--color-danger)"
                    : "var(--color-ink-strong)",
                letterSpacing: "0.02em",
              }}
            >
              {span.title}
            </span>
          )}

          {isBrief && (
            <span
              className="text-[9.5px] uppercase"
              style={{
                color: "var(--color-ink-faint)",
                letterSpacing: "0.20em",
                fontWeight: 500,
              }}
            >
              terminal
            </span>
          )}

          <span className="ml-auto inline-flex items-baseline gap-3 shrink-0">
            {span.traceId && (
              <a
                href={`https://console.cloud.google.com/traces/list?tid=${encodeURIComponent(span.traceId)}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono text-[10px] uppercase hover:underline"
                style={{
                  color: "var(--color-info)",
                  letterSpacing: "0.12em",
                  textUnderlineOffset: 3,
                }}
                title={`Cloud Trace · ${span.traceId}`}
              >
                <ExternalLink size={10} strokeWidth={1.8} />
                Cloud Trace
              </a>
            )}
            {span.durationMs !== null && (
              <span
                className="font-mono text-[11px] tabular-nums"
                style={{ color: "var(--color-ink-faint)" }}
              >
                {formatDuration(span.durationMs)}
              </span>
            )}
            {span.status && (
              <span
                className="text-[9.5px] uppercase"
                style={{
                  color: STATUS_TONE[span.status],
                  letterSpacing: "0.18em",
                  fontWeight: 500,
                }}
              >
                {span.status}
              </span>
            )}
          </span>
        </div>

        {span.detail && (
          <p
            className="mt-1 text-[13px] leading-[1.5] truncate"
            style={{
              color: isFinding ? "var(--color-ink)" : "var(--color-ink-muted)",
              fontFamily: isFinding ? "Spectral, serif" : undefined,
              fontStyle: isFinding ? "italic" : undefined,
            }}
            title={span.detail}
          >
            {span.detail}
          </p>
        )}
      </div>
    </li>
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
