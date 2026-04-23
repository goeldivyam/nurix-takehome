#!/usr/bin/env python3
"""Reset the demo database to a clean chronology.

Idempotent across repeated runs. Stops the app so the scheduler drops
its DB connections, truncates the five mutable tables in one transaction,
then brings the app back up.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg


def _dsn() -> str:
    # Read the DSN the app itself uses so the script works both from the host
    # (against the docker-compose Postgres) and inside a CI container that
    # already has DATABASE_URL exported.
    env_file = Path(".env")
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1]
    return os.environ.get("DATABASE_URL", "postgresql://nurix:nurix@localhost:5442/nurix")


def _host_dsn(dsn: str) -> str:
    # docker-compose exposes Postgres on the host via HOST_PG_PORT (default
    # 5442). If the config DSN points at the container alias `postgres`,
    # rewrite it to localhost:$HOST_PG_PORT so host-side scripts can reach it.
    if "@postgres:" in dsn:
        host_port = os.environ.get("HOST_PG_PORT", "5442")
        return dsn.replace("@postgres:5432", f"@localhost:{host_port}")
    return dsn


async def _truncate_all() -> None:
    dsn = _host_dsn(_dsn())
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                TRUNCATE TABLE
                  scheduler_audit,
                  webhook_inbox,
                  scheduler_campaign_state,
                  calls,
                  campaigns
                RESTART IDENTITY CASCADE
                """
            )
        print(
            "[reset] truncated: campaigns, calls, scheduler_campaign_state, "
            "webhook_inbox, scheduler_audit"
        )
    finally:
        await conn.close()


def _run(cmd: list[str]) -> None:
    # Static command literals — no shell, no user-supplied arguments.
    print(f"[reset] $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)  # noqa: S603


def main() -> int:
    # Stop the app (but NOT postgres) so in-flight scheduler connections
    # release cleanly before the TRUNCATE. `stop` is correct here — the
    # container still exists, we just pause its process.
    _run(["docker", "compose", "stop", "app"])
    try:
        asyncio.run(_truncate_all())
    finally:
        # `up -d app` (not `start`) so if the container was removed for any
        # reason, it gets recreated rather than failing silently.
        _run(["docker", "compose", "up", "-d", "app"])
    print("[reset] ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
