// Core tests mirroring the Python suite (test_model / test_storage subset).
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"path/filepath"
	"testing"
	"time"
)

func rec(i int, kw map[string]interface{}) map[string]interface{} {
	base := map[string]interface{}{
		"timestamp":    "2026-11-02T19:55:38.436314676+08:00",
		"execution_id": fmt.Sprintf("exec-%d", i),
		"model":        "devin/swe-2",
		"alias":        "swe-2",
		"latency_ms":   2000,
		"ttft_ms":      500,
		"failed":       false,
		"tokens": map[string]interface{}{
			"input_tokens":           1000,
			"output_tokens":          100,
			"cache_read_tokens":      400,
			"cache_creation_tokens":  100,
		},
		"token_breakdown": map[string]interface{}{"quality": "complete"},
	}
	for k, v := range kw {
		base[k] = v
	}
	return base
}

func itoa(i int) string { return fmt.Sprintf("exec-%d", i) }

func rowMap(r map[string]interface{}) map[string]interface{} {
	row := recordToRow(r)
	out := map[string]interface{}{}
	for i, c := range Columns {
		out[c] = row[i]
	}
	return out
}

func TestParseTS(t *testing.T) {
	ts, err := parseTS("2026-11-02T19:55:38.436314676+08:00")
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(ts-1793620538.436314) > 0.001 {
		t.Fatalf("ts %v", ts)
	}
	if ts2, err := parseTS("2026-11-02T11:55:38Z"); err != nil || ts2 <= 0 {
		t.Fatalf("z suffix: %v %v", ts2, err)
	}
}

func TestDecodeTPS(t *testing.T) {
	if got := decodeTPS(100.0, 2000.0, 500.0); got == nil ||
		math.Abs(*got-100/1.5) > 1e-9 {
		t.Fatalf("normal: %v", got)
	}
	if decodeTPS(100.0, 400.0, 200.0) != nil {
		t.Fatal("decode_ms < 300 should be nil")
	}
	if decodeTPS(10.0, 2000.0, 500.0) != nil {
		t.Fatal("below min tokens should be nil")
	}
	if decodeTPS(nil, 2000.0, 500.0) != nil ||
		decodeTPS(100.0, nil, 500.0) != nil {
		t.Fatal("missing -> nil")
	}
}

func TestE2ETPS(t *testing.T) {
	if got := e2eTPS(100.0, 2000.0); got == nil || *got != 50 {
		t.Fatalf("%v", got)
	}
	if e2eTPS(nil, 2000.0) != nil || e2eTPS(100.0, 0.0) != nil {
		t.Fatal("missing/zero -> nil")
	}
}

func TestPctile(t *testing.T) {
	if pctile(nil, 0.5) != nil {
		t.Fatal("empty -> nil")
	}
	got := pctile([]float64{1, 2, 3, 4}, 0.5)
	if got == nil || *got != 2.5 {
		t.Fatalf("p50: %v", got)
	}
}

func TestRecordToRow(t *testing.T) {
	r := rowMap(rec(1, nil))
	if strv(r["id"]) != "exec-1" {
		t.Fatalf("id %v", r["id"])
	}
	if strv(r["model"]) != "devin/swe-2" {
		t.Fatalf("model %v", r["model"])
	}
	if derefI(i64(r["stream"])) != 0 {
		t.Fatal("stream")
	}
	if derefI(i64(r["failed"])) != 0 {
		t.Fatal("failed")
	}
	if f, ok := parseFloat(r["latency_ms"]); !ok || f != 2000 {
		t.Fatalf("latency_ms %v", r["latency_ms"])
	}
	if f, ok := parseFloat(r["ttft_ms"]); !ok || f != 500 {
		t.Fatalf("ttft_ms %v", r["ttft_ms"])
	}
	if r["latency_ms"] == nil || r["ttft_ms"] == nil {
		t.Fatal("latency/ttft nil")
	}
	if strv(r["token_quality"]) != "complete" {
		t.Fatalf("quality %v", r["token_quality"])
	}
}

func TestSensitiveKeysDropped(t *testing.T) {
	r := rowMap(rec(1, map[string]interface{}{"api_key": "sk-SECRET"}))
	var m map[string]interface{}
	if err := json.Unmarshal([]byte(strv(r["raw"])), &m); err != nil {
		t.Fatal(err)
	}
	if _, found := m["api_key"]; found {
		t.Fatal("api_key leaked into raw")
	}
}

func TestSha1FallbackID(t *testing.T) {
	kw := map[string]interface{}{"execution_id": nil, "request_id": "req-9"}
	r := rec(1, kw)
	delete(r, "execution_id")
	row := rowMap(r)
	if len(strv(row["id"])) != 40 {
		t.Fatalf("digest id %q", row["id"])
	}
}

func TestBadTimestampNow(t *testing.T) {
	r := rec(1, map[string]interface{}{"timestamp": "garbage"})
	row := rowMap(r)
	ts, ok := parseFloat(row["ts"])
	if !ok || ts <= 0 {
		t.Fatalf("ts %v", row["ts"])
	}
}

func TestTtftZeroToNull(t *testing.T) {
	row := rowMap(rec(1, map[string]interface{}{"ttft_ms": 0}))
	if row["ttft_ms"] != nil {
		t.Fatalf("ttft_ms 0 should be NULL, got %v", row["ttft_ms"])
	}
	row = rowMap(rec(1, map[string]interface{}{"ttft_ms": nil}))
	if row["ttft_ms"] != nil {
		t.Fatal("ttft_ms nil -> NULL")
	}
}

func TestTokenQualityEmptyToNull(t *testing.T) {
	r := rec(1, nil)
	r["token_breakdown"] = map[string]interface{}{"quality": ""}
	row := rowMap(r)
	if row["token_quality"] != nil {
		t.Fatalf("empty quality -> NULL, got %v", row["token_quality"])
	}
}

func TestTokenParts(t *testing.T) {
	r := Row{"input_tokens": int64(1000), "cache_read_tokens": int64(400),
		"cache_write_tokens": int64(100)}
	total, uncached, read, write := tokenParts(r)
	// total = uncached+read+write = 500+400+100 = 1000
	if total != 1000 || uncached != 500 || read != 400 || write != 100 {
		t.Fatalf("%v %v %v %v", total, uncached, read, write)
	}
	// claude-shaped: write outside input → uncached = input - read = 200,
	// total = 200+300+400 = 900
	r = Row{"input_tokens": int64(500), "cache_read_tokens": int64(300),
		"cache_write_tokens": int64(400)}
	total, uncached, read, write = tokenParts(r)
	if uncached != 200 || total != 900 {
		t.Fatalf("%v %v", uncached, total)
	}
}

func credFixture(kw map[string]interface{}) map[string]interface{} {
	base := map[string]interface{}{
		"id": "devin-a.json", "name": "devin-a.json", "provider": "devin",
		"label": "a@example.com", "status": "active", "email": "a@example.com",
		"quota": map[string]interface{}{
			"observed_at": "2026-11-06T14:23:38+08:00",
			"signals": map[string]interface{}{
				"plan":                             "pro",
				"daily_quota_remaining_percent":    "42",
				"weekly_quota_remaining_percent":   "63",
				"daily_quota_reset_at":             "2026-11-08T00:00:00+08:00",
				"weekly_quota_reset_at":            "2026-11-14T00:00:00+08:00",
			},
		},
	}
	for k, v := range kw {
		base[k] = v
	}
	return base
}

func codexFixture(kw map[string]interface{}) map[string]interface{} {
	base := map[string]interface{}{
		"id": "codex-a.json", "name": "codex-a.json", "provider": "codex",
		"label": "c@example.com", "status": "active", "email": "c@example.com",
		"quota": map[string]interface{}{
			"observed_at": "2026-11-06T14:23:38+08:00",
			"signals": map[string]interface{}{
				"X-Codex-Plan-Type":                            "pro",
				"X-Codex-Primary-Used-Percent":                 "42",
				"X-Codex-Primary-Window-Minutes":               "300",
				"X-Codex-Primary-Reset-After-Seconds":          "3600",
				"X-Codex-Secondary-Used-Percent":               "63",
				"X-Codex-Secondary-Window-Minutes":             "10080",
				"X-Codex-Secondary-Reset-At":                   "1762531200",
				"X-Codex-Credits-Has-Credits":                  "true",
				"X-Codex-Credits-Balance":                      "12.5",
				"X-Codex-Allowed":                              "true",
				"X-Codex-Limit-Reached":                        "false",
				"X-Codex-Additional-GPT-Spark-Primary-Used-Percent": "3",
				"X-Codex-Additional-GPT-Spark-Limit-Name":       "GPT-5.3-Spark",
				"X-Codex-Code-Review-Primary-Used-Percent":      "17",
				"Retry-After":                                  "120",
			},
		},
	}
	for k, v := range kw {
		base[k] = v
	}
	return base
}

func claudeFixture(kw map[string]interface{}) map[string]interface{} {
	base := map[string]interface{}{
		"id": "claude-a.json", "name": "claude-a.json", "provider": "claude",
		"label": "b@example.com", "status": "active", "email": "b@example.com",
		"quota": map[string]interface{}{
			"observed_at": "2026-11-06T14:23:38+08:00",
			"signals": map[string]interface{}{
				"Anthropic-Ratelimit-Unified-5h-Utilization": "0.42",
				"Anthropic-Ratelimit-Unified-7d-Utilization": "0.63",
				"Anthropic-Ratelimit-Unified-5h-Reset":       "1762531200",
				"Anthropic-Ratelimit-Unified-7d-Reset":       "1763136000",
			},
		},
	}
	for k, v := range kw {
		base[k] = v
	}
	return base
}

func TestCodexNormalized(t *testing.T) {
	creds := ParseCredentials(map[string]interface{}{
		"files": []interface{}{codexFixture(nil)}})
	if len(creds) != 1 {
		t.Fatal("one cred")
	}
	c := creds[0]
	if c.Provider != "codex" || c.Plan != "pro" {
		t.Fatalf("%v %v", c.Provider, c.Plan)
	}
	if c.DailyPct == nil || *c.DailyPct != 58 {
		t.Fatalf("daily %v", c.DailyPct)
	}
	if c.WeeklyPct == nil || *c.WeeklyPct != 37 {
		t.Fatalf("weekly %v", c.WeeklyPct)
	}
	if c.DailyLabel != "5h" || c.WeeklyLabel != "7d" {
		t.Fatalf("labels %v %v", c.DailyLabel, c.WeeklyLabel)
	}
	if c.WeeklyReset == nil || *c.WeeklyReset != 1762531200 {
		t.Fatalf("weekly_reset %v", c.WeeklyReset)
	}
	if c.Limited {
		t.Fatal("not limited")
	}
	if c.RetryAfterTS == nil {
		t.Fatal("retry_after")
	}
	for k := range c.Extra {
		if len(k) >= 7 && k[:7] == "x-codex" || k == "retry-after" {
			t.Fatalf("consumed key %q leaked into extra", k)
		}
	}
}

func TestCodexNamedLimits(t *testing.T) {
	c := ParseCredentials(map[string]interface{}{
		"files": []interface{}{codexFixture(nil)}})[0]
	labels := map[string]bool{}
	var spark map[string]interface{}
	for _, lim := range c.Limits {
		labels[strv(lim["label"])] = true
		if strv(lim["label"]) == "GPT-5.3-Spark" {
			spark = lim
		}
	}
	if !labels["GPT-5.3-Spark"] {
		t.Fatalf("labels %v", labels)
	}
	if !labels["code review"] {
		t.Fatalf("labels %v", labels)
	}
	short, _ := spark["short"].(map[string]interface{})
	if pctPtr, ok := short["pct"].(*int64); !ok || *pctPtr != 97 {
		t.Fatalf("spark pct %v", short["pct"])
	}
	if c.Credits["has-credits"] != "true" || c.Credits["balance"] != "12.5" {
		t.Fatalf("credits %v", c.Credits)
	}
}

func TestCodexLimited(t *testing.T) {
	c := ParseCredentials(map[string]interface{}{
		"files": []interface{}{codexFixture(map[string]interface{}{
			"quota": map[string]interface{}{"signals": map[string]interface{}{
				"X-Codex-Limit-Reached":           "true",
				"X-Codex-Primary-Used-Percent":    "100",
			}},
		})}})[0]
	if !c.Limited {
		t.Fatal("should be limited")
	}
	if c.DailyPct == nil || *c.DailyPct != 0 {
		t.Fatalf("daily %v", c.DailyPct)
	}
}

func TestClaudeNormalized(t *testing.T) {
	c := ParseCredentials(map[string]interface{}{
		"files": []interface{}{claudeFixture(nil)}})[0]
	if c.Provider != "claude" {
		t.Fatal("provider")
	}
	if c.DailyPct == nil || *c.DailyPct != 58 {
		t.Fatalf("daily %v", c.DailyPct)
	}
	if c.WeeklyPct == nil || *c.WeeklyPct != 37 {
		t.Fatalf("weekly %v", c.WeeklyPct)
	}
	if c.DailyLabel != "5h" || c.WeeklyLabel != "7d" {
		t.Fatal("labels")
	}
	if c.DailyReset == nil || *c.DailyReset != 1762531200 {
		t.Fatalf("daily_reset %v", c.DailyReset)
	}
	for k := range c.Extra {
		if len(k) >= 9 && k[:9] == "anthropic" {
			t.Fatalf("consumed key %q leaked", k)
		}
	}
}

func TestClaudeRejectedLimited(t *testing.T) {
	c := ParseCredentials(map[string]interface{}{
		"files": []interface{}{claudeFixture(map[string]interface{}{
			"quota": map[string]interface{}{"signals": map[string]interface{}{
				"Anthropic-Ratelimit-Unified-5h-Status":      "rejected",
				"Anthropic-Ratelimit-Unified-Status":          "rejected",
				"Anthropic-Ratelimit-Unified-5h-Utilization":  "1.0",
			}},
		})}})[0]
	if !c.Limited {
		t.Fatal("should be limited")
	}
	if c.DailyPct == nil || *c.DailyPct != 0 {
		t.Fatalf("daily %v", c.DailyPct)
	}
}

func TestCredSortWorstFirst(t *testing.T) {
	good := ParseCredentials(map[string]interface{}{
		"files": []interface{}{credFixture(nil)}})[0]
	bad := ParseCredentials(map[string]interface{}{
		"files": []interface{}{credFixture(map[string]interface{}{
			"id": "b", "name": "b", "unavailable": true})}})[0]
	lim := ParseCredentials(map[string]interface{}{
		"files": []interface{}{codexFixture(map[string]interface{}{
			"quota": map[string]interface{}{"signals": map[string]interface{}{
				"X-Codex-Limit-Reached": "true"}},
		})}})[0]
	arr := []CredInfo{good, bad, lim}
	sortByKey := func(i, j int) bool {
		bi, bj := CredSortKey(&arr[i]), CredSortKey(&arr[j])
		return credLess(bi, bj)
	}
	for i := 0; i < len(arr); i++ {
		for j := i + 1; j < len(arr); j++ {
			if sortByKey(j, i) {
				arr[i], arr[j] = arr[j], arr[i]
			}
		}
	}
	if !arr[0].Blocked {
		t.Fatalf("worst first: %v", arr[0].Name)
	}
}

func TestInstanceManglesName(t *testing.T) {
	f := credFixture(nil)
	f["_instance"] = "127.0.0.1:27100"
	c := ParseCredentials(map[string]interface{}{
		"files": []interface{}{f}})[0]
	if c.Instance != "127.0.0.1:27100" {
		t.Fatal("instance")
	}
	if c.Name != "devin-a.json@127.0.0.1:27100" {
		t.Fatalf("name %v", c.Name)
	}
}

func TestParseCredentialsEmpty(t *testing.T) {
	if ParseCredentials(nil) != nil {
		t.Fatal("nil -> nil")
	}
	if ParseCredentials(map[string]interface{}{}) != nil {
		t.Fatal("{} -> nil")
	}
	if ParseCredentials(map[string]interface{}{"files": "nope"}) != nil {
		t.Fatal("files=nope -> nil")
	}
}

// ---------------------------------------------------------------- forecast

func histRow(ts, pct float64) QuotaHistRow {
	p := int64(pct)
	return QuotaHistRow{TS: ts, WeeklyPct: &p}
}

func TestQuotaForecastSteady(t *testing.T) {
	rows := []QuotaHistRow{histRow(1000, 90), histRow(2000, 89),
		histRow(3000, 88), histRow(4600, 85)}
	fc := QuotaForecast(rows, "weekly_pct", 1800, 0.1)
	if fc == nil {
		t.Fatal("should forecast")
	}
	if fc[1] <= 0 {
		t.Fatal("positive drop rate")
	}
	if fc[2] <= 4600 {
		t.Fatal("eta in future")
	}
}

func TestQuotaForecastRisingReset(t *testing.T) {
	// rising value = window reset -> no forecast from before it
	rows := []QuotaHistRow{histRow(1000, 30), histRow(2000, 29),
		histRow(3000, 90), histRow(4600, 88), histRow(5200, 87),
		histRow(5800, 86)}
	fc := QuotaForecast(rows, "weekly_pct", 1000, 0.1)
	if fc == nil {
		t.Fatal("forecast")
	}
	if fc[0] != 86 {
		t.Fatalf("current pct %v", fc[0])
	}
}

func TestQuotaForecastThin(t *testing.T) {
	if QuotaForecast([]QuotaHistRow{histRow(1, 90), histRow(2, 89)},
		"weekly_pct", 1800, 0.1) != nil {
		t.Fatal("too thin")
	}
}

// ---------------------------------------------------------------- storage

func tempDB(t *testing.T) (*sql.DB, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "requests.db")
	db, err := OpenDB(path)
	if err != nil {
		t.Fatal(err)
	}
	return db, path
}

func TestSchemaVersion(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	if metaGet(db, "schema_version") != "4" {
		t.Fatalf("version %v", metaGet(db, "schema_version"))
	}
}

func TestInsertDedup(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	recs := []map[string]interface{}{rec(1, nil), rec(2, nil)}
	n, err := InsertRecords(db, recs)
	if err != nil || n != 2 {
		t.Fatalf("%v %v", n, err)
	}
	// same again -> ignored
	n, err = InsertRecords(db, recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("dup insert n=%v", n)
	}
	if got := len(LoadRows(db, 0, "", "", 0, "")); got != 2 {
		t.Fatalf("stored rows %v", got)
	}
}

func TestLoadRowsFilters(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	a := rec(1, map[string]interface{}{"model": "m-1", "session_id": "sess-a"})
	b := rec(2, map[string]interface{}{"model": "m-2", "session_id": "sess-b"})
	if _, err := InsertRecords(db, []map[string]interface{}{a, b}); err != nil {
		t.Fatal(err)
	}
	rows := LoadRows(db, 0, "m-1", "", 0, "")
	if len(rows) != 1 || strv(rows[0]["model"]) != "m-1" {
		t.Fatalf("model filter: %v", rows)
	}
	rows = LoadRows(db, 0, "", "sess-b", 0, "")
	if len(rows) != 1 {
		t.Fatalf("session filter: %v", rows)
	}
	rows = LoadRows(db, 0, "", "", 0, "acct")
	_ = rows
}

func TestQuotaHistoryRoundTrip(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	creds := ParseCredentials(map[string]interface{}{
		"files": []interface{}{credFixture(nil)}})
	tx, _ := db.Begin()
	saveQuotaHistory(tx, creds, 1000.0)
	tx.Commit()
	rows := loadQuotaHistory(db, 0, "")
	if len(rows) != 1 {
		t.Fatalf("%v rows", len(rows))
	}
	if rows[0].Name != "devin-a.json" {
		t.Fatalf("name %v", rows[0].Name)
	}
	if rows[0].DailyPct == nil || *rows[0].DailyPct != 42 {
		t.Fatalf("daily %v", rows[0].DailyPct)
	}
	// per-name filter
	if loadQuotaHistory(db, 0, "other") != nil {
		t.Fatal("name filter")
	}
}

func TestQuotaSnapshotRoundTrip(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	snap := map[string]interface{}{
		"fetched_at": 1234.5,
		"files":      []interface{}{credFixture(nil)},
	}
	saveQuotaSnapshot(db, snap)
	got := loadQuotaSnapshot(db)
	if got == nil {
		t.Fatal("snapshot")
	}
	if v, _ := parseFloat(got["fetched_at"]); v != 1234.5 {
		t.Fatalf("fetched_at %v", got["fetched_at"])
	}
	if files, ok := got["files"].([]interface{}); !ok || len(files) != 1 {
		t.Fatalf("files %v", got["files"])
	}
}

func TestMetaRoundTrip(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	metaSet(db, "k1", "v1")
	metaSet(db, "k1", "v2") // upsert
	if metaGet(db, "k1") != "v2" {
		t.Fatal("upsert")
	}
	metaSet(db, "ts", 1794029291.02019)
	if metaGet(db, "ts") != "1794029291.02019" {
		t.Fatalf("float meta %q", metaGet(db, "ts"))
	}
}

func TestPrune(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	old := rec(1, map[string]interface{}{
		"timestamp": "2020-01-01T00:00:00+08:00"})
	fresh := rec(2, map[string]interface{}{
		"timestamp": time.Now().UTC().Format("2006-01-02T15:04:05Z")})
	if _, err := InsertRecords(db,
		[]map[string]interface{}{old, fresh}); err != nil {
		t.Fatal(err)
	}
	n, err := Prune(db, 86400)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("pruned %v", n)
	}
	if len(LoadRows(db, 0, "", "", 0, "")) != 1 {
		t.Fatal("fresh survived")
	}
}

func TestDBTotals(t *testing.T) {
	db, _ := tempDB(t)
	defer db.Close()
	if _, err := InsertRecords(db,
		[]map[string]interface{}{rec(1, nil), rec(2, nil)}); err != nil {
		t.Fatal(err)
	}
	a := DBTotals(db, nil)
	// prompt_total = max(input, read+write) = max(1000, 500) = 1000 per row
	if a.N != 2 || a.InTok != 2000 || a.OutTok != 200 {
		t.Fatalf("totals n=%v in=%v out=%v", a.N, a.InTok, a.OutTok)
	}
	if a.CacheRatio == nil || math.Abs(*a.CacheRatio-800.0/2000) > 1e-6 {
		t.Fatalf("cache_ratio %v", a.CacheRatio)
	}
}

// ------------------------------------------------------------------- misc

func TestParseDuration(t *testing.T) {
	for in, want := range map[string]float64{
		"90s": 90, "15m": 900, "2h": 7200, "7d": 604800, "1w": 604800,
	} {
		if got, err := parseDuration(in); err != nil || got != want {
			t.Fatalf("%q -> %v %v", in, got, err)
		}
	}
	if _, err := parseDuration("bogus"); err == nil {
		t.Fatal("bad duration should error")
	}
}

func TestParseVersion(t *testing.T) {
	v, ok := parseVersion("v2.3.0")
	if !ok || v != [3]int{2, 3, 0} {
		t.Fatalf("%v %v", v, ok)
	}
	if _, ok := parseVersion("nope"); ok {
		t.Fatal("unparseable")
	}
	if !versionLess([3]int{2, 3, 0}, [3]int{2, 3, 1}) {
		t.Fatal("2.3.0 < 2.3.1")
	}
}

func TestParsePricing(t *testing.T) {
	p, err := ParsePricing("2,0.2,8")
	if err != nil || p == nil || !p.Enabled() {
		t.Fatalf("%v %v", p, err)
	}
	if p.Cost(1000, 500, 200) != (1000*2+500*0.2+200*8)/1e6 {
		t.Fatal("cost")
	}
	p, err = ParsePricing("")
	if err != nil || p.Enabled() {
		t.Fatal("empty disabled")
	}
	if _, err = ParsePricing("x,y"); err == nil {
		t.Fatal("bad price")
	}
}

func TestStaleQuotaCredentials(t *testing.T) {
	now := nowSecs()
	// stale: observed far in the past; fresh: within max_age
	freshTS := time.Unix(int64(now)-10, 0).Format("2006-01-02T15:04:05-07:00")
	snap := map[string]interface{}{
		"files": []interface{}{
			map[string]interface{}{
				"name": "stale-a", "provider": "codex",
				"quota": map[string]interface{}{
					"observed_at": "2020-01-01T00:00:00+08:00"}},
			map[string]interface{}{
				"name": "fresh-b", "provider": "codex",
				"quota": map[string]interface{}{
					"observed_at": freshTS}},
			map[string]interface{}{
				"name": "non-quota", "provider": "gemini",
				"quota": map[string]interface{}{
					"observed_at": "2020-01-01T00:00:00+08:00"}},
		},
	}
	stale := staleQuotaCredentials(snap, 300, now)
	if len(stale) != 1 || stale[0] != "stale-a" {
		t.Fatalf("stale %v", stale)
	}
}

func TestWindowLabel(t *testing.T) {
	if windowLabel(86400) != "1d" || windowLabel(3600) != "1h" ||
		windowLabel(600) != "10m" || windowLabel(90) != "90s" {
		t.Fatal("windowLabel")
	}
}

func TestInstanceTag(t *testing.T) {
	if instanceTag("http://127.0.0.1:27100") != "127.0.0.1:27100" {
		t.Fatal("instanceTag")
	}
}

func TestFmtTokUnits(t *testing.T) {
	// token counts use K / M / B — B (billion) for >= 1e9 (#3)
	cases := map[float64]string{
		999:         "999",
		1500:        "1.5K",
		23345:       "23K",
		163250888:   "163.25M",
		999999999:   "1000.00M",
		1000000000:  "1.00B",
		6860000000:  "6.86B",
		16325088888: "16.33B",
	}
	for in, want := range cases {
		if got := fmtTok(in); got != want {
			t.Fatalf("fmtTok(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestQuotaBar(t *testing.T) {
	if quotaBar(nil) != "—" {
		t.Fatal("nil -> —")
	}
	cases := map[int64]string{
		100: "██████████ 100%",
		62:  "██████░░░░ 62%",
		20:  "██░░░░░░░░ 20%",
		0:   "░░░░░░░░░░ 0%",
	}
	for in, want := range cases {
		if got := quotaBar(i64(in)); got != want {
			t.Fatalf("quotaBar(%d) = %q, want %q", in, got, want)
		}
	}
}
