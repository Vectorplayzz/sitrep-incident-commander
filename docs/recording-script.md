# Recording script

Everything you say and type, in order. Narrate live over your own screen.

The raw agent runs are longer than three minutes, so **record in four
separate clips and join them**. That is far easier solo than one perfect
take, and it means a fluffed line costs you one clip instead of everything.

---

## OBS setup

- **Base + output resolution:** 1920×1080, 30 fps. Nobody needs 60 for a
  terminal.
- **Scene:** one Display Capture, or Window Capture on the terminal. Add your
  mic as an Audio Input Capture.
- **Recording format:** mp4, quality "Indistinguishable" or CQP 18. You are
  capturing small text; compression artefacts on a terminal look terrible.
- **Hotkey:** bind Start/Stop Recording to a key you can hit without looking.

### Terminal

- **~110 columns wide**, and a **much larger font than feels comfortable** —
  16–18pt. Judges may watch this in a small embedded player. If you can read
  it at arm's length, it is big enough.
- Dark background, high contrast. Clear scrollback before each clip (`cls`).
- Turn off any notification popups. Windows Focus Assist on.

### Your voice

Talk slightly slower than feels natural, and **stop talking during the
approval gate**. Silence there is doing work.

---

## Before you record

```bash
make demo-prep
```

That wipes everything, rebuilds, re-registers the agent, builds a traffic
baseline, and warms the Daytona sandbox. Takes about four minutes. It ends by
printing the stack status.

**Do not record until `error_rate` reads `0.0`.** If it does not, wait 60
seconds and run `make status` again.

**Incident memory must be empty.** `make demo-prep` guarantees this. If you
re-record clip 3 or 4 you must run `make demo-prep` again, or the agent will
recognise the incident immediately and clip 3 will have no investigation in
it.

Have this file open on a second monitor or phone.

---

# Clip 1 — the problem (~25 seconds)

**Type:**

```bash
make status
```

**Say, over the output:**

> When a service breaks at three in the morning, the on-call engineer does the
> same six things every time. Read the alert. Pull the logs. Chart the error
> rate. Find the change that caused it. Decide what to do. Write it up.
>
> SITREP is an agent that does all six — and is not allowed to touch
> production without asking a human first.
>
> This is a small e-commerce stack, healthy. Zero percent errors, p99 around
> thirty-five milliseconds.

*Stop recording.*

---

# Clip 2 — break it (~25 seconds)

**Type:**

```bash
make incident
```

**Say:**

> This ships a real regression. Version 1.5.0 added a "ships from" badge to
> the order summary, and to get the warehouse it fetches every cart line from
> the inventory service individually, instead of in one batch call. It is a
> twenty-line diff that would pass code review.

**Wait about 90 seconds** — say nothing, you will cut this out. Then type:

```bash
make status
```

**Say:**

> Error rate climbing past twenty percent, p99 blown out to six hundred
> milliseconds. An alert fires on its own.

*Stop recording.*

---

# Clip 3 — hand it to the agent (~90 seconds in the edit)

This is the important clip. The run takes two to four minutes; you will speed
up the middle in the edit.

**Type:**

```bash
python scripts/run_incident.py
```

**Say, as the trace scrolls:**

> Here it goes. It reads the alert, checks whether it has seen this before,
> and pulls the service topology.

*When the three `subagent` lines appear:*

> And it delegates. Three subagents in parallel — one on triage, reading logs
> and traces. One on analytics. One on forensics, going through deploy
> history.

*When you see `exec(` and `get_request_sample`:*

> The analytics subagent pulls a thousand raw request rows and runs pandas
> and scipy in a Daytona sandbox. It is not eyeballing a chart — it is doing
> statistics.

*When the conclusion appears — read the actual numbers off your screen, they
change slightly every run:*

> And here is the finding. It tested every attribute on every request and
> found that failures cluster on cart size. Effect size zero point nine nine,
> p of ten to the minus one hundred. Nothing told it carts mattered. It
> worked that out from the data.

*The approval gate appears.* **Stop talking. Let two full seconds pass.**

> And it stops.
>
> It wants to roll back production, and it is not allowed to. The harness
> holds it here until a human decides.

*Type `y` slowly and deliberately. Do not rush this.*

> Approved.

*As it verifies:*

> It does not assume the fix worked. It queries the metrics — and here, it
> rejects its own first check, because the bucket only had two requests in
> it. It asks again before it will call this recovered.

*Second gate, for the postmortem:*

> Second gate for the write-up, because filing a postmortem is also something
> a human should sign off. Approved. Filed.

*Stop recording.*

---

# Clip 4 — the encore (~35 seconds in the edit)

**Type:**

```bash
make incident
```

*Wait ~90 seconds in silence, you will cut it.* Then:

```bash
python scripts/run_incident.py
```

**Say:**

> Same incident, a second time.

*When `search_incident_memory` appears:*

> It checks its own memory first, and recognises the signature it filed two
> minutes ago.

*When it reaches the conclusion:*

> Thirteen tool calls instead of thirty-three. No subagents at all. Half the
> tokens. It already knows what this is, so it goes straight to confirming
> it and asking for the rollback.

**Do not say "faster."** Wall clock is actually slightly longer, because the
first run's three subagents ran in parallel. The win is in resources, not
latency — and a judge who checks will find that out.

*Approve, let it finish, stop recording.*

---

# Clip 5 — the close (~20 seconds)

Record over the final screen, or over the repo README.

**Say:**

> Everything you just saw runs on open-weight models, through Ollama Cloud.
>
> TrueForge did the work underneath: real MCP tools, a real sandbox, parallel
> subagents, session state that persists between incidents — and the approval
> gate, which is the reason an agent can be trusted with a rollback button at
> all.
>
> It ships with the thing that breaks, so you can run the whole demo
> yourself with two commands.

*Stop recording.*

---

## Editing

Join the five clips in order. Then:

| section | what to do |
|---|---|
| clip 2's 90s wait | **cut entirely** |
| clip 3, tool-call stretches | speed to 3–4× |
| clip 3, **the approval gate** | **real time, untouched** |
| clip 3, the statistics on screen | hold long enough to read |
| clip 4's 90s wait | **cut entirely** |
| clip 4, the middle | speed to 4×, or cut straight from memory-lookup to conclusion |

Target 2:45–3:00. Under is better than over.

If you add captions, caption the approval gate at minimum — that is the beat
that carries the whole submission.

---

## If something goes wrong

**The agent proposes scaling instead of a rollback.**
Check `make status` shows `v1.5.0` active and `loadgen_workers: 12`. A
leftover surge from experimenting changes what the correct answer *is*, and
the agent will correctly give a different one. `make restore`, then
`make demo-prep`.

**Clip 3 has no subagents and finishes instantly.**
Incident memory was not empty. Run `make demo-prep` and re-record clips 3
and 4.

**Clip 4 still spawns subagents.**
The clip 3 postmortem was denied rather than approved, so nothing was filed.
Re-record clip 3 and approve both gates.

**The first analysis times out in the sandbox.**
The Daytona snapshot went cold. Re-run `make demo-prep` — the warm-up at the
end of it is exactly for this.

**It says nothing is wrong.**
You ran `make incident` less than a minute before the agent. Wait, check
`make status`, then start.

---

## Worth mentioning if you have room

Two more scenarios exist and prove the agent is not just pattern-matching
"alert → roll back the last deploy":

```bash
make surge      # demand outgrows capacity: correct answer is scale, not rollback
make degrade    # the dependency slows: correct answer is neither, escalate
```

They will not fit in three minutes. Put them in the write-up instead — the
contrast table in the README is the fastest way to show it.
