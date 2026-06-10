"""Worker entry point - `uv run python -m manthan_api.workers.main`.

Runs the deterministic workers only:
  actor      - drains approved actions and fires real writes
  prettifier - turns raw events into one-line human summaries

The old investigate worker (PG LISTEN 'manthan_event' -> agent -> event
mirror) is RETIRED. The investigator agent now writes its own events and
projections through manthan_api.services.case_store.
"""

from __future__ import annotations

import asyncio


async def _run_workers() -> None:
    from manthan_api.workers.actor import main as actor_main
    from manthan_api.workers.prettifier import main as prettifier_main

    await asyncio.gather(actor_main(), prettifier_main())


def main() -> None:
    asyncio.run(_run_workers())


if __name__ == "__main__":
    main()
