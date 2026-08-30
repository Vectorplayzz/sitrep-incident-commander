"""Drive an incident through the agent from the terminal.

Creates a session, hands the agent the incident, and streams what it does:
which tools it reaches for, when it spawns subagents, what the sandbox
returns. When it asks to change production, execution stops here and waits
for a real key press.

The approval prompt is the point. Nothing about this run touches the stack
until a person says so, and if you deny it, the agent is told why and has to
carry on without it.

    python scripts/run_incident.py
    python scripts/run_incident.py --approve-all     # unattended; CI only
    python scripts/run_incident.py --deny-all        # prove the gate holds
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The model emits arrows, em-dashes and box characters. On Windows the
# console defaults to cp1252 and raises UnicodeEncodeError mid-stream, which
# kills the run partway through an incident.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BASE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")
AGENT = "sitrep-commander"

PROMPT = (
    "An alert is firing. Take it: work out what broke, quantify the impact,"
    " and tell me what you want to do about it."
)

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
RED, GREEN, YELLOW, BLUE = "\033[31m", "\033[32m", "\033[33m", "\033[34m"


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


if not _supports_colour():
    DIM = BOLD = RESET = RED = GREEN = YELLOW = BLUE = ""


def request(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:400].decode()}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"cannot reach TrueForge at {base} ({exc.reason}).\n"
            "Start it with:  docker compose up -d trueforge"
        )


def stream_turn(base: str, session_id: str, items: list[dict]):
    """POST a turn and yield SSE events as they arrive."""
    body = json.dumps({"input": items, "stream": True}).encode()
    req = urllib.request.Request(
        f"{base}/api/v1/sessions/{session_id}/turns",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def subagent_brief(args: dict) -> str:
    """A one-line description of what a subagent was asked to do.

    The argument name create_sub_agent uses is not documented, so rather than
    guessing one key, take the longest string in the payload -- the brief is
    always the biggest field by a wide margin.
    """
    for key in ("instructions", "task", "prompt", "description", "goal"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:120]
    strings = [v for v in args.values() if isinstance(v, str)]
    if strings:
        return " ".join(max(strings, key=len).split())[:120]
    return "(no brief)"


def summarise_args(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)[:200]


def ask(tool_calls: list[dict], mode: str, known: dict[str, dict]) -> tuple[bool, str]:
    """Render the approval request and get a decision.

    The approval event carries only a tool_call id and its source event --
    no name, no arguments. Those arrived earlier in the delta stream, so
    the operator only sees what they are approving if the caller kept that
    buffer. An approval prompt that says '?' is worse than no prompt: it
    trains people to click yes on things they cannot read.
    """
    print()
    print(f"{BOLD}{YELLOW}{'=' * 74}{RESET}")
    print(f"{BOLD}{YELLOW}  APPROVAL REQUIRED — the agent wants to change production{RESET}")
    print(f"{BOLD}{YELLOW}{'=' * 74}{RESET}")
    for call in tool_calls:
        call_id = call.get("id") or call.get("tool_call_id") or ""
        buffered = known.get(call_id, {})
        function = call.get("function") or {}
        info = call.get("tool_info") or {}
        name = (
            function.get("name")
            or info.get("name")
            or buffered.get("name")
            or call.get("name")
            or call.get("tool_name")
            or "?"
        )
        raw = function.get("arguments")
        if raw is None:
            raw = call.get("arguments") or call.get("args") or buffered.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except json.JSONDecodeError:
                raw = {"raw": raw}
        print(f"\n  {BOLD}{name}{RESET}")
        for key, value in raw.items():
            print(f"    {DIM}{key}:{RESET} {value}")
    print()

    if mode == "approve":
        print(f"  {GREEN}auto-approved (--approve-all){RESET}")
        return True, ""
    if mode == "deny":
        print(f"  {RED}auto-denied (--deny-all){RESET}")
        return False, "Denied by policy for this run."

    try:
        answer = input(f"  {BOLD}approve? [y/N]{RESET} ").strip().lower()
    except EOFError:
        answer = "n"
    if answer in ("y", "yes"):
        return True, ""
    reason = input("  reason for denial (optional): ").strip()
    return False, reason or "Operator declined."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--agent", default=AGENT)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--approve-all", action="store_true")
    parser.add_argument("--deny-all", action="store_true")
    args = parser.parse_args()

    mode = "approve" if args.approve_all else "deny" if args.deny_all else "ask"
    base = args.base_url.rstrip("/")

    session = request(base, "POST", "/api/v1/sessions", {"agent": {"name": args.agent}})
    session_id = session.get("data", session).get("id")
    print(f"{DIM}session {session_id} · agent {args.agent}{RESET}\n")

    items: list[dict] = [{"type": "user.message", "content": args.prompt}]
    started = time.time()
    approvals = 0
    tool_calls_seen = 0
    subagents = 0

    tokens = {"input": 0, "output": 0}
    # Every tool call seen this session, by id. The approval event refers
    # to calls by id alone, so this is what makes the prompt readable.
    known_calls: dict[str, dict] = {}

    while items is not None:
        pending: list[dict] = []
        thread_id = None
        text_open = False
        # Tool calls stream in fragments: the name arrives once, the arguments
        # accumulate across deltas. Buffer by call id and report when the
        # model finishes the call rather than on every fragment.
        calls: dict[str, dict] = {}

        for event in stream_turn(base, session_id, items):
            kind = event.get("type", "")

            if kind == "model.message.delta":
                for fragment in event.get("tool_calls") or []:
                    call_id = fragment.get("id") or str(fragment.get("index", 0))
                    entry = calls.setdefault(call_id, {"name": "", "arguments": ""})
                    function = fragment.get("function") or {}
                    if function.get("name"):
                        entry["name"] = function["name"]
                    entry["arguments"] += function.get("arguments") or ""

                chunk = event.get("content")
                if chunk:
                    if not text_open:
                        print(f"\n{BOLD}agent{RESET}  ", end="")
                        text_open = True
                    print(chunk, end="", flush=True)

                if event.get("finish_reason") == "tool_calls":
                    if text_open:
                        print()
                        text_open = False
                    known_calls.update(calls)
                    for entry in calls.values():
                        if not entry["name"]:
                            continue
                        try:
                            parsed = json.loads(entry["arguments"] or "{}")
                        except json.JSONDecodeError:
                            parsed = {}
                        tool_calls_seen += 1
                        if entry["name"] == "create_sub_agent":
                            subagents += 1
                            print(f"  {BLUE}subagent{RESET} {subagent_brief(parsed)}")
                        else:
                            thread = event.get("thread_id")
                            prefix = (
                                f"  {DIM}tool{RESET}  "
                                if thread in (None, "main")
                                else f"  {BLUE}|{RESET} {DIM}"
                            )
                            suffix = "" if thread in (None, "main") else RESET
                            print(f"{prefix}{entry['name']}"
                                  f"({summarise_args(parsed)}){suffix}")
                    calls = {}

                usage = event.get("usage")
                if usage:
                    tokens["input"] += usage.get("input_tokens", 0)
                    tokens["output"] += usage.get("output_tokens", 0)

            elif kind == "tool.approval_required":
                if text_open:
                    print()
                    text_open = False
                pending = event.get("tool_calls", [])
                thread_id = event.get("thread_id")

            elif kind == "turn.done":
                if text_open:
                    print()
                    text_open = False
                state = event.get("state") or {}
                if state.get("status") not in ("done", None):
                    print(f"\n{RED}turn ended {state.get('status')}:{RESET}"
                          f" {json.dumps(state)[:300]}")

            elif kind in ("turn.failed", "turn.error"):
                print(f"\n{RED}turn failed:{RESET} {json.dumps(event)[:400]}")
                return 1

        if pending:
            allow, reason = ask(pending, mode, known_calls)
            approvals += 1
            decision = {"status": "allow"} if allow else {"status": "deny", "reason": reason}
            items = [
                {
                    "type": "user.tool_approval",
                    "thread_id": thread_id,
                    "tool_call_id": call.get("id") or call.get("tool_call_id"),
                    "approval": decision,
                }
                for call in pending
            ]
            print()
        else:
            items = None

    elapsed = time.time() - started
    print(
        f"\n{DIM}{'-' * 74}{RESET}\n"
        f"{DIM}done in {elapsed:.0f}s · {tool_calls_seen} tool calls ·"
        f" {subagents} subagents · {approvals} approval gates ·"
        f" {tokens['input']:,} in / {tokens['output']:,} out tokens{RESET}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
