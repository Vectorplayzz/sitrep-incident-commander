"""checkout-api cart pricing — v1.5.0 (the bad deploy).

The regression, for the record, is realistic and boring: someone needed
per-item warehouse data for a new "ships from" badge, and reached for the
single-item endpoint inside the existing loop instead of extending the
batch call. Code review passed. It looks completely fine.

Total upstream cost: ONE lookup PER CART LINE. With ~25 lines per cart and
a 28ms upstream, p99 walks straight through the 2s client timeout and the
route starts returning 503.

The diff between this file and v1_4_2.py is what the forensics subagent is
meant to find.
"""

from __future__ import annotations

import httpx

VERSION = "v1.5.0"
COMMIT_SHA = "3d9e77c"


async def price_cart(
    client: httpx.AsyncClient,
    inventory_url: str,
    cart: list[dict[str, object]],
    headers: dict[str, str],
) -> dict[str, object]:
    total = 0.0
    upstream_calls = 0

    for line in cart:
        item_id = str(line["item_id"])

        # REGRESSION: per-line lookup so we can show `warehouse` on the
        # order summary. This should have been folded into /items/batch.
        response = await client.get(
            f"{inventory_url}/items/{item_id}",
            headers=headers,
            timeout=2.0,
        )
        response.raise_for_status()
        item = response.json()
        upstream_calls += 1

        if item["in_stock"]:
            total += float(line["price"]) * int(line["qty"])

    return {"total": round(total, 2), "upstream_calls": upstream_calls, "lines": len(cart)}
