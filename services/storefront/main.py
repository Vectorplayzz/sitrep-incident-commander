"""storefront — the shop a customer actually sees.

Everything else in this stack is JSON. This is the part where an outage looks
like an outage: an order that will not go through, on a page, in front of a
person.

It also carries a small ops panel, so a demo can ship the bad deploy and
watch the shop break without anyone touching a terminal.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

_COMMON = Path(__file__).resolve().parent.parent / "common"
sys.path.insert(0, str(_COMMON) if _COMMON.exists() else "/app/common")
import telemetry  # noqa: E402

SERVICE = "storefront"
VERSION = "v3.1.0"
CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout-api:8000")
STATIC = Path(__file__).resolve().parent / "static"

# Shipping the storefront's own version history alongside checkout's keeps the
# deploy timeline honest: a forensics pass should see that the shop itself has
# not changed in days.
RELEASES = {
    "v1.4.2": {
        "commit_sha": "8c1f4ab",
        "author": "priya@example.com",
        "summary": "checkout: batch inventory lookups when pricing a cart",
    },
    "v1.5.0": {
        "commit_sha": "3d9e77c",
        "author": "marcus@example.com",
        "summary": "checkout: show ships-from warehouse on the order summary",
    },
}

app = FastAPI(title=SERVICE, version=VERSION)
_client: httpx.AsyncClient | None = None


class Order(BaseModel):
    cart_lines: int = 4


class Deploy(BaseModel):
    version: str


@app.on_event("startup")
async def startup() -> None:
    global _client
    telemetry.init_db()
    _client = httpx.AsyncClient()
    if telemetry.active_version(SERVICE, default="") == "":
        telemetry.record_deploy(
            service=SERVICE,
            version=VERSION,
            commit_sha="f20c8ad",
            author="sam@example.com",
            summary="storefront: seasonal banner copy",
            ts=time.time() - 3 * 86_400,
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()


@app.middleware("http")
async def record(request: Request, call_next):
    started = time.perf_counter()
    trace_id = request.headers.get("x-trace-id", uuid.uuid4().hex)
    span_id = uuid.uuid4().hex[:16]
    request.state.trace_id = trace_id
    response: Response = await call_next(request)
    if request.url.path.startswith("/api/order"):
        telemetry.record_request(
            service=SERVICE,
            route="/api/order",
            method=request.method,
            status=response.status_code,
            duration_ms=(time.perf_counter() - started) * 1000,
            trace_id=trace_id,
            span_id=span_id,
            version=VERSION,
        )
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE, "version": VERSION}


@app.post("/api/order")
async def place_order(order: Order, request: Request) -> JSONResponse:
    """Place a real order through checkout-api and report honestly."""
    started = time.perf_counter()
    try:
        response = await _client.post(
            f"{CHECKOUT_URL}/checkout",
            json={"cart_lines": order.cart_lines},
            headers={"x-trace-id": request.state.trace_id},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        telemetry.log(
            service=SERVICE,
            level="error",
            message="order failed: checkout-api unreachable",
            trace_id=request.state.trace_id,
            error=type(exc).__name__,
        )
        return JSONResponse(
            {"ok": False, "error": "Checkout is unavailable. Please try again."},
            status_code=503,
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    if response.status_code >= 500:
        telemetry.log(
            service=SERVICE,
            level="error",
            message="order rejected by checkout-api",
            trace_id=request.state.trace_id,
            cart_lines=order.cart_lines,
            upstream_status=response.status_code,
            duration_ms=elapsed_ms,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": "We could not complete your order. Please try again.",
                "cart_lines": order.cart_lines,
                "duration_ms": elapsed_ms,
                "trace_id": request.state.trace_id,
            },
            status_code=502,
        )

    body = response.json()
    return JSONResponse(
        {
            "ok": True,
            "order_id": uuid.uuid4().hex[:10].upper(),
            "total": body.get("total"),
            "cart_lines": order.cart_lines,
            "duration_ms": elapsed_ms,
        }
    )


@app.get("/api/status")
async def status() -> dict[str, object]:
    """Live health of the checkout path, for the strip along the top."""
    try:
        conn = telemetry.connect(read_only=True)
    except Exception:
        return {"available": False}

    try:
        since = time.time() - 60
        rows = conn.execute(
            "SELECT status, duration_ms FROM requests"
            " WHERE service = 'checkout-api' AND route = '/checkout' AND ts >= ?",
            (since,),
        ).fetchall()
        active = telemetry.active_version("checkout-api", default="unknown")
    finally:
        conn.close()

    if not rows:
        return {"available": True, "samples": 0, "version": active}

    errors = sum(1 for r in rows if r["status"] >= 500)
    durations = sorted(r["duration_ms"] for r in rows)
    p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
    return {
        "available": True,
        "samples": len(rows),
        "error_rate": round(errors / len(rows), 4),
        "p99_ms": round(p99, 1),
        "version": active,
    }


@app.post("/api/ops/deploy")
async def ops_deploy(deploy: Deploy) -> JSONResponse:
    """Ship a version of checkout-api.

    This is the demo's chaos button. It is deliberately part of the shop's
    own ops panel rather than a terminal command, so an incident can be
    caused, investigated and resolved without anyone leaving the browser.
    """
    if deploy.version not in RELEASES:
        return JSONResponse(
            {"ok": False, "error": f"unknown version {deploy.version}"}, status_code=400
        )
    meta = RELEASES[deploy.version]
    telemetry.record_deploy(service="checkout-api", version=deploy.version, **meta)
    telemetry.log(
        service="checkout-api",
        level="info",
        message=f"deploy {deploy.version} rolled out to 100% of fleet",
        version=deploy.version,
        commit_sha=meta["commit_sha"],
        author=meta["author"],
    )
    return JSONResponse({"ok": True, "version": deploy.version, **meta})
