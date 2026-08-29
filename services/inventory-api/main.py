"""inventory-api — the healthy upstream that checkout-api leans on.

This service never breaks. It is here so that the outage has a *shape*:
when checkout-api ships a bad deploy it starts hammering this service
25 times per request instead of once, and the latency budget blows up.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
import uuid

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from pathlib import Path

_COMMON = Path(__file__).resolve().parent.parent / "common"
sys.path.insert(0, str(_COMMON) if _COMMON.exists() else "/app/common")
import telemetry  # noqa: E402

SERVICE = "inventory-api"
VERSION = "v2.0.1"

# Every lookup costs a fixed-ish amount of time. One call is cheap.
# Twenty-five of them, serially, is an outage.
LOOKUP_LATENCY_MS = float(os.environ.get("INVENTORY_LATENCY_MS", "28"))

# A bounded worker pool, like any real service. This is the mechanism that
# turns an N+1 into an outage: 12 concurrent checkouts each firing ~25
# serial lookups is 300 requests contending for 24 slots, so queue time
# dominates and callers start blowing their timeout budget. Baseline
# traffic (one batch call per checkout) never comes close to the limit.
POOL_SIZE = int(os.environ.get("INVENTORY_POOL_SIZE", "24"))

app = FastAPI(title=SERVICE, version=VERSION)
_pool: asyncio.Semaphore | None = None


@app.on_event("startup")
async def startup() -> None:
    global _pool
    telemetry.init_db()
    _pool = asyncio.Semaphore(POOL_SIZE)


class BatchRequest(BaseModel):
    item_ids: list[str]


@app.middleware("http")
async def record(request: Request, call_next):
    started = time.perf_counter()
    trace_id = request.headers.get("x-trace-id", uuid.uuid4().hex)
    parent_span = request.headers.get("x-span-id")
    span_id = uuid.uuid4().hex[:16]
    response: Response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    telemetry.record_request(
        service=SERVICE,
        route=request.url.path,
        method=request.method,
        status=response.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        span_id=span_id,
        parent_span=parent_span,
        version=VERSION,
    )
    return response


async def _lookup(item_id: str) -> dict[str, object]:
    async with _pool:
        jitter = random.uniform(0.85, 1.15)
        await asyncio.sleep((LOOKUP_LATENCY_MS * jitter) / 1000)
        return {
            "item_id": item_id,
            "in_stock": True,
            "warehouse": random.choice(["ams-1", "sfo-2", "blr-3"]),
        }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.get("/items/{item_id}")
async def get_item(item_id: str) -> dict[str, object]:
    """Single-item lookup. Cheap on its own, ruinous in a loop."""
    return await _lookup(item_id)


@app.post("/items/batch")
async def get_items_batch(body: BatchRequest) -> dict[str, object]:
    """Batch lookup. Costs roughly one lookup regardless of item count."""
    await asyncio.sleep(LOOKUP_LATENCY_MS / 1000)
    return {
        "items": [
            {"item_id": i, "in_stock": True, "warehouse": "ams-1"} for i in body.item_ids
        ]
    }
