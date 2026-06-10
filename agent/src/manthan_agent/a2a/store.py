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
