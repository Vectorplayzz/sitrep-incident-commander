# SITREP — Autonomous Incident Commander

An on-call agent that investigates a production incident end to end, does its
statistics in a sandbox instead of guessing, and **stops dead and asks a human
before it touches production**.

Built on [TrueForge](https://github.com/truefoundry/trueforge) for
[The Agent Harness Hackathon](https://www.wemakedevs.org/hackathons/trueforge).

> Status: in active development. See [PLAN.md](PLAN.md) for the build plan.

---

## The job it does

When a service breaks at 3am, the on-call engineer does the same six things
every time: read the alert, pull the logs, chart the error rate, find the
deploy that caused it, decide whether to roll back, and write the postmortem.

SITREP does all six. It reaches real telemetry over MCP, delegates triage,
analytics and forensics to parallel subagents, runs pandas and scipy in a
Daytona sandbox to find the exact moment things broke and how far the damage
spread, and then **halts at an approval gate** before any rollback or outward
-facing action. Afterwards it writes the postmortem and remembers the
incident, so the second occurrence resolves faster than the first.

## It ships with the thing that breaks

The hard part of demoing an incident responder is having an incident. This
repo includes a small e-commerce backend that has a genuine, reproducible
regression:

| | baseline (`v1.4.2`) | after the bad deploy (`v1.5.0`) |
|---|---|---|
| p99 latency | 69 ms | 602 ms |
| error rate | 0.0% | 32% |

The root cause is a real 20-line diff. `v1.5.0` added a "ships from" badge to
the order summary and fetched each cart line from `inventory-api` individually
instead of extending the existing batch call. Small carts still fit inside the
600 ms upstream budget; large ones do not:

```
large carts (>=15 lines)   n=121   errors=100   -> 82.6%
small carts (<15 lines)    n=295   errors=0     ->  0.0%
```

That correlation is the finding. It is not hardcoded anywhere — it falls out
of the system's actual behaviour, and the agent has to discover it.

## Quick start

```bash
cp .env.example .env      # add your model + Daytona keys
make up                   # start the stack, wait ~90s for a baseline
make incident             # ship the bad deploy
make status               # watch it burn
```

Full setup, agent import, and demo instructions: see `docs/`.

## Model flexibility

SITREP runs on any OpenAI-compatible endpoint. The default configuration
targets Ollama Cloud, so the entire agent — including multi-hop tool calling
and subagent delegation — runs on **open-weight models**. Candidates are
benchmarked for tool-calling fidelity by `scripts/model_smoke.py`; results in
`docs/model-compatibility.json`.

## Qodo Code Review Evidence

_Populated as pull requests land. Every substantive change in this repository
goes through a pull request reviewed by Qodo before merge._

## License

MIT — see [LICENSE](LICENSE).
