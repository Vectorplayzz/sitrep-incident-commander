"""loadgen — steady, boring traffic against checkout-api.

Without constant traffic there is no baseline, and without a baseline the
analytics subagent has nothing to detect a change point against. This runs
at a fixed rate for the life of the stack.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

import httpx

from pathlib import Path

_COMMON = Path(__file__).resolve().parent.parent / "common"
sys.path.insert(0, str(_COMMON) if _COMMON.exists() else "/app/common")
import telemetry  # noqa: E402

CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout-api:8000")
RPS = float(os.environ.get("LOADGEN_RPS", "6"))
BASE_THINK_TIME_MS = float(os.environ.get("LOADGEN_THINK_TIME_MS", "2000"))
BASELINE_WORKERS = int(os.environ.get("LOADGEN_CONCURRENCY", "12"))

# Every worker is started up front; only the first `loadgen_workers` of them
# actually send traffic. A surge is then a control change rather than a
# container restart, which keeps the telemetry continuous across it -- and a
# gap in the baseline would wreck the change-point analysis.
MAX_WORKERS = int(os.environ.get("LOADGEN_MAX_WORKERS", "64"))


def active_workers() -> int:
    return telemetry.get_control_int("loadgen_workers", BASELINE_WORKERS)


def think_time_s() -> float:
    """Delay between a worker's requests.

    Worker count alone does not create load: 52 clients each pausing two
    seconds is still only ~25 rps, far below what the upstream can absorb.
    Demand is workers divided by think time, so a surge has to move both.
    """
    return telemetry.get_control_float("loadgen_think_time_ms", BASE_THINK_TIME_MS) / 1000


async def worker(client: httpx.AsyncClient, index: int, interval: float) -> None:
    while True:
        if index >= active_workers():
            await asyncio.sleep(1.0)
            continue
        try:
            await client.post(f"{CHECKOUT_URL}/checkout", timeout=10.0)
        except Exception:
            # A dead or timing-out upstream is exactly the condition we are
            # simulating; the request row is recorded server-side either way.
            pass
        await asyncio.sleep(think_time_s() * random.uniform(0.7, 1.3))


async def main() -> None:
    telemetry.init_db()
    if not telemetry.get_control("loadgen_workers", ""):
        telemetry.set_control("loadgen_workers", BASELINE_WORKERS)
    if not telemetry.get_control("loadgen_think_time_ms", ""):
        telemetry.set_control("loadgen_think_time_ms", BASE_THINK_TIME_MS)
    interval = BASELINE_WORKERS / RPS
    async with httpx.AsyncClient() as client:
        # Wait for checkout-api to come up before generating a baseline.
        for _ in range(60):
            try:
                r = await client.get(f"{CHECKOUT_URL}/health", timeout=2.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        await asyncio.gather(
            *(worker(client, i, interval) for i in range(MAX_WORKERS))
        )


if __name__ == "__main__":
    asyncio.run(main())
