# Setup

From nothing to a running incident in about ten minutes, most of which is
Docker pulling images and Daytona building a sandbox snapshot.

## What you need

- **Docker** — runs everything, including TrueForge itself
- **Python 3.12+** — runs the setup script
- **A model API key.** Any OpenAI-compatible endpoint works. The defaults
  target [Ollama Cloud](https://ollama.com), so the whole agent runs on
  open-weight models. See [model choice](#model-choice) below.
- **A [Daytona](https://daytona.io) API key with WRITE permissions.**

  This is the single most common setup failure, so it is worth being precise.
  TrueForge builds a snapshot in your Daytona account the first time it
  configures the provider. A **read-scoped key authenticates fine** — it will
  happily list sandboxes and snapshots — and then fails with a bare
  `Daytona rejected the API key`, which sends you hunting for a typo in a key
  that is not typo'd.

  When creating the key, grant **sandbox and snapshot create** permissions,
  not just read. `scripts/setup_trueforge.py` preflights this and tells you
  plainly if the key cannot write.

  The sandbox is not optional: skills are materialised into it, so without a
  working sandbox the agent loses its entire playbook.

## 1. Credentials

```bash
cp .env.example .env
```

Fill in `OLLAMA_API_KEY` and `DAYTONA_API_KEY`. The file is gitignored.

## 2. Start everything

```bash
docker compose up -d --build
```

(There is a `Makefile` with the same shortcuts, but `make` is not installed
on Windows by default, so every step here uses the underlying command.)

That brings up `checkout-api`, `inventory-api`, a load generator, the SITREP
MCP server, and **TrueForge itself** — the harness runs as a compose service
on `http://localhost:8790`.

Running the harness in a container is not a stylistic choice. TrueForge
v0.1.4 cannot start natively on Windows: it hands an absolute Windows path to
the ESM loader without a `file://` scheme and dies with
`Received protocol 'c:'`. Its local sandbox provider is also macOS/Linux
only. Containerising it sidesteps both, puts the harness on the same Docker
network as the MCP server (so the connector URL is service DNS rather than a
host loopback hop), and makes setup identical on every OS.

If you would rather run TrueForge on the host — fine on macOS and Linux:

```bash
docker compose up -d checkout-api inventory-api loadgen mcp-server
npx @truefoundry/trueforge@latest
python scripts/setup_trueforge.py --mcp-url http://localhost:8931/mcp
```

**Give it about 90 seconds.** The agent needs healthy traffic to compare
against — without a baseline, the change-point analysis has nothing to detect
a change *from*. Check it is settled:

```bash
docker compose exec -T checkout-api python -m chaos.main status
# {"active_version": "v1.4.2", "error_rate": 0.0, "p99_ms": 35.4}
```

## 3. Configure it

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

## 4. Break something

Open the shop at `http://localhost:8099`, expand **Operations**, and click
**Deploy checkout v1.5.0**. Or from the command line:

```bash
docker compose exec -T checkout-api python -m chaos.main incident
```

This ships `checkout-api v1.5.0`, which prices carts by fetching each line
from `inventory-api` individually instead of in one batch. Large carts blow
the 600 ms upstream budget; small ones do not. Within a minute or so the
error rate climbs to roughly 30% and an alert fires.

## 5. Hand it to the agent

Open `http://localhost:8790`, start a session with **sitrep-commander**, and
say:

> An alert is firing. Take it.

Then watch. It will orient, spawn subagents, run statistics in the sandbox,
and eventually **stop and ask you** for permission to roll back. Nothing
touches the stack until you approve.

Approve it, then place a 24-item order in the shop yourself. It goes
through.

## 6. The second time

Once it has filed a postmortem, deploy `v1.5.0` again and give it the same
instruction.

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
`docker compose ps` should show `trueforge` running. If it is running but
unreachable, check it is binding all interfaces — TrueForge defaults `HOST`
to `localhost`, which inside a container means the published port reaches
nothing. The image sets `HOST=0.0.0.0` for this reason.

**`Daytona rejected the API key`**
Almost always a read-scoped key rather than a wrong one. Confirm directly:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $DAYTONA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"permcheck","imageName":"ubuntu:24.04"}' \
  https://app.daytona.io/api/snapshots
```

`403` means read-only — create a new key with snapshot create permission.
`GET /api/snapshots` returning `200` proves nothing; reads are not the
problem.

**`skills require a sandbox provider`**
Downstream of the above. The agent cannot be created until the sandbox
provider is configured, because it references a skill.

**TrueForge will not start on Windows outside Docker**
Known upstream limitation in v0.1.4 and v0.2.0-rc.0. Use the compose
service.

**`--check` reports fewer than 13 tools**
The MCP server is unreachable from TrueForge. `docker compose ps` should
show `mcp-server` running; `curl http://localhost:8931/mcp` should not
connection-refuse.

**The agent says nothing is wrong**
You probably deployed `v1.5.0` less than a minute ago, or skipped the
90-second baseline. Check the health strip at the top of the shop.

**The agent proposes a rollback without investigating**
Check the skill registered: `python scripts/setup_trueforge.py --check`
should list `incident-command`. Without it the agent has tools but no
playbook. Skills also require the sandbox to be working.

**Reset everything**

```bash
python scripts/fresh.py
```

Wipes all telemetry, including filed incidents — which is what you want
before recording a demo, since incident memory would otherwise short-circuit
the first investigation.
