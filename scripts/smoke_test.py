"""End-to-end smoke test for the victim stack, without Docker.

Boots inventory-api and checkout-api as local uvicorn processes, drives
traffic through them, and asserts that the stack is healthy before the bad
deploy and visibly broken after it. This is what proves the incident is
real and reproducible rather than a hardcoded story.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "services"
DB = ROOT / "data" / "smoke.db"

INVENTORY_PORT = 8810
CHECKOUT_PORT = 8811

BASELINE_SECONDS = float(os.environ.get("SMOKE_BASELINE_S", "25"))
INCIDENT_SECONDS = float(os.environ.get("SMOKE_INCIDENT_S", "45"))
CONCURRENCY = 12

sys.path.insert(0, str(SERVICES / "common"))
import telemetry  # noqa: E402


def _python() -> str:
    venv = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
    return str(venv) if venv.exists() else sys.executable


def start_service(app_dir: Path, port: int, env_extra: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ, "SITREP_DB": str(DB), **env_extra}
    return subprocess.Popen(
        [
            _python(),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(app_dir),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=str(ROOT),
    )


async def wait_healthy(client, url: str, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = await client.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"{url} never became healthy")


async def drive(client, url: str, seconds: float) -> None:
    """Hold steady traffic against /checkout for the given duration."""
    stop = time.time() + seconds

    async def worker() -> None:
        while time.time() < stop:
            try:
                await client.post(f"{url}/checkout", timeout=10.0)
            except Exception:
                pass
            await asyncio.sleep(0.15)

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))


def window_stats(since: float, until: float) -> dict[str, float]:
    conn = telemetry.connect(str(DB), read_only=True)
    try:
        rows = conn.execute(
            "SELECT status, duration_ms FROM requests"
            " WHERE service = 'checkout-api' AND route = '/checkout'"
            " AND ts >= ? AND ts <= ?",
            (since, until),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"count": 0, "error_rate": 0.0, "p99_ms": 0.0}

    errors = sum(1 for r in rows if r["status"] >= 500)
    durations = sorted(r["duration_ms"] for r in rows)
    p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
    return {
        "count": len(rows),
        "error_rate": errors / len(rows),
        "p99_ms": p99,
    }


async def main() -> int:
    if DB.parent.exists():
        shutil.rmtree(DB.parent, ignore_errors=True)
    DB.parent.mkdir(parents=True, exist_ok=True)
    telemetry.init_db(str(DB))

    inventory = start_service(
        SERVICES / "inventory-api", INVENTORY_PORT, {"INVENTORY_LATENCY_MS": "28"}
    )
    checkout = start_service(
        SERVICES / "checkout-api",
        CHECKOUT_PORT,
        {"INVENTORY_URL": f"http://127.0.0.1:{INVENTORY_PORT}"},
    )

    import httpx

    failures: list[str] = []
    try:
        async with httpx.AsyncClient() as client:
            await wait_healthy(client, f"http://127.0.0.1:{INVENTORY_PORT}")
            await wait_healthy(client, f"http://127.0.0.1:{CHECKOUT_PORT}")

            print(f"[1/3] healthy baseline for {BASELINE_SECONDS:.0f}s ...")
            base_start = time.time()
            await drive(client, f"http://127.0.0.1:{CHECKOUT_PORT}", BASELINE_SECONDS)
            baseline = window_stats(base_start + 5, time.time())
            print(f"      baseline: {baseline}")

            print("[2/3] shipping the bad deploy (v1.5.0) ...")
            subprocess.run(
                [_python(), str(SERVICES / "chaos" / "main.py"), "incident"],
                env={**os.environ, "SITREP_DB": str(DB)},
                check=True,
            )

            print(f"[3/3] incident window for {INCIDENT_SECONDS:.0f}s ...")
            inc_start = time.time()
            await drive(client, f"http://127.0.0.1:{CHECKOUT_PORT}", INCIDENT_SECONDS)
            incident = window_stats(inc_start + 5, time.time())
            print(f"      incident: {incident}")

        if baseline["count"] < 50:
            failures.append(f"baseline traffic too thin: {baseline['count']} requests")
        if baseline["error_rate"] > 0.02:
            failures.append(f"baseline was not healthy: {baseline['error_rate']:.1%} errors")
        if baseline["p99_ms"] > 250:
            failures.append(f"baseline p99 too high: {baseline['p99_ms']:.0f}ms")
        if incident["p99_ms"] < baseline["p99_ms"] * 3:
            failures.append(
                f"incident did not degrade latency: p99 {baseline['p99_ms']:.0f}ms"
                f" -> {incident['p99_ms']:.0f}ms"
            )
        if not 0.05 <= incident["error_rate"] <= 0.80:
            failures.append(
                "incident error rate outside the useful 5-80% band:"
                f" {incident['error_rate']:.1%} (a partial outage is the point)"
            )

        conn = telemetry.connect(str(DB), read_only=True)
        alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
        conn.close()
        if alerts < 1:
            failures.append("no alert fired during the incident window")
        else:
            print(f"      alerts fired: {alerts}")

    finally:
        for proc in (checkout, inventory):
            proc.terminate()
        for proc in (checkout, inventory):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"\nPASS  baseline p99 {baseline['p99_ms']:.0f}ms / {baseline['error_rate']:.1%} errors"
        f"  ->  incident p99 {incident['p99_ms']:.0f}ms / {incident['error_rate']:.1%} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
