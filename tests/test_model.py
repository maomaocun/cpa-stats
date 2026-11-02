"""Unit tests for pure metric/parsing functions."""

from cpa_stats.model import (MIN_DECODE_MS, Pricing, decode_tps, e2e_tps,
                             parse_pricing, parse_ts, pct, record_to_row,
                             token_parts, token_quality)


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
        ["id", "ts", "model", "alias", "response_model", "provider", "effort",
         "service_tier", "endpoint", "source", "auth_index", "session_id",
         "request_id", "user_agent", "stream", "failed", "status_code", "error",
         "latency_ms", "ttft_ms", "input_tokens", "output_tokens",
         "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
         "token_quality", "raw"],
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
