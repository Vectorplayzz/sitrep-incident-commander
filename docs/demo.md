# Demo script — 3 minutes

Rehearse once before recording.

**Measured from a clean rehearsal**, so plan the edit around these rather
than hoping:

| | wall clock | tool calls | subagents |
|---|---|---|---|
| first incident | 2–4 min | 30–40 | 3 |
| recurrence | ~3 min | 13 | 0 |

Both runs together are well over three minutes, so **the raw footage will not
fit and must be cut**. What to do about it:

- Speed the tool-call stretches to 3–4x. They read fine sped up — the point
  is the volume and variety of evidence gathered, not any individual call.
- Keep the **approval gate at real time**. It is the single most important
  thing in the video and it must not look rushed.
- Keep the change-point and discriminator numbers on screen long enough to
  read.
- For the recurrence, consider cutting straight from `search_incident_memory`
  to the conclusion. Everything between is the agent confirming what it
  already knows, and it is the *absence* of work that is the point.

## Before you hit record

```bash
make reset && make up                     # clean slate, no prior incidents
python scripts/setup_trueforge.py         # re-register after the volume reset
# wait ~90s for a baseline, then:
make status                               # must read 0.0 error rate
python scripts/run_incident.py --prompt "List the firing alerts." --approve-all
```

That last line is not part of the demo. It warms the Daytona sandbox and the
model connection, so the recorded run does not spend its first fifteen
seconds on a cold start. Then `make reset && make up` once more, wait for the
baseline, and record.

**Incident memory must be empty for the first run.** If a postmortem is
already filed, the agent will recognise the signature immediately and the
encore has nothing left to show. `make reset` clears it.

Terminal at ~110 columns. Larger font than feels necessary.

---

## 0:00 – 0:20 · The problem

> When a service breaks at 3am, the on-call engineer does the same six things
> every time. Read the alert. Pull the logs. Chart the error rate. Find the
> change. Decide what to do. Write it up.
>
> This is an agent that does all six — and is not allowed to touch production
> without asking.

On screen: `make status` — healthy, 0% errors, p99 ~34ms.

## 0:20 – 0:35 · Break it

```bash
make incident
```

> That ships a real regression. v1.5.0 prices carts by fetching each line
> from the inventory service individually instead of in one batch. It is a
> twenty-line diff that would pass code review.

Show `make status` climbing: error rate ~20%, p99 ~600ms. Alert fires.

## 0:35 – 2:05 · Hand it over

```bash
python scripts/run_incident.py
```

Narrate over the trace, but **do not talk over the approval gate**.

- **Subagents.** Three spawn at once — triage, analytics, forensics. Point at
  them: *"it delegates, and they run in parallel."*
- **The sandbox.** *"The analytics subagent pulls a thousand raw request rows
  and runs pandas and scipy in a Daytona sandbox. It is not eyeballing a
  chart."*
- **The finding.** Read the number out loud: *"It found the failures cluster
  on cart size. Effect size 0.99, p of 1.6 times ten to the minus 88. Nothing
  told it carts mattered — it tested every attribute and that is the one that
  separated failures from successes."*
- **The gate.** Let it land in silence for two seconds, then:
  *"And here it stops. It wants to roll back production, and it is not
  allowed to."*

Type `y` deliberately. Do not rush this.

- **Recovery.** *"It does not assume the fix worked."* Point at the line where
  it rejects a 2-request bucket as too small and re-queries.
- **Postmortem.** Second gate, approve, filed.

## 2:05 – 2:40 · The encore

```bash
make incident
python scripts/run_incident.py
```

> Same incident, second time.

> It checks its own incident memory first, recognises the signature it filed
> two minutes ago, and skips the entire investigation. Thirteen tool calls
> instead of thirty-three. No subagents at all. Half the tokens.

Be accurate here: **do not say "faster"**. Wall clock is slightly longer,
because the first run's three subagents ran in parallel. The win is in
resources, not latency. Saying otherwise invites a judge to check.

## 2:40 – 3:00 · Close

> Everything you just saw runs on open-weight models through Ollama Cloud.
> TrueForge did the work: real MCP tools, a real sandbox, parallel subagents,
> session state — and the approval gate, which is the reason an agent can be
> trusted with a rollback button at all.
>
> It ships with the thing that breaks, so you can run it yourself.

---

## If a take goes wrong

**The agent finishes too fast to narrate.** Fine — narrate over the recording
in post, or pause between the beats.

**It hits the gate before you have explained the subagents.** Approve, let it
finish, and re-record. Do not narrate out of order.

**It proposes scaling instead of a rollback.** Check `make status` shows
`v1.5.0` active and that traffic is at baseline (12 workers). A leftover
surge from a previous take changes the correct answer, and the agent will
correctly give a different one.

**The encore still spawns subagents.** Incident memory was not empty, or the
first postmortem was denied rather than approved. `make reset` and start
over.

**Sandbox timeout on the first analysis.** The Daytona snapshot went cold.
Run the warm-up command above and re-record.

## Alternative cuts

The three-minute limit does not fit everything. Ranked by what they prove:

1. **Full arc + encore** (this script) — shows the memory payoff.
2. **Full arc only** — calmer, more room to actually see the subagent
   fan-out and the chart.
3. **Deny then approve** — strongest control-and-safety story. Show the gate
   denied, the agent escalating to a human rather than routing around it,
   then a second run where it is approved.

`make surge` and `make degrade` are worth mentioning in the write-up even if
they do not fit on camera — they are what show the agent distinguishes
incident classes rather than always reaching for a rollback.
