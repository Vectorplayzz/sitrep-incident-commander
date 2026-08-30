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

# A bounded worker pool, like any real service has.
#
# To be precise about what this does and does not do: the N+1 in checkout
# v1.5.0 is *serial*, so a checkout worker only ever has one lookup in
# flight. With the shipped generators capping concurrency at 12 against 24
# permits, this semaphore does not saturate and is NOT what causes the
# outage. The outage is purely serial latency (~28ms x cart size) exceeding
# checkout's 600ms request budget.
#
# The pool is here because a real upstream has one, and because lowering
# INVENTORY_POOL_SIZE below the checkout concurrency turns queue contention
# into a second, independent failure mode worth investigating. Left at 24 by
# default so the demo has exactly one root cause to find.
DEFAULT_POOL_SIZE = int(os.environ.get("INVENTORY_POOL_SIZE", "24"))

# Replica count is the knob an operator actually turns. Capacity scales with
# it, so the agent's scale_service tool has something real to change.
WORKERS_PER_REPLICA = 24

app = FastAPI(title=SERVICE, version=VERSION)


def current_latency_ms() -> float:
    return telemetry.get_control_float("inventory_latency_ms", LOOKUP_LATENCY_MS)


def current_pool_size() -> int:
    replicas = telemetry.get_control_int("inventory_replicas", 1)
    return max(1, replicas * WORKERS_PER_REPLICA)


class DynamicLimiter:
    """A semaphore whose ceiling can move while requests are queued.

    asyncio.Semaphore fixes its capacity at construction, so an approved
    scale-up would not reach the requests already waiting -- exactly the ones
    the operator scaled up to rescue. This re-reads the limit on every
    acquire and wakes waiters when it changes.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self._waiters: list[asyncio.Future] = []

    async def __aenter__(self) -> "DynamicLimiter":
        while self._in_flight >= current_pool_size():
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
        self._in_flight += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._in_flight -= 1
        self._wake_one()

    def _wake_one(self) -> None:
        while self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.done():
                waiter.set_result(None)
                return

    def wake_all(self) -> None:
        """Called on a ticker so a raised ceiling reaches queued requests."""
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    @property
    def in_flight(self) -> int:
        return self._in_flight


_pool = DynamicLimiter()


async def _capacity_ticker() -> None:
    while True:
        await asyncio.sleep(0.5)
        _pool.wake_all()


@app.on_event("startup")
async def startup() -> None:
    telemetry.init_db()
    if not telemetry.get_control("inventory_replicas", ""):
        telemetry.set_control("inventory_replicas", 1)

    # Seed a deploy record. Without one, get_deploys(inventory-api) is empty
    # and the topology reports a null active_version, so an investigator
    # cannot tell whether this service changed recently -- and is pushed
    # toward blaming the only service that does have a deploy history.
    if telemetry.active_version(SERVICE, default="") == "":
        telemetry.record_deploy(
            service=SERVICE,
            version=VERSION,
            commit_sha="a41b90e",
            author="dana@example.com",
            summary="inventory: add warehouse field to lookup responses",
            ts=time.time() - 86_400,
        )

    asyncio.create_task(_capacity_ticker())


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
        await asyncio.sleep((current_latency_ms() * jitter) / 1000)
        return {
            "item_id": item_id,
            "in_stock": True,
            "warehouse": random.choice(["ams-1", "sfo-2", "blr-3"]),
        }


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": SERVICE,
        "version": VERSION,
        "replicas": telemetry.get_control_int("inventory_replicas", 1),
        "worker_pool": current_pool_size(),
        "in_flight": _pool.in_flight,
    }


@app.get("/items/{item_id}")
async def get_item(item_id: str) -> dict[str, object]:
    """Single-item lookup. Cheap on its own, ruinous in a loop."""
    return await _lookup(item_id)


@app.post("/items/batch")
async def get_items_batch(body: BatchRequest) -> dict[str, object]:
    """Batch lookup. Costs roughly one lookup regardless of item count."""
    async with _pool:
        # Same jitter as the single-item path. Without it every batch call
        # takes exactly the configured latency, so a degraded upstream either
        # misses the caller's budget entirely or blows it on every single
        # request -- never the partial, ragged failure a real one produces.
        jitter = random.uniform(0.85, 1.15)
        await asyncio.sleep((current_latency_ms() * jitter) / 1000)
    return {
        "items": [
            {"item_id": i, "in_stock": True, "warehouse": "ams-1"} for i in body.item_ids
        ]
    }
