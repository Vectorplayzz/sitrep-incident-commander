# SITREP — Autonomous Incident Commander
### Build plan for The Agent Harness Hackathon (TrueForge × Qodo), deadline **Aug 30, 20:00 London**

---

## 0. Verified facts about the stack (researched, not assumed)

| Thing | Reality | Consequence for us |
|---|---|---|
| TrueForge CLI | `npx @truefoundry/trueforge@latest`, v0.1.4, Node 22+ | Local server + chat UI, SQLite state, default port **8790** |
| Agent definition | JSON spec: `model`, `instructions`, `mcp_servers[]`, `skills[]`, `config{}` | We check `agents/*.json` into the repo — judges import them |
| MCP transport | **Remote HTTP only.** No stdio. | Our tool server must be an HTTP MCP server on a URL |
| Approval gates | `mcp_servers[].require_approval_for_tools: ["..."]`; default `["@write","@destructive"]` | This is the scored "human pause". We name tools explicitly. |
| Subagents | **Dynamic only** — root calls built-in `create_sub_agent`. One level deep. Shares MCP tools + sandbox. Cannot ask the user questions. | Delegation strategy must be written into the **skill**, not config |
| Skills | A **git repo dir** rooted at `SKILL.md` (YAML frontmatter: `name`, `description`). Materialized at `/opt/tfy/skills/{name}`. **Requires sandbox.** | `skills/incident-command/` in this repo, registered by URL + path + ref |
| Sandbox | **Daytona only.** API key needs sandbox **and snapshot** permissions. | Hard external dependency — provision first |
| SDK | `@truefoundry/trueforge-sdk`, Agent→Session→Turn→Event→Delta, `createTurnStream`, resume via `user.tool_approval` | Powers the custom console + reconnect story |
| UI SDK | `@truefoundry/trueforge-ui` v0.2.4 | Custom incident console |
| Model | Ollama Cloud, OpenAI-compatible at `https://ollama.com/v1`, `Authorization: Bearer $OLLAMA_API_KEY` | **Risk: `/v1` degrades tool-calling on some models.** Smoke-test first. |

---

## 1. The pitch (one paragraph, reused in README + video + blog)

> When a service breaks at 3am, the on-call engineer does the same six things every time: read the alert, pull the logs, chart the error rate, find the deploy that did it, decide whether to roll back, and write the postmortem. SITREP is an agent that does all six — but it is **not allowed to touch production without a human saying yes**. It reaches real telemetry through MCP, does its statistics in a Daytona sandbox with pandas and scipy instead of guessing, delegates triage/analytics/forensics to parallel subagents, and stops dead at an approval gate before any rollback. Then it writes the postmortem and remembers the incident, so the second time it happens it is faster.

**Why this is not the cookbook `incident-investigator`:** that one reads Sentry and summarizes. SITREP *quantifies* (change-point detection, blast radius, deploy correlation), *acts* (gated rollback), and *learns* (incident memory across sessions).

---

## 2. Architecture

```
                      ┌──────────────────────────────────────┐
   docker compose up  │  VICTIM STACK (the thing that breaks)│
                      │  checkout-api  (fragile FastAPI)     │
                      │  loadgen       (steady traffic)      │
                      │  chaos         (injects the outage)  │
                      │  telemetry.db  (logs/metrics/deploys)│
                      └───────────────┬──────────────────────┘
                                      │ reads
                      ┌───────────────▼──────────────────────┐
                      │  SITREP MCP SERVER  :8931/mcp        │
                      │  ── read tools ──────────────────────│
                      │   get_alerts, get_logs, get_metrics, │
                      │   get_traces, get_deploys,           │
                      │   get_service_topology,              │
                      │   search_incident_memory             │
                      │  ── GATED write tools ───────────────│
                      │   rollback_service      [GATE]       │
                      │   scale_service         [GATE]       │
                      │   post_status_update    [GATE]       │
                      │   file_postmortem       [GATE]       │
                      └───────────────┬──────────────────────┘
                                      │ MCP (HTTP)
                      ┌───────────────▼──────────────────────┐
                      │  TRUEFORGE HARNESS  :8790            │
                      │  agent: sitrep-commander             │
                      │   skill: incident-command (this repo)│
                      │   sandbox: Daytona (pandas/scipy/mpl)│
                      │   dynamic subagents: ON              │
                      │   require_approval_for_tools: [4]    │
                      └───────────────┬──────────────────────┘
                                      │ SDK / UI SDK
                      ┌───────────────▼──────────────────────┐
                      │  INCIDENT CONSOLE  :5173  (React)    │
                      │  live timeline · subagent lanes ·    │
                      │  blast-radius chart · APPROVE/REJECT │
                      └──────────────────────────────────────┘
```

### The run, beat by beat (this is also the video script)

1. `make incident` — chaos injects a bad "deploy" of `checkout-api`; p99 latency and 5xx climb.
2. Alert fires and is dropped into the console.
3. Commander reads the alert, loads the `incident-command` skill.
4. Spawns **3 subagents in parallel** via `create_sub_agent`:
   - *Triage* — logs + traces, what is failing and where
   - *Analytics* — pulls the metric series, runs **Python in the Daytona sandbox**: change-point detection to find the exact break time, error-rate delta, blast-radius estimate, renders a chart
   - *Forensics* — deploy history + GitHub diff of the suspect commit
5. Commander synthesizes and names a root cause with a **confidence score and the evidence**.
6. **APPROVAL GATE**: "Roll back `checkout-api` to `v1.4.2`? This is irreversible." Agent halts. Console shows the pause. Human clicks approve.
7. Rollback executes. Metrics recover — verified by re-querying, not assumed.
8. **APPROVAL GATE 2**: post status update + file postmortem.
9. Postmortem written; incident indexed into memory.
10. **Encore:** `make incident` again. Commander hits `search_incident_memory`, recognizes the signature, and reaches the same conclusion in a fraction of the steps.

That encore is the moment that wins the demo.

---

## 3. Scoring map (6 equally-weighted criteria)

| Criterion | How we hit it |
|---|---|
| Potential Impact | On-call toil is universally hated and expensively human. Obvious hand-over. |
| Creativity & Originality | Sandbox *statistics* not summaries; incident memory; a shippable victim stack; runs on open-weight models |
| Technical Excellence | Reproducible outage, typed MCP server, tests, CI, one-command setup |
| Use of Sponsor Tools | Every harness feature: MCP, sandbox, skills, dynamic subagents, approvals, session persistence, generative UI, model flexibility |
| Control & Safety | 4 explicitly gated tools, dry-run previews, an audit log of every gated decision, credentials never enter the sandbox |
| Presentation | Custom console plus a tight 3-min video with the encore beat |

---

## 4. PR sequence (Qodo reviews every one — this is also the Q Branch entry)

`main` starts with README + LICENSE + .gitignore only. Everything else lands via PR.

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/victim-stack` | docker-compose, checkout-api, loadgen, chaos, telemetry schema, `make incident` |
| 2 | `feat/mcp-server` | HTTP MCP server, 7 read tools + 4 gated write tools, tests |
| 3 | `feat/agent-spec` | `agents/sitrep-commander.json`, model provider docs, approval config |
| 4 | `feat/incident-skill` | `skills/incident-command/SKILL.md`, playbook, analysis scripts |
| 5 | `feat/sandbox-analytics` | change-point detection, blast radius, chart rendering |
| 6 | `feat/incident-memory` | memory index, `search_incident_memory`, the encore path |
| 7 | `feat/console` | React incident console on the UI SDK |
| 8 | `docs/submission` | README, Qodo Code Review Evidence, architecture diagram, screenshots |

Rule: **High-severity Qodo findings are fixed, or dismissed with written reasoning in the thread.** Never merge without the review comment visible.

---

## 5. Timeline (from ~14:00 IST Aug 29 to deadline ~00:30 IST Aug 31)

| # | Window | Task | Gate to pass before moving on |
|---|---|---|---|
| 0 | +0:00–1:00 | **Accounts and smoke test** (see §6) | TrueForge boots; Ollama Cloud model completes a 2-hop tool call; Daytona sandbox runs `print(1+1)` |
| 1 | +1:00–4:00 | PR 1 victim stack | `make incident` produces a visible 5xx spike in telemetry.db |
| 2 | +4:00–8:00 | PR 2 MCP server | TrueForge lists all 11 tools under Connectors |
| 3 | +8:00–10:00 | PR 3 agent spec | Agent answers "what is broken?" using real tools |
| 4 | +10:00–13:00 | PR 4 skill and subagents | 3 subagents visibly spawn in parallel |
| 5 | +13:00–15:00 | **SLEEP** | non-negotiable; the video needs a functioning brain |
| 6 | +15:00–18:00 | PR 5 sandbox analytics | A real matplotlib chart comes back from Daytona |
| 7 | +18:00–20:00 | PR 6 incident memory, approval gates verified end-to-end | Agent halts at rollback; approve resumes it |
| 8 | +20:00–23:00 | PR 7 console | Approval card renders and works |
| 9 | +23:00–25:00 | PR 8 docs, README, Qodo evidence | Fresh clone to working in under 10 min |
| 10 | +25:00–27:00 | **Demo video** | 3:00 or under; shows the pause and the encore |
| 11 | +27:00–28:30 | Blog post and social posts | Field Report + Radio Traffic entries |
| 12 | +28:30 | **Submit** — do not wait for the deadline | Form submitted with 90 min to spare |

**Cut order if we slip:** console → incident memory → forensics subagent.
**Never cut:** approval gate, sandbox, README, video.

---

## 6. Things only you can do (do these first, in parallel with my building)

1. **Register** for the hackathon — https://forms.gle/dNHFh7wH8uJj4bZH8
2. **GitHub**: create a public repo, push the scaffold.
3. **Qodo**: sign in at https://app.qodo.ai/signin and install the GitHub app **on that repo**. Do it before PR #1 or the review trail has a hole in it.
4. **Daytona**: create an account, generate an API key with **sandbox + snapshot** permissions. This is the one thing that can hard-block us.
5. **Ollama Cloud**: generate an API key.
6. Optional: a GitHub PAT (read-only, public repos) so the forensics subagent can use the GitHub MCP server.

Put every key in `.env` — never commit it. `.gitignore` covers it.

---

## 7. Known risks

| Risk | Mitigation |
|---|---|
| Ollama Cloud `/v1` breaks nested tool calls | Smoke-test in hour 0. Fallback: Gemini free tier. Provider is one config line. |
| Daytona key lacks snapshot permission | Test in hour 0. Sandbox is required for **skills** too, so this blocks two features. |
| Subagents cannot ask questions, so approval must sit on the **root** | Gated tools are called by the commander, never delegated. Written into the skill. |
| Demo video runs long | Script it. Pre-warm the sandbox. Rehearse the encore once. |
| Qodo review trail is thin | Eight real PRs, each with a genuine review. Not cosmetic. |
