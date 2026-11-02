"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
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
from .storage import load_rows, meta_get, open_db, prune


def parse_duration(text: str) -> float:
    """'90s', '15m', '2h', '7d', '1w' -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid duration: {text!r} (examples: 30m, 2h, 7d)")
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return float(m.group(1)) * unit


DEFAULT_CPA_DIR = Path.home() / f"cliproxy-{os.environ.get('USER') or os.getlogin()}"
DEFAULT_DB = (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
              / "cpa-stats" / "requests.db")


def cmd_export(args) -> None:
    conn = open_db(Path(args.db), readonly=True)
    rows = list(reversed(load_rows(conn, time.time() - args.since, args.model, args.session)))
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
            d = {c: r[c] for c in COLUMNS if c != "raw"}
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
    conn.execute("VACUUM")
    print(f"deleted {n} records older than {window_label(args.older_than)}")


def cmd_doctor(args) -> None:
    ok = True

    def line(good, msg):
        nonlocal ok
        ok &= good
        print(("\033[32m✓\033[0m " if good else "\033[31m✗\033[0m ") + msg)

    try:
        cfg = load_cpa_config(args)
        line(True, f"CPA endpoint {cfg.url}")
    except SystemExit:
        line(False, "cannot load CPA config")
        sys.exit(1)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"{cfg.url}/v0/management/config",
                                     headers={"Authorization": f"Bearer {cfg.mgmt_key}"})
        with opener.open(req, timeout=5) as resp:
            body = json.loads(resp.read() or b"{}")
        line(True, "management API reachable, key accepted")
        flat = json.dumps(body)
        m = re.search(r'"usage-statistics-enabled":\s*(true|false)', flat)
        if m:
            line(m.group(1) == "true", f"usage-statistics-enabled = {m.group(1)}")
        m = re.search(r'"redis-usage-queue-retention-seconds":\s*(\d+)', flat)
        if m:
            line(True, f"usage queue retention = {m.group(1)}s")
    except urllib.error.HTTPError as exc:
        line(False, f"management API returned HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        line(False, f"CPA not reachable: {exc}")
    db = Path(args.db)
    if db.exists():
        conn = open_db(db, readonly=True)
        n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        last = conn.execute("SELECT MAX(ts) FROM requests").fetchone()[0]
        line(True, f"database {db} ({n} records, last "
                   f"{fmt_age(time.time() - last) if last else 'never'})")
        ver = meta_get(conn, "schema_version")
        line(True, f"schema version {ver or 'pre-v2'}")
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
    exe = shutil.which("cpa-stats") or sys.argv[0]
    unit = f"""[Unit]
Description=cpa-stats collector (CLIProxyAPI usage queue -> SQLite)
After=cliproxy-{os.environ.get('USER')}.service
Wants=cliproxy-{os.environ.get('USER')}.service

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
    common.add_argument("--url", help="CPA base URL (default: from config.yaml)")
    common.add_argument("--mgmt-key-file",
                        help="management key file (default: <cpa-dir>/management.key)")

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("-m", "--model", help="filter: substring of model / alias")
    filters.add_argument("-s", "--session", help="filter: session id prefix")

    p = argparse.ArgumentParser(prog="cpa-stats", description=DOC,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                parents=[common])
    p.add_argument("--version", action="version", version=f"cpa-stats {__version__}")
    sub = p.add_subparsers(dest="cmd")

    from .views import cmd_summary, cmd_tail, cmd_watch

    w = sub.add_parser("watch", parents=[common, filters], help="live dashboard (default)")
    w.add_argument("-w", "--window", type=parse_duration, default=parse_duration("1h"),
                   help="stats window (default 1h)")
    w.add_argument("-n", "--limit", type=int, help="recent rows to show (default: fit terminal)")
    w.add_argument("-r", "--refresh", type=float, default=1.0, help="refresh seconds")
    w.add_argument("--once", action="store_true", help="render once and exit")
    add_price_arg(w)
    w.set_defaults(func=cmd_watch)

    t = sub.add_parser("tail", parents=[common, filters], help="stream one line per request")
    t.add_argument("--since", type=parse_duration, default=parse_duration("5m"),
                   help="backfill window (default 5m)")
    t.add_argument("-r", "--refresh", type=float, default=0.5)
    t.set_defaults(func=cmd_tail)

    s = sub.add_parser("summary", parents=[common, filters], help="aggregate report")
    s.add_argument("--since", type=parse_duration, default=parse_duration("24h"),
                   help="window (default 24h)")
    s.add_argument("--by", choices=["model", "effort", "session", "account", "hour", "day"],
                   default="model")
    add_price_arg(s)
    s.set_defaults(func=cmd_summary)

    e = sub.add_parser("export", parents=[common, filters], help="export rows as csv / jsonl")
    e.add_argument("--since", type=parse_duration, default=parse_duration("24h"))
    e.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    e.add_argument("-o", "--output", help="output file (default stdout)")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("collect", parents=[common],
                       help="drain CPA usage queue into the database (daemon)")
    c.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    c.add_argument("--batch", type=int, default=500)
    c.add_argument("--retention-days", type=float, default=90,
                   help="delete records older than this, checked hourly (0 = keep forever)")
    c.add_argument("--once", action="store_true", help="drain once and exit")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_collect)

    pr = sub.add_parser("prune", parents=[common], help="delete old records")
    pr.add_argument("--older-than", type=parse_duration, required=True)
    pr.set_defaults(func=cmd_prune)

    d = sub.add_parser("doctor", parents=[common], help="check CPA, collector and database")
    d.set_defaults(func=cmd_doctor)

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
