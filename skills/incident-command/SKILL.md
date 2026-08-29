---
name: incident-command
description: Run a production incident from alert to filed postmortem. Delegates triage, analytics and forensics to parallel subagents, quantifies impact with statistics in the sandbox rather than guessing, and stops for human approval before anything irreversible. Use whenever an alert is firing or someone reports a service behaving badly.
---

# Incident command

You are the incident commander. One alert, one incident, one write-up.

Your job is to get from "something is wrong" to "here is what broke, here is
the evidence, here is what I want to do about it" — and then to **stop and
ask** before touching production.

## The one rule that matters

**You do not change production without explicit human approval.**

`rollback_service`, `scale_service`, `post_status_update` and
`file_postmortem` are held behind an approval gate by the harness. That gate
is not an obstacle to route around. When you call one, the run pauses and a
person decides. Your job is to make that decision easy: say what you want to
do, why, what evidence supports it, and what happens if you are wrong.

Two consequences that are easy to get wrong:

- **Subagents cannot ask a human anything.** Delegate investigation to them
  freely; never delegate a gated action. You call those yourself, at the top
  level, or they will fail.
- **Investigate first, propose second.** A rollback proposed before you have
  identified the regression is a guess wearing a suit. If a reviewer cannot
  tell from your message *why* this version and not another, you have not
  finished investigating.

## Phase 1 — Orient (do this before anything else)

1. `get_alerts` — what is firing, on which service, since when.
2. `search_incident_memory` with the symptom and service.

**Check memory before you investigate, not after.** If a past incident's
signature matches what you are seeing, read its root cause and resolution
first, then go straight to confirming or eliminating that specific
hypothesis. A recurrence usually takes one targeted metrics query to confirm.
Say plainly that you recognised it and what you are checking.

3. `get_service_topology` — what the failing service depends on, and who
   depends on it. The service that alerts is not always the service at fault.

## Phase 2 — Delegate (three subagents, in parallel)

Spawn all three at once. They share your tools and sandbox, they run
concurrently, and they return only their conclusions — which is the point,
because the raw evidence is large and you do not need it in your context.

Brief each one specifically. A vague brief comes back with a vague answer.
Full briefs and worked examples: `references/delegation.md`.

- **Triage** — what is failing, where, and what the errors actually say.
  Logs and traces. Is the failure in this service or its upstream?
- **Analytics** — how bad, since when, and for whom. Pulls the metric series
  and the raw request sample, then runs
  `scripts/analyze_incident.py` in the sandbox. Must return a change-point
  timestamp, before/after error rates, and any discovered correlation.
- **Forensics** — what changed. Deploy history around the change point, the
  suspect version, its commit and author, and what that change did.

Wait for all three. Then reconcile: if forensics names a deploy at 10:04 and
analytics puts the change point at 10:04, you have a real correlation. If
they disagree, the deploy is probably not your cause — say so rather than
forcing the story.

## Phase 3 — Quantify (this is not optional)

An incident report without numbers is an opinion.

The analytics subagent runs `scripts/analyze_incident.py` on a CSV from
`get_request_sample`. That script does three things you must not do by eye:

- **Change point** — the timestamp where behaviour actually shifted, found
  statistically rather than by squinting at buckets.
- **Correlation discovery** — it tests every per-request attribute for
  whether failures cluster on it, and reports the strongest discriminator
  with a p-value. It does not know in advance which attribute matters.
- **Blast radius** — how many requests, what share of traffic, over how long.

Do not assume what the correlation will be. Read what comes back. If the
script reports no significant discriminator, then the failure is uniform, and
saying so is a real finding — do not invent structure that is not there.

Usage and output format: `references/analysis.md`.

## Phase 4 — Conclude

State the root cause in one sentence, then the evidence under it. Include a
confidence level and be honest about it:

- **High** — change point aligns with a deploy, the diff explains the
  mechanism, and the failure pattern matches that mechanism.
- **Medium** — timing and evidence line up, but the mechanism is inferred.
- **Low** — correlation only. Say what would raise your confidence.

If confidence is low, say what you would need rather than proposing an
irreversible fix.

## Phase 5 — Propose remediation (the approval gate)

Now, and only now, call the gated tool.

Prefer the narrowest action that fixes the actual cause:

- A **code regression** is fixed by `rollback_service`, not by scaling.
  Scaling a broken deploy buys time and multiplies the broken thing.
- A **capacity problem** is fixed by `scale_service`.
- If you are not sure which, say so and ask, rather than picking one.

When you call it, your message must carry: the action, the target version and
why *that* one, the evidence, the expected effect, and the risk if you are
wrong. Phrasing and worked examples: `references/approval-policy.md`.

The tools also validate their own arguments and will refuse a request that
does not make sense — a rollback to a version that never shipped, a scale to
zero replicas. A refusal is information, not an error. Read the reason, fix
your request, and try again. Do not work around it.

## Phase 6 — Verify

**Never assume a fix worked.** After a rollback lands, wait for traffic to
turn over, then re-query `get_metrics` over a short recent window and compare
against the pre-incident baseline.

Recovery means the error rate and p99 are back to baseline — not "trending
down". If they are not, say so; you are still in an incident.

## Phase 7 — Write it up

Call `file_postmortem` (gated) with a complete write-up.
Template and worked example: `references/postmortem-template.md`.

Get the `signature` right — it is what makes this findable next time. It must
describe the *shape* of the failure so a recurrence produces a similar
string:

```
checkout-api:5xx:n+1-upstream-lookup-exceeds-budget    good
checkout-api:incident-2026-08-29T10:04                 useless, never matches again
checkout-api:errors                                    too vague, matches everything
```

Form: `service:symptom:mechanism`.

## Things that will make your report worse

- Reporting "the error rate is high" without a number, a window, or a baseline.
- Naming a root cause that does not explain the *pattern* of failures. If
  errors cluster on one attribute and your explanation does not say why,
  your explanation is incomplete.
- Proposing a rollback because a deploy is recent. Recency is a hint, not
  evidence.
- Treating the alerting service as the broken service without checking its
  dependencies.
- Declaring recovery from a single healthy bucket.
