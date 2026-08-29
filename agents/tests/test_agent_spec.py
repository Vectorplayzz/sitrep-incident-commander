"""Contract tests for the agent spec.

The spec is a JSON file that nothing type-checks, wired to three things that
live elsewhere: a skill directory, an MCP server's tool names, and TrueForge's
own schema. Every one of those links can rot silently.

The failure that matters most: `require_approval_for_tools` naming a tool that
does not exist, or -- worse -- failing to name one that does. TrueForge does
not error on an approval entry that matches nothing. It just does not gate
that tool. A renamed tool would quietly turn the rollback into an unattended
action, and nothing would look wrong until it fired.

So this asserts the two lists agree in both directions, against the real
server rather than a copy of its names.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC_PATH = ROOT / "agents" / "sitrep-commander.json"

sys.path.insert(0, str(ROOT / "mcp-server" / "src"))


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest(spec) -> dict:
    return spec["manifest"]


@pytest.fixture(scope="module")
def server_tools() -> dict:
    """Tool name -> annotations, from the actual MCP server definition."""
    import server

    tools = asyncio.run(server.server.list_tools())
    return {t.name: t.annotations for t in tools}


# ------------------------------------------------------------------ structure


def test_spec_has_the_required_top_level_fields(spec):
    assert spec["name"] == "sitrep-commander"
    assert "manifest" in spec
    assert "model" in spec["manifest"], "model is the only required AgentSpec field"


def test_model_is_a_provider_qualified_name(manifest):
    name = manifest["model"]["name"]
    assert "/" in name, f"model must be provider/model, got {name!r}"
    provider, model = name.split("/", 1)
    assert provider and model


def test_config_keys_are_ones_trueforge_recognises(manifest):
    """A typo'd config key is silently ignored, not rejected."""
    allowed = {
        "sandbox",
        "generative_ui",
        "ask_user_questions",
        "dynamic_sub_agents",
        "context_management",
        "iteration_limit",
    }
    unknown = set(manifest["config"]) - allowed
    assert not unknown, f"unrecognised config keys: {sorted(unknown)}"


def test_mcp_server_keys_are_ones_trueforge_recognises(manifest):
    allowed = {
        "name",
        "enable_tools",
        "disable_tools",
        "preload",
        "preload_tools",
        "require_approval_for_tools",
    }
    for entry in manifest["mcp_servers"]:
        unknown = set(entry) - allowed
        assert not unknown, f"unrecognised mcp_servers keys: {sorted(unknown)}"


def test_iteration_limit_is_within_range(manifest):
    limit = manifest["config"]["iteration_limit"]
    assert 1 <= limit <= 1024


# -------------------------------------------------------------- capabilities


def test_sandbox_is_enabled_because_skills_require_it(manifest):
    assert manifest["config"]["sandbox"]["enabled"] is True, (
        "skills are materialised into the sandbox; with it disabled the agent"
        " silently loses its entire playbook"
    )


def test_subagents_are_enabled(manifest):
    assert manifest["config"]["dynamic_sub_agents"]["enabled"] is True


def test_the_referenced_skill_exists_in_this_repo(manifest):
    names = [s["name"] for s in manifest["skills"]]
    assert names == ["incident-command"]
    skill_md = ROOT / "skills" / "incident-command" / "SKILL.md"
    assert skill_md.exists(), f"agent references a skill with no SKILL.md at {skill_md}"
    assert skill_md.read_text(encoding="utf-8").startswith("---"), (
        "SKILL.md needs YAML frontmatter with name and description"
    )


# ------------------------------------------------- the approval gate contract


def test_every_approval_entry_names_a_real_tool(manifest, server_tools):
    gated = set(manifest["mcp_servers"][0]["require_approval_for_tools"])
    selectors = {t for t in gated if t.startswith("@")}
    literals = gated - selectors

    missing = literals - set(server_tools)
    assert not missing, (
        f"approval list names tools the server does not define: {sorted(missing)}."
        " TrueForge does not error on this -- the entry simply matches nothing."
    )


def test_every_world_changing_tool_is_gated(manifest, server_tools):
    """The one that would fail silently and dangerously."""
    gated = set(manifest["mcp_servers"][0]["require_approval_for_tools"])

    not_read_only = {
        name
        for name, ann in server_tools.items()
        if not (ann and ann.read_only_hint)
    }

    ungated = not_read_only - gated
    assert not ungated, (
        f"these tools change the world but are not behind the approval gate:"
        f" {sorted(ungated)}. They would run unattended."
    )


def test_no_read_only_tool_is_needlessly_gated(manifest, server_tools):
    """Gating reads trains the operator to click approve without reading."""
    gated = {
        t for t in manifest["mcp_servers"][0]["require_approval_for_tools"]
        if not t.startswith("@")
    }
    read_only = {
        name for name, ann in server_tools.items() if ann and ann.read_only_hint
    }
    over_gated = gated & read_only
    assert not over_gated, f"read-only tools behind an approval gate: {sorted(over_gated)}"


def test_preloaded_tools_exist_and_are_read_only(manifest, server_tools):
    preload = manifest["mcp_servers"][0].get("preload_tools", [])
    for name in preload:
        assert name in server_tools, f"preload_tools names an unknown tool: {name}"
        ann = server_tools[name]
        assert ann and ann.read_only_hint, (
            f"{name} is preloaded into context but is not read-only"
        )


def test_instructions_state_the_approval_rule(manifest):
    """The gate is enforced by the harness, but the model should know why."""
    instructions = manifest["instructions"].lower()
    assert "approval" in instructions
    assert any(word in instructions for word in ("do not change", "without human"))
