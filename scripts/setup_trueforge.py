"""Configure a local TrueForge instance for SITREP, in one command.

Registers the model provider, the Daytona sandbox, the MCP connector and the
skill, then creates the agent from `agents/sitrep-commander.json`.

This exists because the alternative is a judge following nine steps of
click-through from a README and giving up on step four. Everything here is
idempotent: run it twice and the second run updates rather than duplicates.

    python scripts/setup_trueforge.py
    python scripts/setup_trueforge.py --check      # verify, change nothing

Reads OLLAMA_API_KEY, OLLAMA_BASE_URL and DAYTONA_API_KEY from .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_SPEC = ROOT / "agents" / "sitrep-commander.json"

DEFAULT_BASE = "http://localhost:8790"
PROVIDER_NAME = "ollama-cloud"
CONNECTOR_NAME = "sitrep"
SKILL_NAME = "incident-command"

# Verified against Ollama Cloud with scripts/model_smoke.py. Every one of
# these chains dependent tool calls correctly; deepseek-v4-pro is the
# default because it was both fastest and cleanest.
MODELS = [
    ("deepseek-v4-pro", "deepseek-v4-pro:0813", 163_840),
    ("glm-5.2", "glm-5.2", 131_072),
    ("deepseek-v4-flash", "deepseek-v4-flash:0731", 163_840),
    ("kimi-k3", "kimi-k3", 262_144),
]

SKILL_DESCRIPTION = (
    "Run a production incident from alert to filed postmortem. Delegates"
    " triage, analytics and forensics to parallel subagents, quantifies"
    " impact with statistics in the sandbox, and stops for human approval"
    " before anything irreversible."
)


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


class TrueForge:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode(errors="replace")[:400]}
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"cannot reach TrueForge at {self.base} ({exc.reason}).\n"
                "Start it with:  docker compose up -d trueforge"
            ) from exc


def upsert(tf: TrueForge, path: str, manifest: dict, label: str) -> bool:
    """POST to create; fall back to PUT when it already exists."""
    status, body = tf.request("POST", path, {"manifest": manifest})
    if status < 300:
        print(f"  created  {label}")
        return True

    status, body = tf.request("PUT", path, {"manifest": manifest})
    if status < 300:
        print(f"  updated  {label}")
        return True

    print(f"  FAILED   {label}: HTTP {status} {json.dumps(body)[:300]}")
    return False


def configure_model_provider(tf: TrueForge, api_key: str, base_url: str) -> bool:
    manifest = {
        "type": "custom",
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "auth": {"api_key": api_key},
        "models": [
            {
                "name": name,
                "model_id": model_id,
                "properties": {"context_length": ctx},
            }
            for name, model_id, ctx in MODELS
        ],
    }
    return upsert(tf, "/api/v1/settings/model-providers", manifest,
                  f"model provider {PROVIDER_NAME} ({len(MODELS)} models)")


def preflight_daytona(api_key: str) -> bool:
    """Check the key can actually write before handing it to TrueForge.

    A read-scoped Daytona key authenticates fine and lists sandboxes happily,
    so it looks correct right up until TrueForge tries to build its snapshot
    and returns a bare 'Daytona rejected the API key'. That message sends you
    looking for a typo in a key that is not typo'd. Probing the write path
    here turns a confusing failure into an actionable one.
    """
    req = urllib.request.Request(
        "https://app.daytona.io/api/snapshots",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print("  FAILED   Daytona key cannot even read snapshots (HTTP"
                  f" {exc.code}). Check the key itself.")
            return False
    except urllib.error.URLError as exc:
        print(f"  warning  could not reach Daytona to preflight the key ({exc.reason});"
              " continuing anyway")
        return True

    # Reads work. Now the part that actually matters.
    probe = urllib.request.Request(
        "https://app.daytona.io/api/snapshots",
        data=json.dumps({"name": "sitrep-preflight", "imageName": "ubuntu:24.04"}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(probe, timeout=60) as resp:
            resp.read()
        print("  preflight  Daytona key can create snapshots")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print(
                "  FAILED   Daytona key is read-only.\n"
                "           It can list sandboxes and snapshots, but POST"
                " /api/snapshots returns 403.\n"
                "           TrueForge builds a snapshot on first configuration,"
                " so this key cannot work.\n"
                "           Create a new key at https://app.daytona.io with"
                " sandbox AND snapshot\n"
                "           create permissions, then put it in .env as"
                " DAYTONA_API_KEY.\n"
                "           Without a sandbox there are no skills either, so"
                " the agent loses its playbook."
            )
            return False
        # Anything else (a name conflict, a quota message) means the key can
        # write; the request was refused for an unrelated reason.
        print(f"  preflight  Daytona key accepted for writes (HTTP {exc.code})")
        return True
    except urllib.error.URLError as exc:
        print(f"  warning  Daytona preflight inconclusive ({exc.reason}); continuing")
        return True


def configure_sandbox(tf: TrueForge, api_key: str) -> bool:
    manifest = {
        "type": "daytona",
        "auth": {"api_key": api_key},
        # The analysis script pip-installs pandas/scipy/matplotlib on first
        # use, so give it room.
        "exec_timeout_ms": 300_000,
        # All three intervals are required in practice, despite the OpenAPI
        # schema listing them as optional. Omitting them returns HTTP 400
        # "expected number, received undefined".
        #
        # Stop idle sandboxes quickly so a demo run does not burn quota, but
        # never auto-delete (0): deletion would discard the built snapshot and
        # the next run would pay the multi-minute rebuild again.
        "auto_stop_interval_in_minutes": 15,
        "auto_archive_interval_in_minutes": 60,
        "auto_delete_interval_in_minutes": 0,
    }
    status, body = tf.request("PUT", "/api/v1/settings/sandbox-providers",
                              {"manifest": manifest})
    if status < 300:
        print("  configured  Daytona sandbox provider")
        print("              (first configuration builds a snapshot; can take a few minutes)")
        return True
    print(f"  FAILED   sandbox provider: HTTP {status} {json.dumps(body)[:300]}")
    return False


def configure_connector(tf: TrueForge, mcp_url: str) -> bool:
    manifest = {
        "type": "remote",
        "name": CONNECTOR_NAME,
        "url": mcp_url,
        "description": (
            "Telemetry and remediation for this stack: alerts, metrics, logs,"
            " traces, deploys, incident memory, and four approval-gated"
            " actions."
        ),
    }
    return upsert(tf, "/api/v1/settings/mcp-servers", manifest,
                  f"connector {CONNECTOR_NAME} -> {mcp_url}")


def configure_skill(tf: TrueForge, repo_url: str, ref: str) -> bool:
    manifest = {
        "type": "git",
        "name": SKILL_NAME,
        "url": repo_url,
        "path": "skills/incident-command",
        "ref": ref,
        "description": SKILL_DESCRIPTION,
    }
    return upsert(tf, "/api/v1/settings/skills", manifest,
                  f"skill {SKILL_NAME} <- {repo_url}@{ref}")


def create_agent(tf: TrueForge, model: str) -> bool:
    spec = json.loads(AGENT_SPEC.read_text(encoding="utf-8"))
    spec["manifest"]["model"]["name"] = f"{PROVIDER_NAME}/{model}"

    status, existing = tf.request("GET", "/api/v1/agents")
    match = None
    if status < 300:
        for agent in existing.get("agents", existing.get("data", [])) or []:
            if agent.get("name") == spec["name"]:
                match = agent
                break

    if match:
        status, body = tf.request("PUT", f"/api/v1/agents/{match['id']}", spec)
        verb = "updated"
    else:
        status, body = tf.request("POST", "/api/v1/agents", spec)
        verb = "created"

    if status < 300:
        print(f"  {verb}  agent {spec['name']} on {spec['manifest']['model']['name']}")
        return True
    print(f"  FAILED   agent: HTTP {status} {json.dumps(body)[:400]}")
    return False


def check(tf: TrueForge) -> int:
    """Report what is configured without changing anything."""
    problems = []
    for label, path, key in [
        ("model providers", "/api/v1/settings/model-providers", None),
        ("sandbox provider", "/api/v1/settings/sandbox-providers", None),
        ("connectors", "/api/v1/settings/mcp-servers", None),
        ("skills", "/api/v1/settings/skills", None),
        ("agents", "/api/v1/agents", None),
    ]:
        status, body = tf.request("GET", path)
        if status >= 300:
            problems.append(f"{label}: HTTP {status}")
            print(f"  {label:18} HTTP {status}")
        else:
            summary = json.dumps(body)
            print(f"  {label:18} {summary[:150]}")

    status, body = tf.request("GET", f"/api/v1/mcp-servers/{CONNECTOR_NAME}/tools")
    if status < 300:
        tools = body.get("tools", body.get("data", []))
        print(f"  {'connector tools':18} {len(tools)} discovered")
        if len(tools) < 13:
            problems.append(
                f"expected 13 tools from the {CONNECTOR_NAME} connector, saw {len(tools)}"
                " -- is the MCP server running? (docker compose up -d)"
            )
    else:
        problems.append(f"connector {CONNECTOR_NAME} tools unreachable: HTTP {status}")
        print(f"  {'connector tools':18} HTTP {status}")

    if problems:
        print("\nproblems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nOK")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("TRUEFORGE_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--mcp-url", default=os.environ.get("SITREP_MCP_URL", "http://mcp-server:8931/mcp"))
    parser.add_argument("--repo-url", default=os.environ.get(
        "SITREP_REPO_URL", "https://github.com/Vectorplayzz/sitrep-incident-commander"))
    parser.add_argument("--ref", default=os.environ.get("SITREP_SKILL_REF", "main"))
    parser.add_argument("--model", default=os.environ.get("SITREP_MODEL", MODELS[0][0]))
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    args = parser.parse_args()

    tf = TrueForge(args.base_url)
    print(f"TrueForge at {tf.base}\n")

    if args.check:
        return check(tf)

    ollama_key = os.environ.get("OLLAMA_API_KEY", "")
    daytona_key = os.environ.get("DAYTONA_API_KEY", "")
    if not ollama_key or not daytona_key:
        print("OLLAMA_API_KEY and DAYTONA_API_KEY must be set (see .env.example)")
        return 2

    ok = True
    ok &= configure_model_provider(tf, ollama_key, os.environ.get(
        "OLLAMA_BASE_URL", "https://ollama.com/v1"))
    if preflight_daytona(daytona_key):
        ok &= configure_sandbox(tf, daytona_key)
    else:
        ok = False
    ok &= configure_connector(tf, args.mcp_url)
    ok &= configure_skill(tf, args.repo_url, args.ref)
    ok &= create_agent(tf, args.model)

    if not ok:
        print("\nsome steps failed -- see above")
        return 1

    print(f"\nReady. Open {tf.base} and start a session with sitrep-commander.")
    print("Verify anytime with:  python scripts/setup_trueforge.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
