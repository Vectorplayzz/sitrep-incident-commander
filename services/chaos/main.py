"""chaos — the incident trigger.

`chaos deploy` ships the bad version of checkout-api. That is the whole
outage: one row in the deploys table. It is deterministic, reversible, and
completely honest about what it did, which is what makes the demo
reproducible on a judge's machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from pathlib import Path

_COMMON = Path(__file__).resolve().parent.parent / "common"
sys.path.insert(0, str(_COMMON) if _COMMON.exists() else "/app/common")
import telemetry  # noqa: E402

SERVICE = "checkout-api"

RELEASES = {
    "v1.4.2": {
        "commit_sha": "8c1f4ab",
        "author": "priya@example.com",
        "summary": "checkout: batch inventory lookups when pricing a cart",
    },
    "v1.5.0": {
        "commit_sha": "3d9e77c",
        "author": "marcus@example.com",
        "summary": "checkout: show ships-from warehouse on the order summary",
    },
}


def deploy(version: str) -> None:
    if version not in RELEASES:
        raise SystemExit(f"unknown version {version!r}; known: {list(RELEASES)}")
    meta = RELEASES[version]
    telemetry.init_db()
    telemetry.record_deploy(service=SERVICE, version=version, **meta)
    telemetry.log(
        service=SERVICE,
        level="info",
        message=f"deploy {version} rolled out to 100% of fleet",
        version=version,
        commit_sha=meta["commit_sha"],
        author=meta["author"],
    )
    print(f"deployed {SERVICE} {version} ({meta['commit_sha']}) — {meta['summary']}")


SURGE_WORKERS = 64
SURGE_THINK_TIME_MS = 60.0
DEGRADED_LATENCY_MS = 590.0


def surge(workers: int = SURGE_WORKERS) -> None:
    """Scenario 2: demand outgrows capacity. No deploy, no code change.

    checkout-api is on the good version and doing exactly one batch call per
    request. There is simply more traffic than inventory-api has worker slots,
    so queue time climbs until the request budget runs out. The correct
    remediation is capacity, not a rollback -- and the failures should be
    spread evenly across cart sizes, which is what tells the agent this is
    not the N+1 again.
    """
    telemetry.init_db()
    telemetry.set_control("loadgen_workers", workers)
    telemetry.set_control("loadgen_think_time_ms", SURGE_THINK_TIME_MS)
    telemetry.log(
        service="loadgen",
        level="info",
        message=(
            f"traffic surge: {workers} active clients,"
            f" think time {SURGE_THINK_TIME_MS:.0f}ms"
        ),
        workers=workers,
        think_time_ms=SURGE_THINK_TIME_MS,
    )
    print(
        f"traffic surge: {workers} clients at {SURGE_THINK_TIME_MS:.0f}ms think time"
        " (baseline 12 clients at 2000ms)"
    )


def degrade_inventory(latency_ms: float = DEGRADED_LATENCY_MS) -> None:
    """Scenario 3: the dependency is the problem, not the service that alerts.

    checkout-api will page, because checkout-api is what users hit. But it is
    healthy: its upstream got slow. An agent that stops at the alerting
    service gets this wrong.
    """
    telemetry.init_db()
    telemetry.set_control("inventory_latency_ms", latency_ms)
    telemetry.log(
        service="inventory-api",
        level="warn",
        message=f"lookup latency degraded to {latency_ms:.0f}ms",
        latency_ms=latency_ms,
    )
    print(f"inventory-api lookup latency degraded to {latency_ms:.0f}ms (baseline 28ms)")


def restore() -> None:
    """Undo every scenario knob and return to the healthy baseline."""
    telemetry.init_db()
    telemetry.set_control("loadgen_workers", 12)
    telemetry.set_control("loadgen_think_time_ms", 2000.0)
    telemetry.set_control("inventory_latency_ms", 28.0)
    telemetry.set_control("inventory_replicas", 1)
    deploy("v1.4.2")
    print("restored: baseline traffic, healthy upstream, 1 replica, v1.4.2")


def status() -> None:
    telemetry.init_db()
    conn = telemetry.connect(read_only=True)
    try:
        active = telemetry.active_version(SERVICE, default="(none)")
        since = time.time() - 120
        rows = conn.execute(
            "SELECT status, duration_ms FROM requests"
            " WHERE service = ? AND route = '/checkout' AND ts >= ?",
            (SERVICE, since),
        ).fetchall()
        errors = sum(1 for r in rows if r["status"] >= 500)
        durations = sorted(r["duration_ms"] for r in rows) or [0.0]
        p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
        print(
            json.dumps(
                {
                    "active_version": active,
                    "requests_last_120s": len(rows),
                    "error_rate": round(errors / len(rows), 4) if rows else 0.0,
                    "p99_ms": round(p99, 1),
                    "loadgen_workers": telemetry.get_control_int("loadgen_workers", 12),
                    "inventory_latency_ms": telemetry.get_control_float(
                        "inventory_latency_ms", 28.0
                    ),
                    "inventory_replicas": telemetry.get_control_int(
                        "inventory_replicas", 1
                    ),
                },
                indent=2,
            )
        )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="chaos")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_deploy = sub.add_parser("deploy", help="ship a version of checkout-api")
    p_deploy.add_argument("version", choices=sorted(RELEASES))

    sub.add_parser("incident", help="scenario 1: ship the known-bad version (v1.5.0)")
    p_surge = sub.add_parser("surge", help="scenario 2: demand outgrows capacity")
    p_surge.add_argument("--workers", type=int, default=SURGE_WORKERS)
    p_degrade = sub.add_parser(
        "degrade-inventory", help="scenario 3: the upstream dependency gets slow"
    )
    p_degrade.add_argument("--latency-ms", type=float, default=DEGRADED_LATENCY_MS)
    sub.add_parser("heal", help="ship the known-good version (v1.4.2)")
    sub.add_parser("restore", help="undo every scenario and return to baseline")
    sub.add_parser("status", help="show active version, controls, and health")

    args = parser.parse_args()
    if args.cmd == "deploy":
        deploy(args.version)
    elif args.cmd == "incident":
        deploy("v1.5.0")
    elif args.cmd == "surge":
        surge(args.workers)
    elif args.cmd == "degrade-inventory":
        degrade_inventory(args.latency_ms)
    elif args.cmd == "heal":
        deploy("v1.4.2")
    elif args.cmd == "restore":
        restore()
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
