from __future__ import annotations

from pydantic import BaseModel


class WebhookIngestResponse(BaseModel):
    received: bool
