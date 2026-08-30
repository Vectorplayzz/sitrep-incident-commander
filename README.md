# SITREP — Autonomous Incident Commander

An on-call agent that investigates a production incident end to end, proves
its conclusions with statistics rather than guessing, and **stops dead and
asks a human before it touches production**.

Built on [TrueForge](https://github.com/truefoundry/trueforge) for
[The Agent Harness Hackathon](https://www.wemakedevs.org/hackathons/trueforge).

```bash
cp .env.example .env    # add your model + Daytona keys
make up                 # stack + harness, wait ~90s for a baseline
python scripts/setup_trueforge.py
```

Then open two tabs and drive the whole thing from the browser:

| | |
|---|---|
| **http://localhost:8099** | the shop. Break it from the Operations panel, watch orders fail. |
| **http://localhost:8790** | the harness. Tell the agent to investigate, approve its fix. |

Setup: **[docs/setup.md](docs/setup.md)** ·
How the bug works: **[docs/how-it-works.md](docs/how-it-works.md)**

---

## The job it does

When a service breaks at 3am, the on-call engineer does the same six things
every time: read the alert, pull the logs, chart the error rate, find the
change that caused it, decide what to do, and write the postmortem.

SITREP does all six. It reaches real telemetry over MCP, delegates triage,
analytics and forensics to parallel subagents, runs pandas and scipy in a
Daytona sandbox to find when things broke and what the failures have in
common — and then **halts at an approval gate** before any rollback, scaling
change, or outward-facing message. Afterwards it files a postmortem and
remembers the incident.

## It ships with the things that break

Demoing an incident responder requires an incident. This repo contains a
small e-commerce backend and three genuine, reproducible failures — not
scripted outcomes, but real behaviour of a real system under stress.

```bash
make incident   # a code regression: N+1 upstream lookups
make surge      # demand outgrows capacity: no deploy, no code change
make degrade    # the upstream dependency slows down
```

Crucially, **the correct response differs in each case**, and the evidence
that distinguishes them is emergent rather than labelled:

| | regression | capacity | dependency |
|---|---|---|---|
| checkout-api errors | 32% | 1–2.7% | 39.6% |
| clustered on cart size | **82.6% vs 0.0%** | uniform | uniform |
| inventory-api latency | 28 ms | 28 ms | **579 ms** |
| inventory-api errors | 0% | 0% | 0% |
| traffic volume | normal | **57×** | normal |
| deploy at change point | **yes** | no | no |
| correct action | roll back | scale up | neither — escalate |

An agent that pattern-matches "alert → roll back the last deploy" gets two
of these three wrong.

## What it actually did

Not a description of intent — measured output from real runs, with the agent
running entirely on **open-weight models** via Ollama Cloud.

**The regression** — 77s, 33 tool calls, 3 parallel subagents, 2 approval
gates. It found the change point at p=1.16e-31 and independently identified
`cart_lines` as the discriminator (effect size 0.991, p=1.6e-88, median 25
lines when failing versus 4 when succeeding). Nothing told it carts mattered.
After the approved rollback it checked recovery and said:

> The last bucket shows 0 errors and p99 back to 30ms, but only 2 requests —
> too small to declare recovery.

then re-queried twice before accepting it.

**The capacity incident** — it ruled the deploy out explicitly ("v1.4.2
shipped 62 minutes earlier and ran clean"), used the *absence* of a
discriminator as evidence ("failures are uniform, not clustered on any
request shape, which rules out a per-request code bug"), and chose
`scale_service` over `rollback_service`. It reported **medium** confidence
and named what it could not verify rather than rounding up to certainty.

**The dependency failure** — the hardest of the three, and the most
interesting result. It correctly separated this from the capacity incident
("request volume was normal, ~150 req/30s, versus ~10k during the surge"),
confirmed the failures were uniform (Mann-Whitney p=0.30 on cart size), and
localised the latency to `inventory-api`'s batch endpoint. Then it declined
to act at all:

> There is no rollback target... this isn't clearly a capacity problem, so
> `scale_service` would be treating a symptom I haven't confirmed. The honest
> position is that the real fix is engineering work on the batch endpoint.

Zero approval gates on that run, which is the correct outcome — no tool it
holds can fix a slow dependency.

It did misattribute the *trigger*, blaming the batching change in
`checkout-api v1.4.2` rather than the upstream degradation. It marked that
medium confidence and separated measured from inferred, but it was wrong.
Two things contributed, and both were bugs in this repo that the agent found
by complaining about them: `inventory-api` had no deploy record at all, so it
looked unchangeable and the only service with a deploy history absorbed the
blame; and `chaos restore` reset the replica count silently, leaving the
audit log showing a scale-up with no matching scale-down. Both are fixed.

**When denied** — told no at the gate, it stopped and escalated to a human
instead of looking for another route.

**The second time** — a recurrence of the regression resolved with 13 tool
calls instead of 33, no subagents at all, and half the tokens, because
`search_incident_memory` matched the signature it had filed earlier. Wall
clock was actually *longer* (116s vs 77s), since the first run's subagents
ran in parallel — the win is in resources, not latency.

## Architecture

```
   docker compose up
        │
        ├─ checkout-api ─→ inventory-api      the system that breaks
        ├─ loadgen                            steady traffic = a baseline
        ├─ chaos                              the three scenarios
        │
        ├─ MCP SERVER :8931/mcp
        │    9 read tools    alerts, metrics, request samples, logs,
        │                    traces, deploys, topology, incident memory,
        │                    audit log
        │    4 GATED tools   rollback_service, scale_service,
        │                    post_status_update, file_postmortem
        │
        └─ TRUEFORGE :8790
             agent: sitrep-commander
             skill: incident-command (this repo)
             sandbox: Daytona — pandas, scipy, matplotlib
             dynamic subagents · approval gates · session persistence
```

### Harness capabilities used

| capability | how |
|---|---|
| **MCP tools** | 13 tools over remote HTTP, annotated so the harness knows which are destructive |
| **Sandbox** | change-point detection, correlation discovery and chart rendering in Daytona |
| **Skills** | the playbook is a git directory in this repo, materialised into the sandbox |
| **Subagents** | triage, analytics and forensics spawned in parallel via `create_sub_agent` |
| **Approvals** | four tools gated; verified to halt, to resume on approval, and to escalate on denial |
| **Session persistence** | incident memory makes a recurrence measurably cheaper |
| **Model flexibility** | any OpenAI-compatible endpoint; four open-weight models verified |

## Control and safety

The approval gate is the reason an agent can be trusted with these tools at
all, so it is defended in more than one place.

- **Four tools are gated.** `require_approval_for_tools` names them
  explicitly rather than relying on the `@write` / `@destructive` selectors,
  because those resolve through MCP annotations, and depending on two things
  being right is worse than depending on one.
- **A test asserts the gate cannot silently disappear.** TrueForge does not
  error when an approval entry names a tool that does not exist — it simply
  does not gate it. `test_every_world_changing_tool_is_gated` introspects the
  real MCP server and asserts every non-read-only tool is covered. Removing
  one fails the suite.
- **The tools validate their own arguments.** An approval means "do the thing
  you described", not "do anything". A rollback target must be a version that
  actually shipped; scaling below one replica is refused as a second outage
  rather than a remediation; status updates go to an allowlisted channel.
- **Every attempt is audited**, refusals included, via `get_audit_log`.
- **Subagents never hold gated tools.** They cannot ask a human anything, so
  delegating an irreversible action would strand it.

## Honest limitations

- The stack is a simulation. The failures are real behaviour of real
  processes under real load, but it is not production traffic.
- `scale_service` only changes capacity for `inventory-api`, the one service
  with a bounded worker pool. Asked to scale anything else, it reports that
  it changed nothing rather than pretending.
- The dependency scenario has no remediation tool. That is deliberate: the
  correct outcome is escalation, and an agent should be able to reach it.
- Analysis quality depends on a baseline. A window containing only the outage
  yields no change point, which the tooling reports honestly rather than
  inventing one.

## Model flexibility

Everything runs on open-weight models. Four are registered by default, all
verified to chain dependent tool calls without inventing arguments or
reaching for irreversible tools unprompted:

| model | verdict | latency |
|---|---|---|
| `deepseek-v4-pro` | pass | 4.4 s |
| `glm-5.2` | pass | 7.0 s |
| `deepseek-v4-flash` | pass | 7.0 s |
| `kimi-k3` | pass | 8.8 s |

```bash
python scripts/model_smoke.py glm-5.3      # check any model yourself
```

Results in [`docs/model-compatibility.json`](docs/model-compatibility.json).
Nothing in the agent, tools or skill is provider-specific.

## Testing

```bash
pytest mcp-server/tests skills/incident-command/tests agents/tests -q
```

The tests are weighted toward the things that fail silently:

- **`test_reports_no_discriminator_when_failures_are_uniform`** — plants
  random failures and asserts the analysis reports *nothing*. A script that
  always finds a correlation would let the agent confabulate a root cause for
  any outage, which is worse than having no analysis at all.
- **`test_every_world_changing_tool_is_gated`** — described above.
- **Every refusal path** on the gated tools. An untested safety claim is just
  a comment.

CI runs all three suites plus a compose config check.

## Qodo Code Review Evidence

Every substantive change in this repository went through a pull request
reviewed by Qodo before merge. No direct pushes to `main`.

Representative PR: **[#1 — reproducible victim stack](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/1)**

Qodo raised four findings; all four were fixed rather than dismissed. The
most valuable was *"Worker pool never saturates"*, which caught a comment
claiming a mechanism the arithmetic did not support: the N+1 is serial, so
12 concurrent workers never contend for 24 permits, and the outage was purely
serial latency exceeding the request budget. The behaviour was right; the
explanation was wrong. The
[response thread](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/1#issuecomment-5461639729)
covers all four.

All pull requests:
[#1](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/1) ·
[#2](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/2) ·
[#3](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/3) ·
[#4](https://github.com/Vectorplayzz/sitrep-incident-commander/pull/4)

## License

MIT — see [LICENSE](LICENSE).
