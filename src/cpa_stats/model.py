"""Record parsing and metric math. Pure functions — no I/O, no rich."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime

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


def short_model(row) -> str:
    name = row["alias"] or row["model"] or "?"
    return name.removeprefix("devin/")


COLUMNS = [
    "id", "ts", "model", "alias", "response_model", "provider", "effort", "service_tier",
    "endpoint", "source", "auth_index", "session_id", "request_id", "user_agent", "stream",
    "failed", "status_code", "error", "latency_ms", "ttft_ms", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "token_quality", "raw",
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
