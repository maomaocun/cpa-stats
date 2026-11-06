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


# --------------------------------------------------------------------------- quota

# Signal keys CPA writes for Devin credentials (from GetUserStatus); anything
# else in `signals` (x-codex-*, anthropic-ratelimit-*, plugin values) is shown
# verbatim as "extra".
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
    daily_reset: float | None = None
    weekly_reset: float | None = None
    plan_start: float | None = None
    plan_end: float | None = None
    observed_at: str = ""
    signals: dict | None = None
    extra: dict | None = None       # non-Devin signals, e.g. x-codex-*/anthropic-*
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
        out.append(CredInfo(
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
            extra={k: v for k, v in signals.items() if k not in DEVIN_QUOTA_KEYS},
            cooldowns=list(f.get("cooldowns") or []),
            model_quotas=f.get("model_quotas") if isinstance(f.get("model_quotas"), dict) else {},
        ))
    return out


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
