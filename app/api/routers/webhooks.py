from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.schemas.webhook import WebhookIngestResponse
from app.api.webhooks_ingest import handle_webhook_ingest
from app.deps import Deps

router = APIRouter(tags=["webhooks"])


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


# Using the Annotated[..., Depends(...)] form keeps FastAPI's DI idiom
# while avoiding the B008 "function call in default argument" lint.
DepsDep = Annotated[Deps, Depends(get_deps)]


@router.post("/webhooks/provider", response_model=WebhookIngestResponse)
async def receive_webhook(
    request: Request,
    deps: DepsDep,
) -> WebhookIngestResponse:
    # Raw body is needed for signature verification — JSON round-trip would
    # change byte-level whitespace and invalidate the HMAC on real adapters.
    raw_body = await request.body()
    try:
        payload: Any = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    headers = {k.lower(): v for k, v in request.headers.items()}
    result = await handle_webhook_ingest(
        deps,
        provider="mock",
        payload=payload,
        raw_body=raw_body,
        headers=headers,
    )
    return WebhookIngestResponse(**result)
