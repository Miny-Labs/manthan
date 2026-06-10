"""CaseStore — the state surface A2A reads from.

A2A query skills (get_case, get_brief, ...) and the investigate action both go
through this protocol. manthan-api provides a Postgres-backed implementation;
InMemoryCaseStore backs the unit tests and local demos with zero infra.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CaseStore(Protocol):
    """Everything the A2A surface needs to read or start case work."""

    async def create_investigation(self, trigger: dict[str, Any]) -> str:
        """Start a new investigation from a trigger; return its case id."""
        ...

    async def get_case(self, case_id: str) -> dict[str, Any] | None: ...

    async def list_cases(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def get_brief(self, case_id: str) -> dict[str, Any] | None: ...

    async def get_findings(self, case_id: str) -> list[dict[str, Any]]: ...

    async def get_actions(self, case_id: str) -> list[dict[str, Any]]: ...

    async def get_audit_trail(self, case_id: str) -> list[dict[str, Any]]: ...

    # ---- advisor surface (skills v2) ----

    async def ask(self, question: str, case_id: str | None = None) -> dict[str, Any]:
        """Answer a question grounded in one case (case_id) or the portfolio."""
        ...

    async def precheck_refund(
        self, customer_ref: str, amount_minor: int
    ) -> dict[str, Any]:
        """Approval gate + prior history for a refund another agent wants to issue."""
        ...

    async def get_customer_history(self, customer_ref: str) -> list[dict[str, Any]]:
        """Every case on file for a customer reference."""
        ...

    async def dispute_exposure(self) -> dict[str, Any]:
        """Portfolio roll-up: open cases + total disputed amount at risk."""
        ...

    async def contribute_evidence(
        self, case_id: str, evidence: dict[str, Any], contributor: str
    ) -> dict[str, Any]:
        """Append an external evidence record to a case's audit trail."""
        ...


class InMemoryCaseStore:
    """Dict-backed CaseStore for tests + local demos.

    Seed it with whole case records of the shape:
        {
          "case_id": "...", "short_id": "...", "status": "...",
          "customer_ref": "...", "decision": {...},
          "brief": {...}, "findings": [...], "actions": [...], "events": [...],
        }
    """

    def __init__(self, cases: dict[str, dict[str, Any]] | None = None) -> None:
        self._cases: dict[str, dict[str, Any]] = dict(cases or {})
        self._seq = 0

    # ---- writes ----

    def seed(self, case: dict[str, Any]) -> None:
        self._cases[case["case_id"]] = case

    async def create_investigation(self, trigger: dict[str, Any]) -> str:
        self._seq += 1
        case_id = trigger.get("case_id") or f"case-{self._seq:06d}"
        self._cases[case_id] = {
            "case_id": case_id,
            "short_id": trigger.get("short_id", f"C-{self._seq:04d}"),
            "status": "queued",
            "customer_ref": trigger.get("customer_ref", ""),
            "trigger": trigger,
            "brief": None,
            "findings": [],
            "actions": [],
            "events": [{"type": "case_opened", "data": trigger}],
        }
        return case_id

    # ---- reads ----

    async def get_case(self, case_id: str) -> dict[str, Any] | None:
        c = self._cases.get(case_id)
        if c is None:
            return None
        # Return the summary projection (not the heavy nested artifacts).
        return {
            "case_id": c["case_id"],
            "short_id": c.get("short_id"),
            "status": c.get("status"),
            "customer_ref": c.get("customer_ref"),
            "decision": c.get("decision") or (c.get("brief") or {}).get("decision"),
        }

    async def list_cases(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in self._cases.values():
            if status and c.get("status") != status:
                continue
            out.append({
                "case_id": c["case_id"],
                "short_id": c.get("short_id"),
                "status": c.get("status"),
                "customer_ref": c.get("customer_ref"),
            })
            if len(out) >= limit:
                break
        return out

    async def get_brief(self, case_id: str) -> dict[str, Any] | None:
        c = self._cases.get(case_id)
        return c.get("brief") if c else None

    async def get_findings(self, case_id: str) -> list[dict[str, Any]]:
        c = self._cases.get(case_id)
        return list(c.get("findings", [])) if c else []

    async def get_actions(self, case_id: str) -> list[dict[str, Any]]:
        c = self._cases.get(case_id)
        return list(c.get("actions", [])) if c else []

    async def get_audit_trail(self, case_id: str) -> list[dict[str, Any]]:
        c = self._cases.get(case_id)
        return list(c.get("events", [])) if c else []

    # ---- advisor surface (skills v2) ----
    # Simple, deterministic logic over the seeded records: enough for the
    # protocol tests + local demos; PgCaseStore supplies the real queries.

    # HITL gate thresholds (minor units) — mirrors types.HitlGate's locked
    # bands: <$50 auto, $50-$500 one-click, >=$500 two-person.
    _GATE_AUTO_BELOW = 5_000
    _GATE_ONE_CLICK_BELOW = 50_000
    _CLOSED_STATUSES = frozenset({"resolved", "errored"})

    @staticmethod
    def _decision_of(c: dict[str, Any]) -> dict[str, Any]:
        d = c.get("decision") or (c.get("brief") or {}).get("decision") or {}
        return d if isinstance(d, dict) else {}

    async def ask(self, question: str, case_id: str | None = None) -> dict[str, Any]:
        if case_id:
            c = self._cases.get(case_id)
            if c is None:
                return {
                    "question": question, "case_id": case_id,
                    "answer": f"No case '{case_id}' on file.", "grounded": False,
                }
            decision = self._decision_of(c)
            brief = c.get("brief") or {}
            parts = [f"Case {c.get('short_id') or case_id} is {c.get('status', 'unknown')}."]
            if decision.get("action"):
                amt = decision.get("amount_minor")
                parts.append(
                    f"Decision: {decision['action']}"
                    + (f" for {int(amt)} minor units" if amt else "") + "."
                )
            if brief.get("tldr"):
                parts.append(str(brief["tldr"]))
            cited = sorted({
                i for f in c.get("findings", []) if isinstance(f, dict)
                for i in f.get("citations", []) if isinstance(i, int)
            })
            return {
                "question": question, "case_id": case_id,
                "answer": " ".join(parts), "grounded": True,
                "findings_count": len(c.get("findings", [])),
                "evidence_cited": cited,
            }
        open_ids = [
            c["case_id"] for c in self._cases.values()
            if c.get("status") not in self._CLOSED_STATUSES
        ]
        return {
            "question": question, "case_id": None,
            "answer": (
                f"{len(self._cases)} case(s) on file, {len(open_ids)} open. "
                "Pass case_id for a case-grounded answer."
            ),
            "grounded": True, "open_case_ids": open_ids,
        }

    async def precheck_refund(
        self, customer_ref: str, amount_minor: int
    ) -> dict[str, Any]:
        amount = int(amount_minor or 0)
        if amount < self._GATE_AUTO_BELOW:
            gate = "auto"
        elif amount < self._GATE_ONE_CLICK_BELOW:
            gate = "one-click"
        else:
            gate = "two-person"
        history = await self.get_customer_history(customer_ref)
        open_cases = [h for h in history if h.get("status") not in self._CLOSED_STATUSES]
        prior_refunded = sum(
            int(h["decision"].get("amount_minor") or 0)
            for h in history
            if isinstance(h.get("decision"), dict) and h["decision"].get("action") == "refund"
        )
        return {
            "customer_ref": customer_ref,
            "amount_minor": amount,
            "gate": gate,
            # An open case means context an auto-refund would bypass.
            "auto_approvable": gate == "auto" and not open_cases,
            "prior_cases": len(history),
            "open_cases": len(open_cases),
            "prior_refunded_minor": prior_refunded,
        }

    async def get_customer_history(self, customer_ref: str) -> list[dict[str, Any]]:
        if not customer_ref:
            return []
        return [
            {
                "case_id": c["case_id"],
                "short_id": c.get("short_id"),
                "status": c.get("status"),
                "decision": self._decision_of(c) or None,
            }
            for c in self._cases.values()
            if c.get("customer_ref") == customer_ref
        ]

    async def dispute_exposure(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        open_ids: list[str] = []
        total = 0
        for c in self._cases.values():
            status = c.get("status") or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            if status in self._CLOSED_STATUSES:
                continue
            open_ids.append(c["case_id"])
            total += int(self._decision_of(c).get("amount_minor") or 0)
        return {
            "open_cases": len(open_ids),
            "total_exposure_minor": total,
            "currency": "usd",
            "by_status": by_status,
            "open_case_ids": open_ids,
        }

    async def contribute_evidence(
        self, case_id: str, evidence: dict[str, Any], contributor: str
    ) -> dict[str, Any]:
        c = self._cases.get(case_id)
        if c is None:
            return {
                "ok": False, "recorded": False, "case_id": case_id,
                "error": f"unknown case '{case_id}'",
            }
        entry = {
            "evidence": dict(evidence or {}),
            "contributor": contributor or "unknown",
        }
        c.setdefault("contributed_evidence", []).append(entry)
        c.setdefault("events", []).append({
            "type": "evidence_contributed",
            "actor": f"external:{entry['contributor']}",
            "data": entry,
        })
        return {
            "ok": True, "recorded": True, "case_id": case_id,
            "evidence_count": len(c["contributed_evidence"]),
        }
