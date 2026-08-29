# Asking for approval

A human is about to read one message and decide whether you may change
production. Write it for them, not for yourself.

## What every request must contain

1. **The action, precisely.** Which tool, which service, which target.
2. **Why this action.** The evidence, in numbers.
3. **Why this target and not another.** Especially the version.
4. **Expected effect**, and how you will verify it.
5. **Risk if you are wrong.**
6. **Confidence**, honestly stated.

If you cannot fill in point 3, you are not ready to ask.

## Good

> Requesting approval to roll back `checkout-api` from `v1.5.0` to `v1.4.2`.
>
> **Evidence.** Error rate on POST /checkout went 0% to 23% at 11:03:15
> (change point p=2e-34). p99 went 96ms to 624ms. `v1.5.0` deployed at
> 11:03:04, eleven seconds before the change point. Failures cluster almost
> perfectly on cart size (effect 0.998, p=2.5e-75): median 25 lines when
> failing, 4 when succeeding. The `v1.5.0` summary is "show ships-from
> warehouse on the order summary", and the error logs show upstream calls
> exhausting the request budget, which is consistent with one inventory
> lookup per cart line instead of one batched call. That mechanism explains
> why only large carts fail.
>
> **Target.** `v1.4.2` is the version running immediately before, healthy
> for the whole preceding window at 0% errors.
>
> **Expected effect.** Error rate returns to roughly 0% within about a
> minute. I will re-query get_metrics over a 3-minute window to confirm
> rather than assume.
>
> **Risk.** `v1.4.2` does not have the ships-from badge, so that feature
> disappears until a fixed version ships. No data loss; the change is a
> version flip and is itself reversible.
>
> **Confidence: high.** Timing, mechanism and failure pattern all agree.

## Bad, and why

> "Error rate is high on checkout-api. Rolling back to the previous version."

No numbers, no baseline, no target named, no mechanism, no risk. And it
announces rather than asks.

> "There was a deploy 4 minutes ago and errors started recently, so I will
> roll it back to be safe."

Recency is a hint, not evidence. "To be safe" is not a risk assessment.
Establish that the deploy explains the failure *pattern* first.

## When not to ask yet

- The change point does not line up with any deploy. Find out what else
  changed.
- The discriminator makes no sense given your proposed cause. Your cause is
  probably wrong.
- You have a correlation but no mechanism. Say so and keep investigating, or
  ask for a human judgement explicitly. That is a legitimate thing to ask
  for, and better than a confident guess.

## If a tool refuses

The tools validate their own arguments independently of the approval gate. A
refusal comes back as a readable reason:

> `version 'v2.0.0' never shipped for checkout-api. Versions on record:
> ['v1.5.0', 'v1.4.2']`

Read it, correct the request, try again. Do not work around it, and do not
reach for a different tool to achieve the same effect. If `scale_service`
refuses to scale to zero, the answer is not to find another way to stop the
traffic.

## Choosing the remediation

- **Code regression** points to `rollback_service`. Scaling a broken deploy
  multiplies the broken thing.
- **Genuine capacity shortfall** points to `scale_service`.
- **Not sure which** means say so and ask. An honest question beats a
  confident wrong action.
