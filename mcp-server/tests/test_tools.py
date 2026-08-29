"""Tests for the MCP tool layer.

The read tools get light coverage -- they are mostly SQL and the smoke test
exercises them against real traffic. The gated write tools get heavy
coverage, because every refusal path here is a safety claim the README
makes, and an untested safety claim is just a comment.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
COMMON = Path(__file__).resolve().parent.parent.parent / "services" / "common"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(COMMON))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A telemetry store with a healthy stretch, a bad deploy, and an outage."""
    path = tmp_path / "telemetry.db"
    monkeypatch.setenv("SITREP_DB", str(path))

    import telemetry

    telemetry.DB_PATH = str(path)
    telemetry.init_db(str(path))

    import store

    store.DB_PATH = str(path)

    now = time.time()
    conn = telemetry.connect(str(path))

    conn.execute(
        "INSERT INTO deploys (id, ts, service, version, commit_sha, author, summary,"
        " active) VALUES (?,?,?,?,?,?,?,0)",
        (uuid.uuid4().hex, now - 3600, "checkout-api", "v1.4.2", "8c1f4ab",
         "priya@example.com", "batch inventory lookups"),
    )
    conn.execute(
        "INSERT INTO deploys (id, ts, service, version, commit_sha, author, summary,"
        " active) VALUES (?,?,?,?,?,?,?,1)",
        (uuid.uuid4().hex, now - 600, "checkout-api", "v1.5.0", "3d9e77c",
         "marcus@example.com", "ships-from badge"),
    )

    # 10 minutes healthy, then 10 minutes of a partial outage on large carts.
    rows = []
    for i in range(600):
        ts = now - 1200 + i
        rows.append((uuid.uuid4().hex, ts, "checkout-api", "/checkout", "POST", 200,
                     45.0, uuid.uuid4().hex, uuid.uuid4().hex[:16], None, "v1.4.2",
                     '{"cart_lines": 4}'))
    for i in range(600):
        ts = now - 600 + i
        large = i % 3 == 0
        status = 503 if large else 200
        duration = 615.0 if large else 120.0
        lines = 22 if large else 4
        rows.append((uuid.uuid4().hex, ts, "checkout-api", "/checkout", "POST", status,
                     duration, uuid.uuid4().hex, uuid.uuid4().hex[:16], None, "v1.5.0",
                     '{"cart_lines": %d}' % lines))

    conn.executemany(
        "INSERT INTO requests (id, ts, service, route, method, status, duration_ms,"
        " trace_id, span_id, parent_span, version, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO alerts (id, ts, service, name, severity, summary, status, labels)"
        " VALUES (?,?,?,?,?,?,'firing','{}')",
        (uuid.uuid4().hex, now - 500, "checkout-api", "CheckoutHighErrorRate",
         "critical", "error rate 33% and p99 615ms"),
    )
    conn.commit()
    conn.close()

    import actions

    actions.__dict__["_connect"] = store._connect
    return str(path)


# ------------------------------------------------------------- read tools


def test_get_alerts_returns_firing_alert(db):
    import store

    result = store.get_alerts()
    assert result["alerts"], "expected the seeded alert"
    assert result["alerts"][0]["service"] == "checkout-api"
    assert result["alerts"][0]["severity"] == "critical"


def test_get_metrics_shows_the_change(db):
    import store

    result = store.get_metrics("checkout-api", route="/checkout", window_minutes=30,
                               bucket_seconds=60)
    series = result["series"]
    assert len(series) >= 10

    healthy = [b for b in series if b["error_rate"] == 0]
    broken = [b for b in series if b["error_rate"] > 0.1]
    assert healthy and broken, "window must contain both a baseline and an outage"
    assert max(b["p99_ms"] for b in broken) > max(b["p99_ms"] for b in healthy) * 3


def test_get_request_sample_is_csv_with_attrs(db):
    import store

    result = store.get_request_sample("checkout-api", window_minutes=30, limit=200)
    assert "cart_lines" in result["columns"], "per-request attrs must survive to CSV"
    header, *body = result["csv"].splitlines()
    assert header.startswith("epoch,route,status,duration_ms,version")
    assert len(body) == result["rows"]


def test_get_request_sample_preserves_the_correlation(db):
    """The signal the analytics subagent has to find must actually be there."""
    import store

    result = store.get_request_sample("checkout-api", window_minutes=12, limit=1000)
    idx = {name: i for i, name in enumerate(result["columns"])}
    large_errors = large_total = 0
    for line in result["csv"].splitlines()[1:]:
        cells = line.split(",")
        if cells[idx["version"]] != "v1.5.0":
            continue
        if int(cells[idx["cart_lines"]]) >= 15:
            large_total += 1
            large_errors += int(cells[idx["status"]]) >= 500
    assert large_total > 0
    assert large_errors / large_total > 0.9


def test_get_deploys_marks_the_active_version(db):
    import store

    deploys = store.get_deploys("checkout-api")["deploys"]
    active = [d for d in deploys if d["active"]]
    assert len(active) == 1
    assert active[0]["version"] == "v1.5.0"
    assert active[0]["commit_sha"] == "3d9e77c"


def test_topology_reports_the_dependency(db):
    import store

    services = {s["name"]: s for s in store.get_service_topology()["services"]}
    assert "inventory-api" in services["checkout-api"]["depends_on"]
    assert services["checkout-api"]["active_version"] == "v1.5.0"


def test_limits_are_clamped_not_trusted(db):
    import store

    result = store.get_request_sample("checkout-api", limit=10**9)
    assert result["rows"] <= store.MAX_LIMIT


# ------------------------------------------------- gated write tools: refusals


def test_rollback_refuses_a_version_that_never_shipped(db):
    import actions

    result = actions.rollback_service("checkout-api", "v9.9.9", "guessing")
    assert result["refused"] is True
    assert "never shipped" in result["reason"]
    assert "v1.4.2" in result["reason"], "refusal should name the real options"


def test_rollback_refuses_the_currently_active_version(db):
    import actions

    result = actions.rollback_service("checkout-api", "v1.5.0", "already there")
    assert result["refused"] is True
    assert "already running" in result["reason"]


def test_rollback_refuses_without_a_reason(db):
    import actions

    result = actions.rollback_service("checkout-api", "v1.4.2", "   ")
    assert result["refused"] is True


def test_scale_refuses_zero_replicas(db):
    """Scaling to zero is a second outage, not a remediation."""
    import actions

    result = actions.scale_service("checkout-api", 0, "stop the errors")
    assert result["refused"] is True
    assert "outage, not a remediation" in result["reason"]


def test_scale_refuses_above_the_ceiling(db):
    import actions

    assert actions.scale_service("checkout-api", 500, "more")["refused"] is True


def test_status_update_refuses_unknown_channel(db):
    import actions

    result = actions.post_status_update("#general", "hello")
    assert result["refused"] is True
    assert "allowlisted" in result["reason"]


def test_status_update_refuses_empty_and_oversized(db):
    import actions

    assert actions.post_status_update("#incidents", "  ")["refused"] is True
    assert actions.post_status_update("#incidents", "x" * 5000)["refused"] is True


def test_postmortem_refuses_without_a_root_cause(db):
    import actions

    result = actions.file_postmortem(
        service="checkout-api", title="t", signature="s", root_cause="",
        resolution="r", postmortem_markdown="m",
    )
    assert result["refused"] is True


# -------------------------------------------------- gated write tools: effects


def test_rollback_flips_the_active_version(db):
    import actions
    import store

    result = actions.rollback_service("checkout-api", "v1.4.2", "N+1 regression")
    assert result["ok"] is True
    assert result["rolled_back_from"] == "v1.5.0"
    assert result["now_running"] == "v1.4.2"

    deploys = store.get_deploys("checkout-api")["deploys"]
    active = [d for d in deploys if d["active"]]
    assert len(active) == 1, "exactly one version may be active"
    assert active[0]["version"] == "v1.4.2"


def test_postmortem_is_findable_afterwards(db):
    import actions
    import store

    actions.file_postmortem(
        service="checkout-api",
        title="Checkout 5xx after v1.5.0",
        signature="checkout-api:5xx:n+1-inventory-lookup",
        root_cause="v1.5.0 fetched each cart line individually",
        resolution="rolled back to v1.4.2",
        postmortem_markdown="# Postmortem\n\nDetails.",
    )

    found = store.search_incident_memory("n+1")
    assert found["count"] == 1
    assert found["incidents"][0]["signature"] == "checkout-api:5xx:n+1-inventory-lookup"
    assert found["incidents"][0]["resolved_at"] is not None


def test_every_attempt_is_audited_including_refusals(db):
    import actions

    actions.rollback_service("checkout-api", "v9.9.9", "bad")
    actions.scale_service("checkout-api", 0, "bad")
    actions.rollback_service("checkout-api", "v1.4.2", "good")

    log = actions.get_audit_log()
    assert log["count"] == 3
    results = [a["result"] for a in log["actions"]]
    assert sum(r.startswith("REFUSED") for r in results) == 2
    assert any(r.startswith("rolled back") for r in results)
