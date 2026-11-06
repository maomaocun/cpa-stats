"""Collector daemon: drains the CPA usage queue into SQLite."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .storage import (insert_records, meta_set, open_db, prune,
                      save_quota_snapshot)


def die(msg: str, code: int = 1) -> None:
    print(f"cpa-stats: {msg}", file=sys.stderr)
    sys.exit(code)


@dataclass
class CPAConfig:
    url: str
    mgmt_key: str


def find_cpa_dir(args) -> Path | None:
    """Locate the CLIProxyAPI install dir: --cpa-dir / CPA_DIR, then the usual
    suspects under $HOME."""
    if args.cpa_dir:
        d = Path(os.path.expanduser(args.cpa_dir))
        return d if (d / "config.yaml").exists() else None
    for cand in (Path.home() / ".config" / "cliproxy",
                 Path.home() / ".cliproxy",
                 Path.home() / "cliproxy"):
        if (cand / "config.yaml").exists():
            return cand
    # Legacy per-user installs: ~/cliproxy-<anything>/config.yaml
    for d in sorted(Path.home().glob("cliproxy-*/config.yaml")):
        return d.parent
    return None


def load_cpa_config(args) -> CPAConfig:
    cpa_dir = find_cpa_dir(args)
    url = args.url or os.environ.get("CPA_URL")
    if not url:
        if not cpa_dir:
            die("cannot find a CLIProxyAPI install dir; pass --url or --cpa-dir")
        cfg_path = cpa_dir / "config.yaml"
        if not cfg_path.exists():
            die(f"cannot find {cfg_path}; pass --url or --cpa-dir")
        text = cfg_path.read_text()
        m = re.search(r"^server:\s*\n(?:[ \t]+.*\n)*?[ \t]+port:\s*(\d+)", text, re.M) or re.search(
            r"^port:\s*(\d+)", text, re.M
        )
        if not m:
            die(f"cannot find server port in {cfg_path}; pass --url")
        url = f"http://127.0.0.1:{m.group(1)}"
    key = os.environ.get("CPA_MGMT_KEY")
    if not key:
        key_file = Path(os.path.expanduser(str(
            args.mgmt_key_file or (cpa_dir / "management.key" if cpa_dir else ""))))
        if not key_file.exists():
            die("cannot locate management.key; set CPA_MGMT_KEY, "
                "--mgmt-key-file or --cpa-dir"
                + (f" (tried {key_file})" if args.mgmt_key_file else ""))
        key = key_file.read_text().strip()
    return CPAConfig(url=url.rstrip("/"), mgmt_key=key)


# Usage-queue endpoint across CPA API versions. Newest first; the first one that
# does not return 404 wins and is cached for the process lifetime.
USAGE_QUEUE_PATHS = (
    "/v8/management/observability/usage/queue",
    "/v0/management/usage-queue",
)

# Credentials endpoints carry per-account quota signals / cooldown state.
CREDENTIALS_PATHS = (
    "/v8/management/credentials",
    "/v0/management/auth-files",
)

_usage_queue_path: str | None = None
_credentials_path: str | None = None


def _mgmt_get(cfg: CPAConfig, path: str, timeout: float):
    """GET a management endpoint; returns (parsed_json, error)."""
    req = urllib.request.Request(
        f"{cfg.url}{path}",
        headers={"Authorization": f"Bearer {cfg.mgmt_key}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}"), None
    except urllib.error.HTTPError as exc:
        return None, exc


def _fetch_queue_path(cfg: CPAConfig, path: str, batch: int, timeout: float):
    return _mgmt_get(cfg, f"{path}?count={batch}", timeout)


def fetch_queue(cfg: CPAConfig, batch: int, timeout: float = 10.0) -> list[dict]:
    """Drain the usage queue. Auto-detects the management API version."""
    global _usage_queue_path
    candidates = [_usage_queue_path] if _usage_queue_path else list(USAGE_QUEUE_PATHS)
    last_err = None
    for path in candidates:
        data, err = _fetch_queue_path(cfg, path, batch, timeout)
        if err is None:
            _usage_queue_path = path
            if isinstance(data, dict):  # tolerate {"items": [...]} envelopes
                data = data.get("items") or data.get("data") or []
            return data if isinstance(data, list) else []
        last_err = err
        if err.code == 404:
            continue  # try the next API version
        raise
    if last_err:
        raise last_err
    return []


def fetch_credentials(cfg: CPAConfig, timeout: float = 10.0) -> dict:
    """Snapshot of managed credentials: quota signals, status, cooldowns."""
    global _credentials_path
    candidates = [_credentials_path] if _credentials_path else list(CREDENTIALS_PATHS)
    last_err = None
    for path in candidates:
        data, err = _mgmt_get(cfg, path, timeout)
        if err is None:
            _credentials_path = path
            if isinstance(data, list):  # tolerate a bare [...] body
                data = {"files": data}
            return data if isinstance(data, dict) else {"files": []}
        last_err = err
        if err.code == 404:
            continue
        raise
    if last_err:
        raise last_err
    return {"files": []}


def cmd_collect(args) -> None:
    cfg = load_cpa_config(args)
    db_path = Path(args.db)
    conn = open_db(db_path)
    spool = db_path.with_name("spool.jsonl")
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def log(msg):
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)

    # Replay records that were popped but could not be written last time.
    if spool.exists():
        recs = [json.loads(line) for line in spool.read_text().splitlines() if line.strip()]
        with conn:
            for r in recs:
                insert_records(conn, [r])
        spool.unlink()
        log(f"replayed {len(recs)} spooled records")

    from . import __version__

    log(f"collector v{__version__} started: {cfg.url} -> {db_path} (interval {args.interval}s)")
    meta_set(conn, "collector_pid", os.getpid())
    meta_set(conn, "collector_started", time.time())
    conn.commit()

    backoff = args.interval
    last_error = None
    total = 0
    last_prune = 0.0
    quota_every = max(float(args.quota_interval), 0.0)
    last_quota = 0.0
    quota_err = None
    while not stop["flag"]:
        try:
            got = 0
            while True:
                recs = fetch_queue(cfg, args.batch)
                if not recs:
                    break
                try:
                    got += insert_records(conn, recs)
                except Exception as exc:  # never lose popped data
                    with spool.open("a") as fh:
                        for r in recs:
                            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    raise RuntimeError(
                        f"db write failed, spooled {len(recs)} records: {exc}"
                    ) from exc
                if len(recs) < args.batch:
                    break
            total += got
            if got and args.verbose:
                log(f"stored {got} records (total {total})")
            with conn:
                meta_set(conn, "collector_heartbeat", time.time())
                meta_set(conn, "collector_last_error", "")
            if last_error:
                log("recovered")
                last_error = None
            backoff = args.interval
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            msg = str(getattr(exc, "reason", exc))
            if isinstance(exc, urllib.error.HTTPError):
                msg = f"HTTP {exc.code} from usage queue"
                if exc.code in (401, 403):
                    msg += " (management key rejected)"
                elif exc.code == 404:
                    msg += " (management API disabled? set management.secret-key)"
            if msg != last_error:
                log(f"error: {msg}")
            last_error = msg
            try:
                with conn:
                    meta_set(conn, "collector_last_error", f"{time.time()}|{msg}")
            except sqlite3.Error:
                pass
            backoff = min(max(backoff * 2, args.interval), 30)
        if quota_every and time.time() - last_quota >= quota_every:
            try:
                snap = fetch_credentials(cfg)
                payload = {"fetched_at": time.time(),
                           "observed_at": snap.get("observed_at"),
                           "files": snap.get("files") or []}
                with conn:
                    save_quota_snapshot(conn, payload)
                if quota_err:
                    log("quota snapshot recovered")
                    quota_err = None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                msg = str(getattr(exc, "reason", exc))
                if isinstance(exc, urllib.error.HTTPError):
                    msg = f"HTTP {exc.code} from credentials endpoint"
                if msg != quota_err:
                    log(f"quota snapshot error: {msg}")
                quota_err = msg
            last_quota = time.time()
        if args.retention_days and args.retention_days > 0 and time.time() - last_prune > 3600:
            try:
                n = prune(conn, args.retention_days * 86400)
                if n:
                    log(f"pruned {n} records older than {args.retention_days:g}d")
            except sqlite3.Error as exc:
                log(f"prune failed: {exc}")
            last_prune = time.time()
        if args.once:
            break
        slept = 0.0
        while slept < backoff and not stop["flag"]:
            time.sleep(0.2)
            slept += 0.2

    log(f"collector stopped (stored {total} records this run)")
