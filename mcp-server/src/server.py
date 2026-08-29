"""The SITREP MCP server.

TrueForge connects to remote HTTP MCP servers, so this runs as a long-lived
streamable-HTTP service rather than a stdio subprocess.

Tool annotations matter here and are not decorative. TrueForge's approval
config accepts the selectors `@write` and `@destructive`, which resolve
against the MCP `readOnlyHint` / `destructiveHint` annotations. Getting these
right is what makes `require_approval_for_tools: ["@write", "@destructive"]`
actually stop the agent at the right moment, rather than silently letting a
rollback through because a tool forgot to declare itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions as act  # noqa: E402
import store  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

HOST = os.environ.get("SITREP_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("SITREP_MCP_PORT", "8931"))

INSTRUCTIONS = """\
Telemetry and remediation for the services in this stack.

Read tools return evidence, not conclusions. They will not tell you what
broke; they give you the alerts, metrics, logs, traces, deploy history and
raw request rows to work it out from.

Four tools change the real world -- rollback_service, scale_service,
post_status_update and file_postmortem. They are annotated so the harness
holds them behind a human approval gate. Propose them; do not expect them to
run unattended. Each also validates its own arguments and will refuse a
request that does not make sense, with a reason.

Before a long investigation, consider search_incident_memory. If this has
happened before, the previous root cause is a faster path than the logs.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

server = MCPServer(
    name="sitrep",
    title="SITREP incident telemetry",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


# ------------------------------------------------------------- read tools


@server.tool(annotations=READ_ONLY)
def get_alerts(status: str = "firing", limit: int = 20) -> dict:
    """List alerts. Start here: an alert names the service and the symptom.

    Args:
        status: "firing", "resolved", or "all".
        limit: Maximum alerts to return (1-100).
    """
    return store.get_alerts(status=status, limit=limit)


@server.tool(annotations=READ_ONLY)
def get_metrics(
    service: str,
    route: str | None = None,
    window_minutes: int = 30,
    bucket_seconds: int = 30,
) -> dict:
    """Bucketed request volume, error rate and latency percentiles.

    Use a window wide enough to include healthy traffic before the problem
    started, otherwise there is no baseline to compare against.

    Args:
        service: Service name, e.g. "checkout-api".
        route: Optional route filter, e.g. "/checkout".
        window_minutes: How far back to look (1-1440).
        bucket_seconds: Bucket width (5-3600). Narrow buckets locate a change
            point more precisely; wide buckets are less noisy.
    """
    return store.get_metrics(
        service=service,
        route=route,
        window_minutes=window_minutes,
        bucket_seconds=bucket_seconds,
    )


@server.tool(annotations=READ_ONLY)
def get_request_sample(
    service: str,
    route: str | None = None,
    window_minutes: int = 30,
    limit: int = 500,
    only_errors: bool = False,
) -> dict:
    """Raw per-request rows as CSV, including per-request attributes.

    Reach for this when aggregates are not enough and you need to look for
    structure inside the failures -- whether they cluster on some property of
    the request rather than being uniformly distributed. The `attrs` columns
    carry whatever the service recorded per request.

    The CSV drops straight into pandas.read_csv in the sandbox.

    Args:
        service: Service name.
        route: Optional route filter.
        window_minutes: How far back to look.
        limit: Maximum rows (1-1000).
        only_errors: Return only 5xx responses. Usually you want both, so you
            can compare failures against successes.
    """
    return store.get_request_sample(
        service=service,
        route=route,
        window_minutes=window_minutes,
        limit=limit,
        only_errors=only_errors,
    )


@server.tool(annotations=READ_ONLY)
def get_logs(
    service: str | None = None,
    level: str | None = None,
    contains: str | None = None,
    window_minutes: int = 30,
    limit: int = 50,
) -> dict:
    """Structured application logs.

    Args:
        service: Optional service filter.
        level: Optional level filter: INFO, WARN, ERROR.
        contains: Substring match against the message and its fields.
        window_minutes: How far back to look.
        limit: Maximum entries (1-200).
    """
    return store.get_logs(
        service=service,
        level=level,
        contains=contains,
        window_minutes=window_minutes,
        limit=limit,
    )


@server.tool(annotations=READ_ONLY)
def get_traces(
    trace_id: str | None = None,
    service: str | None = None,
    only_errors: bool = True,
    window_minutes: int = 30,
    limit: int = 5,
) -> dict:
    """Distributed traces reassembled from spans, to see where time went.

    Args:
        trace_id: Fetch one specific trace.
        service: Filter traces that touched this service.
        only_errors: Sample only failed requests.
        window_minutes: How far back to look.
        limit: Maximum traces (1-25). Traces are verbose; a handful is enough.
    """
    return store.get_traces(
        trace_id=trace_id,
        service=service,
        only_errors=only_errors,
        window_minutes=window_minutes,
        limit=limit,
    )


@server.tool(annotations=READ_ONLY)
def get_deploys(service: str | None = None, limit: int = 10) -> dict:
    """Deploy history: versions, commits, authors and when they shipped.

    A change in behaviour that starts close to a deploy is worth correlating,
    but proximity is not proof. Confirm it against the metrics.

    Args:
        service: Optional service filter.
        limit: Maximum deploys (1-50).
    """
    return store.get_deploys(service=service, limit=limit)


@server.tool(annotations=READ_ONLY)
def get_service_topology() -> dict:
    """Services, what they do, what they depend on, and their active versions.

    Useful for working out blast radius: which callers sit upstream of a
    failing service, and which of its dependencies could be the real cause.
    """
    return store.get_service_topology()


@server.tool(annotations=READ_ONLY)
def search_incident_memory(query: str = "", limit: int = 5) -> dict:
    """Past incidents already investigated and written up.

    Worth checking early. If the symptoms match something already seen, the
    recorded root cause and resolution are usually far faster than starting
    from the logs again. An empty query returns the most recent incidents.

    Args:
        query: Substring matched against signature, title and root cause.
        limit: Maximum incidents (1-20).
    """
    return store.search_incident_memory(query=query, limit=limit)


@server.tool(annotations=READ_ONLY)
def get_audit_log(limit: int = 50) -> dict:
    """Every change this agent has made or attempted, and the outcome.

    Args:
        limit: Maximum entries (1-200).
    """
    return act.get_audit_log(limit=limit)


# --------------------------------------------------- gated, world-changing


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False
    )
)
def rollback_service(service: str, target_version: str, reason: str) -> dict:
    """IRREVERSIBLE. Roll a service back to a previously shipped version.

    This changes what real users are served. Do not call it on a hunch:
    establish which deploy introduced the regression and what the last known
    good version was first. The target must be a version that actually
    shipped for this service.

    Args:
        service: Service to roll back.
        target_version: A version that appears in get_deploys for this service.
        reason: Why. This is recorded and ends up in the postmortem.
    """
    return act.rollback_service(
        service=service, target_version=target_version, reason=reason
    )


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True
    )
)
def scale_service(service: str, replicas: int, reason: str) -> dict:
    """IRREVERSIBLE. Change a service's replica count.

    Capacity treats symptoms. If the cause is a code regression, scaling buys
    time but does not fix anything. Scaling below one replica is refused.

    Args:
        service: Service to scale.
        replicas: Target replica count (1-20).
        reason: Why this is the right remediation.
    """
    return act.scale_service(service=service, replicas=replicas, reason=reason)


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=True
    )
)
def post_status_update(channel: str, message: str) -> dict:
    """Post an update visible to people outside this session.

    Other humans read this and act on it, so it cannot be walked back. Say
    what is known, what is not yet known, and what is being done. Do not
    speculate about a cause that has not been confirmed.

    Args:
        channel: One of "#incidents", "#status-page", "#engineering".
        message: The update. Plain text, up to 2000 characters.
    """
    return act.post_status_update(channel=channel, message=message)


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False
    )
)
def file_postmortem(
    service: str,
    title: str,
    signature: str,
    root_cause: str,
    resolution: str,
    postmortem_markdown: str,
    opened_at_epoch: float | None = None,
) -> dict:
    """File the incident write-up into permanent memory.

    The signature is what makes this findable later. Describe the shape of
    the failure -- service, symptom and mechanism -- specifically enough that
    the same incident recurring would produce a similar string, but without
    timestamps or IDs that will never match again.

    Args:
        service: Service the incident was about.
        title: One line, human readable.
        signature: Searchable fingerprint of the failure mode.
        root_cause: What actually caused it, with the evidence.
        resolution: What was done about it.
        postmortem_markdown: The full write-up: timeline, impact, follow-ups.
        opened_at_epoch: When the incident began, if known.
    """
    return act.file_postmortem(
        service=service,
        title=title,
        signature=signature,
        root_cause=root_cause,
        resolution=resolution,
        postmortem_markdown=postmortem_markdown,
        opened_at_epoch=opened_at_epoch,
    )


def main() -> None:
    print(f"sitrep mcp server on http://{HOST}:{PORT}/mcp", flush=True)
    server.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
