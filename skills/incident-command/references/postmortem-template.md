# Postmortem

Fill this in and pass it as `postmortem_markdown` to `file_postmortem`.

Write for someone who was not there, and who is reading it in six months
because it happened again.

## Structure

```markdown
# {service}: {one-line symptom}

## Summary
Two or three sentences. What broke, for how long, who was affected, and what
fixed it.

## Impact
- Duration: {start} to {end} ({minutes} minutes)
- Failed requests: {n} ({share}% of traffic in the window)
- Affected routes: {routes}
- Latency: p99 {before}ms to {after}ms
- What a user actually experienced.

## Timeline
| Time (UTC) | Event |
|---|---|
| 11:03:04 | v1.5.0 deployed |
| 11:03:15 | Change point: error rate 0% to 23% |
| 11:04:02 | Alert fired |
| 11:06:30 | Rollback to v1.4.2 approved and applied |
| 11:07:10 | Error rate back to 0% |

## Root cause
What actually happened, mechanically. Not "a bad deploy" but what the code
did differently and why that produced these symptoms.

## Evidence
The numbers supporting the root cause: change point and its p-value,
before/after rates, the discriminator and its effect size. Reference the
chart. Someone should be able to check your reasoning.

## Why it was not caught earlier
Honest. Usually the answer is that the change looked fine in review and
nothing tested the condition that broke.

## Resolution
What was done, who approved it, and how recovery was confirmed.

## Follow-ups
Concrete and assignable. "Be more careful" is not a follow-up.
- The actual fix, as distinct from the rollback.
- The detection gap: what would have caught this sooner?
- The prevention gap: what would have stopped it shipping?
```

## On the signature

The signature is how a future investigation finds this. Form:
`service:symptom:mechanism`.

```
checkout-api:5xx:n+1-upstream-lookup-exceeds-budget     good
checkout-api:latency:unbatched-inventory-calls          good
checkout-api:incident-2026-08-29T11:04                  useless, never matches
checkout-api:errors                                     matches everything
```

Test it: if this exact failure happened again next month, would you write
roughly the same string? If not, it is too specific. Would it also match a
completely different failure? Then it is too vague.

## On honesty

If you did not establish something, say so. "The mechanism is inferred from
the deploy summary; we did not read the diff" is a useful sentence. A
postmortem that overstates its certainty teaches the wrong lesson to whoever
reads it next.
