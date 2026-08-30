"""Reset the whole stack to a clean, running, healthy state.

Wipes every volume, rebuilds, re-registers the agent with TrueForge, waits
for a traffic baseline to build, warms the sandbox, and then checks the shop
is actually healthy rather than merely running.

The Makefile has the same target, but `make` is not installed on Windows by
default, and this project is meant to be runnable on any of the three
platforms without a detour into installing build tools.

    python scripts/fresh.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BASELINE_SECONDS = 110
STARTUP_SECONDS = 45


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(ROOT), **kwargs)


def countdown(seconds: int, label: str) -> None:
    print(f"\n{label} ({seconds}s)", flush=True)
    for remaining in range(seconds, 0, -10):
        print(f"  {remaining}s", end="\r", flush=True)
        time.sleep(min(10, remaining))
    print("  done      ", flush=True)


def main() -> int:
    print("=" * 62)
    print("  Resetting the stack. This takes about four minutes.")
    print("=" * 62)

    run(["docker", "compose", "down", "-v"])
    result = run(["docker", "compose", "up", "-d", "--build"])
    if result.returncode != 0:
        print("\ndocker compose failed to start. Is Docker Desktop running?")
        return 1

    countdown(STARTUP_SECONDS, "Waiting for services to come up")

    result = run([sys.executable, "scripts/setup_trueforge.py"])
    if result.returncode != 0:
        print("\nTrueForge configuration failed -- see the error above.")
        return 1

    # The agent compares against normal traffic, so there has to be some.
    countdown(BASELINE_SECONDS, "Building a traffic baseline")

    print("\nWarming the sandbox and the model connection...")
    run([
        sys.executable, "scripts/run_incident.py",
        "--prompt", "List the firing alerts. Nothing else.",
        "--approve-all",
    ])

    print("\n" + "=" * 62)
    print("  Health check")
    print("=" * 62)
    run([
        "docker", "compose", "exec", "-T", "checkout-api",
        "python", "-m", "chaos.main", "status",
    ])

    print(
        "\nIf error_rate above reads 0.0, the stack is healthy and ready.\n"
        "\n"
        "  The shop      http://localhost:8099\n"
        "  The harness   http://localhost:8790\n"
        "\n"
        "Break it from the shop's Operations panel, then ask the agent to\n"
        "investigate. See docs/how-it-works.md.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
