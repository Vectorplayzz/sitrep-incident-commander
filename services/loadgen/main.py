"""loadgen — steady, boring traffic against checkout-api.

Without constant traffic there is no baseline, and without a baseline the
analytics subagent has nothing to detect a change point against. This runs
at a fixed rate for the life of the stack.
"""

from __future__ import annotations

import asyncio
import os
import random

import httpx

CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout-api:8000")
RPS = float(os.environ.get("LOADGEN_RPS", "6"))
CONCURRENCY = int(os.environ.get("LOADGEN_CONCURRENCY", "12"))


async def worker(client: httpx.AsyncClient, interval: float) -> None:
    while True:
        try:
            await client.post(f"{CHECKOUT_URL}/checkout", timeout=10.0)
        except Exception:
            # A dead or timing-out upstream is exactly the condition we are
            # simulating; the request row is recorded server-side either way.
            pass
        await asyncio.sleep(interval * random.uniform(0.7, 1.3))


async def main() -> None:
    interval = CONCURRENCY / RPS
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

        await asyncio.gather(*(worker(client, interval) for _ in range(CONCURRENCY)))


if __name__ == "__main__":
    asyncio.run(main())
