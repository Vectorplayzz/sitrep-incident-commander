# Briefing the three subagents

Spawn all three at once. They run concurrently, share your MCP tools and
sandbox, and return only their conclusions — the raw evidence stays in their
context, not yours. That is the entire point: a full request sample is
thousands of rows, and you need the finding, not the rows.

**Subagents cannot ask a human anything.** Give them everything they need up
front, and never delegate a gated tool — `rollback_service`,
`scale_service`, `post_status_update` and `file_postmortem` are yours alone.

## What makes a brief good

A bad brief is "investigate checkout-api". It comes back with a paragraph of
narration and no numbers.

A good brief states the alert, the window, the tools to use, and the exact
shape of the answer you want. Ask for specific fields and you get specific
fields.

Always include:

- the **service** and the **alert**, quoted
- a **time window** wide enough to contain healthy traffic — without a
  baseline there is nothing to compare against
- the **tools** to use
- the **answer format**, field by field
- an instruction to **report uncertainty** rather than smoothing over it

## Triage

> The alert `CheckoutHighErrorRate` is firing on `checkout-api`:
> "POST /checkout error rate 32% and p99 909ms". Find out what is actually
> failing and where.
>
> Use `get_logs` (service `checkout-api`, level ERROR, last 20 minutes) and
> `get_traces` (service `checkout-api`, only_errors, limit 5). Also check
> `get_logs` for `inventory-api` — the failure may be downstream of the
> service that alerted.
>
> Report:
> - the dominant error message, quoted, and how many occurrences
> - which route(s) are affected
> - from the traces: where the time is actually spent, per service
> - whether the fault originates in checkout-api or an upstream dependency,
>   and what in the trace tells you that
> - anything in the logs that looks relevant but that you cannot explain

## Analytics

> Quantify the `checkout-api` incident. I need numbers, not impressions.
>
> 1. `get_metrics` for `checkout-api` route `/checkout`, window 30 minutes,
>    bucket 30s, to see the overall shape.
> 2. `get_request_sample` for the same service and route, window 30 minutes,
>    limit 1000. Save the `csv` field to a file in the sandbox.
> 3. Run `/opt/tfy/skills/incident-command/scripts/analyze_incident.py` on
>    that file with `--out-dir .`. Pass `--deploy-epoch` if a suspect deploy
>    time is known.
>
> Report, from the script's JSON output:
> - the change-point timestamp and its p-value
> - error rate and p99 before vs after
> - blast radius: failed requests, share of traffic, failures per minute
> - the top discriminator: which attribute, effect size, p-value, and what
>   the interpretation field says
> - the path to the generated chart
>
> Do not guess which attribute will matter. Report what the script found. If
> it reports no significant discriminator, say that — a uniform failure
> distribution is a real and useful finding.

## Forensics

> Something changed on `checkout-api` around {CHANGE_POINT}. Find out what.
>
> Use `get_deploys` for `checkout-api`, and `get_service_topology` to
> understand what it depends on.
>
> Report:
> - every deploy within 30 minutes of {CHANGE_POINT}, with version, commit
>   sha, author, and time relative to the change point
> - the last version running before that, which is the rollback candidate
> - what the suspect deploy's summary says it did
> - a mechanism: how could that change plausibly produce these symptoms?
>   Say explicitly whether this is established from evidence or inferred.
> - if no deploy lines up with the change point, say so clearly. A recent
>   deploy that does not match the timing is not the cause.

## Reconciling what comes back

Three findings agreeing is a root cause. Three findings disagreeing is a
signal you are not finished.

- Analytics puts the change point at 10:04, forensics finds a deploy at
  10:04, triage's error message matches the mechanism that deploy would
  cause → **high confidence**.
- Timing lines up but the mechanism does not explain the *pattern* of
  failures → **medium**. Say which part is inferred.
- Only timing lines up → **low**. Say what would raise it.

If the discriminator analytics found does not fit the story forensics tells,
trust the statistics and re-examine the story. The numbers are measured; the
mechanism is a narrative you constructed.
