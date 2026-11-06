"""Record parsing and metric math. Pure functions — no I/O, no rich."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

# Below these thresholds the decode window is too short to give a meaningful TPS.
MIN_DECODE_MS = 300
MIN_DECODE_TOKENS = 16

# Fields never written to the database.
SENSITIVE_KEYS = ("api_key", "response_headers", "access_token_sha256")


def parse_ts(value: str) -> float:
    # Go RFC3339Nano: 2026-11-02T19:55:38.436314676+08:00 -> trim to microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(value).timestamp()


def decode_tps(out_tokens, latency_ms, ttft_ms):
    if not out_tokens or latency_ms is None or ttft_ms is None:
        return None
    decode_ms = latency_ms - ttft_ms
    if decode_ms < MIN_DECODE_MS or out_tokens < MIN_DECODE_TOKENS:
        return None
    return out_tokens / (decode_ms / 1000.0)


def e2e_tps(out_tokens, latency_ms):
    if not out_tokens or not latency_ms:
        return None
    return out_tokens / (latency_ms / 1000.0)


def pct(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def token_parts(row):
    """(prompt_total, uncached, cache_read, cache_write) for one request.

    Most Devin records satisfy input = uncached + cache_read + cache_write.
    Some (Claude, GPT cache-creation turns) report cache_write outside input;
    then uncached = input - cache_read.
    """
    inp = (row["input_tokens"] or 0)
    read = (row["cache_read_tokens"] or 0) if "cache_read_tokens" in row.keys() else 0
    write = (row["cache_write_tokens"] or 0) if "cache_write_tokens" in row.keys() else 0
    uncached = inp - read - write
    if uncached < 0:
        uncached = max(inp - read, 0)
    return uncached + read + write, uncached, read, write


def token_quality(rec: dict) -> str:
    """'complete' | 'inconsistent' | 'partial' | '' — from upstream token_breakdown."""
    tb = rec.get("token_breakdown") or {}
    return tb.get("quality") or ""


def field(row, name, default=None):
    """row[name] tolerant of databases written before the column existed."""
    try:
        return row[name] if name in row.keys() else default
    except (IndexError, KeyError):
        return default


def short_model(row) -> str:
    name = row["alias"] or row["model"] or "?"
    return name.removeprefix("devin/")


def short_endpoint(ep) -> str:
    """'POST /v1/responses' -> 'responses'; '' stays ''."""
    if not ep:
        return ""
    ep = ep.split()[-1]  # drop the HTTP method
    return ep.rsplit("/", 1)[-1] or ep


def _opt_bool(v):
    return None if v is None else int(bool(v))


COLUMNS = [
    "id", "ts", "model", "alias", "response_model", "provider", "effort", "service_tier",
    "endpoint", "source", "auth_index", "session_id", "request_id", "user_agent", "stream",
    "failed", "status_code", "error", "latency_ms", "ttft_ms", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "token_quality", "raw",
    # v3: routing, session topology, upstream detail
    "executor_type", "auth_type", "access_provider", "is_native_key",
    "trace_id", "parent_session_id", "node_kind", "is_fork", "is_compaction",
    "client_ip", "resolved_client_ip", "x_forwarded_for",
    "effective_service_tier", "response_service_tier", "upstream_service_tier",
    "speed", "inference_geo", "fail_code", "total_tokens", "generate",
]

INSERT_SQL = (
    f"INSERT OR IGNORE INTO requests({','.join(COLUMNS)}) "
    f"VALUES({','.join('?' * len(COLUMNS))})"
)


def record_to_row(rec: dict) -> tuple:
    """Convert one usage-queue record into a requests-table row (drops sensitive keys)."""
    tokens = rec.get("tokens") if isinstance(rec.get("tokens"), dict) else {}
    tb = rec.get("token_breakdown") if isinstance(rec.get("token_breakdown"), dict) else {}
    fail = rec.get("fail") if isinstance(rec.get("fail"), dict) else {}
    ident = rec.get("execution_id") or ""
    digest_src = f"{rec.get('timestamp')}|{rec.get('request_id')}|{ident}|{rec.get('model')}"
    rid = ident or hashlib.sha1(digest_src.encode()).hexdigest()
    clean = {k: v for k, v in rec.items() if k not in SENSITIVE_KEYS}
    cache_write = tokens.get("cache_creation_tokens")
    if cache_write is None:
        cache_write = (tb.get("input") or {}).get("cache_write_tokens")
    cache_read = tokens.get("cache_read_tokens")
    if cache_read is None:
        cache_read = tokens.get("cached_tokens")
    error = (fail.get("body") or "")[:2000] or None
    try:
        ts = parse_ts(rec["timestamp"]) if rec.get("timestamp") else time.time()
    except (ValueError, TypeError):
        ts = time.time()  # a malformed timestamp must not drop the record
    values = {
        "id": rid,
        "ts": ts,
        "model": rec.get("model"),
        "alias": rec.get("alias"),
        "response_model": rec.get("response_model"),
        "provider": rec.get("provider"),
        "effort": rec.get("reasoning_effort"),
        "service_tier": rec.get("service_tier"),
        "endpoint": rec.get("endpoint"),
        "source": rec.get("source"),
        "auth_index": rec.get("auth_index"),
        "session_id": rec.get("session_id"),
        "request_id": rec.get("request_id"),
        "user_agent": rec.get("user_agent"),
        "stream": int(bool(rec.get("stream"))),
        "failed": int(bool(rec.get("failed"))),
        "status_code": fail.get("status_code"),
        "error": error,
        "latency_ms": rec.get("latency_ms"),
        "ttft_ms": rec.get("ttft_ms") or None,
        "input_tokens": tokens.get("input_tokens"),
        "output_tokens": tokens.get("output_tokens"),
        "reasoning_tokens": tokens.get("reasoning_tokens"),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "token_quality": token_quality(rec) or None,
        "raw": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        "executor_type": rec.get("executor_type"),
        "auth_type": rec.get("auth_type"),
        "access_provider": rec.get("access_provider"),
        "is_native_key": _opt_bool(rec.get("is_native_key")),
        "trace_id": rec.get("trace_id"),
        "parent_session_id": rec.get("parent_session_id"),
        "node_kind": rec.get("node_kind"),
        "is_fork": _opt_bool(rec.get("is_fork")),
        "is_compaction": _opt_bool(rec.get("is_compaction")),
        "client_ip": rec.get("client_ip"),
        "resolved_client_ip": rec.get("resolved_client_ip"),
        "x_forwarded_for": rec.get("x_forwarded_for"),
        "effective_service_tier": rec.get("effective_service_tier"),
        "response_service_tier": rec.get("response_service_tier"),
        "upstream_service_tier": rec.get("upstream_service_tier"),
        "speed": rec.get("speed"),
        "inference_geo": rec.get("inference_geo"),
        "fail_code": fail.get("code") or None,
        "total_tokens": tokens.get("total_tokens") or tb.get("total_tokens"),
        "generate": _opt_bool(rec.get("generate")),
    }
    return tuple(values[c] for c in COLUMNS)


# --------------------------------------------------------------------------- pricing


@dataclass(frozen=True)
class Pricing:
    """USD per 1M tokens. cached_read applies to cache-read tokens."""

    input_per_m: float = 0.0
    cached_read_per_m: float = 0.0
    output_per_m: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.input_per_m > 0 or self.output_per_m > 0 or self.cached_read_per_m > 0

    def cost(self, uncached: float, cache_read: float, out_tok: float) -> float:
        return (
            uncached * self.input_per_m
            + cache_read * self.cached_read_per_m
            + out_tok * self.output_per_m
        ) / 1e6


def parse_pricing(text: str | None) -> Pricing:
    """'in,cached,out' USD per 1M tokens, e.g. '2,0.2,8'. Empty -> disabled."""
    if not text:
        return Pricing()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise ValueError("--price needs 'input,cached_read,output' USD per 1M tokens, e.g. '2,0.2,8'")
    try:
        return Pricing(*(float(p or 0) for p in parts))
    except ValueError as exc:
        raise ValueError(f"invalid --price value: {exc}") from exc


# --------------------------------------------------------------------------- aggregation


@dataclass
class Agg:
    n: int = 0
    failed: int = 0
    ttft_p50: float | None = None
    ttft_p95: float | None = None
    lat_p50: float | None = None
    lat_p95: float | None = None
    tps_p50: float | None = None
    tps_p10: float | None = None
    tps_p90: float | None = None
    e2e_tps: float | None = None
    e2e_p50: float | None = None
    burst: int = 0
    in_tok: int = 0
    uncached: int = 0
    out_tok: int = 0
    reasoning_tok: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_ratio: float | None = None
    rpm: float | None = None
    first_ts: float | None = None
    n_complete: int = 0
    cost_usd: float | None = None

    @property
    def quality_ratio(self) -> float | None:
        return self.n_complete / self.n if self.n else None


def aggregate(rows, window_s: float | None = None, pricing: Pricing | None = None) -> Agg:
    a = Agg(n=len(rows))
    if not rows:
        return a
    ok = [r for r in rows if not r["failed"]]
    a.failed = a.n - len(ok)
    ttfts = [r["ttft_ms"] for r in ok if r["ttft_ms"]]
    lats = [r["latency_ms"] for r in ok if r["latency_ms"]]
    tps = [decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"]) for r in ok]
    tps = [t for t in tps if t is not None]
    a.ttft_p50, a.ttft_p95 = pct(ttfts, 0.5), pct(ttfts, 0.95)
    a.e2e_p50 = pct([e2e_tps(r["output_tokens"], r["latency_ms"]) for r in ok], 0.5)
    a.burst = sum(1 for r in ok if (r["output_tokens"] or 0) >= MIN_DECODE_TOKENS and r["latency_ms"]
                  and r["ttft_ms"] and r["latency_ms"] - r["ttft_ms"] < MIN_DECODE_MS)
    a.lat_p50, a.lat_p95 = pct(lats, 0.5), pct(lats, 0.95)
    a.tps_p50, a.tps_p10, a.tps_p90 = pct(tps, 0.5), pct(tps, 0.1), pct(tps, 0.9)
    for r in rows:
        total, uncached, read, write = token_parts(r)
        a.in_tok += total
        a.uncached += uncached
        a.cache_read += read
        a.cache_write += write
    a.out_tok = sum(r["output_tokens"] or 0 for r in rows)
    a.reasoning_tok = sum(r["reasoning_tokens"] or 0 for r in rows)
    a.n_complete = sum(1 for r in rows if _row_quality(r) == "complete")
    a.cache_ratio = a.cache_read / a.in_tok if a.in_tok else None
    total_lat = sum(lats)
    a.e2e_tps = (sum(r["output_tokens"] or 0 for r in ok if r["latency_ms"]) / (total_lat / 1000)
                 if total_lat else None)
    if window_s:
        a.rpm = a.n / (window_s / 60)
    a.first_ts = min((r["ts"] for r in rows), default=None)
    if pricing and pricing.enabled:
        a.cost_usd = pricing.cost(a.uncached, a.cache_read, a.out_tok)
    return a


def _row_quality(r) -> str:
    """token_quality column first; fall back to raw JSON for databases written before v2."""
    q = r["token_quality"] if "token_quality" in r.keys() else None
    if q:
        return q
    raw = r["raw"]
    if not raw:
        return ""
    try:
        return token_quality(json.loads(raw))
    except (ValueError, TypeError):
        return ""


def group_by(rows, key):
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return groups


def bucket_counts(rows, since_ts: float, bucket_s: float, buckets: int) -> list[int]:
    """Request counts per time bucket, oldest -> newest."""
    counts = [0] * buckets
    for r in rows:
        age = r["ts"] - since_ts
        idx = int(age / bucket_s)
        if 0 <= idx < buckets:
            counts[idx] += 1
    return counts


# Friendly bucket sizes for `trend` (seconds).
BUCKET_LADDER = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
                 43200, 86400)


def pick_bucket(window_s: float, target: int = 40) -> tuple[float, int]:
    """(bucket_s, n_buckets) covering window_s, snapped to a friendly size."""
    if window_s <= 0:
        window_s = 3600
    for b in BUCKET_LADDER:
        if window_s / b <= target:
            return float(b), max(1, math.ceil(window_s / b))
    b = float(math.ceil(window_s / target / 86400) * 86400)
    return b, max(1, math.ceil(window_s / b))


def bucket_aggs(rows, since_ts: float, bucket_s: float, nbuckets: int,
                pricing=None) -> list:
    """One Agg per time bucket (oldest -> newest); None for empty buckets."""
    buckets: list[list] = [[] for _ in range(nbuckets)]
    for r in rows:
        idx = int((r["ts"] - since_ts) / bucket_s)
        if 0 <= idx < nbuckets:
            buckets[idx].append(r)
    return [aggregate(b, bucket_s, pricing) if b else None for b in buckets]


def export_dict(r) -> dict:
    """Flat dict for one request row — the export/tui-`e`/`show --json` shape."""
    d = {c: r[c] for c in COLUMNS if c != "raw" and c in r.keys()}
    total, uncached, read, _ = token_parts(r)
    d["prompt_tokens_total"], d["uncached_tokens"] = total, uncached
    d["cache_hit_ratio"] = round(read / total, 4) if total else None
    d["decode_tps"] = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
    d["e2e_tps"] = e2e_tps(r["output_tokens"], r["latency_ms"])
    d["iso_time"] = datetime.fromtimestamp(r["ts"]).astimezone().isoformat()
    return d


# --------------------------------------------------------------------------- quota

# Signal keys CPA writes for Devin credentials (from GetUserStatus). Codex
# (x-codex-*) and Claude (anthropic-ratelimit-*) snapshots are normalized into
# the same daily/weekly fields below; anything left over lands in `extra`.
DEVIN_QUOTA_KEYS = {
    "plan", "daily_quota_remaining_percent", "weekly_quota_remaining_percent",
    "daily_quota_reset_at", "weekly_quota_reset_at", "plan_start", "plan_end",
}


def parse_pct(value) -> int | None:
    """'81%' / '81' / 81 -> 81; anything else -> None."""
    if value is None:
        return None
    m = re.match(r"\s*(-?\d+)", str(value))
    return int(m.group(1)) if m else None


def parse_iso_ts(value) -> float | None:
    """RFC3339 -> epoch seconds; None when unparseable."""
    if not value:
        return None
    try:
        return parse_ts(str(value))
    except (ValueError, TypeError):
        return None


def _parse_float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_epoch(value) -> float | None:
    """Unix seconds / milliseconds or RFC3339 -> epoch seconds."""
    if value is None:
        return None
    f = _parse_float(value)
    if f is not None and f > 0:
        if f > 1e12:            # milliseconds
            f /= 1000.0
        return f
    return parse_iso_ts(value)


def _retry_after_ts(value, base: float) -> float | None:
    """Retry-After header (seconds or HTTP-date) -> absolute epoch."""
    if value is None:
        return None
    secs = _parse_float(value)
    if secs is not None:
        return base + max(0.0, secs)
    try:
        return parsedate_to_datetime(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _window_minutes_label(minutes) -> str:
    """300 -> '5h', 10080 -> '7d', 45 -> '45m'; '' when unknown."""
    m = _parse_float(minutes)
    if not m or m <= 0:
        return ""
    if m % 1440 == 0:
        return f"{int(m // 1440)}d"
    if m % 60 == 0:
        return f"{int(m // 60)}h"
    return f"{int(m)}m"


def _remaining_from_used(used) -> int | None:
    """used-percent signal ('42' / '42%') -> remaining pct."""
    u = _parse_float(used)
    if u is None:
        return None
    return max(0, min(100, round(100 - u)))


def _remaining_from_util(util) -> int | None:
    """Claude utilization fraction (0.42) -> remaining pct; >1 is already %."""
    u = _parse_float(util)
    if u is None:
        return None
    rem = (1 - u) * 100 if u <= 1.0 else 100 - u
    return max(0, min(100, round(rem)))


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _falsy(value) -> bool:
    return str(value).strip().lower() in ("false", "0", "no")


# Named Codex windows: x-codex-additional-<slug>-*, x-codex-code-review-* and
# short-name namespaces like x-codex-bengalfox-primary-used-percent.
_CODEX_WIN_SUFFIXES = ("used-percent", "window-minutes", "reset-after-seconds",
                       "reset-at", "over-secondary-limit-percent")
_CODEX_WIN_RE = re.compile(
    r"^(?:(?P<slug>.+)-)?(?P<win>primary|secondary)-(?P<fld>"
    + "|".join(_CODEX_WIN_SUFFIXES) + r")$")
_CODEX_FLAG_RE = re.compile(
    r"^(?:(?P<slug>.+)-)?(?P<fld>allowed|limit-reached|limit-name|status)$")
_CLAUDE_STATUS_KEYS = ("anthropic-ratelimit-unified-status",
                       "anthropic-ratelimit-unified-5h-status",
                       "anthropic-ratelimit-unified-7d-status")


def _codex_window_reset(sig: dict, prefix: str, used: set, base: float):
    """reset-at epoch, else observed + reset-after-seconds."""
    key_at = f"{prefix}reset-at"
    key_after = f"{prefix}reset-after-seconds"
    t = None
    if key_at in sig:
        used.add(key_at)
        t = _parse_epoch(sig[key_at])
    if key_after in sig:
        used.add(key_after)
        if t is None:
            secs = _parse_float(sig[key_after])
            if secs is not None:
                t = base + secs
    return t


def _normalize_codex(c: "CredInfo", sig: dict, used: set, base: float) -> None:
    """Map x-codex-* signals onto the shared daily/weekly quota fields.

    Codex reports *used* percent per window: primary is the short window
    (typically 5h, x-codex-primary-window-minutes says exactly), secondary the
    long one (typically 7d)."""
    if sig.get("x-codex-plan-type"):
        used.add("x-codex-plan-type")
        c.plan = c.plan or str(sig["x-codex-plan-type"])
    for win, pct_attr, reset_attr, label_attr, dflt in (
            ("primary", "daily_pct", "daily_reset", "daily_label", "5h"),
            ("secondary", "weekly_pct", "weekly_reset", "weekly_label", "7d")):
        ukey = f"x-codex-{win}-used-percent"
        if ukey in sig:
            used.add(ukey)
            rem = _remaining_from_used(sig[ukey])
            if rem is not None:
                setattr(c, pct_attr, rem)
        rt = _codex_window_reset(sig, f"x-codex-{win}-", used, base)
        if rt:
            setattr(c, reset_attr, rt)
        mkey = f"x-codex-{win}-window-minutes"
        label = ""
        if mkey in sig:
            used.add(mkey)
            label = _window_minutes_label(sig[mkey])
        setattr(c, label_attr, label or dflt)
    for key in ("x-codex-active-limit", "x-codex-allowed",
                "x-codex-limit-reached"):
        if key in sig:
            used.add(key)
    if _truthy(sig.get("x-codex-limit-reached")) or _falsy(sig.get("x-codex-allowed")):
        c.limited = True
    credits = {}
    for k, v in sig.items():
        if k.startswith("x-codex-credits-"):
            used.add(k)
            credits[k[len("x-codex-credits-"):]] = v
    c.credits = credits or None
    # Named extra windows (additional limits, code review, short-name slugs).
    groups: dict[str, dict] = {}
    for k, v in sig.items():
        if not k.startswith("x-codex-") or k in used:
            continue
        body = k[len("x-codex-"):]
        ns, rest = "", body
        if body.startswith("additional-"):
            ns, rest = "additional", body[len("additional-"):]
        elif body.startswith("code-review-"):
            ns, rest = "code review", body[len("code-review-"):]
        elif body.startswith(("primary-", "secondary-")):
            continue                # main window handled above
        m = _CODEX_WIN_RE.match(rest)
        if m:
            name = m.group("slug") or ns
            if not name:
                continue
            ent = groups.setdefault(name, {"name": name, "ns": ns, "windows": {}})
            ent["windows"].setdefault(m.group("win"), {})[m.group("fld")] = v
            used.add(k)
            continue
        m = _CODEX_FLAG_RE.match(rest)
        if m:
            name = m.group("slug") or ns
            if not name:
                continue            # bare 'allowed' etc. = main-window scalar
            ent = groups.setdefault(name, {"name": name, "ns": ns, "windows": {}})
            ent[m.group("fld")] = v
            used.add(k)
    c.limits = []
    for name, ent in sorted(groups.items()):
        prefix = ("x-codex-code-review" if ent["ns"] == "code review"
                  else f"x-codex-additional-{name}" if ent["ns"] == "additional"
                  else f"x-codex-{name}")
        item = {"name": name, "label": ent.get("limit-name") or name,
                "sig_prefix": prefix}
        if _truthy(ent.get("limit-reached")) or _falsy(ent.get("allowed")):
            item["limited"] = True
        for win, tag in (("primary", "short"), ("secondary", "long")):
            w = ent["windows"].get(win)
            if not w:
                continue
            rt = _parse_epoch(w.get("reset-at"))
            if rt is None:
                secs = _parse_float(w.get("reset-after-seconds"))
                rt = base + secs if secs is not None else None
            item[tag] = {
                "pct": _remaining_from_used(w.get("used-percent")),
                "window": _window_minutes_label(w.get("window-minutes")),
                "reset": rt,
            }
            over = _parse_float(w.get("over-secondary-limit-percent"))
            if over is not None:
                item[tag]["over_pct"] = over
        if not item.get("short") and not item.get("long") and not item.get("limited"):
            continue                # limit-name only, no window data — noise
        c.limits.append(item)


def _normalize_claude(c: "CredInfo", sig: dict, used: set, base: float) -> None:
    """Map anthropic-ratelimit-unified-* signals onto daily/weekly fields.

    Utilization is a 0..1 *used* fraction; remaining = (1 - u) * 100."""
    c.daily_label, c.weekly_label = "5h", "7d"
    for win, pct_attr, reset_attr in (
            ("5h", "daily_pct", "daily_reset"),
            ("7d", "weekly_pct", "weekly_reset")):
        ukey = f"anthropic-ratelimit-unified-{win}-utilization"
        if ukey in sig:
            used.add(ukey)
            rem = _remaining_from_util(sig[ukey])
            if rem is not None:
                setattr(c, pct_attr, rem)
        for suffix in (f"-{win}-reset", f"-{win}-status"):
            key = f"anthropic-ratelimit-unified{suffix}"
            if key in sig:
                used.add(key)
        rkey = f"anthropic-ratelimit-unified-{win}-reset"
        t = _parse_epoch(sig.get(rkey))
        if t:
            setattr(c, reset_attr, t)
    if "anthropic-ratelimit-unified-reset" in sig:
        used.add("anthropic-ratelimit-unified-reset")
        t = _parse_epoch(sig["anthropic-ratelimit-unified-reset"])
        if t and c.daily_reset is None:
            c.daily_reset = t
    if "anthropic-ratelimit-unified-status" in sig:
        used.add("anthropic-ratelimit-unified-status")
    statuses = {str(sig.get(k) or "").strip().lower()
                for k in _CLAUDE_STATUS_KEYS}
    if "rejected" in statuses:
        c.limited = True


def _normalize_signals(c: "CredInfo", observed: float | None) -> set:
    """Dispatch to the provider's signal mapper; returns consumed key set."""
    used: set = set()
    sig = {str(k).lower(): v for k, v in (c.signals or {}).items()}
    base = observed or time.time()
    provider = c.provider.lower()
    if provider == "codex":
        _normalize_codex(c, sig, used, base)
    elif provider == "claude":
        _normalize_claude(c, sig, used, base)
    if "retry-after" in sig:
        used.add("retry-after")
        c.retry_after_ts = _retry_after_ts(sig["retry-after"], base)
    return used


@dataclass
class CredInfo:
    """One managed credential from a /credentials snapshot."""

    name: str = ""
    provider: str = ""
    account: str = ""
    status: str = ""
    status_message: str = ""
    disabled: bool = False
    blocked: bool = False       # quota_blocked or marked unavailable
    ok: int = 0
    fail: int = 0
    plan: str = ""
    daily_pct: int | None = None
    weekly_pct: int | None = None
    daily_label: str = "daily"      # window name: 'daily', '5h', ...
    weekly_label: str = "weekly"
    daily_reset: float | None = None
    weekly_reset: float | None = None
    plan_start: float | None = None
    plan_end: float | None = None
    limited: bool = False           # upstream signals limit reached / rejected
    retry_after_ts: float | None = None
    credits: dict | None = None     # codex credits-*
    limits: list | None = None      # named extra windows (codex additional/*)
    observed_at: str = ""
    instance: str = ""              # CPA instance host (multi-url collector)
    signals: dict | None = None
    extra: dict | None = None       # signals not normalized above
    cooldowns: list | None = None
    model_quotas: dict | None = None


def parse_credentials(snap) -> list[CredInfo]:
    """Normalize a credentials-snapshot payload ({files: [...]} or bare list)."""
    if not snap:
        return []
    files = snap.get("files") if isinstance(snap, dict) else snap
    if not isinstance(files, list):
        return []
    out: list[CredInfo] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        quota = f.get("quota") if isinstance(f.get("quota"), dict) else {}
        signals = dict(quota.get("signals") or {})
        status = str(f.get("status") or "?").lower()
        disabled = bool(f.get("disabled"))
        if disabled:
            status = "disabled"
        account = (f.get("email") or f.get("account") or f.get("label")
                   or f.get("name") or f.get("id") or "?")
        c = CredInfo(
            name=str(f.get("name") or f.get("id") or "?"),
            provider=str(f.get("provider") or f.get("type") or ""),
            account=str(account),
            status=status,
            status_message=str(f.get("status_message") or ""),
            disabled=disabled,
            blocked=bool(f.get("quota_blocked") or f.get("unavailable")),
            ok=int(f.get("success") or 0),
            fail=int(f.get("failed") or 0),
            plan=str(signals.get("plan") or ""),
            daily_pct=parse_pct(signals.get("daily_quota_remaining_percent")),
            weekly_pct=parse_pct(signals.get("weekly_quota_remaining_percent")),
            daily_reset=parse_iso_ts(signals.get("daily_quota_reset_at")),
            weekly_reset=parse_iso_ts(signals.get("weekly_quota_reset_at")),
            plan_start=parse_iso_ts(signals.get("plan_start")),
            plan_end=parse_iso_ts(signals.get("plan_end")),
            observed_at=str(quota.get("observed_at") or ""),
            signals=signals,
            cooldowns=list(f.get("cooldowns") or []),
            model_quotas=f.get("model_quotas") if isinstance(f.get("model_quotas"), dict) else {},
        )
        instance = str(f.get("_instance") or f.get("instance") or "")
        if instance:
            c.instance = instance
            # keep history names unique when two instances share file names
            c.name = f"{c.name}@{instance}"
        used = _normalize_signals(c, parse_iso_ts(c.observed_at))
        c.extra = {str(k).lower(): v for k, v in signals.items()
                   if str(k).lower() not in DEVIN_QUOTA_KEYS
                   and str(k).lower() not in used}
        out.append(c)
    return out


def cred_sort_key(c: CredInfo):
    """Worst-first ordering for quota tables: blocked, then limited, then the
    lowest weekly/daily percentage."""
    return (not c.blocked, not c.limited,
            c.weekly_pct is None, c.weekly_pct if c.weekly_pct is not None else 0,
            c.daily_pct is None, c.daily_pct if c.daily_pct is not None else 0,
            c.account)


def quota_forecast(history_rows, key: str = "weekly_pct",
                   min_span_s: float = 1800, max_step_rate: float = 0.1):
    """Burn-rate forecast for one credential's history rows (ts ascending).

    Returns (current_pct, pct_drop_per_day, eta_ts) or None when the quota
    isn't shrinking or history is too thin. A rising value between two polls
    means a window reset, so only the trailing non-increasing run is fitted.
    The slope is least-squares over that run and requires `min_span_s` of
    coverage (30 min default) — quota is consumed in bursts, so a slope from
    a couple of minutes is meaningless noise.

    A single-step drop faster than max_step_rate pct/s (default 0.1 ≈ 6%/min)
    is treated as a watermark correction — a stale observation replaced by a
    fresh one — rather than consumption, and cuts the run there. Real burn
    almost never moves a multi-hour window that fast.
    """
    pts = sorted(
        (r["ts"], r[key]) for r in history_rows
        if r["ts"] is not None and r[key] is not None
    )
    if len(pts) < 3:
        return None
    end = len(pts) - 1
    start = end
    while start > 0:
        t_prev, p_prev = pts[start - 1]
        t_cur, p_cur = pts[start]
        if p_prev < p_cur:
            break                        # window reset / refill
        if p_prev - p_cur > max(3.0, max_step_rate * max(1.0, t_cur - t_prev)):
            break                        # corrected watermark, not burn
        start -= 1
    run = pts[start:]
    if len(run) < 3:
        return None
    (t0, p0), (t1, p1) = run[0], run[-1]
    if t1 - t0 < min_span_s or p1 >= p0:
        return None
    n = len(run)
    sx = sum(t for t, _ in run)
    sy = sum(p for _, p in run)
    sxx = sum(t * t for t, _ in run)
    sxy = sum(t * p for t, p in run)
    denom = n * sxx - sx * sx
    if denom <= 0:
        return None
    slope = (n * sxy - sx * sy) / denom  # pct per second, negative = draining
    if slope >= 0:
        return None
    return p1, -slope * 86400.0, t1 + p1 / -slope


def quota_pct_series(history_rows, key: str = "weekly_pct") -> list:
    """pct values over time for sparklines (None-safe)."""
    return [r[key] for r in sorted(history_rows, key=lambda r: r["ts"])
            if r[key] is not None]


def quota_pace(pct, reset_ts, burn_per_day, now: float):
    """(budget %/day, burn %/day, over_pace) — the %/day drain that lands on 0
    exactly at the window reset, vs the current burn. None when unknown."""
    if pct is None or not reset_ts or reset_ts <= now:
        return None
    budget = pct * 86400.0 / (reset_ts - now)
    over = burn_per_day is not None and burn_per_day > budget * 1.05
    return budget, burn_per_day, over


def signal_pct_series(history_rows, sig_key: str) -> list:
    """Remaining-% series for a codex used-percent signal key, read back from
    the raw signals JSON stored in quota_history. Case-insensitive."""
    out = []
    want = sig_key.lower()
    for r in sorted(history_rows, key=lambda r: r["ts"]):
        try:
            sig = json.loads(r["signals"] or "{}")
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(sig, dict):
            continue
        v = sig.get(sig_key)
        if v is None:
            v = next((sig[k] for k in sig if str(k).lower() == want), None)
        rem = _remaining_from_used(v)
        if rem is not None:
            out.append(rem)
    return out


def recovery_stats(rows, within_s: float = 300.0):
    """How fast failures recover: for each failed row, time until the next
    successful request for the same model. Returns (median_s, share_within).
    None when there are no failures or no following successes."""
    import bisect

    by_model_ok: dict[str, list] = {}
    fails = []
    for r in sorted(rows, key=lambda r: r["ts"]):
        model = short_model(r)
        if r["failed"]:
            fails.append(r)
        else:
            by_model_ok.setdefault(model, []).append(r["ts"])
    if not fails:
        return None
    deltas = []
    for r in fails:
        oks = by_model_ok.get(short_model(r))
        if not oks:
            continue
        i = bisect.bisect_right(oks, r["ts"])
        if i < len(oks):
            deltas.append(oks[i] - r["ts"])
    if not deltas:
        return None
    med = pct(deltas, 0.5)
    share = sum(1 for d in deltas if d <= within_s) / len(deltas)
    return med, share


# --------------------------------------------------------------------------- detail view


def _yesno(v) -> str:
    return "—" if v is None else ("yes" if v else "no")


def request_detail_pairs(row) -> list[tuple[str, str]]:
    """(label, value) lines describing one request — shared by `show` and the
    TUI detail modal. Column values come first; anything only present in the
    raw upstream record (cache TTL split, tool/media counts, iterations,
    upstream cost) is appended after."""
    from .format import fmt_cost, fmt_ms, fmt_pct, fmt_tok, fmt_tps

    raw: dict = {}
    if field(row, "raw"):
        try:
            raw = json.loads(row["raw"]) or {}
        except (ValueError, TypeError):
            raw = {}
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    tb = raw.get("token_breakdown") if isinstance(raw.get("token_breakdown"), dict) else {}
    tb_in = tb.get("input") if isinstance(tb.get("input"), dict) else {}
    tb_out = tb.get("output") if isinstance(tb.get("output"), dict) else {}

    total, uncached, read, write = token_parts(row)
    ratio = read / total if total else None
    tps = decode_tps(row["output_tokens"], row["latency_ms"], row["ttft_ms"])
    e2e = e2e_tps(row["output_tokens"], row["latency_ms"])
    burst = (not row["failed"]
             and (row["output_tokens"] or 0) >= MIN_DECODE_TOKENS
             and row["latency_ms"] and row["ttft_ms"]
             and row["latency_ms"] - row["ttft_ms"] < MIN_DECODE_MS)

    tiers = " → ".join([
        row["service_tier"] or "—",
        field(row, "effective_service_tier") or "—",
        field(row, "response_service_tier") or "—",
        field(row, "upstream_service_tier") or "—",
    ])
    session = row["session_id"] or "—"
    parent = field(row, "parent_session_id")
    if parent:
        session += f"  ← parent {parent}"
    node = " / ".join(x for x in (
        field(row, "node_kind"),
        "fork" if field(row, "is_fork") else None,
        "compaction" if field(row, "is_compaction") else None,
    ) if x) or "—"
    ips = " / ".join(x for x in (
        field(row, "client_ip"),
        field(row, "resolved_client_ip"),
        field(row, "x_forwarded_for"),
    ) if x) or "—"
    speed_geo = " / ".join(x for x in (field(row, "speed"),
                                      field(row, "inference_geo")) if x) or "—"
    access = " · ".join(x for x in (
        f"{field(row, 'auth_type') or '—'} via {field(row, 'access_provider') or '—'}",
        f"native {_yesno(field(row, 'is_native_key'))}",
    ) if x)

    if row["failed"]:
        status = f"✗ FAILED {row['status_code'] or ''}"
        if field(row, "fail_code"):
            status += f" ({field(row, 'fail_code')})"
    else:
        status = "✓ ok"
    status += f"  stream={_yesno(row['stream'])} generate={_yesno(field(row, 'generate'))}"

    lat = f"{fmt_ms(row['latency_ms'])} / {fmt_ms(row['ttft_ms'])}"
    speed = f"{fmt_tps(tps)} / {fmt_tps(e2e)} tok/s" + ("  (burst)" if burst else "")
    input_line = (f"{fmt_tok(total)} = {fmt_tok(uncached)} new + {fmt_tok(read)} read"
                  f" + {fmt_tok(write)} write  (hit {fmt_pct(ratio)})")
    if tb_in:
        input_line += (f"   breakdown: total {fmt_tok(tb_in.get('total_tokens'))}"
                       f" · uncached {fmt_tok(tb_in.get('uncached_tokens'))}")
    out_line = fmt_tok(row["output_tokens"])
    if row["reasoning_tokens"]:
        out_line += f"  reasoning {fmt_tok(row['reasoning_tokens'])}"
    if tb_out:
        out_line += (f"   breakdown: total {fmt_tok(tb_out.get('total_tokens'))}"
                     f" · reasoning {fmt_tok(tb_out.get('reasoning_tokens'))}"
                     f" · non-reasoning {fmt_tok(tb_out.get('non_reasoning_tokens'))}")

    pairs: list[tuple[str, str]] = [
        ("time", datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")),
        ("model", f"{short_model(row)}  (upstream: {row['response_model'] or '—'})"),
        ("provider / executor",
         f"{row['provider'] or '—'} / {field(row, 'executor_type') or '—'}"),
        ("effort", row["effort"] or "—"),
        ("tier req→eff→resp→up", tiers),
        ("endpoint", row["endpoint"] or "—"),
        ("speed / geo", speed_geo),
        ("account", f"{row['source'] or '—'}  (auth_index {row['auth_index'] or '—'})"),
        ("access", access),
        ("session", session),
        ("node", node),
        ("request / exec / trace",
         f"{row['request_id'] or '—'} / {row['id']} / {field(row, 'trace_id') or '—'}"),
        ("client", row["user_agent"] or "—"),
        ("client ip / resolved / xff", ips),
        ("status", status),
        ("latency / TTFT", lat),
        ("TPS decode / e2e", speed),
        ("input tokens", input_line),
        ("output tokens", out_line),
        ("total tokens", f"{fmt_tok(field(row, 'total_tokens') or tokens.get('total_tokens'))}"
         f"   unclassified {fmt_tok(tb.get('unclassified_tokens'))}"),
        ("token quality", row["token_quality"] or tb.get("quality") or "—"),
    ]

    # --- fields that only exist inside the raw upstream record ----------------
    def nz(d, keys):
        return {k: d[k] for k in keys if isinstance(d, dict) and d.get(k)}

    extra_tokens = nz(tokens, (
        "cache_creation_5m_tokens", "cache_creation_30m_tokens",
        "cache_creation_1h_tokens", "cache_creation_12h_tokens",
        "text_input_tokens", "image_input_tokens", "audio_input_tokens",
        "video_input_tokens", "file_input_tokens", "cached_text_tokens",
        "text_output_tokens", "image_output_tokens", "audio_output_tokens",
        "accepted_prediction_tokens", "rejected_prediction_tokens",
    ))
    if extra_tokens:
        pairs.append(("token extras", " · ".join(
            f"{k.removesuffix('_tokens')} {fmt_tok(v)}" for k, v in extra_tokens.items())))

    cache = raw.get("cache_details") if isinstance(raw.get("cache_details"), dict) else {}
    cache_bits = [f"{k} {v}" for k, v in cache.items() if v and k != "by_ttl"]
    by_ttl = cache.get("by_ttl") if isinstance(cache.get("by_ttl"), dict) else {}
    cache_bits += [f"ttl {k} {fmt_tok(v)}" for k, v in sorted(by_ttl.items())]
    if cache_bits:
        pairs.append(("cache details", " · ".join(cache_bits)))

    tools = nz(raw.get("tool_usage_details"), (
        "web_search_calls", "web_search_queries", "web_fetch_calls",
        "num_search_results", "num_sources_used", "num_server_side_tools_used",
        "tool_calls_requested", "tool_calls_executed", "code_execution_calls",
        "file_search_calls", "mcp_calls", "document_search_calls",
        "image_generation_calls", "x_search_calls", "x_posts_fetched",
        "x_users_fetched",
    ))
    if tools:
        pairs.append(("server tools", " · ".join(f"{k} {v}" for k, v in tools.items())))

    media = nz(raw.get("media_counts"),
               ("prompt_media", "completion_media", "prompt_audio", "audio_seconds"))
    for k in ("web_search_requests", "image_count"):
        if raw.get(k):
            media[k] = raw[k]
    if media:
        pairs.append(("media", " · ".join(f"{k} {v}" for k, v in media.items())))

    img = raw.get("image_details") or raw.get("requested_image_details")
    if isinstance(img, dict) and any(img.values()):
        pairs.append(("image", " · ".join(f"{k} {v}" for k, v in img.items() if v)))

    iters = raw.get("iterations") if isinstance(raw.get("iterations"), list) else []
    if iters:
        def _it(i):
            if not isinstance(i, dict):
                return "?"
            return (f"{i.get('type') or 'iter'}:{i.get('model') or '?'} "
                    f"in {fmt_tok(i.get('input_tokens'))}"
                    f"/out {fmt_tok(i.get('output_tokens'))}"
                    + (f" think {fmt_tok(i.get('thinking_tokens'))}"
                       if i.get("thinking_tokens") else ""))
        pairs.append((f"iterations [{len(iters)}]", "   ".join(_it(i) for i in iters[:8])
                      + ("  …" if len(iters) > 8 else "")))

    cost = nz(raw.get("cost_details"), (
        "cost", "cost_in_usd_ticks", "upstream_inference_cost",
        "upstream_inference_prompt_cost", "upstream_inference_completions_cost"))
    if cost:
        parts = [f"{k} {fmt_cost(v) if 'cost' in k else v}" for k, v in cost.items()]
        if raw.get("cost_details", {}).get("is_byok"):
            parts.append("byok")
        pairs.append(("upstream cost", " · ".join(parts)))

    if row["error"]:
        pairs.append(("error", row["error"][:600]))
    return pairs
