"""The four tools that change something, and the rules they answer to.

Defence in depth. TrueForge holds these behind a human approval gate, and
that gate is the primary control -- nothing here runs until a person clicks
approve. But an approval is a human saying "yes, do the thing you described",
not "yes, do anything at all", so each tool validates its own arguments
before acting:

  - a rollback target must be a version that actually shipped
  - scaling to zero is an outage, not a remediation
  - status updates go to an allowlisted channel

A refusal here is a bug caught between approval and effect. Every attempt,
allowed or refused, is written to the audit log.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from store import _connect, _iso

ACTOR = "sitrep-agent"

# Scaling bounds. Zero replicas is not a remediation, it is a second outage,
# and no approval prompt phrased as "scale checkout-api" implies consent to
# take the service down.
MIN_REPLICAS = 1
MAX_REPLICAS = 20

ALLOWED_CHANNELS = {"#incidents", "#status-page", "#engineering"}
MAX_MESSAGE_CHARS = 2000


class ActionRefused(Exception):
    """Raised when a gated action fails validation. Never a crash; a verdict."""


def _audit(action: str, target: str, params: dict[str, Any], result: str) -> str:
    action_id = uuid.uuid4().hex
    with _connect(read_only=False) as conn:
        conn.execute(
            "INSERT INTO actions (id, ts, actor, action, target, params, result)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                action_id,
                time.time(),
                ACTOR,
                action,
                target,
                json.dumps(params, default=str),
                result,
            ),
        )
        conn.commit()
    return action_id


def _refuse(action: str, target: str, params: dict[str, Any], why: str) -> dict[str, Any]:
    _audit(action, target, params, f"REFUSED: {why}")
    return {"ok": False, "refused": True, "reason": why}


def rollback_service(
    service: str, target_version: str, reason: str
) -> dict[str, Any]:
    params = {"service": service, "target_version": target_version, "reason": reason}

    if not reason.strip():
        return _refuse(
            "rollback_service", service, params,
            "a rollback must carry a written reason; it ends up in the postmortem",
        )

    with _connect(read_only=False) as conn:
        known = conn.execute(
            "SELECT version, active FROM deploys WHERE service = ? AND version = ?"
            " ORDER BY ts DESC LIMIT 1",
            (service, target_version),
        ).fetchone()

        if known is None:
            shipped = [
                r["version"]
                for r in conn.execute(
                    "SELECT DISTINCT version FROM deploys WHERE service = ?"
                    " ORDER BY ts DESC LIMIT 10",
                    (service,),
                ).fetchall()
            ]
            return _refuse(
                "rollback_service", service, params,
                f"version {target_version!r} never shipped for {service}."
                f" Versions on record: {shipped or 'none'}",
            )

        if known["active"]:
            return _refuse(
                "rollback_service", service, params,
                f"{service} is already running {target_version}; nothing to roll back",
            )

        previous = conn.execute(
            "SELECT version FROM deploys WHERE service = ? AND active = 1",
            (service,),
        ).fetchone()

        row = conn.execute(
            "SELECT commit_sha, author, summary FROM deploys"
            " WHERE service = ? AND version = ? ORDER BY ts DESC LIMIT 1",
            (service, target_version),
        ).fetchone()

        conn.execute("UPDATE deploys SET active = 0 WHERE service = ?", (service,))
        conn.execute(
            "INSERT INTO deploys (id, ts, service, version, commit_sha, author,"
            " summary, active) VALUES (?,?,?,?,?,?,?,1)",
            (
                uuid.uuid4().hex,
                time.time(),
                service,
                target_version,
                row["commit_sha"],
                ACTOR,
                f"rollback to {target_version}: {reason}",
            ),
        )
        conn.execute(
            "INSERT INTO logs (id, ts, service, level, message, trace_id, fields)"
            " VALUES (?,?,?,?,?,NULL,?)",
            (
                uuid.uuid4().hex,
                time.time(),
                service,
                "INFO",
                f"rolled back to {target_version} by {ACTOR}",
                json.dumps({"reason": reason, "rolled_back_from": previous["version"] if previous else None}),
            ),
        )
        conn.commit()

    action_id = _audit("rollback_service", service, params, f"rolled back to {target_version}")
    return {
        "ok": True,
        "action_id": action_id,
        "service": service,
        "rolled_back_from": previous["version"] if previous else None,
        "now_running": target_version,
        "note": (
            "Traffic shifts within a couple of seconds. Re-query get_metrics over"
            " a short window to confirm recovery rather than assuming it."
        ),
    }


def scale_service(service: str, replicas: int, reason: str) -> dict[str, Any]:
    params = {"service": service, "replicas": replicas, "reason": reason}

    if replicas < MIN_REPLICAS:
        return _refuse(
            "scale_service", service, params,
            f"refusing to scale {service} to {replicas}. Taking a service to zero"
            " is an outage, not a remediation; if that is genuinely intended it"
            " needs a human doing it deliberately, not an agent doing it as a fix",
        )
    if replicas > MAX_REPLICAS:
        return _refuse(
            "scale_service", service, params,
            f"{replicas} exceeds the {MAX_REPLICAS} replica ceiling for this stack",
        )
    if not reason.strip():
        return _refuse("scale_service", service, params, "a scaling change needs a reason")

    # Only inventory-api models capacity in this stack; it is the service with
    # a bounded worker pool. Scaling anything else is accepted and audited but
    # changes nothing, and says so rather than pretending.
    scalable = service == "inventory-api"
    previous = None
    if scalable:
        with _connect(read_only=False) as conn:
            row = conn.execute(
                "SELECT value FROM controls WHERE key = 'inventory_replicas'"
            ).fetchone()
            previous = int(float(row["value"])) if row else 1
            conn.execute(
                "INSERT INTO controls (key, value, updated_ts) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_ts = excluded.updated_ts",
                ("inventory_replicas", str(replicas), time.time()),
            )
            conn.execute(
                "INSERT INTO logs (id, ts, service, level, message, trace_id, fields)"
                " VALUES (?,?,?,?,?,NULL,?)",
                (
                    uuid.uuid4().hex,
                    time.time(),
                    service,
                    "INFO",
                    f"scaled from {previous} to {replicas} replicas by {ACTOR}",
                    json.dumps({"reason": reason, "previous_replicas": previous}),
                ),
            )
            conn.commit()

    action_id = _audit("scale_service", service, params, f"scaled to {replicas}")
    return {
        "ok": True,
        "action_id": action_id,
        "service": service,
        "replicas": replicas,
        "previous_replicas": previous,
        "effective": scalable,
        "note": (
            "Capacity takes effect within a couple of seconds. Re-query"
            " get_metrics to confirm recovery rather than assuming it."
            if scalable
            else f"{service} has no capacity limit in this stack, so this"
            " changed nothing. If the bottleneck is elsewhere, scale that"
            " service instead."
        ),
    }


def post_status_update(channel: str, message: str) -> dict[str, Any]:
    params = {"channel": channel, "message": message}

    if channel not in ALLOWED_CHANNELS:
        return _refuse(
            "post_status_update", channel, params,
            f"{channel!r} is not an allowlisted channel."
            f" Allowed: {sorted(ALLOWED_CHANNELS)}",
        )
    if not message.strip():
        return _refuse("post_status_update", channel, params, "refusing to post an empty update")
    if len(message) > MAX_MESSAGE_CHARS:
        return _refuse(
            "post_status_update", channel, params,
            f"message is {len(message)} chars, over the {MAX_MESSAGE_CHARS} limit",
        )

    action_id = _audit("post_status_update", channel, params, "posted")
    return {
        "ok": True,
        "action_id": action_id,
        "channel": channel,
        "posted_at": _iso(time.time()),
        "message": message,
    }


def file_postmortem(
    service: str,
    title: str,
    signature: str,
    root_cause: str,
    resolution: str,
    postmortem_markdown: str,
    opened_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Write the incident up and file it so a future investigation can find it.

    The `signature` is what makes the memory useful. It should describe the
    shape of the failure -- the service, the symptom, and the mechanism --
    tightly enough that the same incident recurring would produce a similar
    string, and loosely enough that it is not just a timestamp.
    """
    params = {"service": service, "title": title, "signature": signature}

    if not root_cause.strip():
        return _refuse(
            "file_postmortem", service, params,
            "a postmortem without a root cause is just an outage report;"
            " investigate further before filing",
        )
    if not signature.strip():
        return _refuse(
            "file_postmortem", service, params,
            "a signature is required, otherwise this incident cannot be found again",
        )

    incident_id = uuid.uuid4().hex
    now = time.time()
    with _connect(read_only=False) as conn:
        conn.execute(
            "INSERT INTO incidents (id, opened_ts, resolved_ts, service, title,"
            " signature, root_cause, resolution, postmortem)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                incident_id,
                opened_at_epoch or now,
                now,
                service,
                title,
                signature,
                root_cause,
                resolution,
                postmortem_markdown,
            ),
        )
        conn.commit()

    action_id = _audit("file_postmortem", service, params, f"filed {incident_id}")
    return {
        "ok": True,
        "action_id": action_id,
        "incident_id": incident_id,
        "signature": signature,
        "note": "Filed. search_incident_memory will surface this next time.",
    }


def get_audit_log(limit: int = 50) -> dict[str, Any]:
    """Everything this agent has changed, or tried to change, and what happened."""
    limit = max(1, min(200, limit))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    return {
        "count": len(rows),
        "actions": [
            {
                "ts": _iso(r["ts"]),
                "actor": r["actor"],
                "action": r["action"],
                "target": r["target"],
                "params": json.loads(r["params"]),
                "result": r["result"],
            }
            for r in reversed(rows)
        ],
    }
