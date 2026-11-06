"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, __doc__ as DOC
from .collector import cmd_collect, die, load_cpa_config
from .format import fmt_age, fmt_tok, window_label
from .model import COLUMNS, decode_tps, e2e_tps, parse_pricing, token_parts
from .storage import load_rows, meta_get, open_db, open_db_ro, prune


def parse_duration(text: str) -> float:
    """'90s', '15m', '2h', '7d', '1w' -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid duration: {text!r} (examples: 30m, 2h, 7d)")
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return float(m.group(1)) * unit


DEFAULT_CPA_DIR = os.environ.get("CPA_DIR", "")  # empty -> auto-detect in find_cpa_dir()
DEFAULT_DB = (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
              / "cpa-stats" / "requests.db")


def cmd_export(args) -> None:
    conn = open_db_ro(args.db)
    rows = list(reversed(load_rows(conn, time.time() - args.since,
                                   args.model, args.session,
                                   account=getattr(args, "account", None))))
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    fields = [c for c in COLUMNS if c != "raw"] + [
        "prompt_tokens_total", "uncached_tokens", "cache_hit_ratio",
        "decode_tps", "e2e_tps", "iso_time",
    ]
    try:
        if args.format == "csv":
            w = csv.DictWriter(out, fieldnames=fields)
            w.writeheader()
        for r in rows:
            d = {c: r[c] for c in COLUMNS if c != "raw" and c in r.keys()}
            total, uncached, read, _ = token_parts(r)
            d["prompt_tokens_total"], d["uncached_tokens"] = total, uncached
            d["cache_hit_ratio"] = round(read / total, 4) if total else None
            d["decode_tps"] = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
            d["e2e_tps"] = e2e_tps(r["output_tokens"], r["latency_ms"])
            d["iso_time"] = datetime.fromtimestamp(r["ts"]).astimezone().isoformat()
            if args.format == "csv":
                w.writerow(d)
            else:
                out.write(json.dumps(d, ensure_ascii=False) + "\n")
    finally:
        if args.output:
            out.close()
            print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)


def cmd_prune(args) -> None:
    conn = open_db(Path(args.db))
    n = prune(conn, args.older_than)
    if args.vacuum:
        try:
            conn.execute("VACUUM")
            print("vacuumed")
        except sqlite3.Error as exc:
            print(f"vacuum failed: {exc}", file=sys.stderr)
    print(f"deleted {n} records older than {window_label(args.older_than)}")


def cmd_doctor(args) -> None:
    ok = True

    def line(good, msg):
        nonlocal ok
        ok &= good
        print(("\033[32m✓\033[0m " if good else "\033[31m✗\033[0m ") + msg)

    try:
        cfgs = load_cpa_configs(args)
        line(True, f"CPA endpoint {cfgs[0].url}"
                   + (f" (+{len(cfgs) - 1} more)" if len(cfgs) > 1 else ""))
    except SystemExit:
        line(False, "cannot load CPA config")
        sys.exit(1)
    for cfg in cfgs:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            body = None
            last_code = None
            for path in ("/v8/management/config", "/v0/management/config"):
                req = urllib.request.Request(
                    f"{cfg.url}{path}",
                    headers={"Authorization": f"Bearer {cfg.mgmt_key}"})
                try:
                    with opener.open(req, timeout=5) as resp:
                        body = json.loads(resp.read() or b"{}")
                    break
                except urllib.error.HTTPError as exc:
                    last_code = exc.code
                    if exc.code != 404:
                        raise
            if body is None:
                raise urllib.error.HTTPError(cfg.url, last_code or 404,
                                           "management config not found", {}, None)
            line(True, f"{cfg.url}: management API reachable, key accepted")
            try:
                req = urllib.request.Request(
                    f"{cfg.url}/v8/management/server/latest-version",
                    headers={"Authorization": f"Bearer {cfg.mgmt_key}"})
                with opener.open(req, timeout=5) as resp:
                    ver = json.loads(resp.read() or b"{}")
                latest = ver.get("latest-version") or ver.get("latest_version")
                if latest:
                    line(True, f"{cfg.url}: latest upstream release {latest}")
            except Exception:  # noqa: BLE001 — informational only
                pass
            flat = json.dumps(body)
            m = re.search(r'"usage-statistics-enabled":\s*(true|false)', flat)
            if m:
                line(m.group(1) == "true", f"usage-statistics-enabled = {m.group(1)}")
            m = re.search(r'"redis-usage-queue-retention-seconds":\s*(\d+)', flat)
            if m:
                line(True, f"usage queue retention = {m.group(1)}s")
        except urllib.error.HTTPError as exc:
            line(False, f"{cfg.url}: management API returned HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            line(False, f"{cfg.url}: CPA not reachable: {exc}")
    db = Path(args.db)
    if db.exists():
        conn = open_db(db, readonly=True)
        n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        last = conn.execute("SELECT MAX(ts) FROM requests").fetchone()[0]
        line(True, f"database {db} ({n} records, last "
                   f"{fmt_age(time.time() - last) if last else 'never'})")
        ver = meta_get(conn, "schema_version")
        line(True, f"schema version {ver or 'pre-v2'}")
        snap = None
        try:
            from .storage import load_quota_snapshot

            snap = load_quota_snapshot(conn)
        except sqlite3.Error:
            pass
        if snap and isinstance(snap.get("files"), list):
            age = time.time() - float(snap.get("fetched_at") or 0)
            line(age < 3 * 3600,
                 f"quota snapshot: {len(snap['files'])} credential(s), "
                 f"{fmt_age(age)}")
        else:
            line(False, "no quota snapshot yet (restart collect ≥2.1; "
                        "--quota-interval)")
        hb = meta_get(conn, "collector_heartbeat")
        age = time.time() - float(hb) if hb else None
        line(age is not None and age < 15, f"collector heartbeat {fmt_age(age)}")
        err = meta_get(conn, "collector_last_error")
        if err:
            line(False, f"collector last error: {err.partition('|')[2]}")
    else:
        line(False, f"database {db} does not exist yet (start the collector)")
    sys.exit(0 if ok else 1)


def cmd_install_service(args) -> None:
    """Write a systemd --user unit for the collector and enable it."""
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("cpa-stats")
    if not exe:
        die("cannot find 'cpa-stats' on PATH; install the package first "
            "(pip install --user .) or run from its directory")
    exe = str(Path(exe).resolve())
    unit = f"""[Unit]
Description=cpa-stats collector (CLIProxyAPI usage queue -> SQLite)
After=cliproxy-api.service
Wants=cliproxy-api.service

[Service]
Type=simple
ExecStart={exe} --db {args.db} collect --retention-days {args.retention_days}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    path = unit_dir / "cpa-stats.service"
    path.write_text(unit)
    print(f"wrote {path}")
    if not args.no_enable:
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "cpa-stats"]):
            try:
                subprocess.run(cmd, check=True)
                print("+ " + " ".join(cmd))
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                print(f"systemctl failed ({exc}); enable manually: systemctl --user enable --now cpa-stats",
                      file=sys.stderr)
                break


def add_price_arg(p):
    p.add_argument("--price", metavar="IN,CACHED,OUT",
                   help="USD per 1M tokens for input / cache-read / output; "
                        "enables the est. cost card (e.g. '2,0.2,8')")


def main(argv=None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("CPA_STATS_DB", str(DEFAULT_DB)),
                        help="SQLite path")
    common.add_argument("--cpa-dir", default=os.environ.get("CPA_DIR", str(DEFAULT_CPA_DIR)),
                        help="CLIProxyAPI install dir (reads config.yaml and management.key)")
    common.add_argument("--url", help="CPA base URL(s), comma-separated for "
                        "multi-instance collect (default: from config.yaml)")
    common.add_argument("--mgmt-key-file",
                        help="management key file(s), comma-separated to match "
                             "--url (default: <cpa-dir>/management.key)")

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("-m", "--model", help="filter: substring of model / alias")
    filters.add_argument("-s", "--session", help="filter: session id prefix")
    filters.add_argument("-a", "--account", help="filter: substring of account / auth index")

    p = argparse.ArgumentParser(prog="cpa-stats", description=DOC,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                parents=[common])
    p.add_argument("--version", action="version", version=f"cpa-stats {__version__}")
    sub = p.add_subparsers(dest="cmd")

    from .views import (cmd_errors, cmd_quota, cmd_show, cmd_summary,
                        cmd_tail, cmd_top, cmd_watch)

    w = sub.add_parser("watch", parents=[common, filters], help="live dashboard (default)")
    w.add_argument("-w", "--window", type=parse_duration, default=parse_duration("1h"),
                   help="stats window (default 1h)")
    w.add_argument("-n", "--limit", type=int, help="recent rows to show (default: fit terminal)")
    w.add_argument("-r", "--refresh", type=float, default=1.0, help="refresh seconds")
    w.add_argument("--once", action="store_true", help="render once and exit")
    add_price_arg(w)
    w.set_defaults(func=cmd_watch)

    tu = sub.add_parser("tui", parents=[common],
                        help="interactive TUI (textual): keys 1-4 window, f filter, enter detail")
    tu.add_argument("-r", "--refresh", type=float, default=2.0)
    add_price_arg(tu)
    from .tui import cmd_tui
    tu.set_defaults(func=cmd_tui)

    t = sub.add_parser("tail", parents=[common, filters], help="stream one line per request")
    t.add_argument("--since", type=parse_duration, default=parse_duration("5m"),
                   help="backfill window (default 5m)")
    t.add_argument("-r", "--refresh", type=float, default=0.5)
    t.add_argument("-v", "--verbose", action="store_true",
                   help="print a second line per request: account, endpoint, "
                        "tiers, node/fork/compaction, exec/trace ids, client ip")
    t.set_defaults(func=cmd_tail)

    s = sub.add_parser("summary", parents=[common, filters], help="aggregate report")
    s.add_argument("--since", type=parse_duration, default=parse_duration("24h"),
                   help="window (default 24h)")
    s.add_argument("--by", choices=["model", "effort", "session", "account",
                                    "endpoint", "provider", "upstream",
                                    "node_kind", "hour", "day"],
                   default="model")
    add_price_arg(s)
    s.set_defaults(func=cmd_summary)

    q = sub.add_parser("quota", parents=[common],
                       help="credential quota/status snapshot collected from CPA")
    q.add_argument("-r", "--refresh", action="store_true",
                   help="force CPA to re-observe credentials upstream first "
                        "(POST /credentials/refresh), then store + show")
    q.set_defaults(func=cmd_quota)

    er = sub.add_parser("errors", parents=[common, filters],
                        help="failure breakdown by model / status / code / endpoint")
    er.add_argument("--since", type=parse_duration, default=parse_duration("24h"),
                    help="window (default 24h)")
    er.set_defaults(func=cmd_errors)

    tp = sub.add_parser("top", parents=[common, filters],
                        help="heaviest requests by tokens / output / cost / latency")
    tp.add_argument("--since", type=parse_duration, default=parse_duration("24h"),
                    help="window (default 24h)")
    tp.add_argument("--by", choices=["tokens", "output", "cost", "latency"],
                    default="tokens")
    tp.add_argument("-n", "--limit", type=int, default=15)
    add_price_arg(tp)
    tp.set_defaults(func=cmd_top)

    sh = sub.add_parser("show", parents=[common],
                        help="full detail for one request (id / request_id / trace_id prefix)")
    sh.add_argument("request_id")
    sh.add_argument("--raw", action="store_true",
                    help="also dump the raw upstream record JSON")
    sh.set_defaults(func=cmd_show)

    e = sub.add_parser("export", parents=[common, filters], help="export rows as csv / jsonl")
    e.add_argument("--since", type=parse_duration, default=parse_duration("24h"))
    e.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    e.add_argument("-o", "--output", help="output file (default stdout)")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("collect", parents=[common],
                       help="drain CPA usage queue into the database (daemon)")
    c.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    c.add_argument("--batch", type=int, default=500)
    c.add_argument("--quota-interval", type=float, default=60.0,
                   help="also snapshot /credentials quota+status every N seconds "
                        "(0 = off)")
    c.add_argument("--quota-refresh", type=float, default=300.0,
                   help="force-refresh quota-observable credentials whose data "
                        "is older than N seconds before snapshotting "
                        "(0 = never refresh, default 300)")
    c.add_argument("--retention-days", type=float, default=90,
                   help="delete records older than this, checked hourly (0 = keep forever)")
    c.add_argument("--alert-webhook",
                   help="POST text alerts to this URL on quota transitions "
                        "(low %, blocked/limited, recovery; Lark bot webhooks work)")
    c.add_argument("--alert-below", type=float, default=20.0,
                   help="alert when a quota window drops below this %% (default 20)")
    c.add_argument("--alert-eta-hours", type=float, default=0.0,
                   help="also alert when the burn forecast reaches 0%% within "
                        "N hours (0 = off)")
    c.add_argument("--once", action="store_true", help="drain once and exit")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_collect)

    pr = sub.add_parser("prune", parents=[common], help="delete old records")
    pr.add_argument("--older-than", type=parse_duration, required=True)
    pr.add_argument("--vacuum", action="store_true",
                    help="rebuild the db file to reclaim space (locks the db; "
                         "omit for routine pruning)")
    pr.set_defaults(func=cmd_prune)

    d = sub.add_parser("doctor", parents=[common], help="check CPA, collector and database")
    d.set_defaults(func=cmd_doctor)

    al = sub.add_parser("alert", parents=[common],
                        help="show alert state / send a test webhook alert")
    al.add_argument("--webhook", help="POST a test alert to this URL")
    from .views import cmd_alert, cmd_serve
    al.set_defaults(func=cmd_alert)

    sv = sub.add_parser("serve", parents=[common, filters],
                        help="read-only web dashboard with auto-refresh")
    sv.add_argument("-w", "--window", type=parse_duration,
                    default=parse_duration("1h"), help="stats window (default 1h)")
    sv.add_argument("--bind", default="127.0.0.1",
                    help="bind address (default localhost only)")
    sv.add_argument("--port", type=int, default=8399)
    sv.add_argument("-r", "--refresh", type=int, default=5,
                    help="page auto-refresh seconds")
    sv.add_argument("-n", "--limit", type=int, default=30,
                    help="recent rows shown (default 30)")
    sv.add_argument("--width", type=int, default=140, help="render width")
    add_price_arg(sv)
    sv.set_defaults(func=cmd_serve)

    i = sub.add_parser("install-service", parents=[common],
                       help="install a systemd --user unit for the collector")
    i.add_argument("--retention-days", type=float, default=90)
    i.add_argument("--no-enable", action="store_true", help="write the unit but don't enable it")
    i.set_defaults(func=cmd_install_service)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv = ["watch", *argv]
    args = p.parse_args(argv)
    if getattr(args, "price", None):
        try:
            args.pricing = parse_pricing(args.price)
        except ValueError as exc:
            die(str(exc))
    else:
        args.pricing = parse_pricing(None)
    args.func(args)


def entrypoint() -> None:
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    entrypoint()
