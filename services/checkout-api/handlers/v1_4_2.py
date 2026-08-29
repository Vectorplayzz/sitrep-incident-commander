"""checkout-api cart pricing — v1.4.2 (the good version).

Prices a cart by asking inventory-api for every item in ONE batch call.
Total upstream cost: ~1 lookup, regardless of cart size.
"""

from __future__ import annotations

import httpx

VERSION = "v1.4.2"
COMMIT_SHA = "8c1f4ab"


async def price_cart(
    client: httpx.AsyncClient,
    inventory_url: str,
    cart: list[dict[str, object]],
    headers: dict[str, str],
) -> dict[str, object]:
    item_ids = [str(line["item_id"]) for line in cart]

    response = await client.post(
        f"{inventory_url}/items/batch",
        json={"item_ids": item_ids},
        headers=headers,
        timeout=2.0,
    )
    response.raise_for_status()
    stock = {row["item_id"]: row for row in response.json()["items"]}

    total = 0.0
    for line in cart:
        item_id = str(line["item_id"])
        if item_id in stock:
            total += float(line["price"]) * int(line["qty"])

    return {"total": round(total, 2), "upstream_calls": 1, "lines": len(cart)}
