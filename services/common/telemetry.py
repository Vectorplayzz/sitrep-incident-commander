"""Shared telemetry store for the SITREP victim stack.

Every service in the stack writes here; the MCP server only ever reads.
SQLite in WAL mode is enough for a demo-scale stack and keeps the whole
thing to one `docker compose up` with no external database.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("SITREP_DB", "/data/telemetry.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS requests (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    service     TEXT NOT NULL,
    route       TEXT NOT NULL,
    method      TEXT NOT NULL,
    status      INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    trace_id    TEXT NOT NULL,
    span_id     TEXT NOT NULL,
    parent_span TEXT,
    version     TEXT NOT NULL,
    attrs       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_svc_ts ON requests(service, ts);
CREATE INDEX IF NOT EXISTS idx_requests_trace ON requests(trace_id);

CREATE TABLE IF NOT EXISTS logs (
    id       TEXT PRIMARY KEY,
    ts       REAL NOT NULL,
    service  TEXT NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL,
    trace_id TEXT,
    fields   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_svc_level_ts ON logs(service, level, ts);

CREATE TABLE IF NOT EXISTS deploys (
    id        TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    service   TEXT NOT NULL,
    version   TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    author    TEXT NOT NULL,
    summary   TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deploys_svc_ts ON deploys(service, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    ts         REAL NOT NULL,
    service    TEXT NOT NULL,
    name       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'firing',
    labels     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS actions (
    id        TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    params    TEXT NOT NULL DEFAULT '{}',
    result    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);

CREATE TABLE IF NOT EXISTS incidents (
    id           TEXT PRIMARY KEY,
    opened_ts    REAL NOT NULL,
    resolved_ts  REAL,
    service      TEXT NOT NULL,
    title        TEXT NOT NULL,
    signature    TEXT NOT NULL,
    root_cause   TEXT NOT NULL DEFAULT '',
    resolution   TEXT NOT NULL DEFAULT '',
    postmortem   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_sig ON incidents(signature);
"""


def connect(path: str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    target = path or DB_PATH
    if read_only:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
    else:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        conn = sqlite3.connect(target, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _write(path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# Buffered writer.
#
# Telemetry must never appear in the latency it is measuring. Opening a
# SQLite connection per request on the event loop added seconds of p99 that
# had nothing to do with the simulated outage. Hot-path writes are queued
# and flushed in batches from a background thread instead.
# --------------------------------------------------------------------------

_QUEUE: "queue.Queue[tuple[str, tuple[Any, ...]] | None]" = queue.Queue(maxsize=20000)
_FLUSH_INTERVAL = 0.25
_BATCH_MAX = 500
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()


def _drain_once(conn: sqlite3.Connection) -> bool:
    """Pull up to one batch off the queue and commit it. False on shutdown."""
    batch: list[tuple[str, tuple[Any, ...]]] = []
    shutting_down = False
    try:
        item = _QUEUE.get(timeout=_FLUSH_INTERVAL)
    except queue.Empty:
        return True

    while True:
        if item is None:
            shutting_down = True
        else:
            batch.append(item)
        if len(batch) >= _BATCH_MAX:
            break
        try:
            item = _QUEUE.get_nowait()
        except queue.Empty:
            break

    if batch:
        by_sql: dict[str, list[tuple[Any, ...]]] = {}
        for sql, params in batch:
            by_sql.setdefault(sql, []).append(params)
        for sql, rows in by_sql.items():
            conn.executemany(sql, rows)
        conn.commit()

    return not shutting_down


def _writer_loop() -> None:
    conn = connect()
    try:
        while _drain_once(conn):
            pass
    finally:
        conn.close()


def _ensure_writer() -> None:
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    with _writer_lock:
        if _writer_thread is not None and _writer_thread.is_alive():
            return
        _writer_thread = threading.Thread(
            target=_writer_loop, name="telemetry-writer", daemon=True
        )
        _writer_thread.start()


def _enqueue(sql: str, params: tuple[Any, ...]) -> None:
    _ensure_writer()
    try:
        _QUEUE.put_nowait((sql, params))
    except queue.Full:
        # Dropping telemetry beats stalling the request path. Real agents
        # deal with lossy telemetry; so should this one.
        pass


def flush(timeout: float = 5.0) -> None:
    """Block until queued writes have landed. Used by tests and the CLI."""
    deadline = time.time() + timeout
    while not _QUEUE.empty() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(_FLUSH_INTERVAL * 1.5)


def _shutdown() -> None:
    if _writer_thread is not None and _writer_thread.is_alive():
        try:
            _QUEUE.put_nowait(None)
        except queue.Full:
            return
        _writer_thread.join(timeout=3.0)


atexit.register(_shutdown)


def record_request(
    *,
    service: str,
    route: str,
    method: str,
    status: int,
    duration_ms: float,
    trace_id: str,
    span_id: str,
    version: str,
    parent_span: str | None = None,
    ts: float | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    _enqueue(
        "INSERT INTO requests (id, ts, service, route, method, status, duration_ms,"
        " trace_id, span_id, parent_span, version, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
                _new_id(),
                ts or time.time(),
                service,
                route,
                method,
                status,
                duration_ms,
            trace_id,
            span_id,
            parent_span,
            version,
            json.dumps(attrs or {}, default=str),
        ),
    )


def log(
    *,
    service: str,
    level: str,
    message: str,
    trace_id: str | None = None,
    ts: float | None = None,
    **fields: Any,
) -> None:
    _enqueue(
        "INSERT INTO logs (id, ts, service, level, message, trace_id, fields)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            _new_id(),
            ts or time.time(),
            service,
            level.upper(),
            message,
            trace_id,
            json.dumps(fields, default=str),
        ),
    )


def record_deploy(
    *,
    service: str,
    version: str,
    commit_sha: str,
    author: str,
    summary: str,
    ts: float | None = None,
) -> str:
    """Insert a deploy row and make it the active version for the service."""
    deploy_id = _new_id()
    _VERSION_CACHE.pop(service, None)
    with _write() as conn:
        conn.execute("UPDATE deploys SET active = 0 WHERE service = ?", (service,))
        conn.execute(
            "INSERT INTO deploys (id, ts, service, version, commit_sha, author, summary, active)"
            " VALUES (?,?,?,?,?,?,?,1)",
            (deploy_id, ts or time.time(), service, version, commit_sha, author, summary),
        )
    return deploy_id


_VERSION_CACHE: dict[str, tuple[float, str]] = {}
_VERSION_TTL = 2.0


def active_version(service: str, default: str = "v1.4.2") -> str:
    """Current deployed version, cached briefly so it stays off the hot path.

    The 2s TTL is deliberate and visible in the demo: after the agent's gated
    rollback lands, recovery shows up within a couple of seconds.
    """
    cached = _VERSION_CACHE.get(service)
    if cached is not None and (time.time() - cached[0]) < _VERSION_TTL:
        return cached[1]

    resolved = _read_active_version(service, default)
    _VERSION_CACHE[service] = (time.time(), resolved)
    return resolved


def _read_active_version(service: str, default: str) -> str:
    try:
        conn = connect(read_only=True)
    except sqlite3.OperationalError:
        return default
    try:
        row = conn.execute(
            "SELECT version FROM deploys WHERE service = ? AND active = 1"
            " ORDER BY ts DESC LIMIT 1",
            (service,),
        ).fetchone()
        return row["version"] if row else default
    except sqlite3.OperationalError:
        return default
    finally:
        conn.close()


def raise_alert(
    *,
    service: str,
    name: str,
    severity: str,
    summary: str,
    ts: float | None = None,
    **labels: Any,
) -> str:
    alert_id = _new_id()
    with _write() as conn:
        conn.execute(
            "INSERT INTO alerts (id, ts, service, name, severity, summary, status, labels)"
            " VALUES (?,?,?,?,?,?,'firing',?)",
            (
                alert_id,
                ts or time.time(),
                service,
                name,
                severity,
                summary,
                json.dumps(labels, default=str),
            ),
        )
    return alert_id


def record_action(
    *, actor: str, action: str, target: str, params: dict[str, Any], result: str
) -> str:
    """Audit log. Every gated tool the agent invokes lands here."""
    action_id = _new_id()
    with _write() as conn:
        conn.execute(
            "INSERT INTO actions (id, ts, actor, action, target, params, result)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                action_id,
                time.time(),
                actor,
                action,
                target,
                json.dumps(params, default=str),
                result,
            ),
        )
    return action_id
