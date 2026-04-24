-- Single source of truth for the DB schema. Applied on container init by the
-- postgres image (/docker-entrypoint-initdb.d/) and re-applied by `make reset-db`.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Campaigns -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL,
    status          TEXT        NOT NULL
        CHECK (status IN ('PENDING', 'ACTIVE', 'COMPLETED', 'FAILED')),
    timezone        TEXT        NOT NULL,
    schedule        JSONB       NOT NULL,
    max_concurrent  INT         NOT NULL CHECK (max_concurrent > 0),
    retry_config    JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS campaigns_status_created_idx
    ON campaigns (status, created_at DESC);

-- Calls ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id        UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    phone              TEXT        NOT NULL,
    status             TEXT        NOT NULL
        CHECK (status IN ('QUEUED', 'DIALING', 'IN_PROGRESS',
                          'RETRY_PENDING', 'COMPLETED', 'FAILED',
                          'NO_ANSWER', 'BUSY')),
    attempt_epoch      INT         NOT NULL DEFAULT 0,
    retries_remaining  INT         NOT NULL,
    next_attempt_at    TIMESTAMPTZ NULL,
    provider_call_id   TEXT        NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Phone-level in-flight guard: the same phone can only be claimed once at a
-- time across the system. COMPLETED/FAILED/etc. rows don't count so a
-- campaign's retry history is preserved alongside new dials.
CREATE UNIQUE INDEX IF NOT EXISTS calls_phone_in_flight_uk
    ON calls (phone)
    WHERE status IN ('QUEUED', 'DIALING', 'IN_PROGRESS');

-- Tick-path indexes: eligibility + concurrency + claim all share this.
CREATE INDEX IF NOT EXISTS calls_campaign_status_nextattempt_idx
    ON calls (campaign_id, status, next_attempt_at);

-- System-level retry fan: the scheduler counts retries_due across all campaigns.
CREATE INDEX IF NOT EXISTS calls_retry_pending_system_idx
    ON calls (next_attempt_at) WHERE status = 'RETRY_PENDING';

-- Per-campaign retry-due lookup (find_retry_due_campaign_ids).
CREATE INDEX IF NOT EXISTS calls_retry_pending_campaign_idx
    ON calls (campaign_id, next_attempt_at) WHERE status = 'RETRY_PENDING';

-- Webhook processor reverse lookup on provider_call_id.
CREATE INDEX IF NOT EXISTS calls_provider_call_id_idx
    ON calls (provider_call_id) WHERE provider_call_id IS NOT NULL;

-- Scheduler round-robin cursor ---------------------------------------------
CREATE TABLE IF NOT EXISTS scheduler_campaign_state (
    campaign_id       UUID        PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    last_dispatch_at  TIMESTAMPTZ NULL
);

-- Webhook inbox -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_inbox (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider           TEXT        NOT NULL,
    provider_event_id  TEXT        NOT NULL,
    payload            JSONB       NOT NULL,
    headers            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at       TIMESTAMPTZ NULL,
    UNIQUE (provider, provider_event_id)
);

CREATE INDEX IF NOT EXISTS webhook_inbox_unprocessed_idx
    ON webhook_inbox (processed_at) WHERE processed_at IS NULL;

-- Scheduler audit log (observability surface) ------------------------------
-- `phone` and `attempt_epoch` are denormalized emit-time snapshots of the
-- call-scoped context that produced this row. They stay NULL for campaign-
-- level events (SKIP_CONCURRENCY / SKIP_BUSINESS_HOUR / CAMPAIGN_PROMOTED_ACTIVE
-- / CAMPAIGN_COMPLETED). The rule for expanding these columns is strict:
-- denormalize only IMMUTABLE call-identity or EMIT-TIME snapshot fields.
-- Phone is immutable per call_id (set once in CallRepo.create_batch, never
-- mutated). attempt_epoch varies across a call's lifetime, but the value at
-- emit time is the specific attempt this event belongs to — forensic truth,
-- not live state. Mutable fields like `retries_remaining` or `status` MUST
-- NOT be denormalized here; their live value belongs on the calls table.
CREATE TABLE IF NOT EXISTS scheduler_audit (
    id             BIGSERIAL   PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type     TEXT        NOT NULL,
    campaign_id    UUID        NULL,
    call_id        UUID        NULL,
    phone          TEXT        NULL,
    attempt_epoch  INT         NULL,
    reason         TEXT        NOT NULL,
    state_before   TEXT        NULL,
    state_after    TEXT        NULL,
    extra          JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS scheduler_audit_ts_id_idx
    ON scheduler_audit (ts DESC, id DESC);

CREATE INDEX IF NOT EXISTS scheduler_audit_campaign_ts_idx
    ON scheduler_audit (campaign_id, ts DESC);

CREATE INDEX IF NOT EXISTS scheduler_audit_event_ts_idx
    ON scheduler_audit (event_type, ts DESC);

-- Partial index for the operator phone-substring filter:
-- `SELECT ... WHERE phone LIKE '%<digits>%' ORDER BY ts DESC`.
-- Phone IS NOT NULL keeps campaign-level rows out of the index. Leading-
-- wildcard LIKE can't use the btree for pruning, but the (phone, ts DESC)
-- shape keeps the ORDER BY index-covered on the candidate set.
CREATE INDEX IF NOT EXISTS scheduler_audit_phone_ts_idx
    ON scheduler_audit (phone, ts DESC) WHERE phone IS NOT NULL;

-- Idempotent ALTERs guard against a partially-migrated database where the
-- CREATE TABLE above was already applied (without the new columns) on an
-- earlier container start. `CREATE TABLE IF NOT EXISTS` is a no-op when the
-- table exists, so column additions must come through ADD COLUMN IF NOT
-- EXISTS to reach those rows without a `make reset-db`. Both ALTERs are
-- constant-time catalog updates on empty/nullable columns.
ALTER TABLE scheduler_audit ADD COLUMN IF NOT EXISTS phone TEXT NULL;
ALTER TABLE scheduler_audit ADD COLUMN IF NOT EXISTS attempt_epoch INT NULL;
