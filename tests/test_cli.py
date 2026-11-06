"""End-to-end tests: CLI parsing, collector against a fake usage-queue server,
export, prune."""

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cpa_stats.cli import main, parse_duration
from cpa_stats.model import record_to_row
from cpa_stats.storage import load_rows, open_db


class TestParseDuration:
    @pytest.mark.parametrize("text,sec", [
        ("90s", 90), ("15m", 900), ("2h", 7200), ("7d", 604800),
        ("1w", 604800), ("30", 30), ("1.5h", 5400),
    ])
    def test_ok(self, text, sec):
        assert parse_duration(text) == sec

    def test_bad(self):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_duration("abc")


class FakeQueue:
    """Tiny HTTP server that behaves like CPA's /v0/management/usage-queue."""

    def __init__(self, records, credentials=None):
        self.records = list(records)
        self.credentials = credentials
        self.requests = 0

        class H(BaseHTTPRequestHandler):
            def do_GET(self_inner):  # noqa: N805
                self.requests += 1
                if "credentials" in self_inner.path or "auth-files" in self_inner.path:
                    if self.credentials is None:
                        self_inner.send_error(404)
                        return
                    body = json.dumps(self.credentials).encode()
                    self_inner.send_response(200)
                    self_inner.send_header("Content-Type", "application/json")
                    self_inner.end_headers()
                    self_inner.wfile.write(body)
                    return
                if "usage-queue" not in self_inner.path:
                    self_inner.send_error(404)
                    return
                n = int(self_inner.path.split("count=")[-1].split("&")[0])
                batch, self.records = self.records[:n], self.records[n:]
                body = json.dumps(batch).encode()
                self_inner.send_response(200)
                self_inner.send_header("Content-Type", "application/json")
                self_inner.end_headers()
                self_inner.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()


def make_rec(i):
    return {
        "timestamp": datetime.now().astimezone().isoformat(),
        "execution_id": f"e{i}",
        "model": "devin/swe-2",
        "latency_ms": 1000,
        "ttft_ms": 300,
        "tokens": {"input_tokens": 100 + i, "output_tokens": 10},
        "token_breakdown": {"quality": "complete"},
    }


class TestCollect:
    def test_drains_queue(self, tmp_path):
        recs = [make_rec(i) for i in range(7)]
        with FakeQueue(recs) as q:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db), "--url", f"http://127.0.0.1:{q.port}",
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            rows = load_rows(conn, 0)
            assert len(rows) == 7
            assert rows[0]["token_quality"] == "complete"

    def test_credentials_snapshot(self, tmp_path):
        creds = {"observed_at": "2026-11-06T14:23:38+08:00", "files": [{
            "id": "devin-a.json", "name": "devin-a.json", "provider": "devin",
            "status": "active", "success": 10, "failed": 1,
            "email": "a@example.com",
            "quota": {"observed_at": "2026-11-06T14:23:38+08:00",
                      "signals": {"plan": "Max",
                                  "daily_quota_remaining_percent": "100%",
                                  "weekly_quota_remaining_percent": "81%",
                                  "weekly_quota_reset_at": "2026-11-08T08:00:00Z"}}}]}
        with FakeQueue([], credentials=creds) as q:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db), "--url", f"http://127.0.0.1:{q.port}",
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            from cpa_stats.storage import load_quota_snapshot
            snap = load_quota_snapshot(conn)
            assert snap["files"][0]["name"] == "devin-a.json"
            assert snap["fetched_at"] > 0

    def test_credentials_off_when_interval_zero(self, tmp_path):
        with FakeQueue([], credentials={"files": [{"name": "x"}]}) as q:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db), "--url", f"http://127.0.0.1:{q.port}",
                  "--mgmt-key-file", str(self._key(tmp_path)),
                  "--quota-interval", "0", "--once"])
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            from cpa_stats.storage import load_quota_snapshot
            assert load_quota_snapshot(conn) is None

    def test_v8_path_preferred(self, tmp_path):
        """v8 endpoint should be tried first and cached."""
        from cpa_stats import collector

        collector._usage_queue_path = None  # reset cache
        seen_paths = []

        recs = [make_rec(1)]

        class H(BaseHTTPRequestHandler):
            def do_GET(self_inner):  # noqa: N805
                seen_paths.append(self_inner.path.split("?")[0])
                if "v8/management" not in self_inner.path:
                    self_inner.send_error(404)
                    return
                body = json.dumps(recs).encode()
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(body)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db),
                  "--url", f"http://127.0.0.1:{server.server_address[1]}",
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            assert seen_paths[0] == "/v8/management/observability/usage/queue"
            assert "/v0/management/usage-queue" not in seen_paths
        finally:
            server.shutdown()
            collector._usage_queue_path = None

    @staticmethod
    def _key(tmp_path):
        k = tmp_path / "management.key"
        k.write_text("test-key\n")
        return k


class TestExport:
    def test_csv_and_jsonl(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1), make_rec(2)])
        conn.close()

        csv_out = tmp_path / "out.csv"
        main(["export", "--db", str(db), "-o", str(csv_out)])
        lines = csv_out.read_text().splitlines()
        assert len(lines) == 3  # header + 2 rows
        assert "decode_tps" in lines[0]
        assert "token_quality" in lines[0]

        jsonl_out = tmp_path / "out.jsonl"
        main(["export", "--db", str(db), "--format", "jsonl", "-o", str(jsonl_out)])
        recs = [json.loads(x) for x in jsonl_out.read_text().splitlines()]
        assert len(recs) == 2
        assert recs[0]["token_quality"] == "complete"


class TestReadonlyGuards:
    def test_missing_db_friendly_error(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["export", "--db", str(tmp_path / "missing.db")])
        assert exc.value.code == 1
        assert "collector" in capsys.readouterr().err.lower()

    def test_summary_missing_db(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["summary", "--db", str(tmp_path / "missing.db")])


class TestPriceFlag:
    def test_pricing_attached(self, tmp_path, capsys):
        # `summary` with --price renders without crashing.
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1)])
        conn.close()
        main(["summary", "--db", str(db), "--since", "1d", "--price", "2,0.2,8"])
        out = capsys.readouterr().out
        assert "$" in out or "cost" in out


class TestQuotaCmd:
    def _db_with_snapshot(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import save_quota_snapshot
        save_quota_snapshot(conn, {
            "fetched_at": time.time(),
            "files": [{"name": "devin-a.json", "provider": "devin",
                       "status": "active", "success": 10, "failed": 1,
                       "email": "a@example.com",
                       "quota": {"signals": {
                           "plan": "Max",
                           "daily_quota_remaining_percent": "100%",
                           "weekly_quota_remaining_percent": "81%"}}}]})
        conn.commit()
        conn.close()
        return db

    def test_quota(self, tmp_path, capsys):
        db = self._db_with_snapshot(tmp_path)
        main(["quota", "--db", str(db)])
        out = capsys.readouterr().out
        assert "a@example.com" in out
        assert "81%" in out and "100%" in out
        assert "Max" in out

    def test_quota_empty(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        conn.close()
        main(["quota", "--db", str(db)])
        out = capsys.readouterr().out
        assert "no credential snapshot" in out


class TestShowCmd:
    def test_show_by_id_prefix(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1)])
        conn.close()
        main(["show", "--db", str(db), "e1"])
        out = capsys.readouterr().out
        assert "request detail" in out
        assert "swe-2" in out
        assert "input tokens" in out

    def test_show_missing(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        conn.close()
        with pytest.raises(SystemExit):
            main(["show", "--db", str(db), "nope"])
