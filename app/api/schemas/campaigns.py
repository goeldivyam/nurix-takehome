from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: time
    end: time

    @model_validator(mode="after")
    def _start_before_end(self) -> TimeWindow:
        # Midnight-wrap windows (22:00-02:00) must be split into two rows at the
        # API boundary — see `app/scheduler/business_hours.py`. A wrapping row
        # would silently make the business-hour predicate return False all day.
        if not (self.start < self.end):
            raise ValueError(
                f"TimeWindow.start ({self.start.isoformat()}) must be strictly "
                f"less than end ({self.end.isoformat()})"
            )
        return self


class RetryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(ge=0, le=10)
    backoff_base_seconds: int = Field(ge=1, le=3600)


class CampaignCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=200)
    timezone: str
    schedule: dict[Weekday, list[TimeWindow]]
    max_concurrent: int | None = Field(default=None, ge=1, le=100)
    retry_config: RetryConfig
    phones: list[str] = Field(min_length=1, max_length=10_000)

    @field_validator("timezone")
    @classmethod
    def _tz_valid(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {v}") from exc
        return v

    @field_validator("phones")
    @classmethod
    def _normalize_phones(cls, v: list[str]) -> list[str]:
        # Every entry must parse as international (+cc-prefixed) E.164. Nurix
        # operates India and US campaigns in the same service; a bare
        # 10-digit number is ambiguous between the two dial plans, so we
        # require explicit +cc on every row rather than guessing a default
        # region. The phone-level partial unique index on
        # `(phone) WHERE status IN ('QUEUED','DIALING','IN_PROGRESS')` relies
        # on E.164 — mixed formats would silently defeat the in-flight guard.
        errors: list[dict[str, object]] = []
        normalized: list[str] = []
        for idx, raw in enumerate(v):
            stripped = raw.strip()
            if not stripped.startswith("+"):
                errors.append(
                    {
                        "index": idx,
                        "input": raw,
                        "reason": "missing country code; expected +cc... (e.g. +1... or +91...)",
                    }
                )
                continue
            try:
                parsed = phonenumbers.parse(stripped, None)
            except phonenumbers.NumberParseException as exc:
                errors.append({"index": idx, "input": raw, "reason": str(exc)})
                continue
            if not phonenumbers.is_valid_number(parsed):
                errors.append({"index": idx, "input": raw, "reason": "invalid"})
                continue
            normalized.append(
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            )
        if errors:
            raise ValueError({"invalid_phones": errors})
        if not normalized:
            raise ValueError("zero valid phones after normalization")
        return normalized


class CampaignResponse(BaseModel):
    id: UUID
    name: str
    status: str
    timezone: str
    schedule: dict[str, list[TimeWindow]]
    max_concurrent: int
    retry_config: RetryConfig
    created_at: datetime
    updated_at: datetime


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignResponse]
    next_cursor: str | None


class CampaignStatsResponse(BaseModel):
    total: int
    completed: int
    failed: int
    retries_attempted: int
    in_progress: int


__all__ = [
    "CampaignCreate",
    "CampaignListResponse",
    "CampaignResponse",
    "CampaignStatsResponse",
    "RetryConfig",
    "TimeWindow",
    "Weekday",
]
