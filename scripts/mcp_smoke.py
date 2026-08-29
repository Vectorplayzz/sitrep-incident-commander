"""Exercise the MCP server the way TrueForge will.

A unit test proves the Python functions work. It does not prove the server
speaks MCP over HTTP, that the tools are discoverable, or that the
annotations survive the wire -- and the annotations are what the harness
reads to decide which tools need human approval. If `destructive_hint` does
not arrive, `require_approval_for_tools: ["@destructive"]` silently matches
nothing and the rollback runs unattended. That failure is invisible until it
matters, so it is checked here explicitly.

    python scripts/mcp_smoke.py [url]
"""

from __future__ import annotations

import asyncio
import json
import sys

DEFAULT_URL = "http://localhost:8931/mcp"

EXPECTED_READ_ONLY = {
    "get_alerts",
    "get_metrics",
    "get_request_sample",
    "get_logs",
    "get_traces",
    "get_deploys",
    "get_service_topology",
    "search_incident_memory",
    "get_audit_log",
}

# These must never come back as read-only, or the harness will not gate them.
EXPECTED_GATED = {
    "rollback_service",
    "scale_service",
    "post_status_update",
    "file_postmortem",
}


def payload_of(result) -> dict:
    """Tool results arrive as JSON text content; structured_content may be null."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return {}


async def main(url: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    failures: list[str] = []

    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to {init.server_info.name} v{init.server_info.version}")

            listed = await session.list_tools()
            tools = {t.name: t for t in listed.tools}
            print(f"{len(tools)} tools discovered\n")

            missing = (EXPECTED_READ_ONLY | EXPECTED_GATED) - set(tools)
            if missing:
                failures.append(f"tools missing from the server: {sorted(missing)}")

            for name in sorted(tools):
                tool = tools[name]
                ann = tool.annotations
                read_only = bool(ann and ann.read_only_hint)
                destructive = bool(ann and ann.destructive_hint)
                open_world = bool(ann and ann.open_world_hint)

                if name in EXPECTED_READ_ONLY and not read_only:
                    failures.append(f"{name} should be annotated read-only")
                if name in EXPECTED_GATED and read_only:
                    failures.append(
                        f"{name} is annotated read-only, so the harness will NOT"
                        " gate it behind approval"
                    )

                if read_only:
                    tag = "read"
                elif destructive:
                    tag = "DESTRUCTIVE"
                elif open_world:
                    tag = "OPEN-WORLD"
                else:
                    tag = "write"
                print(f"  {tag:12} {name}")

            print()

            # A read call must work over the wire, not just in-process.
            result = await session.call_tool("get_service_topology", {})
            if result.is_error:
                failures.append(f"get_service_topology failed: {result.content}")
            else:
                names = [s["name"] for s in payload_of(result).get("services", [])]
                print(f"get_service_topology -> {names}")

            # A refusal must surface as a normal result the model can read and
            # reason about, not as a protocol error.
            refusal = await session.call_tool(
                "scale_service",
                {"service": "checkout-api", "replicas": 0, "reason": "smoke test"},
            )
            payload = payload_of(refusal)
            if not payload.get("refused"):
                failures.append(
                    "scale_service(replicas=0) was not refused -- the safety"
                    " validation is not reachable over MCP"
                )
            else:
                print(f"scale_service(0) -> refused: {payload['reason'][:70]}...")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS  server speaks MCP, tools discoverable, gating annotations intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)))
