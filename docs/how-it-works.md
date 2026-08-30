# The bug, in plain English

How the failure works, why it is realistic, and how the agent finds it.
Nothing here is hand-waved — every claim maps to a file you can open.

---

## 1. The system

Three services and a load generator.

```
   shopper → storefront → checkout-api → inventory-api
                              │
                          telemetry.db  ← everything writes here
                                        ← the MCP server only reads
```

- **storefront** (`services/storefront/`) — the shop. Product grid, basket,
  "Place order".
- **checkout-api** (`services/checkout-api/`) — prices the basket. To do that
  it has to ask inventory about each product.
- **inventory-api** (`services/inventory-api/`) — stock lookups. Each lookup
  takes about **28ms**. It has a worker pool, so only so many can run at once.
- **loadgen** — fake shoppers ordering constantly, so there is always a
  baseline of normal traffic to compare against.

One number matters more than any other: **checkout-api gives itself a 600ms
budget** for all its inventory calls. Past that, it gives up and returns a
503 error. That is a normal thing for a real service to do — you would rather
fail fast than leave a customer staring at a spinner.

It is set in `services/checkout-api/main.py`:

```python
UPSTREAM_BUDGET_S = float(os.environ.get("CHECKOUT_UPSTREAM_BUDGET_S", "0.6"))
```

---

## 2. The bug

There are two versions of the pricing code, and you can read both.

**`v1.4.2` — the good one** (`services/checkout-api/handlers/v1_4_2.py`).
One call, for the whole basket:

```python
response = await client.post(
    f"{inventory_url}/items/batch",
    json={"item_ids": item_ids},      # every product, one request
)
```

**`v1.5.0` — the broken one** (`services/checkout-api/handlers/v1_5_0.py`).
One call **per product**:

```python
for line in cart:
    response = await client.get(
        f"{inventory_url}/items/{item_id}",   # one request per line
    )
```

### Why anyone would write that

This is the important part, and it is why the bug is realistic rather than
silly. Someone was asked to add a "ships from *warehouse*" badge to the order
summary. The batch endpoint did not return the warehouse per item; the
single-item endpoint did. So they reached for the single-item endpoint inside
the loop that was already there.

It is about twenty lines. It works perfectly in testing. It would pass code
review. **That** is what makes it a good demo — it is the kind of bug that
actually ships.

### The arithmetic

This is the whole outage, and you can do it in your head:

| basket | v1.4.2 | v1.5.0 | budget | result |
|---|---|---|---|---|
| 4 items | 1 call ≈ 28ms | 4 calls ≈ 112ms | 600ms | fine either way |
| 24 items | 1 call ≈ 28ms | **24 calls ≈ 670ms** | 600ms | **v1.5.0 fails** |

Small baskets stay under the budget. Large ones do not.

**So the shop only breaks for customers with big baskets.** That is why the
error rate sits around 20–30% rather than 100% — most baskets are small. And
it is why nobody noticed in testing, where you naturally test with two or
three items.

That partial failure is the interesting part. A service that is entirely
down is trivial to diagnose; one that fails for a quarter of requests, with
the failures sharing a property nobody has noticed yet, is the situation an
investigator is actually for.

---

## 3. How to break it and fix it yourself

**No terminal needed.** Open the shop at `http://localhost:8099`.

1. Place a **Full trade order** (24 items). It works — about 35ms.
2. Open **Operations** at the bottom, click **Deploy checkout v1.5.0**.
3. Place a **Full trade order** again. It **fails**.
4. Place a **Small basket** (4 items). It still **works**.
5. Click **Deploy checkout v1.4.2** to put it back.

Steps 3 and 4 together are the demo. Same shop, same second, one order goes
through and one does not — and the only difference is basket size.

The header strip shows the live error rate and p99 latency, so you can watch
it move.

<details>
<summary>Terminal equivalents, if you prefer</summary>

```bash
cd sitrep
docker compose exec -T checkout-api python -m chaos.main incident
docker compose exec -T checkout-api python -m chaos.main heal
docker compose exec -T checkout-api python -m chaos.main status
docker compose exec -T checkout-api python -m chaos.main surge
docker compose exec -T checkout-api python -m chaos.main degrade-inventory
docker compose exec -T checkout-api python -m chaos.main restore
```
</details>

---

## 4. How the agent works it out

It is not told any of the above. Here is what it actually does, and why each
step is there.

**1. Orient.** `get_alerts` — what is firing and on which service.
`search_incident_memory` — have I seen this before? `get_service_topology` —
what does this service depend on?

**2. Delegate.** It spawns three subagents at once, because they do not need
each other's answers:

- *Triage* reads logs and traces. Finds every failure says
  `upstream inventory calls exhausted the request budget`.
- *Analytics* pulls a thousand raw request rows and runs statistics on them
  in a sandbox (next section).
- *Forensics* reads deploy history. Finds `v1.5.0` shipped at the exact
  minute the errors started.

**3. The statistics.** This is the part worth understanding, because it is
what separates this from "the agent read some logs".

`skills/incident-command/scripts/analyze_incident.py` runs in a Daytona
sandbox and does three things:

- **Finds when it broke** — not by looking at a chart, but by testing every
  possible split point in the data and picking the one where "before" and
  "after" are most statistically different. It reports a p-value, so
  *"nothing actually changed"* is a possible answer.
- **Finds what the failures have in common** — it takes every attribute
  recorded on every request and tests each one for whether it separates
  failures from successes. **It does not know what a cart is.** It finds
  `cart_lines` because that is what the data says, and reports an effect size
  near 1.0 with a vanishingly small p-value.
- **Measures the damage** — how many requests, what share of traffic, over
  how long.

The answer is not hardcoded, and there is a test that proves it:
`test_reports_no_discriminator_when_failures_are_uniform` feeds it *randomly*
distributed failures and asserts it reports **nothing**. A tool that always
finds a correlation would let the agent invent a root cause for any outage.

**4. Conclude.** It puts the three together: the change point matches the
deploy, the deploy's description explains the mechanism, and the failure
pattern (only large baskets) is what that mechanism predicts. It states a
confidence level and says which parts are measured and which are inferred.

**5. Ask.** It proposes rolling back to `v1.4.2` and **stops**. TrueForge
holds it there. Nothing happens until you click Allow.

**6. Verify.** After the rollback it re-queries the metrics — and it will
refuse to call it fixed off a handful of requests. In testing it said:
*"only 12 requests, so let me confirm with a fuller bucket."*

**7. Write it up.** Files a postmortem with a searchable signature, so the
next occurrence is recognised instead of re-investigated.

---

## 5. Why the approval gate is the point

An agent that can roll back production is an agent that can take production
down. The gate is what makes the capability safe to hand over.

It is enforced in three places, deliberately:

1. **The harness** holds four tools behind human approval
   (`agents/sitrep-commander.json` → `require_approval_for_tools`).
2. **The tools validate themselves** — you cannot roll back to a version that
   never shipped, and `scale_service` refuses to scale to zero replicas,
   because that is a second outage rather than a fix.
3. **A test** asserts every world-changing tool is actually covered, because
   TrueForge does *not* error if you name a tool that does not exist — it
   just silently stops gating it.

That third one matters more than it sounds: a typo in the config would have
removed the safety gate with no error message anywhere. The test catches it.

---

## 6. Common questions

**Is this a real system or a mock?**
Real processes, real HTTP, real concurrency, real latency. The failure is not
scripted — it emerges from the arithmetic above. Nothing anywhere says "fail
this request". What is simulated is the *deploy*: switching version flips
which handler function runs, rather than rebuilding a container.

**Did the agent really find the cart-size correlation, or was it told?**
Really found it. The tool that returns request data hands back raw rows and
does no analysis. The analysis script scans every attribute column with no
knowledge of the domain. Both are short files you can read.

**What if the answer is not a rollback?**
Then it should not roll back, and it does not. The `surge` scenario creates a
capacity problem with no bad deploy — the agent chose `scale_service` and
explicitly ruled the deploy out. `degrade-inventory` makes the dependency slow —
there, no tool it has can fix the problem, and it **declined to act at all**
and escalated. The contrast table in the README has the numbers.

**What happens if you deny the approval?**
It stops and escalates to a human. It does not look for another route. That
is tested too.

**What was the hardest part to get right?**
Making the outage *partial*. A service that is 100% down is trivial to
diagnose and proves nothing. Getting failures to correlate with one property
of the request — so there is something real to discover — took several
iterations, and the numbers in the README are measured from the running
system, not chosen.
