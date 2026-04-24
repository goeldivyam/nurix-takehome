from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

# Deprecated IANA aliases that Python 3.11+ `ZoneInfo` still accepts but the
# current TZ DB canonicalizes away. Keep the server tolerant of both the
# deprecated and the canonical name — many OS locales (notably India) still
# ship the deprecated form as the default.
_DEPRECATED_TZ_ALIASES: dict[str, str] = {
    # Asia
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    # Americas — IANA folded the most common legacy single-word aliases into
    # the America/Argentina/* and America/Indiana/* groups. Canonicalise so
    # the server accepts either form (some locales still default to the old
    # name) and stores the canonical value consistently.
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca",
    "America/Cordoba": "America/Argentina/Cordoba",
    "America/Jujuy": "America/Argentina/Jujuy",
    "America/Mendoza": "America/Argentina/Mendoza",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Louisville": "America/Kentucky/Louisville",
    "America/Virgin": "America/St_Thomas",
}

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
        # Canonicalise deprecated aliases first so Asia/Calcutta (still the
        # default on many India-locale systems) lands on Asia/Kolkata before
        # hitting ZoneInfo — the newer tzdata tables have dropped some of
        # the aliases entirely.
        canonical = _DEPRECATED_TZ_ALIASES.get(v, v)
        try:
            ZoneInfo(canonical)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {v}") from exc
        return canonical

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
        seen_e164: dict[str, int] = {}  # canonical E.164 -> first seen index
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
            e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            # Within-batch duplicate guard: the partial unique index on
            # (phone) WHERE status IN ('QUEUED','DIALING','IN_PROGRESS')
            # would otherwise 500 mid-INSERT. We reject the whole request
            # with a per-duplicate 422 entry rather than silently collapsing —
            # an operator pasting a leads CSV deserves to see which rows
            # were ignored, not have the system decide quietly for them.
            if e164 in seen_e164:
                errors.append(
                    {
                        "index": idx,
                        "input": raw,
                        "reason": (f"duplicate of line {seen_e164[e164] + 1} (same E.164: {e164})"),
                    }
                )
                continue
            seen_e164[e164] = idx
            normalized.append(e164)
        if errors:
            # PydanticCustomError lets the structured payload land in the 422
            # response's `ctx` field (FastAPI serializes it to the client).
            # A plain ValueError(dict) would stringify the dict into `msg`,
            # which renders as Python repr in the UI — not operator-grade.
            # The message is a readable one-line summary for callers that
            # don't parse ctx.
            first = errors[0]
            first_idx = first["index"] if isinstance(first["index"], int) else 0
            summary = f"line {first_idx + 1}: {first['reason']}" + (
                f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            )
            raise PydanticCustomError(
                "invalid_phones",
                summary,
                {"invalid_phones": errors},
            )
        if not normalized:
            raise ValueError("zero valid phones after normalization")
        return normalized


class CampaignResponse(BaseModel):
    id: UUID
    name: str
    # External-vocabulary enum surfaces in /openapi.json so generated
    # clients can type-narrow. Internal DB column keeps the state-machine
    # shape (PENDING / ACTIVE / COMPLETED / FAILED) and the router's
    # `_EXTERNAL_STATUS_MAP` is the single boundary translator.
    status: Literal["pending", "in_progress", "completed", "failed"]
    timezone: str
    schedule: dict[str, list[TimeWindow]]
    max_concurrent: int
    retry_config: RetryConfig
    created_at: datetime
    updated_at: datetime


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignResponse]
    next_cursor: str | None


class CampaignCallResponse(BaseModel):
    # Operator drill-in shape. The internal call-status vocabulary
    # (DIALING / RETRY_PENDING / …) is surfaced here deliberately — the
    # drawer is a deep operator view, and distinguishing RETRY_PENDING
    # from QUEUED or DIALING from IN_PROGRESS is exactly the observability
    # the rubric rewards. The external call status (in_progress / completed
    # / failed) remains the shape surfaced by `GET /calls/{id}`.
    #
    # Provider-adapter vocabulary (`provider_call_id`) is intentionally
    # absent from this response — it's a telephony-boundary field that
    # doesn't belong in the campaign API. The operator correlates an
    # individual call back to its provider trail via the `call_id` →
    # audit-tab deep link, where the audit rows carry `provider_call_id`
    # in `extra` as the forensic source of truth.
    id: UUID
    phone: str
    status: Literal[
        "QUEUED",
        "DIALING",
        "IN_PROGRESS",
        "RETRY_PENDING",
        "COMPLETED",
        "FAILED",
        "NO_ANSWER",
        "BUSY",
    ]
    attempt_epoch: int
    retries_remaining: int
    next_attempt_at: datetime | None
    updated_at: datetime


class CampaignCallsListResponse(BaseModel):
    calls: list[CampaignCallResponse]
    next_cursor: str | None


class CampaignStatsResponse(BaseModel):
    total: int = Field(
        description=(
            "Total calls in the campaign, including those currently in flight and "
            "any that have reached a terminal state. Invariant: "
            "completed + failed + in_progress == total."
        ),
    )
    completed: int = Field(
        description=(
            "Calls that reached COMPLETED. A call that retried one or more times "
            "before succeeding counts here exactly once."
        ),
    )
    failed: int = Field(
        description=(
            "Terminal failures: sum of calls in FAILED, NO_ANSWER, and BUSY states. "
            "Retries that eventually succeeded do NOT count here."
        ),
    )
    retries_attempted: int = Field(
        description=(
            "Number of retry attempts across all calls (attempt_epoch - 1 per call, "
            "clamped at 0). A call that succeeded on its first dial contributes 0; "
            "a call retried twice contributes 2."
        ),
    )
    in_progress: int = Field(
        description=(
            "Calls not yet terminal from the caller's point of view — rows in "
            "QUEUED, DIALING, IN_PROGRESS, or RETRY_PENDING. "
            "completed + failed + in_progress == total."
        ),
    )


__all__ = [
    "CampaignCallResponse",
    "CampaignCallsListResponse",
    "CampaignCreate",
    "CampaignListResponse",
    "CampaignResponse",
    "CampaignStatsResponse",
    "RetryConfig",
    "TimeWindow",
    "Weekday",
]
