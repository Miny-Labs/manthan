"""Slack thread notifier — RETIRED surface, kept as an explicit no-op.

The Slack-native trigger surface (Events API router + slack_bot service) was
removed in the Track 3 refactor: cases can no longer originate from Slack
mentions/DMs, so there is no thread to mirror events back into. Remaining
callers (the actor worker) still call these hooks behind try/except, so the
module keeps its public API and simply does nothing.

Slack as an ACTION remains fully supported — the actor's `slack_brief`
action kind posts via adapters/slack.py after human approval. Only the
conversational notifier surface is retired.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger("services.slack_notifier")


async def maybe_notify(
    *,
    org_id: UUID,
    thread_id: UUID,
    case_id: UUID,
    event_type: str,
    event_data: dict[str, Any] | None = None,
) -> None:
    """No-op: Slack-originated cases no longer exist."""
    logger.debug(
        "slack notifier retired - skipping %s for case %s", event_type, case_id
    )


async def maybe_notify_case_closed_card(
    *,
    org_id: UUID,
    case_id: UUID,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """No-op: Slack-originated cases no longer exist."""
    logger.debug("slack notifier retired - skipping close card for case %s", case_id)
