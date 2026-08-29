# Running the analysis

`scripts/analyze_incident.py` turns a raw request sample into numbers. It
runs in the sandbox, where the skill is materialised at
`/opt/tfy/skills/incident-command/`.

## Getting the data in

```
get_request_sample(service="checkout-api", route="/checkout",
                   window_minutes=30, limit=1000)
```

Write the `csv` field to a file. Make the window wide enough to include
healthy traffic from before the problem started: the change-point test needs
both sides, and a window containing only the outage will find nothing.

## Running it

```bash
python /opt/tfy/skills/incident-command/scripts/analyze_incident.py \
    sample.csv --out-dir . [--deploy-epoch 1788001384]
```

Pass `--deploy-epoch` once forensics has named a suspect deploy. It gets
drawn on the chart beside the change point, which makes the correlation
visible at a glance.

Dependencies install themselves on first run if the sandbox lacks them.

## What comes back

JSON on stdout, plus `incident_analysis.png`.

- **`change_point`** — when behaviour shifted, with a p-value, and
  before/after error rate, p99 and median latency. `found: false` means no
  statistically significant shift, which is itself an answer: whatever is
  wrong was already wrong when the window opened.

- **`blast_radius`** — failed requests, share of traffic, failures per
  minute, affected routes.

- **`discriminators`** — every request attribute that significantly separates
  failures from successes, ranked by effect size. Numeric attributes get a
  Mann-Whitney U test with a rank-biserial effect size; low-cardinality ones
  get chi-square with Cramer's V. An empty list means failures are uniformly
  distributed.

- **`within_affected_segment`** — a second pass inside the slice the top
  discriminator implicates. When the top result is something obvious such as
  "all failures are on v1.5.0", the interesting question is what separates
  failures *within* that version, and this is where that answer appears.

- **`interpretation`** — one sentence, safe to quote.

## Reading it honestly

The script does not know what this system does. It has no idea what a cart
is. It reports the structure that is present in the data.

So read the output before forming your explanation, not after. If it finds
that failures cluster on some attribute, your root cause has to explain *why
that attribute*. If it finds nothing, do not invent a pattern. Say the
failures are uniform and look for a global cause instead: the service itself,
a dependency, or the environment.

An effect size near 1.0 with a tiny p-value is a near-perfect separation and
is usually the heart of the incident. An effect size of 0.2 that is
technically significant across 10,000 rows is a footnote, not a finding.
