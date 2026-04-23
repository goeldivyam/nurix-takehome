from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


async def handle_webhook_ingest(
    deps: Any,
    *,
    provider: str,
    payload: dict[str, Any],
    raw_body: bytes,
    headers: dict[str, str],
) -> dict[str, Any]:
    # Stubbed for P0. Real body lands in P3B:
    #  1. verify_signature(headers, raw_body) -> 401 on fail
    #  2. INSERT into webhook_inbox (idempotent on UNIQUE(provider, event_id))
    #  3. commit, then spawn process_pending_inbox as a tracked task
    raise NotImplementedError("handle_webhook_ingest is implemented in P3B")
