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

    sub.add_parser("incident", help="ship the known-bad version (v1.5.0)")
    sub.add_parser("heal", help="ship the known-good version (v1.4.2)")
    sub.add_parser("status", help="show active version and current health")

    args = parser.parse_args()
    if args.cmd == "deploy":
        deploy(args.version)
    elif args.cmd == "incident":
        deploy("v1.5.0")
    elif args.cmd == "heal":
        deploy("v1.4.2")
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
