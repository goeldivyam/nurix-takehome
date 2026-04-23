from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Nurix Voice Campaign", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
