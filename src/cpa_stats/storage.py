"""SQLite storage: schema, migrations, queries."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .model import INSERT_SQL, Agg

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                 TEXT PRIMARY KEY,
    ts                 REAL NOT NULL,
    model              TEXT,
    alias              TEXT,
    response_model     TEXT,
    provider           TEXT,
    effort             TEXT,
    service_tier       TEXT,
    endpoint           TEXT,
    source             TEXT,
    auth_index         TEXT,
    session_id         TEXT,
    request_id         TEXT,
    user_agent         TEXT,
    stream             INTEGER,
    failed             INTEGER,
    status_code        INTEGER,
    error              TEXT,
    latency_ms         INTEGER,
    ttft_ms            INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    reasoning_tokens   INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    token_quality      TEXT,
    raw                TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model_ts ON requests(model, ts);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# version -> list of statements applied when upgrading FROM that version.
MIGRATIONS = {
    1: [
        "ALTER TABLE requests ADD COLUMN token_quality TEXT",
        # Backfill from the raw JSON so pre-v2 rows keep their quality flag.
        "UPDATE requests SET token_quality = json_extract(raw, '$.token_breakdown.quality') "
        "WHERE token_quality IS NULL",
    ],
}


def open_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        # Open the file directly so read-only viewers never create or modify the db.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(meta_get(conn, "schema_version") or 1)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    while version < SCHEMA_VERSION:
        for sql in MIGRATIONS.get(version, []):
            if sql.startswith("ALTER TABLE") and "token_quality" in sql and "token_quality" in cols:
                continue  # column already exists
            conn.execute(sql)
        version += 1
    meta_set(conn, "schema_version", SCHEMA_VERSION)


def meta_get(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def load_rows(conn, since_ts: float, model: str | None = None, session: str | None = None, limit=None):
    sql = "SELECT * FROM requests WHERE ts >= ?"
    params: list = [since_ts]
    if model:
        sql += " AND (model LIKE ? OR alias LIKE ? OR response_model LIKE ?)"
        params += [f"%{model}%"] * 3
    if session:
        sql += " AND session_id LIKE ?"
        params.append(f"{session}%")
    sql += " ORDER BY ts DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def db_totals(conn, pricing=None) -> Agg:
    """All-time totals over the whole database, computed in SQL."""
    a = Agg()
    row = conn.execute(
        """
        SELECT COUNT(*)                                        AS n,
               SUM(failed)                                     AS failed,
               SUM(COALESCE(cache_read_tokens, 0))             AS cache_read,
               SUM(COALESCE(cache_write_tokens, 0))            AS cache_write,
               SUM(COALESCE(output_tokens, 0))                 AS out_tok,
               SUM(COALESCE(reasoning_tokens, 0))              AS reasoning_tok,
               SUM(CASE WHEN COALESCE(token_quality,'')='complete' THEN 1 ELSE 0 END) AS n_complete,
               MIN(ts)                                         AS first_ts
        FROM requests
        """
    ).fetchone()
    if not row or not row["n"]:
        return a
    a.n = row["n"]
    a.failed = row["failed"] or 0
    # Mirror token_parts(): when input < read + write, uncached = max(input - read, 0).
    sub = conn.execute(
        """
        SELECT SUM(CASE
                     WHEN COALESCE(input_tokens,0) >= COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)
                       THEN COALESCE(input_tokens,0) - COALESCE(cache_read_tokens,0) - COALESCE(cache_write_tokens,0)
                     ELSE MAX(COALESCE(input_tokens,0) - COALESCE(cache_read_tokens,0), 0)
                   END) AS uncached,
               SUM(MAX(COALESCE(input_tokens,0),
                       COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0))) AS prompt_total
        FROM requests
        """
    ).fetchone()
    a.uncached = sub["uncached"] or 0
    a.in_tok = sub["prompt_total"] or 0
    a.cache_read = row["cache_read"] or 0
    a.cache_write = row["cache_write"] or 0
    a.out_tok = row["out_tok"] or 0
    a.reasoning_tok = row["reasoning_tok"] or 0
    a.n_complete = row["n_complete"] or 0
    a.cache_ratio = a.cache_read / a.in_tok if a.in_tok else None
    a.first_ts = row["first_ts"]
    if pricing and pricing.enabled:
        a.cost_usd = pricing.cost(a.uncached, a.cache_read, a.out_tok)
    return a


def prune(conn, older_than_s: float) -> int:
    import time

    cutoff = time.time() - older_than_s
    with conn:
        n = conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def insert_records(conn, recs) -> int:
    """Insert usage-queue records; returns number actually stored."""
    from .model import record_to_row

    with conn:
        cur = conn.executemany(INSERT_SQL, [record_to_row(r) for r in recs])
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
