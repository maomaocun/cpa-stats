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

    def test_credentials_refresh_when_stale(self, tmp_path):
        """Stale quota-observable creds get a POST /credentials/refresh."""
        refreshed = []
        creds = {"files": [{
            "name": "devin-a.json", "provider": "devin", "status": "active",
            "quota": {"observed_at": "2020-01-01T00:00:00Z",
                      "signals": {"weekly_quota_remaining_percent": "81%"}}}]}

        class H(BaseHTTPRequestHandler):
            def do_GET(self_inner):
                body = json.dumps(creds if ("credentials" in self_inner.path
                                          or "auth-files" in self_inner.path)
                                  else []).encode()
                self_inner.send_response(200)
                self_inner.send_header("Content-Type", "application/json")
                self_inner.end_headers()
                self_inner.wfile.write(body)

            def do_POST(self_inner):
                body_len = int(self_inner.headers.get("Content-Length") or 0)
                body = self_inner.rfile.read(body_len)
                refreshed.append(json.loads(body).get("name"))
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db),
                  "--url", f"http://127.0.0.1:{server.server_address[1]}",
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            assert refreshed == ["devin-a.json"]
        finally:
            server.shutdown()

    def test_credentials_no_refresh_when_fresh(self, tmp_path):
        """Fresh observations are not re-fetched."""
        from datetime import timezone
        recent = datetime.now(timezone.utc).isoformat()
        refreshed = []
        creds = {"files": [{
            "name": "devin-a.json", "provider": "devin",
            "quota": {"observed_at": recent,
                      "signals": {"weekly_quota_remaining_percent": "81%"}}}]}

        class H(BaseHTTPRequestHandler):
            def do_GET(self_inner):
                body = json.dumps(creds if "credentials" in self_inner.path
                                  else []).encode()
                self_inner.send_response(200)
                self_inner.send_header("Content-Type", "application/json")
                self_inner.end_headers()
                self_inner.wfile.write(body)

            def do_POST(self_inner):
                refreshed.append(1)
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            db = tmp_path / "r.db"
            main(["collect", "--db", str(db),
                  "--url", f"http://127.0.0.1:{server.server_address[1]}",
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            assert refreshed == []
        finally:
            server.shutdown()

    def test_v8_path_preferred(self, tmp_path):
        """v8 endpoint should be tried first and cached."""
        from cpa_stats import collector

        collector._usage_queue_path.clear()  # reset cache
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
            collector._usage_queue_path.clear()

    @staticmethod
    def _key(tmp_path):
        k = tmp_path / "management.key"
        k.write_text("test-key\n")
        return k

    def test_multi_url_collect(self, tmp_path):
        """--url a,b drains both queues and merges credential snapshots."""
        creds_a = {"files": [{"name": "devin-a.json", "provider": "devin",
                              "status": "active", "email": "a@x.com",
                              "quota": {"signals": {
                                  "weekly_quota_remaining_percent": "90%"}}}]}
        creds_b = {"files": [{"name": "devin-b.json", "provider": "devin",
                              "status": "active", "email": "b@x.com",
                              "quota": {"signals": {
                                  "weekly_quota_remaining_percent": "55%"}}}]}
        with FakeQueue([make_rec(1), make_rec(2)], credentials=creds_a) as qa, \
                FakeQueue([dict(make_rec(3), execution_id="e3-b"),
                           dict(make_rec(4), execution_id="e4-b")],
                          credentials=creds_b) as qb:
            db = tmp_path / "r.db"
            urls = (f"http://127.0.0.1:{qa.port},"
                    f"http://127.0.0.1:{qb.port}")
            main(["collect", "--db", str(db), "--url", urls,
                  "--mgmt-key-file", str(self._key(tmp_path)), "--once"])
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            rows = load_rows(conn, 0)
            assert len(rows) == 4
            from cpa_stats.model import parse_credentials
            from cpa_stats.storage import load_quota_snapshot
            snap = load_quota_snapshot(conn)
            creds = parse_credentials(snap)
            assert len(creds) == 2
            names = {c.name for c in creds}
            assert any("@" in n for n in names)   # instance-tagged
            insts = {c.instance for c in creds}
            assert f"127.0.0.1:{qa.port}" in insts

    def test_multi_url_key_mismatch_dies(self, tmp_path):
        """Two urls but two key files must still match; empty key file dies."""
        with pytest.raises(SystemExit):
            main(["collect", "--db", str(tmp_path / "r.db"),
                  "--url", "http://127.0.0.1:1,http://127.0.0.1:2",
                  "--mgmt-key-file", str(tmp_path / "nope.key"),
                  "--once"])


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


class TestErrorsCmd:
    def test_errors(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        recs = [make_rec(1), make_rec(2),
                dict(make_rec(3), failed=True,
                     fail={"status_code": 429, "body": "rate limited",
                           "code": "rate_limit"},
                     endpoint="POST /v1/responses")]
        insert_records(conn, recs)
        conn.close()
        main(["errors", "--db", str(db), "--since", "1d"])
        out = capsys.readouterr().out
        assert "1/3 failed" in out
        assert "429" in out and "rate_limit" in out
        assert "limited" in out

    def test_errors_empty(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1)])
        conn.close()
        main(["errors", "--db", str(db), "--since", "1d"])
        assert "no failures" in capsys.readouterr().out


class TestTopCmd:
    def test_top_by_tokens(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("COLUMNS", "160")
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        recs = [make_rec(1),
                dict(make_rec(2), source="acct-b",
                     tokens={"input_tokens": 50000, "output_tokens": 900}),
                dict(make_rec(3), source="acct-a",
                     tokens={"input_tokens": 999999, "output_tokens": 100})]
        insert_records(conn, recs)
        conn.close()
        main(["top", "--db", str(db), "--since", "1d"])
        out = capsys.readouterr().out
        assert "top" in out and "acct-a" in out
        # heaviest row (999k in) sorts first
        assert out.index("acct-a") < out.index("acct-b")

    def test_account_filter(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("COLUMNS", "160")
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [dict(make_rec(1), source="acct-a"),
                              dict(make_rec(2), source="acct-b")])
        conn.close()
        main(["summary", "--db", str(db), "--since", "1d", "-a", "acct-a"])
        out = capsys.readouterr().out
        # by-account table should list only acct-a, not acct-b
        main(["summary", "--db", str(db), "--since", "1d", "--by", "account",
              "-a", "acct-a"])
        out = capsys.readouterr().out
        assert "acct-a" in out and "acct-b" not in out

    def test_top_empty(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        conn.close()
        main(["top", "--db", str(db), "--since", "1d"])
        assert "no requests" in capsys.readouterr().out


class TestAlerts:
    def _args(self, db, webhook):
        import argparse
        return argparse.Namespace(alert_webhook=webhook, alert_below=20.0,
                                  alert_eta_hours=0.0)

    def _db(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        conn.close()
        return db

    def test_alert_fires_once_then_recovers(self, tmp_path):
        """Crossing the threshold POSTs once; recovery POSTs again."""
        sent = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self_inner):
                n = int(self_inner.headers.get("Content-Length") or 0)
                sent.append(json.loads(self_inner.rfile.read(n)))
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            conn = open_db(self._db(tmp_path))
            args = self._args(conn, url)

            from cpa_stats.collector import check_quota_alerts
            from cpa_stats.model import CredInfo
            low = CredInfo(name="c1", account="a@x", weekly_pct=10)
            ok = CredInfo(name="c1", account="a@x", weekly_pct=50)
            log = lambda m: None

            check_quota_alerts(conn, [low], args, log)
            check_quota_alerts(conn, [low], args, log)   # same state → no dup
            check_quota_alerts(conn, [ok], args, log)    # recovery
            assert len(sent) == 2
            assert "10%" in sent[0]["text"]
            assert "recovered" in sent[1]["text"]
            conn.close()
        finally:
            server.shutdown()

    def test_alert_cmd_state(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import meta_set
        meta_set(conn, "alert_state",
                 '{"c1": {"low_weekly_pct": true, "blocked": false}}')
        conn.commit()
        conn.close()
        main(["alert", "--db", str(db)])
        out = capsys.readouterr().out
        assert "low_weekly_pct" in out and "blocked" not in out


class TestServe:
    def test_healthz_and_index(self, tmp_path):
        import urllib.request

        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1)])
        conn.close()

        from cpa_stats.views import make_serve_server
        import argparse
        args = argparse.Namespace(
            db=str(db), window=3600, model=None, session=None, account=None,
            bind="127.0.0.1", port=0, refresh=5, limit=30, width=120,
            pricing=None)
        srv = make_serve_server(args)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=3).read()
            assert body == b"ok"
            html = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=3).read().decode()
            assert "cpa-stats" in html and "refresh" in html
            rows = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/rows", timeout=3).read()
            assert b"e1" in rows
        finally:
            srv.shutdown()

    def _server(self, db_path):
        from cpa_stats.views import make_serve_server
        import argparse
        args = argparse.Namespace(
            db=str(db_path), window=3600, model=None, session=None,
            account=None, bind="127.0.0.1", port=0, refresh=5, limit=30,
            width=120, pricing=None)
        srv = make_serve_server(args)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_metrics_endpoint(self, tmp_path):
        import urllib.request

        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1), make_rec(2)])
        conn.close()
        srv = self._server(db)
        try:
            port = srv.server_address[1]
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=3).read().decode()
            assert "cpa_stats_requests_total 2" in body
            assert 'cpa_stats_window_requests{window="1h"} 2' in body
            assert 'kind="output"' in body
        finally:
            srv.shutdown()

    def test_api_endpoints(self, tmp_path):
        import urllib.request

        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records, save_quota_snapshot
        insert_records(conn, [make_rec(1),
                              dict(make_rec(2), failed=True,
                                   fail={"status_code": 429,
                                         "body": "x", "code": "rate_limit"})])
        save_quota_snapshot(conn, {
            "fetched_at": time.time(),
            "files": [{"name": "devin-a.json", "provider": "devin",
                       "status": "active", "email": "a@example.com",
                       "quota": {"signals": {
                           "weekly_quota_remaining_percent": "81%"}}}]})
        conn.commit()
        conn.close()
        srv = self._server(db)
        try:
            port = srv.server_address[1]
            summary = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/summary", timeout=3).read())
            assert summary["n"] == 2 and summary["failed"] == 1
            quota = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/quota", timeout=3).read())
            assert quota["credentials"][0]["account"] == "a@example.com"
            assert quota["credentials"][0]["weekly_pct"] == 81
            errors = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/errors", timeout=3).read())
            assert errors[0]["status"] == 429 and errors[0]["count"] == 1
        finally:
            srv.shutdown()


class TestTrend:
    def _db(self, tmp_path, recs):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, recs)
        conn.close()
        return db

    def test_trend_table(self, tmp_path, capsys):
        db = self._db(tmp_path, [make_rec(i) for i in range(9)])
        main(["trend", "--db", str(db), "--since", "1h", "--bucket", "15m"])
        out = capsys.readouterr().out
        assert "trend" in out and "req" in out and "ttft" in out

    def test_trend_json(self, tmp_path, capsys):
        db = self._db(tmp_path, [make_rec(1)])
        main(["trend", "--db", str(db), "--since", "1h", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["bucket_s"] > 0 and len(out["buckets"]) >= 1
        assert any(b["req"] for b in out["buckets"])

    def test_trend_bad_metric(self, tmp_path):
        db = self._db(tmp_path, [make_rec(1)])
        with pytest.raises(SystemExit):
            main(["trend", "--db", str(db), "--metric", "nope"])


class TestSql:
    def _db(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1), make_rec(2)])
        conn.close()
        return db

    def test_select(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["sql", "--db", str(db),
              "SELECT model, COUNT(*) n FROM requests GROUP BY model"])
        out = capsys.readouterr().out
        assert "swe-2" in out

    def test_json_format(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["sql", "--db", str(db), "--format", "json",
              "SELECT COUNT(*) n FROM requests"])
        assert json.loads(capsys.readouterr().out)[0]["n"] == 2

    def test_rejects_writes(self, tmp_path):
        db = self._db(tmp_path)
        with pytest.raises(SystemExit):
            main(["sql", "--db", str(db), "DELETE FROM requests"])


class TestReport:
    def test_prints(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1)])
        conn.close()
        main(["report", "--db", str(db), "--since", "1h"])
        out = capsys.readouterr().out
        assert "requests" in out and "swe-2" in out

    def test_webhook(self, tmp_path, capsys):
        sent = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self_inner):
                n = int(self_inner.headers.get("Content-Length") or 0)
                sent.append(json.loads(self_inner.rfile.read(n)))
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            db = tmp_path / "r.db"
            conn = open_db(db)
            from cpa_stats.storage import insert_records
            insert_records(conn, [make_rec(1)])
            conn.close()
            main(["report", "--db", str(db), "--since", "1h",
                  "--webhook", f"http://127.0.0.1:{server.server_address[1]}"])
            assert "requests" in sent[0]["text"]
        finally:
            server.shutdown()


class TestJsonOutputs:
    def _db(self, tmp_path):
        db = tmp_path / "r.db"
        conn = open_db(db)
        from cpa_stats.storage import insert_records
        insert_records(conn, [make_rec(1), dict(make_rec(2), failed=True,
                                                fail={"status_code": 500,
                                                      "body": "boom"})])
        conn.close()
        return db

    def test_summary_json(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["summary", "--db", str(db), "--since", "1h", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["aggregate"]["n"] == 2 and out["aggregate"]["failed"] == 1
        assert "swe-2" in out["groups"]

    def test_errors_json(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["errors", "--db", str(db), "--since", "1h", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["failed"] == 1 and out["groups"][0]["status"] == 500

    def test_top_json(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["top", "--db", str(db), "--since", "1h", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert len(out["rows"]) == 2 and "raw" not in out["rows"][0]

    def test_show_json(self, tmp_path, capsys):
        db = self._db(tmp_path)
        main(["show", "--db", str(db), "e1", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["id"] == "e1" and isinstance(out["raw"], dict)

    def test_quota_json_empty(self, tmp_path, capsys):
        db = tmp_path / "r.db"
        conn = open_db(db)
        conn.close()
        main(["quota", "--db", str(db), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["credentials"] == []


class TestRequestAlerts:
    def test_fail_spike_fires_and_recovers(self, tmp_path):
        sent = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self_inner):
                n = int(self_inner.headers.get("Content-Length") or 0)
                sent.append(json.loads(self_inner.rfile.read(n)))
                self_inner.send_response(200)
                self_inner.end_headers()
                self_inner.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            import argparse
            from collections import deque

            from cpa_stats.collector import check_request_alerts
            conn = open_db(tmp_path / "r.db")
            args = argparse.Namespace(
                alert_webhook=f"http://127.0.0.1:{server.server_address[1]}",
                alert_fail_window=600.0, alert_fail_min=3,
                alert_fail_pct=50.0)
            log = lambda m: None
            now = time.time()
            bad = deque([(now, True, "m1"), (now, True, "m1"),
                         (now, True, "m2"), (now, False, "m1")])
            check_request_alerts(conn, bad, args, log)
            check_request_alerts(conn, bad, args, log)  # no duplicate
            assert len(sent) == 1 and "75%" in sent[0]["text"]
            good = deque([(now, False, "m1")] * 10)
            check_request_alerts(conn, good, args, log)
            assert len(sent) == 2 and "back to" in sent[1]["text"]
            conn.close()
        finally:
            server.shutdown()

    def test_below_min_requests_silent(self, tmp_path):
        import argparse
        from collections import deque

        from cpa_stats.collector import check_request_alerts
        from cpa_stats.storage import meta_get
        conn = open_db(tmp_path / "r.db")
        args = argparse.Namespace(alert_webhook="http://127.0.0.1:1",
                                  alert_fail_window=600.0, alert_fail_min=10,
                                  alert_fail_pct=50.0)
        bad = deque([(time.time(), True, "m")] * 3)  # only 3 < min 10
        check_request_alerts(conn, bad, args, lambda m: None)  # no webhook call
        assert meta_get(conn, "alert_state") in (None, "{}")
        conn.close()


class TestUpgrade:
    def test_parse_version(self):
        from cpa_stats.collector import parse_version
        assert parse_version("v2.3.0") == (2, 3, 0)
        assert parse_version("2.10") == (2, 10, 0)
        assert parse_version("junk") == ()

    def test_update_notice(self, tmp_path):
        from cpa_stats.collector import update_notice
        from cpa_stats.storage import meta_set
        conn = open_db(tmp_path / "r.db")
        assert update_notice(conn) == ""
        meta_set(conn, "latest_release", "v0.0.1")   # older than current
        assert update_notice(conn) == ""
        meta_set(conn, "latest_release", "v99.0.0")
        assert update_notice(conn) == "v99.0.0"
        conn.close()

    def test_install_location_editable(self):
        # the test env installs cpa-stats as `pip install -e .`
        from cpa_stats.collector import install_location
        kind, detail = install_location()
        assert kind == "editable" and detail

    def test_upgrade_already_latest(self, tmp_path, capsys, monkeypatch):
        import cpa_stats.collector as coll
        monkeypatch.setattr(coll, "latest_release_tag",
                            lambda *a, **kw: f"v{__import__('cpa_stats').__version__}")
        main(["upgrade", "--db", str(tmp_path / "r.db")])
        out = capsys.readouterr().out
        assert "up to date" in out

    def test_upgrade_newer_prints_command(self, tmp_path, capsys, monkeypatch):
        import cpa_stats.collector as coll
        monkeypatch.setattr(coll, "latest_release_tag",
                            lambda *a, **kw: "v99.9.9")
        main(["upgrade", "--db", str(tmp_path / "r.db")])
        out = capsys.readouterr().out
        assert "v99.9.9" in out and "upgrade with" in out

    def test_binary_names(self, monkeypatch):
        import platform

        import cpa_stats.collector as coll
        monkeypatch.setattr(coll.sys, "platform", "linux")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        assert coll.binary_asset_name() == "cpa-stats-linux-amd64"
        monkeypatch.setattr(coll.sys, "platform", "darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        assert coll.binary_asset_name() == "cpa-stats-darwin-arm64"
        assert coll.binary_release_url("v9.9.9").endswith(
            "/v9.9.9/cpa-stats-darwin-arm64")

    def test_upgrade_binary_hint(self, tmp_path, capsys, monkeypatch):
        import cpa_stats.collector as coll
        monkeypatch.setattr(coll, "latest_release_tag",
                            lambda *a, **kw: "v99.9.9")
        monkeypatch.setattr(coll, "install_location",
                            lambda: ("binary", "/tmp/cpa-stats"))
        main(["upgrade", "--db", str(tmp_path / "r.db")])
        out = capsys.readouterr().out
        assert "curl" in out and "/v99.9.9/cpa-stats-" in out


class TestInstallSh:
    def test_syntax(self):
        import subprocess
        from pathlib import Path

        sh = Path(__file__).resolve().parent.parent / "install.sh"
        r = subprocess.run(["sh", "-n", str(sh)], capture_output=True)
        assert r.returncode == 0, r.stderr
