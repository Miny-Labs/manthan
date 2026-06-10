"""In-memory event store.

`run_case` (loop.py) appends every Event it yields here; the script
harnesses (`scripts/test_investigate.py`) read it back to assert on the
log. In production the investigator agent service persists the same
Event stream to Postgres via `manthan-api`'s `services/case_store.py` —
this in-memory store stays the framework-free contract for tests.

The event log is the single source of truth. Case state (current step,
retry count, what's been tried) is derived from the log, not stored
separately. This is the 12-Factor Agents pattern (#3 + #5).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .types import Event


class EventStore:
    """Append-only event log, keyed by case_id.

    Replace with a Postgres-backed implementation when we add the
    `events` table from the engineering plan. The interface stays
    identical.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}

    def append(
        self,
        case_id: str,
        kind: str,
        actor: str,
        data: dict[str, Any],
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Event:
        """Append one event. Auto-assigns the next seq number."""
        events = self._events.setdefault(case_id, [])
        evt = Event(
            case_id=case_id,
            seq=len(events),
            kind=kind,  # type: ignore[arg-type]
            actor=actor,
            data=data,
            ts=datetime.utcnow(),
            trace_id=trace_id,
            span_id=span_id,
        )
        events.append(evt)
        return evt

    def list_for_case(self, case_id: str) -> list[Event]:
        return list(self._events.get(case_id, []))

    def filter_for_case(
        self, case_id: str, kinds: Iterable[str]
    ) -> list[Event]:
        kinds_set = set(kinds)
        return [e for e in self._events.get(case_id, []) if e.kind in kinds_set]

    def all_cases(self) -> list[str]:
        return list(self._events.keys())
