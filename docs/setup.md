# Setup

From nothing to a running incident in about ten minutes, most of which is
Docker pulling images and Daytona building a sandbox snapshot.

## What you need

- **Docker** — runs the stack SITREP investigates
- **Node 22+** — runs TrueForge
- **Python 3.12+** — runs the setup script
- **A model API key.** Any OpenAI-compatible endpoint works. The defaults
  target [Ollama Cloud](https://ollama.com), so the whole agent runs on
  open-weight models. See [model choice](#model-choice) below.
- **A [Daytona](https://daytona.io) API key**, with **sandbox and snapshot**
  permissions. TrueForge builds a snapshot in your account the first time it
  configures the provider, so a key without snapshot rights fails at setup.
  The sandbox is not optional here: skills require it.

## 1. Credentials

```bash
cp .env.example .env
```

Fill in `OLLAMA_API_KEY` and `DAYTONA_API_KEY`. The file is gitignored.

## 2. Start the stack

```bash
make up
```

That brings up `checkout-api`, `inventory-api`, a load generator, and the
SITREP MCP server on `http://localhost:8931/mcp`.

**Give it about 90 seconds.** The agent needs healthy traffic to compare
against — without a baseline, the change-point analysis has nothing to detect
a change *from*. Check it is settled:

```bash
make status
# {"active_version": "v1.4.2", "error_rate": 0.0, "p99_ms": 69.5}
```

## 3. Start TrueForge

```bash
npx @truefoundry/trueforge@latest
```

Leave it running. It serves the harness and chat UI on
`http://localhost:8790`.

## 4. Configure it

```bash
python scripts/setup_trueforge.py
```

This registers the model provider, the Daytona sandbox, the MCP connector and
the skill, then creates the `sitrep-commander` agent. It is idempotent — run
it again any time and it updates rather than duplicates.

The Daytona step is the slow one. The first configuration builds a snapshot
in your account, which takes a few minutes.

Verify:

```bash
python scripts/setup_trueforge.py --check
```

You want **13 tools** discovered from the `sitrep` connector. Fewer means the
MCP server is not reachable — check `docker compose ps`.

## 5. Break something

```bash
make incident
```

This ships `checkout-api v1.5.0`, which prices carts by fetching each line
from `inventory-api` individually instead of in one batch. Large carts blow
the 600 ms upstream budget; small ones do not. Within a minute or so the
error rate climbs to roughly 30% and an alert fires.

## 6. Hand it to the agent

Open `http://localhost:8790`, start a session with **sitrep-commander**, and
say:

> An alert is firing. Take it.

Then watch. It will orient, spawn subagents, run statistics in the sandbox,
and eventually **stop and ask you** for permission to roll back. Nothing
touches the stack until you approve.

Approve it, and confirm recovery yourself:

```bash
make status
```

## 7. The second time

Once it has filed a postmortem, run `make incident` again and give it the
same instruction.

This time it checks `search_incident_memory` first, recognises the
signature, and reaches the same conclusion in a fraction of the steps. That
is the difference between an agent that investigates and one that remembers.

---

## Model choice

Anything OpenAI-compatible works. Four Ollama Cloud models are registered by
default, all verified to chain dependent tool calls correctly:

| model | verdict | latency |
|---|---|---|
| `deepseek-v4-pro` | pass | 4.4 s |
| `glm-5.2` | pass | 7.0 s |
| `deepseek-v4-flash` | pass | 7.0 s |
| `kimi-k3` | pass | 8.8 s |

Switch with `--model`:

```bash
python scripts/setup_trueforge.py --model glm-5.2
```

To check a model yourself before trusting it with an incident:

```bash
python scripts/model_smoke.py glm-5.3
```

That runs a scenario which cannot be answered in one hop, so a model that
skips a step or invents an argument fails visibly rather than quietly. It
also checks the model does not reach for an irreversible tool without
approval. Results land in `docs/model-compatibility.json`.

`glm-5.3` passes but takes ~22 s per hop, which makes a three-subagent
investigation slow enough to be unpleasant to watch.

To use a different provider entirely, register it in TrueForge under
Settings → Model providers and pass `--model`. Nothing in the agent, the
tools or the skill is provider-specific.

---

## Troubleshooting

**`cannot reach TrueForge at http://localhost:8790`**
TrueForge is not running, or is on another port. Start it, or pass
`--base-url`.

**Sandbox provider fails to configure**
The Daytona key needs snapshot creation rights, not just sandbox rights.
TrueForge builds a release snapshot on first configuration. Without it,
skills and code execution both fail — and skills are where the playbook
lives.

**`--check` reports fewer than 13 tools**
The MCP server is unreachable from TrueForge. `docker compose ps` should
show `mcp-server` running; `curl http://localhost:8931/mcp` should not
connection-refuse.

**The agent says nothing is wrong**
You probably ran `make incident` less than a minute ago, or skipped the
90-second baseline. Check `make status`.

**The agent proposes a rollback without investigating**
Check the skill registered: `python scripts/setup_trueforge.py --check`
should list `incident-command`. Without it the agent has tools but no
playbook. Skills also require the sandbox to be working.

**Reset everything**

```bash
make reset && make up
```

Wipes all telemetry, including filed incidents — which is what you want
before recording a demo, since incident memory would otherwise short-circuit
the first investigation.
