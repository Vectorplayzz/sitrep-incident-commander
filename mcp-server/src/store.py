"""Read and write access to the telemetry store, shaped for an agent.

Two principles run through this module:

1. **Return evidence, not conclusions.** The tools hand back raw-ish rows and
   honest aggregates. Nothing here computes "the root cause" or buckets
   requests by cart size, because working that out is the agent's job. A
   tool that pre-chews the answer makes the demo a puppet show.

2. **Stay cheap in tokens.** An agent pays for every row it reads. Aggregates
   are pre-bucketed, samples come back as CSV rather than JSON, and every
   list tool has a bounded limit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

DB_PATH = os.environ.get("SITREP_DB", "/data/telemetry.db")

# Upper bounds. An agent asking for a million rows is a bug, not a request.
MAX_LIMIT = 1000
MAX_WINDOW_MINUTES = 24 * 60


def _connect(read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return round(sorted_values[idx], 1)


# ---------------------------------------------------------------- read tools


def get_alerts(status: str = "firing", limit: int = 20) -> dict[str, Any]:
    limit = _clamp(limit, 1, 100)
    with _connect() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status = ? ORDER BY ts DESC LIMIT ?",
                (status, limit),
            ).fetchall()

    return {
        "alerts": [
            {
                "id": r["id"],
                "fired_at": _iso(r["ts"]),
                "age_minutes": round((time.time() - r["ts"]) / 60, 1),
                "service": r["service"],
                "name": r["name"],
                "severity": r["severity"],
                "summary": r["summary"],
                "status": r["status"],
                "labels": json.loads(r["labels"]),
            }
            for r in rows
        ]
    }


def get_metrics(
    service: str,
    route: str | None = None,
    window_minutes: int = 30,
    bucket_seconds: int = 30,
) -> dict[str, Any]:
    """Bucketed request metrics: volume, error rate, and latency percentiles."""
    window_minutes = _clamp(window_minutes, 1, MAX_WINDOW_MINUTES)
    bucket_seconds = _clamp(bucket_seconds, 5, 3600)
    since = time.time() - window_minutes * 60

    sql = (
        "SELECT ts, status, duration_ms FROM requests"
        " WHERE service = ? AND ts >= ?"
    )
    params: list[Any] = [service, since]
    if route:
        sql += " AND route = ?"
        params.append(route)
    sql += " ORDER BY ts"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    buckets: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        key = int(row["ts"] // bucket_seconds) * bucket_seconds
        buckets.setdefault(key, []).append(row)

    series = []
    for key in sorted(buckets):
        group = buckets[key]
        durations = sorted(r["duration_ms"] for r in group)
        errors = sum(1 for r in group if r["status"] >= 500)
        series.append(
            {
                "ts": _iso(key),
                "epoch": key,
                "count": len(group),
                "errors": errors,
                "error_rate": round(errors / len(group), 4),
                "p50_ms": _percentile(durations, 0.50),
                "p95_ms": _percentile(durations, 0.95),
                "p99_ms": _percentile(durations, 0.99),
            }
        )

    total = len(rows)
    total_errors = sum(1 for r in rows if r["status"] >= 500)
    return {
        "service": service,
        "route": route,
        "window_minutes": window_minutes,
        "bucket_seconds": bucket_seconds,
        "total_requests": total,
        "overall_error_rate": round(total_errors / total, 4) if total else 0.0,
        "series": series,
    }


def get_request_sample(
    service: str,
    route: str | None = None,
    window_minutes: int = 30,
    limit: int = 500,
    only_errors: bool = False,
) -> dict[str, Any]:
    """Raw per-request rows as CSV, including per-request attributes.

    This is the tool to reach for when an aggregate is not enough and you
    need to look for structure inside the failures. The `attrs` column
    carries whatever the service recorded about each request; what is in
    there, and whether it correlates with anything, is for the caller to
    work out.
    """
    window_minutes = _clamp(window_minutes, 1, MAX_WINDOW_MINUTES)
    limit = _clamp(limit, 1, MAX_LIMIT)
    since = time.time() - window_minutes * 60

    sql = (
        "SELECT ts, route, status, duration_ms, version, attrs FROM requests"
        " WHERE service = ? AND ts >= ?"
    )
    params: list[Any] = [service, since]
    if route:
        sql += " AND route = ?"
        params.append(route)
    if only_errors:
        sql += " AND status >= 500"
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    # CSV rather than JSON: roughly a third of the tokens for the same data,
    # and it drops straight into pandas.read_csv on the other side.
    attr_keys: list[str] = []
    parsed: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for row in rows:
        try:
            attrs = json.loads(row["attrs"]) or {}
        except (json.JSONDecodeError, TypeError):
            attrs = {}
        parsed.append((row, attrs))
        for key in attrs:
            if key not in attr_keys:
                attr_keys.append(key)

    header = ["epoch", "route", "status", "duration_ms", "version", *attr_keys]
    lines = [",".join(header)]
    for row, attrs in reversed(parsed):
        cells = [
            f"{row['ts']:.3f}",
            row["route"],
            str(row["status"]),
            f"{row['duration_ms']:.1f}",
            row["version"],
            *[str(attrs.get(k, "")) for k in attr_keys],
        ]
        lines.append(",".join(cells))

    return {
        "service": service,
        "rows": len(parsed),
        "truncated": len(parsed) >= limit,
        "columns": header,
        "csv": "\n".join(lines),
    }


def get_logs(
    service: str | None = None,
    level: str | None = None,
    contains: str | None = None,
    window_minutes: int = 30,
    limit: int = 50,
) -> dict[str, Any]:
    window_minutes = _clamp(window_minutes, 1, MAX_WINDOW_MINUTES)
    limit = _clamp(limit, 1, 200)
    since = time.time() - window_minutes * 60

    sql = "SELECT * FROM logs WHERE ts >= ?"
    params: list[Any] = [since]
    if service:
        sql += " AND service = ?"
        params.append(service)
    if level:
        sql += " AND level = ?"
        params.append(level.upper())
    if contains:
        sql += " AND (message LIKE ? OR fields LIKE ?)"
        params.extend([f"%{contains}%", f"%{contains}%"])
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {
        "window_minutes": window_minutes,
        "count": len(rows),
        "truncated": len(rows) >= limit,
        "logs": [
            {
                "ts": _iso(r["ts"]),
                "service": r["service"],
                "level": r["level"],
                "message": r["message"],
                "trace_id": r["trace_id"],
                "fields": json.loads(r["fields"]),
            }
            for r in reversed(rows)
        ],
    }


def get_traces(
    trace_id: str | None = None,
    service: str | None = None,
    only_errors: bool = True,
    window_minutes: int = 30,
    limit: int = 5,
) -> dict[str, Any]:
    """Distributed traces, reassembled from spans across services."""
    window_minutes = _clamp(window_minutes, 1, MAX_WINDOW_MINUTES)
    limit = _clamp(limit, 1, 25)
    since = time.time() - window_minutes * 60

    with _connect() as conn:
        if trace_id:
            trace_ids = [trace_id]
        else:
            sql = "SELECT DISTINCT trace_id FROM requests WHERE ts >= ?"
            params: list[Any] = [since]
            if service:
                sql += " AND service = ?"
                params.append(service)
            if only_errors:
                sql += " AND status >= 500"
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            trace_ids = [r["trace_id"] for r in conn.execute(sql, params).fetchall()]

        traces = []
        for tid in trace_ids:
            spans = conn.execute(
                "SELECT ts, service, route, method, status, duration_ms, span_id,"
                " parent_span FROM requests WHERE trace_id = ? ORDER BY ts",
                (tid,),
            ).fetchall()
            if not spans:
                continue
            traces.append(
                {
                    "trace_id": tid,
                    "started_at": _iso(spans[0]["ts"]),
                    "span_count": len(spans),
                    "total_duration_ms": round(
                        max(s["ts"] + s["duration_ms"] / 1000 for s in spans)
                        - min(s["ts"] for s in spans),
                        1,
                    )
                    * 1000
                    if len(spans) > 1
                    else round(spans[0]["duration_ms"], 1),
                    "spans": [
                        {
                            "service": s["service"],
                            "route": s["route"],
                            "status": s["status"],
                            "duration_ms": round(s["duration_ms"], 1),
                            "offset_ms": round((s["ts"] - spans[0]["ts"]) * 1000, 1),
                        }
                        for s in spans
                    ],
                }
            )

    return {"count": len(traces), "traces": traces}


def get_deploys(service: str | None = None, limit: int = 10) -> dict[str, Any]:
    limit = _clamp(limit, 1, 50)
    sql = "SELECT * FROM deploys"
    params: list[Any] = []
    if service:
        sql += " WHERE service = ?"
        params.append(service)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {
        "deploys": [
            {
                "deployed_at": _iso(r["ts"]),
                "minutes_ago": round((time.time() - r["ts"]) / 60, 1),
                "service": r["service"],
                "version": r["version"],
                "commit_sha": r["commit_sha"],
                "author": r["author"],
                "summary": r["summary"],
                "active": bool(r["active"]),
            }
            for r in rows
        ]
    }


# The topology is static for this stack; a real deployment would read it from
# a service catalogue or a service mesh.
TOPOLOGY = {
    "checkout-api": {
        "role": "public API, prices and places orders",
        "depends_on": ["inventory-api"],
        "routes": ["/checkout", "/health"],
        "source_path": "services/checkout-api",
        "upstream_budget_ms": 600,
        "note": (
            "Has no capacity limit of its own. When it returns 503 the cause"
            " is upstream time, not checkout-api saturation."
        ),
    },
    "inventory-api": {
        "role": "internal service, stock and warehouse lookups",
        "depends_on": [],
        "routes": ["/items/{item_id}", "/items/batch", "/health"],
        "source_path": "services/inventory-api",
    },
}


def get_service_topology() -> dict[str, Any]:
    with _connect() as conn:
        active = {
            r["service"]: r["version"]
            for r in conn.execute(
                "SELECT service, version FROM deploys WHERE active = 1"
            ).fetchall()
        }
        controls = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM controls").fetchall()
        }

    replicas = int(float(controls.get("inventory_replicas", 1)))
    capacity = {
        "inventory-api": {
            "replicas": replicas,
            # The only bounded resource in this stack. Worth knowing before
            # concluding that a latency problem must be a code change.
            "concurrent_lookup_capacity": replicas * 24,
        }
    }

    return {
        "services": [
            {
                "name": name,
                "active_version": active.get(name),
                **meta,
                **({"capacity": capacity[name]} if name in capacity else {}),
            }
            for name, meta in TOPOLOGY.items()
        ]
    }


def search_incident_memory(query: str = "", limit: int = 5) -> dict[str, Any]:
    """Past incidents this agent has already resolved and written up.

    Worth checking before a long investigation: if the current symptoms match
    something already seen, the previous root cause and resolution are
    usually a much faster path than starting from the logs again.
    """
    limit = _clamp(limit, 1, 20)
    with _connect() as conn:
        if query:
            pattern = f"%{query}%"
            rows = conn.execute(
                "SELECT * FROM incidents WHERE signature LIKE ? OR title LIKE ?"
                " OR root_cause LIKE ? ORDER BY opened_ts DESC LIMIT ?",
                (pattern, pattern, pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY opened_ts DESC LIMIT ?", (limit,)
            ).fetchall()

    return {
        "query": query,
        "count": len(rows),
        "incidents": [
            {
                "id": r["id"],
                "opened_at": _iso(r["opened_ts"]),
                "resolved_at": _iso(r["resolved_ts"]) if r["resolved_ts"] else None,
                "service": r["service"],
                "title": r["title"],
                "signature": r["signature"],
                "root_cause": r["root_cause"],
                "resolution": r["resolution"],
            }
            for r in rows
        ],
    }


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
