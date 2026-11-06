"""Storage tests: schema, migrations, totals, dedup."""

import json
import sqlite3

import pytest

from cpa_stats.model import aggregate, parse_pricing, record_to_row
from cpa_stats.storage import (SCHEMA_VERSION, db_totals, insert_records,
                               load_quota_history, load_quota_snapshot,
                               load_rows, meta_get, open_db, prune,
                               save_quota_history, save_quota_snapshot)


def rec(i, **kw):
    base = {
        "timestamp": "2026-11-02T19:55:38.436314676+08:00",
        "execution_id": f"exec-{i}",
        "model": "devin/swe-2",
        "alias": "swe-2",
        "latency_ms": 2000,
        "ttft_ms": 500,
        "failed": False,
        "tokens": {"input_tokens": 1000, "output_tokens": 100,
                   "cache_read_tokens": 400, "cache_creation_tokens": 100},
        "token_breakdown": {"quality": "complete"},
    }
    base.update(kw)
    return base


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "requests.db")
    yield conn
    conn.close()


class TestSchema:
    def test_version_set(self, db):
        assert meta_get(db, "schema_version") == str(SCHEMA_VERSION)

    def test_token_quality_column(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(requests)")}
        assert "token_quality" in cols


class TestMigration:
    def test_v1_db_upgraded(self, tmp_path):
        # Build a v1-shaped db (no token_quality column, no schema_version).
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            """CREATE TABLE requests (id TEXT PRIMARY KEY, ts REAL, model TEXT,
            alias TEXT, response_model TEXT, provider TEXT, effort TEXT,
            service_tier TEXT, endpoint TEXT, source TEXT, auth_index TEXT,
            session_id TEXT, request_id TEXT, user_agent TEXT, stream INTEGER,
            failed INTEGER, status_code INTEGER, error TEXT, latency_ms INTEGER,
            ttft_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, raw TEXT)""")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO requests(id, ts, failed, input_tokens, raw) VALUES(?,?,?,?,?)",
            ("r1", 1793620538.0, 0, 100,
             '{"token_breakdown":{"quality":"inconsistent"}}'))
        conn.commit()
        conn.close()

        conn2 = open_db(path)
        cols = {r["name"] for r in conn2.execute("PRAGMA table_info(requests)")}
        assert "token_quality" in cols
        # Backfilled from raw JSON.
        q = conn2.execute("SELECT token_quality FROM requests WHERE id='r1'").fetchone()[0]
        assert q == "inconsistent"
        assert meta_get(conn2, "schema_version") == str(SCHEMA_VERSION)

    def test_v2_db_upgraded(self, tmp_path):
        """v2 -> v3: new columns added and backfilled from raw JSON."""
        path = tmp_path / "v2.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            """CREATE TABLE requests (id TEXT PRIMARY KEY, ts REAL, model TEXT,
            alias TEXT, response_model TEXT, provider TEXT, effort TEXT,
            service_tier TEXT, endpoint TEXT, source TEXT, auth_index TEXT,
            session_id TEXT, request_id TEXT, user_agent TEXT, stream INTEGER,
            failed INTEGER, status_code INTEGER, error TEXT, latency_ms INTEGER,
            ttft_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, token_quality TEXT, raw TEXT)""")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version','2')")
        raw = {"executor_type": "DevinExecutor", "auth_type": "oauth",
               "trace_id": "tr-7", "is_native_key": True, "node_kind": "main",
               "is_fork": True, "client_ip": "127.0.0.1",
               "upstream_service_tier": "auto", "speed": "fast",
               "generate": True,
               "tokens": {"total_tokens": 76688},
               "fail": {"status_code": 200, "code": ""}}
        conn.execute(
            "INSERT INTO requests(id, ts, failed, raw) VALUES(?,?,?,?)",
            ("r1", 1793620538.0, 0, json.dumps(raw)))
        conn.commit()
        conn.close()

        conn2 = open_db(path)
        conn2.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn2.execute("PRAGMA table_info(requests)")}
        for c in ("executor_type", "auth_type", "access_provider",
                  "is_native_key", "trace_id", "parent_session_id", "node_kind",
                  "is_fork", "is_compaction", "client_ip", "resolved_client_ip",
                  "x_forwarded_for", "effective_service_tier",
                  "response_service_tier", "upstream_service_tier", "speed",
                  "inference_geo", "fail_code", "total_tokens", "generate"):
            assert c in cols
        row = conn2.execute("SELECT * FROM requests WHERE id='r1'").fetchone()
        assert row["executor_type"] == "DevinExecutor"
        assert row["auth_type"] == "oauth"
        assert row["trace_id"] == "tr-7"
        assert row["is_native_key"] == 1
        assert row["is_fork"] == 1
        assert row["is_compaction"] is None
        assert row["client_ip"] == "127.0.0.1"
        assert row["upstream_service_tier"] == "auto"
        assert row["speed"] == "fast"
        assert row["total_tokens"] == 76688
        assert row["generate"] == 1
        assert row["fail_code"] is None  # empty code -> NULL
        assert meta_get(conn2, "schema_version") == str(SCHEMA_VERSION)


class TestQuotaSnapshot:
    def test_roundtrip(self, db):
        snap = {"fetched_at": 1793947000.0, "observed_at": "2026-11-06T14:23:38+08:00",
                "files": [{"name": "a.json", "provider": "devin",
                           "quota": {"signals": {"plan": "Max",
                                                 "weekly_quota_remaining_percent": "81%"}}}]}
        save_quota_snapshot(db, snap)
        db.commit()
        got = load_quota_snapshot(db)
        assert got["files"][0]["quota"]["signals"]["plan"] == "Max"
        assert got["fetched_at"] == 1793947000.0

    def test_missing(self, db):
        assert load_quota_snapshot(db) is None

    def test_corrupt(self, db):
        from cpa_stats.storage import meta_set
        meta_set(db, "credentials_snapshot", "{not json")
        db.commit()
        assert load_quota_snapshot(db) is None


class TestQuotaHistory:
    def _cred(self, name="a.json", weekly=81):
        from cpa_stats.model import parse_credentials
        return parse_credentials({"files": [{
            "name": name, "provider": "devin", "status": "active",
            "success": 5, "failed": 1,
            "quota": {"signals": {"plan": "Max",
                                  "weekly_quota_remaining_percent": f"{weekly}%",
                                  "daily_quota_remaining_percent": "100%"}}}]})[0]

    def test_table_created(self, db):
        cols = {r["name"] for r in db.execute("PRAGMA table_info(quota_history)")}
        assert {"ts", "name", "weekly_pct", "daily_pct", "blocked"} <= cols

    def test_roundtrip(self, db):
        import time
        save_quota_history(db, [self._cred(), self._cred("b.json", 40)],
                           time.time())
        rows = load_quota_history(db)
        assert len(rows) == 2
        assert rows[0]["weekly_pct"] == 81
        b = load_quota_history(db, name="b.json")
        assert len(b) == 1 and b[0]["weekly_pct"] == 40

    def test_missing_table_returns_empty(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "bare.db"))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        assert load_quota_history(conn) == []

    def test_prune_drops_history(self, db):
        import time
        save_quota_history(db, [self._cred()], time.time())
        assert prune(db, 0) >= 0
        assert load_quota_history(db) == []


class TestInsert:
    def test_dedup(self, db):
        assert insert_records(db, [rec(1)]) == 1
        assert insert_records(db, [rec(1)]) == 0
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1

    def test_readonly_open_fails_on_missing_db(self, tmp_path):
        with pytest.raises(Exception):
            open_db(tmp_path / "missing.db", readonly=True)


class TestTotals:
    def test_db_totals(self, db):
        insert_records(db, [rec(1), rec(2), rec(3, failed=True)])
        a = db_totals(db)
        assert a.n == 3
        assert a.failed == 1
        assert a.in_tok == 3000
        assert a.uncached == 3 * 500
        assert a.cache_read == 1200
        assert a.out_tok == 300
        assert a.n_complete == 3
        assert abs(a.cache_ratio - 0.4) < 1e-9

    def test_db_totals_cost(self, db):
        insert_records(db, [rec(1)])
        a = db_totals(db, parse_pricing("2,0.2,8"))
        # uncached 500 * 2 + read 400 * 0.2 + out 100 * 8 = 1000+80+800 = 1880 per 1M
        assert abs(a.cost_usd - 1880 / 1e6) < 1e-9

    def test_db_totals_empty(self, db):
        assert db_totals(db).n == 0


class TestAggregate:
    def test_quality_from_rows(self, db):
        insert_records(db, [rec(1), rec(2, token_breakdown={"quality": "inconsistent"})])
        rows = load_rows(db, 0)
        a = aggregate(rows)
        assert a.n == 2
        assert a.n_complete == 1
        assert abs(a.quality_ratio - 0.5) < 1e-9


class TestPrune:
    def test_prune(self, db):
        insert_records(db, [rec(1)])
        assert prune(db, 0) == 1  # cutoff = now -> everything older than now
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
