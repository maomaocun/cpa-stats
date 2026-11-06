"""Unit tests for pure metric/parsing functions."""

from cpa_stats.model import (COLUMNS, MIN_DECODE_MS, Pricing, decode_tps,
                             e2e_tps, parse_credentials, parse_iso_ts,
                             parse_pricing, parse_pct, parse_ts, pct,
                             quota_forecast, quota_pct_series, record_to_row,
                             request_detail_pairs, token_parts, token_quality)


def rec(**kw):
    base = {
        "timestamp": "2026-11-02T19:55:38.436314676+08:00",
        "execution_id": "exec-1",
        "model": "devin/swe-2",
        "alias": "swe-2",
        "latency_ms": 2000,
        "ttft_ms": 500,
        "failed": False,
        "stream": True,
        "fail": {"status_code": 200},
        "tokens": {"input_tokens": 1000, "output_tokens": 100,
                   "cache_read_tokens": 400, "cache_creation_tokens": 100},
    }
    base.update(kw)
    return base


def row(**kw):
    cols = dict(zip(
        COLUMNS,
        record_to_row(rec(**kw))))
    return cols


class TestParseTs:
    def test_rfc3339_nano(self):
        ts = parse_ts("2026-11-02T19:55:38.436314676+08:00")
        assert abs(ts - 1793620538.436314) < 0.001

    def test_z_suffix(self):
        assert parse_ts("2026-11-02T11:55:38Z") > 0


class TestDecodeTps:
    def test_normal(self):
        assert decode_tps(100, 2000, 500) == 100 / 1.5

    def test_below_decode_window(self):
        assert decode_tps(100, 400, 200) is None  # decode_ms < 300

    def test_below_min_tokens(self):
        assert decode_tps(10, 2000, 500) is None

    def test_missing(self):
        assert decode_tps(None, 2000, 500) is None
        assert decode_tps(100, None, 500) is None


class TestE2eTps:
    def test_normal(self):
        assert e2e_tps(100, 2000) == 50

    def test_missing(self):
        assert e2e_tps(None, 2000) is None
        assert e2e_tps(100, 0) is None


class TestPct:
    def test_empty(self):
        assert pct([], 0.5) is None

    def test_single(self):
        assert pct([5], 0.5) == 5

    def test_median(self):
        assert pct([1, 2, 3], 0.5) == 2

    def test_ignores_none(self):
        assert pct([1, None, 3], 0.5) == 2


class TestTokenParts:
    def test_normal(self):
        total, new, read, write = token_parts(
            {"input_tokens": 1000, "cache_read_tokens": 400, "cache_write_tokens": 100})
        assert (total, new, read, write) == (1000, 500, 400, 100)

    def test_cache_write_outside_input(self):
        # Claude-style: input = new + read, cache_write reported separately.
        total, new, read, write = token_parts(
            {"input_tokens": 500, "cache_read_tokens": 400, "cache_write_tokens": 200})
        assert total == 500 + 200 == 700
        assert new == 100

    def test_none(self):
        assert token_parts({"input_tokens": None}) == (0, 0, 0, 0)


class TestRecordToRow:
    def test_basic(self):
        r = row()
        assert r["id"] == "exec-1"
        assert r["input_tokens"] == 1000
        assert r["cache_read_tokens"] == 400
        assert r["cache_write_tokens"] == 100
        assert r["failed"] == 0
        assert '"api_key"' not in r["raw"]

    def test_sensitive_keys_stripped(self):
        r = row(api_key="secret", response_headers={"x": 1})
        assert "secret" not in r["raw"]

    def test_fallback_id(self):
        r = record_to_row(rec(execution_id=None, request_id="req-9"))
        # id = sha1 digest
        assert len(r[0]) == 40

    def test_cache_fallbacks(self):
        # cache_read_tokens missing -> use cached_tokens; cache_creation missing ->
        # use token_breakdown.input.cache_write_tokens.
        r = row(tokens={"input_tokens": 100, "output_tokens": 10, "cached_tokens": 30},
                token_breakdown={"input": {"cache_write_tokens": 7}})
        assert r["cache_read_tokens"] == 30
        assert r["cache_write_tokens"] == 7

    def test_v3_fields(self):
        r = row(executor_type="DevinExecutor", auth_type="oauth",
                access_provider="config-inline", is_native_key=True,
                trace_id="tr-1", parent_session_id="ps-1", node_kind="main",
                is_fork=True, is_compaction=False, client_ip="10.0.0.1",
                resolved_client_ip="10.0.0.2", x_forwarded_for="1.2.3.4",
                effective_service_tier="auto", response_service_tier="priority",
                upstream_service_tier="auto", speed="fast", inference_geo="us",
                generate=True,
                tokens={"input_tokens": 100, "output_tokens": 10,
                        "total_tokens": 110},
                fail={"status_code": 429, "code": "rate_limit"})
        assert r["executor_type"] == "DevinExecutor"
        assert r["auth_type"] == "oauth"
        assert r["access_provider"] == "config-inline"
        assert r["is_native_key"] == 1
        assert r["trace_id"] == "tr-1"
        assert r["parent_session_id"] == "ps-1"
        assert r["node_kind"] == "main"
        assert r["is_fork"] == 1
        assert r["is_compaction"] == 0
        assert r["client_ip"] == "10.0.0.1"
        assert r["x_forwarded_for"] == "1.2.3.4"
        assert r["effective_service_tier"] == "auto"
        assert r["response_service_tier"] == "priority"
        assert r["upstream_service_tier"] == "auto"
        assert r["speed"] == "fast"
        assert r["inference_geo"] == "us"
        assert r["fail_code"] == "rate_limit"
        assert r["total_tokens"] == 110
        assert r["generate"] == 1

    def test_v3_fields_absent(self):
        r = row()
        assert r["executor_type"] is None
        assert r["is_native_key"] is None
        assert r["generate"] is None
        assert r["total_tokens"] is None  # not summed when upstream omits it


class TestTokenQuality:
    def test_from_breakdown(self):
        assert token_quality({"token_breakdown": {"quality": "complete"}}) == "complete"
        assert token_quality({"token_breakdown": {"quality": "inconsistent"}}) == "inconsistent"

    def test_missing(self):
        assert token_quality({}) == ""
        assert token_quality({"token_breakdown": None}) == ""

    def test_stored_on_row(self):
        assert row(token_breakdown={"quality": "inconsistent"})["token_quality"] == "inconsistent"
        assert row()["token_quality"] is None


class TestPricing:
    def test_disabled(self):
        assert not Pricing().enabled
        assert parse_pricing(None).enabled is False
        assert parse_pricing("").enabled is False

    def test_parse(self):
        p = parse_pricing("2,0.2,8")
        assert p.enabled
        # 1M new input + 1M cached read + 1M output = 2 + 0.2 + 8 = 10.2
        assert abs(p.cost(1e6, 1e6, 1e6) - 10.2) < 1e-9

    def test_parse_spaces(self):
        assert parse_pricing(" 2 , , 8 ").input_per_m == 2

    def test_parse_invalid(self):
        import pytest

        with pytest.raises(ValueError):
            parse_pricing("1,2")
        with pytest.raises(ValueError):
            parse_pricing("a,b,c")


class TestParsePct:
    def test_values(self):
        assert parse_pct("81%") == 81
        assert parse_pct("0%") == 0
        assert parse_pct("42") == 42
        assert parse_pct(7) == 7
        assert parse_pct(None) is None
        assert parse_pct("abc") is None
        assert parse_pct("") is None


class TestParseIsoTs:
    def test_ok(self):
        assert parse_iso_ts("2026-11-08T08:00:00Z") > 0
        assert parse_iso_ts("2026-11-05T15:22:56.911129228+08:00") > 0

    def test_bad(self):
        assert parse_iso_ts("") is None
        assert parse_iso_ts(None) is None
        assert parse_iso_ts("not a date") is None


def cred_fixture(**kw):
    base = {
        "id": "devin-a.json", "name": "devin-a.json", "provider": "devin",
        "label": "a@example.com", "status": "active", "disabled": False,
        "unavailable": False, "success": 100, "failed": 2,
        "email": "a@example.com",
        "quota": {"observed_at": "2026-11-06T14:23:38+08:00",
                  "signals": {
                      "plan": "Max",
                      "daily_quota_remaining_percent": "100%",
                      "weekly_quota_remaining_percent": "81%",
                      "daily_quota_reset_at": "2026-11-06T08:00:00Z",
                      "weekly_quota_reset_at": "2026-11-08T08:00:00Z",
                      "plan_end": "2026-12-03T03:27:33Z",
                      "x-codex-plan-type": "max",
                  }},
    }
    base.update(kw)
    return base


class TestParseCredentials:
    def test_devin_fields(self):
        creds = parse_credentials({"files": [cred_fixture()]})
        assert len(creds) == 1
        c = creds[0]
        assert c.account == "a@example.com"
        assert c.provider == "devin"
        assert c.status == "active"
        assert not c.blocked
        assert c.ok == 100 and c.fail == 2
        assert c.plan == "Max"
        assert c.daily_pct == 100 and c.weekly_pct == 81
        assert c.daily_reset and c.weekly_reset and c.plan_end
        # non-Devin signals land in `extra`
        assert c.extra == {"x-codex-plan-type": "max"}

    def test_blocked_and_disabled(self):
        c = parse_credentials({"files": [
            cred_fixture(unavailable=True, quota_blocked=True, status="error"),
            cred_fixture(id="b", name="b", disabled=True, email="", label=""),
        ]})
        assert c[0].blocked and c[0].status == "error"
        assert c[1].status == "disabled"
        assert c[1].account == "b"  # falls back to name when email is empty

    def test_empty_and_list(self):
        assert parse_credentials(None) == []
        assert parse_credentials({}) == []
        assert parse_credentials({"files": "nope"}) == []
        assert len(parse_credentials([cred_fixture()])) == 1


def codex_fixture(**kw):
    base = {
        "id": "codex-a.json", "name": "codex-a.json", "provider": "codex",
        "label": "c@example.com", "status": "active", "email": "c@example.com",
        "quota": {"observed_at": "2026-11-06T14:23:38+08:00",
                  "signals": {
                      "X-Codex-Plan-Type": "pro",
                      "X-Codex-Primary-Used-Percent": "42",
                      "X-Codex-Primary-Window-Minutes": "300",
                      "X-Codex-Primary-Reset-After-Seconds": "3600",
                      "X-Codex-Secondary-Used-Percent": "63",
                      "X-Codex-Secondary-Window-Minutes": "10080",
                      "X-Codex-Secondary-Reset-At": "1762531200",
                      "X-Codex-Credits-Has-Credits": "true",
                      "X-Codex-Credits-Balance": "12.5",
                      "X-Codex-Allowed": "true",
                      "X-Codex-Limit-Reached": "false",
                      "X-Codex-Additional-GPT-Spark-Primary-Used-Percent": "3",
                      "X-Codex-Additional-GPT-Spark-Limit-Name": "GPT-5.3-Spark",
                      "X-Codex-Code-Review-Primary-Used-Percent": "17",
                      "Retry-After": "120",
                  }},
    }
    base.update(kw)
    return base


def claude_fixture(**kw):
    base = {
        "id": "claude-a.json", "name": "claude-a.json", "provider": "claude",
        "label": "cl@example.com", "status": "active", "email": "cl@example.com",
        "quota": {"observed_at": "2026-11-06T14:23:38+08:00",
                  "signals": {
                      "Anthropic-Ratelimit-Unified-Status": "allowed",
                      "Anthropic-Ratelimit-Unified-5h-Utilization": "0.42",
                      "Anthropic-Ratelimit-Unified-5h-Reset": "1762531200",
                      "Anthropic-Ratelimit-Unified-5h-Status": "allowed",
                      "Anthropic-Ratelimit-Unified-7d-Utilization": "0.63",
                      "Anthropic-Ratelimit-Unified-7d-Reset": "1763136000",
                  }},
    }
    base.update(kw)
    return base


class TestCodexCredentials:
    def test_normalized_windows(self):
        (c,) = parse_credentials({"files": [codex_fixture()]})
        assert c.provider == "codex"
        assert c.plan == "pro"
        assert c.daily_pct == 58          # 100 - 42 used
        assert c.weekly_pct == 37         # 100 - 63 used
        assert c.daily_label == "5h"      # 300 minutes
        assert c.weekly_label == "7d"     # 10080 minutes
        assert c.weekly_reset == 1762531200
        assert not c.limited
        assert c.retry_after_ts is not None
        # consumed keys don't show up in extra
        assert not any(k.startswith("x-codex-") or k == "retry-after"
                       for k in (c.extra or {}))

    def test_named_limits(self):
        (c,) = parse_credentials({"files": [codex_fixture()]})
        labels = {lim["label"] for lim in c.limits}
        assert "GPT-5.3-Spark" in labels           # limit-name wins over slug
        assert "code review" in labels
        spark = next(l for l in c.limits if l["label"] == "GPT-5.3-Spark")
        assert spark["short"]["pct"] == 97          # 100 - 3 used

    def test_limit_reached(self):
        (c,) = parse_credentials({"files": [codex_fixture(
            quota={"signals": {"X-Codex-Limit-Reached": "true",
                               "X-Codex-Primary-Used-Percent": "100"}})]})
        assert c.limited
        assert c.daily_pct == 0

    def test_credits(self):
        (c,) = parse_credentials({"files": [codex_fixture()]})
        assert c.credits == {"has-credits": "true", "balance": "12.5"}


class TestClaudeCredentials:
    def test_normalized_windows(self):
        (c,) = parse_credentials({"files": [claude_fixture()]})
        assert c.provider == "claude"
        assert c.daily_pct == 58           # (1 - 0.42) * 100
        assert c.weekly_pct == 37          # (1 - 0.63) * 100
        assert c.daily_label == "5h" and c.weekly_label == "7d"
        assert c.daily_reset == 1762531200
        assert c.weekly_reset == 1763136000
        assert not c.limited
        # consumed headers don't leak into extra
        assert not any("ratelimit" in k for k in (c.extra or {}))

    def test_rejected_is_limited(self):
        (c,) = parse_credentials({"files": [claude_fixture(
            quota={"signals": {
                "Anthropic-Ratelimit-Unified-5h-Status": "rejected",
                "Anthropic-Ratelimit-Unified-Status": "rejected",
                "Anthropic-Ratelimit-Unified-5h-Utilization": "1.0"}})]})
        assert c.limited
        assert c.daily_pct == 0


class TestCredSort:
    def test_worst_first(self):
        good = parse_credentials({"files": [cred_fixture()]})[0]
        bad = parse_credentials({"files": [cred_fixture(
            id="b", name="b", unavailable=True)]})[0]
        lim = parse_credentials({"files": [codex_fixture(
            quota={"signals": {"X-Codex-Limit-Reached": "true"}})]})[0]
        from cpa_stats.model import cred_sort_key
        assert sorted([good, bad, lim], key=cred_sort_key)[0] is bad


class TestInstanceTag:
    def test_instance_mangles_name(self):
        f = cred_fixture()
        f["_instance"] = "127.0.0.1:27100"
        (c,) = parse_credentials({"files": [f]})
        assert c.instance == "127.0.0.1:27100"
        assert c.name == "devin-a.json@127.0.0.1:27100"


class TestSignalPctSeries:
    def test_reads_history_signals(self):
        import json, time
        now = time.time()
        rows = [
            {"ts": now - 120, "signals": json.dumps(
                {"X-Codex-Additional-Spark-Primary-Used-Percent": "3"})},
            {"ts": now - 60, "signals": json.dumps(
                {"x-codex-additional-spark-primary-used-percent": "10"})},
            {"ts": now, "signals": "{bad json"},
        ]
        from cpa_stats.model import signal_pct_series
        s = signal_pct_series(
            rows, "x-codex-additional-spark-primary-used-percent")
        assert s == [97, 90]      # remaining = 100 - used, case-insensitive


class TestRecoveryStats:
    def test_median_and_share(self):
        from cpa_stats.model import recovery_stats
        import time
        from datetime import datetime
        now = time.time()

        def mk(dt, failed):
            ts = datetime.fromtimestamp(now + dt).astimezone().isoformat()
            return row(timestamp=ts, failed=failed)

        rows = [mk(0, True), mk(30, False), mk(60, True), mk(400, False)]
        med, share = recovery_stats(rows)
        assert med == 30 or med == 340  # deltas [30, 340], midpoint element
        assert share == 0.5

    def test_none_when_clean(self):
        from cpa_stats.model import recovery_stats
        assert recovery_stats([row(failed=False)]) is None


class TestDetailPairs:
    def test_covers_v3_fields(self):
        r = row(request_id="req-1", endpoint="POST /v1/responses",
                executor_type="DevinExecutor", auth_type="oauth",
                access_provider="config-inline", is_native_key=True,
                trace_id="tr-9", session_id="sess-1", parent_session_id="ps-1",
                node_kind="main", is_fork=True, client_ip="127.0.0.1",
                service_tier="auto", response_service_tier="priority",
                response_model="swe-2-max-lightning",
                source="a@example.com", auth_index="abc",
                user_agent="codex-tui/1.0",
                tokens={"input_tokens": 100, "output_tokens": 10,
                        "total_tokens": 110},
                token_breakdown={"quality": "complete",
                                 "total_tokens": 110,
                                 "unclassified_tokens": 0,
                                 "input": {"total_tokens": 100,
                                          "uncached_tokens": 60,
                                          "cache_read_tokens": 40,
                                          "cache_write_tokens": 0},
                                 "output": {"total_tokens": 10,
                                            "non_reasoning_tokens": 10,
                                            "reasoning_tokens": 0}})
        pairs = dict(request_detail_pairs(r))
        assert "DevinExecutor" in pairs["provider / executor"]
        assert "swe-2-max-lightning" in pairs["model"]
        assert pairs["endpoint"] == "POST /v1/responses"
        assert "auto" in pairs["tier req→eff→resp→up"]
        assert "priority" in pairs["tier req→eff→resp→up"]
        assert "sess-1" in pairs["session"] and "ps-1" in pairs["session"]
        assert "fork" in pairs["node"]
        assert "req-1" in pairs["request / exec / trace"]
        assert "tr-9" in pairs["request / exec / trace"]
        assert "a@example.com" in pairs["account"]
        assert "127.0.0.1" in pairs["client ip / resolved / xff"]
        assert "codex-tui" in pairs["client"]
        assert "native yes" in pairs["access"]
        assert pairs["token quality"] == "complete"
        assert "110" in pairs["total tokens"]

    def test_raw_only_extras(self):
        r = row(iterations=[{"type": "model", "model": "swe-2",
                             "input_tokens": 10, "output_tokens": 5}],
                tool_usage_details={"web_search_calls": 2, "mcp_calls": 1},
                cost_details={"cost": 0.0123})
        pairs = dict(request_detail_pairs(r))
        assert "swe-2" in pairs["iterations [1]"]
        assert "web_search_calls 2" in pairs["server tools"]
        assert "$0.012" in pairs["upstream cost"]


def hist(points, key="weekly_pct"):
    """history rows from (hour_offset, pct) pairs, oldest first."""
    import time
    now = time.time()
    return [{"ts": now - off * 3600, key: p} for off, p in reversed(points)]


class TestQuotaForecast:
    def test_decline(self):
        # 81% -> 71% over 2h => ~5%/h -> eta ~14h out
        import time
        rows = hist([(2.0, 81), (1.0, 76), (0.0, 71)])
        fc = quota_forecast(rows, "weekly_pct")
        assert fc is not None
        cur, per_day, eta = fc
        assert cur == 71
        assert abs(per_day - 5 * 24) < 0.5
        assert eta > time.time()

    def test_reset_boundary(self):
        # 100 -> 80 -> 0 (blocked) -> 100 (reset) -> 90: slope uses post-reset run only.
        rows = hist([(4, 80), (3, 0), (2, 100), (1, 95), (0, 90)])
        fc = quota_forecast(rows, "weekly_pct")
        _, per_day, eta = fc
        assert abs(per_day - 120) < 0.5  # 5% per hour

    def test_watermark_correction_cut(self):
        # A stale observation corrected 81% -> 73% in one poll must not poison
        # the slope: only the post-correction run (73 -> 71 over 1h) is fitted.
        rows = hist([(2.0, 81), (1.98, 73), (1.0, 72), (0.0, 71)])
        fc = quota_forecast(rows, "weekly_pct")
        assert fc is not None
        cur, per_day, eta = fc
        assert cur == 71
        assert per_day < 60        # ~2%/h, not the bogus ~500%/d jump fit

    def test_flat_or_insufficient(self):
        assert quota_forecast(hist([(1, 50), (0, 50)]), "weekly_pct") is None
        assert quota_forecast(hist([(0, 50)]), "weekly_pct") is None
        assert quota_forecast([], "weekly_pct") is None

    def test_series(self):
        assert quota_pct_series(hist([(2, 90), (0, 80)]), "weekly_pct") == [90, 80]
        assert quota_pct_series(hist([(2, None), (0, 80)]), "weekly_pct") == [80]
