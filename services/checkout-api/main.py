"""checkout-api — the service that breaks.

Which pricing handler runs is decided per-request by the currently active
deploy row in the telemetry store. That is what makes the incident
reproducible AND reversible without restarting a container: `chaos deploy`
flips the active version forward, the agent's gated `rollback_service`
tool flips it back, and the metrics visibly recover.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from pathlib import Path

_COMMON = Path(__file__).resolve().parent.parent / "common"
sys.path.insert(0, str(_COMMON) if _COMMON.exists() else "/app/common")
import telemetry  # noqa: E402

from handlers import v1_4_2, v1_5_0  # noqa: E402

SERVICE = "checkout-api"
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory-api:8000")

HANDLERS = {
    v1_4_2.VERSION: v1_4_2,
    v1_5_0.VERSION: v1_5_0,
}
DEFAULT_VERSION = v1_4_2.VERSION

# Alerting thresholds. Deliberately simple and deliberately visible in the
# repo, so a judge can see exactly what makes the alert fire.
ERROR_RATE_THRESHOLD = 0.10
P99_THRESHOLD_MS = 550.0
ALERT_WINDOW_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 180

app = FastAPI(title=SERVICE)
_client: httpx.AsyncClient | None = None


# The whole handler gets one upstream budget, the way a real service with an
# SLO would. v1.4.2 spends ~30ms of it on a single batch call. v1.5.0 spends
# ~28ms per cart line, so a large cart walks straight through the budget
# while a small one still fits. That is the outage: partial, and correlated
# with cart size rather than with traffic volume.
UPSTREAM_BUDGET_S = float(os.environ.get("CHECKOUT_UPSTREAM_BUDGET_S", "0.6"))

# Most carts are small; a minority are large. That skew is what makes the
# outage partial rather than total, and it leaves a real signal behind:
# failures correlate with cart size, not with time of day or traffic volume.
LARGE_CART_SHARE = float(os.environ.get("CHECKOUT_LARGE_CART_SHARE", "0.30"))


def _make_cart() -> list[dict[str, object]]:
    """A cart. Size is what turns the N+1 regression into an outage."""
    if random.random() < LARGE_CART_SHARE:
        size = random.randint(18, 28)
    else:
        size = random.randint(2, 6)
    return [
        {
            "item_id": f"sku-{random.randint(1000, 9999)}",
            "qty": random.randint(1, 3),
            "price": round(random.uniform(4.99, 89.99), 2),
        }
        for _ in range(size)
    ]


@app.on_event("startup")
async def startup() -> None:
    global _client
    telemetry.init_db()
    _client = httpx.AsyncClient()

    # Seed the deploy history so there is something for forensics to read.
    if telemetry.active_version(SERVICE, default="") == "":
        telemetry.record_deploy(
            service=SERVICE,
            version=v1_4_2.VERSION,
            commit_sha=v1_4_2.COMMIT_SHA,
            author="priya@example.com",
            summary="checkout: batch inventory lookups when pricing a cart",
            ts=time.time() - 3600,
        )
    telemetry.log(
        service=SERVICE, level="info", message="service started", version=_version()
    )
    asyncio.create_task(_alert_watchdog())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()


def _version() -> str:
    return telemetry.active_version(SERVICE, default=DEFAULT_VERSION)


@app.middleware("http")
async def record(request: Request, call_next):
    started = time.perf_counter()
    trace_id = request.headers.get("x-trace-id", uuid.uuid4().hex)
    span_id = uuid.uuid4().hex[:16]
    request.state.trace_id = trace_id
    request.state.span_id = span_id

    try:
        response: Response = await call_next(request)
    except Exception:  # pragma: no cover - defensive
        response = JSONResponse({"error": "internal"}, status_code=500)

    duration_ms = (time.perf_counter() - started) * 1000
    attrs: dict[str, object] = {}
    if hasattr(request.state, "cart_lines"):
        attrs["cart_lines"] = request.state.cart_lines
    if hasattr(request.state, "upstream_calls"):
        attrs["upstream_calls"] = request.state.upstream_calls

    telemetry.record_request(
        service=SERVICE,
        route=request.url.path,
        method=request.method,
        status=response.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        span_id=span_id,
        version=_version(),
        attrs=attrs,
    )
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE, "version": _version()}


@app.post("/checkout")
async def checkout(request: Request) -> JSONResponse:
    version = _version()
    handler = HANDLERS.get(version, HANDLERS[DEFAULT_VERSION])
    trace_id = request.state.trace_id
    headers = {"x-trace-id": trace_id, "x-span-id": request.state.span_id}
    cart = _make_cart()

    try:
        result = await asyncio.wait_for(
            handler.price_cart(_client, INVENTORY_URL, cart, headers),
            timeout=UPSTREAM_BUDGET_S,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException, httpx.HTTPError) as exc:
        request.state.cart_lines = len(cart)
        telemetry.log(
            service=SERVICE,
            level="error",
            message="checkout failed: upstream inventory calls exhausted the request budget",
            trace_id=trace_id,
            version=version,
            cart_lines=len(cart),
            budget_s=UPSTREAM_BUDGET_S,
            error=type(exc).__name__,
            upstream="inventory-api",
        )
        return JSONResponse(
            {"error": "upstream_timeout", "upstream": "inventory-api"}, status_code=503
        )

    request.state.cart_lines = result["lines"]
    request.state.upstream_calls = result["upstream_calls"]

    if result["upstream_calls"] > 5:
        telemetry.log(
            service=SERVICE,
            level="warn",
            message="cart priced with per-line inventory lookups",
            trace_id=trace_id,
            version=version,
            upstream_calls=result["upstream_calls"],
            cart_lines=result["lines"],
        )

    return JSONResponse({"ok": True, "version": version, **result})


async def _alert_watchdog() -> None:
    """Fires an alert when the route degrades. This is what wakes the agent."""
    last_alert = 0.0
    while True:
        await asyncio.sleep(15)
        try:
            conn = telemetry.connect(read_only=True)
        except Exception:
            continue
        try:
            since = time.time() - ALERT_WINDOW_SECONDS
            rows = conn.execute(
                "SELECT status, duration_ms FROM requests"
                " WHERE service = ? AND route = '/checkout' AND ts >= ?",
                (SERVICE, since),
            ).fetchall()
        except Exception:
            continue
        finally:
            conn.close()

        if len(rows) < 20:
            continue

        errors = sum(1 for r in rows if r["status"] >= 500)
        error_rate = errors / len(rows)
        durations = sorted(r["duration_ms"] for r in rows)
        p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]

        breached = error_rate >= ERROR_RATE_THRESHOLD or p99 >= P99_THRESHOLD_MS
        if breached and (time.time() - last_alert) > ALERT_COOLDOWN_SECONDS:
            last_alert = time.time()
            telemetry.raise_alert(
                service=SERVICE,
                name="CheckoutHighErrorRate",
                severity="critical",
                summary=(
                    f"POST /checkout error rate {error_rate:.0%} and p99 {p99:.0f}ms"
                    f" over the last {ALERT_WINDOW_SECONDS}s"
                ),
                route="/checkout",
                error_rate=round(error_rate, 4),
                p99_ms=round(p99, 1),
                sample_size=len(rows),
            )
