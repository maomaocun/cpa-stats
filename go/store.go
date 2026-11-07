// SQLite storage: schema, migrations, queries (mirrors storage.py).
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const schemaVersion = 4

const schemaSQL = `
CREATE TABLE IF NOT EXISTS requests (
    id                 TEXT PRIMARY KEY,
    ts                 REAL NOT NULL,
    model              TEXT,
    alias              TEXT,
    response_model     TEXT,
    provider           TEXT,
    effort             TEXT,
    service_tier       TEXT,
    endpoint           TEXT,
    source             TEXT,
    auth_index         TEXT,
    session_id         TEXT,
    request_id         TEXT,
    user_agent         TEXT,
    stream             INTEGER,
    failed             INTEGER,
    status_code        INTEGER,
    error              TEXT,
    latency_ms         INTEGER,
    ttft_ms            INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    reasoning_tokens   INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    token_quality      TEXT,
    raw                TEXT,
    executor_type          TEXT,
    auth_type              TEXT,
    access_provider        TEXT,
    is_native_key          INTEGER,
    trace_id               TEXT,
    parent_session_id      TEXT,
    node_kind              TEXT,
    is_fork                INTEGER,
    is_compaction          INTEGER,
    client_ip              TEXT,
    resolved_client_ip     TEXT,
    x_forwarded_for        TEXT,
    effective_service_tier TEXT,
    response_service_tier  TEXT,
    upstream_service_tier  TEXT,
    speed                  TEXT,
    inference_geo          TEXT,
    fail_code              TEXT,
    total_tokens           INTEGER,
    generate               INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model_ts ON requests(model, ts);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE TABLE IF NOT EXISTS quota_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    name         TEXT,
    provider     TEXT,
    status       TEXT,
    blocked      INTEGER,
    daily_pct    INTEGER,
    weekly_pct   INTEGER,
    daily_reset  REAL,
    weekly_reset REAL,
    ok           INTEGER,
    fail         INTEGER,
    signals      TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_history_ts ON quota_history(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
`

// v3 columns -> path inside the raw JSON record, used for the backfill.
var v3JSONPaths = [][2]string{
	{"executor_type", "executor_type"},
	{"auth_type", "auth_type"},
	{"access_provider", "access_provider"},
	{"is_native_key", "is_native_key"},
	{"trace_id", "trace_id"},
	{"parent_session_id", "parent_session_id"},
	{"node_kind", "node_kind"},
	{"is_fork", "is_fork"},
	{"is_compaction", "is_compaction"},
	{"client_ip", "client_ip"},
	{"resolved_client_ip", "resolved_client_ip"},
	{"x_forwarded_for", "x_forwarded_for"},
	{"effective_service_tier", "effective_service_tier"},
	{"response_service_tier", "response_service_tier"},
	{"upstream_service_tier", "upstream_service_tier"},
	{"speed", "speed"},
	{"inference_geo", "inference_geo"},
	{"fail_code", "fail.code"},
	{"total_tokens", "tokens.total_tokens"},
	{"generate", "generate"},
}

var v3ColumnTypes = map[string]string{
	"is_native_key": "INTEGER", "is_fork": "INTEGER", "is_compaction": "INTEGER",
	"total_tokens": "INTEGER", "generate": "INTEGER",
}

func migrationsV1() []string {
	return []string{
		"ALTER TABLE requests ADD COLUMN token_quality TEXT",
		`UPDATE requests SET token_quality = json_extract(raw, '$.token_breakdown.quality') WHERE token_quality IS NULL`,
	}
}

func migrationsV2() []string {
	var stmts []string
	var sets []string
	for _, cp := range v3JSONPaths {
		col, path := cp[0], cp[1]
		typ := v3ColumnTypes[col]
		if typ == "" {
			typ = "TEXT"
		}
		stmts = append(stmts,
			fmt.Sprintf("ALTER TABLE requests ADD COLUMN %s %s", col, typ))
		sets = append(sets,
			fmt.Sprintf("%s = NULLIF(json_extract(raw, '$.%s'), '')", col, path))
	}
	stmts = append(stmts,
		"UPDATE requests SET "+strings.Join(sets, ", ")+" WHERE json_valid(raw)")
	return stmts
}

func migrationsV3() []string {
	return []string{
		`CREATE TABLE IF NOT EXISTS quota_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    name         TEXT,
    provider     TEXT,
    status       TEXT,
    blocked      INTEGER,
    daily_pct    INTEGER,
    weekly_pct   INTEGER,
    daily_reset  REAL,
    weekly_reset REAL,
    ok           INTEGER,
    fail         INTEGER,
    signals      TEXT
)`,
		"CREATE INDEX IF NOT EXISTS idx_quota_history_ts ON quota_history(ts)",
	}
}

var alterColRe = regexp.MustCompile(`^ALTER TABLE requests ADD COLUMN (\w+)`)

func migrate(db *sql.DB) error {
	ver := 1
	if v := metaGet(db, "schema_version"); v != "" {
		fmt.Sscan(v, &ver)
	}
	cols := map[string]bool{}
	rows, err := db.Query("PRAGMA table_info(requests)")
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var cid int
			var name, ctype string
			var notnull, pk int
			var dflt interface{}
			if rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk) == nil {
				cols[name] = true
			}
		}
	}
	for ver < schemaVersion {
		var stmts []string
		switch ver {
		case 1:
			stmts = migrationsV1()
		case 2:
			stmts = migrationsV2()
		case 3:
			stmts = migrationsV3()
		}
		for _, stmt := range stmts {
			if m := alterColRe.FindStringSubmatch(stmt); m != nil && cols[m[1]] {
				continue // column already exists
			}
			if _, err := db.Exec(stmt); err != nil {
				return fmt.Errorf("migration v%d: %w (%s)", ver, err, stmt)
			}
		}
		ver++
	}
	metaSet(db, "schema_version", fmt.Sprint(schemaVersion))
	return nil
}

// OpenDB opens (creating) the database and applies migrations.
func OpenDB(path string) (*sql.DB, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // serialize; mirrors the single sqlite3 connection
	for _, p := range []string{
		"PRAGMA busy_timeout=10000",
		"PRAGMA journal_mode=WAL",
		"PRAGMA synchronous=NORMAL",
	} {
		if _, err := db.Exec(p); err != nil {
			db.Close()
			return nil, err
		}
	}
	if _, err := db.Exec(schemaSQL); err != nil {
		db.Close()
		return nil, err
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	_ = os.Chmod(path, 0o600)
	return db, nil
}

// OpenDBRO opens read-only; viewers never create or modify the db.
func OpenDBRO(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", fmt.Sprintf("file:%s?mode=ro", path))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA busy_timeout=10000"); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// OpenDBROOrDie is OpenDBRO with the friendly missing-db error.
func OpenDBROOrDie(path string) *sql.DB {
	if !fileExists(path) {
		fmt.Fprintf(os.Stderr, "cpa-stats: no database at %s yet. "+
			"Is the collector running? (cpa-stats doctor)\n", path)
		os.Exit(1)
	}
	db, err := OpenDBRO(path)
	if err != nil {
		die("open %s: %v", path, err)
	}
	return db
}

func metaGet(db *sql.DB, key string) string {
	var v string
	err := db.QueryRow("SELECT value FROM meta WHERE key=?", key).Scan(&v)
	if err != nil {
		return ""
	}
	return v
}

// metaValue mirrors Python str(): plain decimals, not Go's %v exponent form.
func metaValue(value interface{}) string {
	switch v := value.(type) {
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64)
	case float32:
		return strconv.FormatFloat(float64(v), 'f', -1, 64)
	case int64:
		return strconv.FormatInt(v, 10)
	case int:
		return strconv.Itoa(v)
	case string:
		return v
	}
	return fmt.Sprint(value)
}

func metaSet(db *sql.DB, key string, value interface{}) {
	_, _ = db.Exec(
		"INSERT INTO meta(key, value) VALUES(?, ?) "+
			"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
		key, metaValue(value))
}

func metaSetTx(tx *sql.Tx, key string, value interface{}) {
	_, _ = tx.Exec(
		"INSERT INTO meta(key, value) VALUES(?, ?) "+
			"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
		key, metaValue(value))
}

var insertSQL = func() string {
	marks := strings.TrimSuffix(strings.Repeat("?,", len(Columns)), ",")
	return fmt.Sprintf("INSERT OR IGNORE INTO requests(%s) VALUES(%s)",
		strings.Join(Columns, ","), marks)
}()

// InsertRecords inserts usage-queue rows; returns rows actually stored.
func InsertRecords(db *sql.DB, recs []map[string]interface{}) (int, error) {
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	stmt, err := tx.Prepare(insertSQL)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	n := 0
	for _, rec := range recs {
		res, err := stmt.Exec(recordToRow(rec)...)
		if err != nil {
			stmt.Close()
			tx.Rollback()
			return n, err
		}
		if ra, err := res.RowsAffected(); err == nil {
			n += int(ra)
		}
	}
	stmt.Close()
	return n, tx.Commit()
}

// saveQuotaSnapshot persists the latest /credentials payload in meta.
func saveQuotaSnapshot(db *sql.DB, snap map[string]interface{}) {
	metaSet(db, "credentials_snapshot", marshalNoEscape(snap))
}

func saveQuotaSnapshotTx(tx *sql.Tx, snap map[string]interface{}) {
	metaSetTx(tx, "credentials_snapshot", marshalNoEscape(snap))
}

func loadQuotaSnapshot(db *sql.DB) map[string]interface{} {
	raw := metaGet(db, "credentials_snapshot")
	if raw == "" {
		return nil
	}
	var data interface{}
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	if dec.Decode(&data) != nil {
		return nil
	}
	if m, ok := data.(map[string]interface{}); ok {
		return m
	}
	if l, ok := data.([]interface{}); ok {
		return map[string]interface{}{"files": l}
	}
	return nil
}

// saveQuotaHistory: one row per credential per poll — burn-rate forecasts.
func saveQuotaHistory(tx *sql.Tx, creds []CredInfo, ts float64) {
	stmt, err := tx.Prepare(
		"INSERT INTO quota_history" +
			"(ts, name, provider, status, blocked, daily_pct, weekly_pct," +
			" daily_reset, weekly_reset, ok, fail, signals)" +
			" VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
	if err != nil {
		return
	}
	defer stmt.Close()
	for _, c := range creds {
		var signals interface{}
		if c.Signals != nil {
			signals = marshalNoEscape(c.Signals)
		}
		_, _ = stmt.Exec(ts, c.Name, c.Provider, c.Status,
			b2i(c.Blocked || c.Limited),
			c.DailyPct, c.WeeklyPct, c.DailyReset, c.WeeklyReset,
			c.OK, c.Fail, signals)
	}
}

// QuotaHistRow mirrors one quota_history row.
type QuotaHistRow struct {
	TS          float64
	Name        string
	Provider    string
	Status      string
	Blocked     int64
	DailyPct    *int64
	WeeklyPct   *int64
	DailyReset  *float64
	WeeklyReset *float64
	OK          int64
	Fail        int64
	Signals     string
}

func loadQuotaHistory(db *sql.DB, sinceTS float64, name string) []QuotaHistRow {
	q := "SELECT ts,name,provider,status,blocked,daily_pct,weekly_pct," +
		"daily_reset,weekly_reset,ok,fail,signals FROM quota_history WHERE ts >= ?"
	args := []interface{}{sinceTS}
	if name != "" {
		q += " AND name = ?"
		args = append(args, name)
	}
	rows, err := db.Query(q+" ORDER BY ts", args...)
	if err != nil {
		return nil // pre-v4 database without the table
	}
	defer rows.Close()
	var out []QuotaHistRow
	for rows.Next() {
		var r QuotaHistRow
		var name, provider, status, signals sql.NullString
		var blocked, okN, failN sql.NullInt64
		var daily, weekly, dr, wr interface{}
		if rows.Scan(&r.TS, &name, &provider, &status, &blocked,
			&daily, &weekly, &dr, &wr, &okN, &failN, &signals) != nil {
			continue
		}
		r.Name, r.Provider, r.Status = name.String, provider.String, status.String
		r.Blocked, r.OK, r.Fail = blocked.Int64, okN.Int64, failN.Int64
		r.DailyPct, r.WeeklyPct = i64(daily), i64(weekly)
		r.DailyReset, r.WeeklyReset = f64(dr), f64(wr)
		r.Signals = signals.String
		out = append(out, r)
	}
	return out
}

// Row is one requests-table row as a column->value map.
type Row map[string]interface{}

// LoadRows mirrors storage.load_rows (newest first).
func LoadRows(db *sql.DB, sinceTS float64, model, session string,
	limit int, account string) []Row {
	q := "SELECT * FROM requests WHERE ts >= ?"
	args := []interface{}{sinceTS}
	if model != "" {
		q += " AND (model LIKE ? OR alias LIKE ? OR response_model LIKE ?)"
		like := "%" + model + "%"
		args = append(args, like, like, like)
	}
	if session != "" {
		q += " AND session_id LIKE ?"
		args = append(args, session+"%")
	}
	if account != "" {
		q += " AND (source LIKE ? OR auth_index LIKE ?)"
		like := "%" + account + "%"
		args = append(args, like, like)
	}
	q += " ORDER BY ts DESC"
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.Query(q, args...)
	if err != nil {
		return nil
	}
	defer rows.Close()
	return scanRows(rows)
}

func scanRows(rows *sql.Rows) []Row {
	cols, err := rows.Columns()
	if err != nil {
		return nil
	}
	var out []Row
	for rows.Next() {
		vals := make([]interface{}, len(cols))
		ptrs := make([]interface{}, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if rows.Scan(ptrs...) != nil {
			continue
		}
		r := Row{}
		for i, c := range cols {
			switch v := vals[i].(type) {
			case []byte:
				r[c] = string(v)
			default:
				r[c] = v
			}
		}
		out = append(out, r)
	}
	return out
}

// Agg mirrors model.Agg for window aggregations (serve/metrics/summary).
type Agg struct {
	N            int      `json:"n"`
	Failed       int      `json:"failed"`
	TTFTP50      *float64 `json:"ttft_p50"`
	TTFTP95      *float64 `json:"ttft_p95"`
	LatP50       *float64 `json:"lat_p50"`
	LatP95       *float64 `json:"lat_p95"`
	TPSP50       *float64 `json:"tps_p50"`
	TPSP10       *float64 `json:"tps_p10"`
	TPSP90       *float64 `json:"tps_p90"`
	E2ETPS       *float64 `json:"e2e_tps"`
	E2EP50       *float64 `json:"e2e_p50"`
	Burst        int      `json:"burst"`
	InTok        int64    `json:"in_tok"`
	Uncached     int64    `json:"uncached"`
	OutTok       int64    `json:"out_tok"`
	ReasoningTok int64    `json:"reasoning_tok"`
	CacheRead    int64    `json:"cache_read"`
	CacheWrite   int64    `json:"cache_write"`
	CacheRatio   *float64 `json:"cache_ratio"`
	RPM          *float64 `json:"rpm"`
	FirstTS      *float64 `json:"first_ts"`
	NComplete    int      `json:"n_complete"`
	CostUSD      *float64 `json:"cost_usd"`
}

func (a *Agg) QualityRatio() *float64 {
	if a.N == 0 {
		return nil
	}
	v := float64(a.NComplete) / float64(a.N)
	return &v
}

const minDecodeMS = 300
const minDecodeTokens = 16

func decodeTPS(outTok interface{}, latencyMS, ttftMS interface{}) *float64 {
	o, ok1 := parseFloat(outTok)
	l, ok2 := parseFloat(latencyMS)
	t, ok3 := parseFloat(ttftMS)
	if !ok1 || o == 0 || !ok2 || !ok3 {
		return nil
	}
	decodeMS := l - t
	if decodeMS < minDecodeMS || o < minDecodeTokens {
		return nil
	}
	v := o / (decodeMS / 1000)
	return &v
}

func e2eTPS(outTok, latencyMS interface{}) *float64 {
	o, ok1 := parseFloat(outTok)
	l, ok2 := parseFloat(latencyMS)
	if !ok1 || o == 0 || !ok2 || l == 0 {
		return nil
	}
	v := o / (l / 1000)
	return &v
}

// pct interpolates the q-quantile like Python's pct().
func pctile(vals []float64, q float64) *float64 {
	var sorted []float64
	for _, v := range vals {
		sorted = append(sorted, v)
	}
	if len(sorted) == 0 {
		return nil
	}
	sort64(sorted)
	if len(sorted) == 1 {
		v := sorted[0]
		return &v
	}
	k := float64(len(sorted)-1) * q
	lo := int(k)
	hi := lo + 1
	if hi > len(sorted)-1 {
		hi = len(sorted) - 1
	}
	v := sorted[lo] + (sorted[hi]-sorted[lo])*(k-float64(lo))
	return &v
}

func sort64(v []float64) {
	for i := 1; i < len(v); i++ {
		for j := i; j > 0 && v[j] < v[j-1]; j-- {
			v[j], v[j-1] = v[j-1], v[j]
		}
	}
}

// tokenParts mirrors model.token_parts.
func tokenParts(r Row) (total, uncached, read, write int64) {
	inp := derefI(i64(r["input_tokens"]))
	read = derefI(i64(r["cache_read_tokens"]))
	write = derefI(i64(r["cache_write_tokens"]))
	uncached = inp - read - write
	if uncached < 0 {
		uncached = inp - read
		if uncached < 0 {
			uncached = 0
		}
	}
	return uncached + read + write, uncached, read, write
}

func rowQuality(r Row) string {
	if q := strv(r["token_quality"]); q != "" {
		return q
	}
	raw := strv(r["raw"])
	if raw == "" {
		return ""
	}
	var m map[string]interface{}
	if json.Unmarshal([]byte(raw), &m) != nil {
		return ""
	}
	if tb, ok := m["token_breakdown"].(map[string]interface{}); ok {
		return strv(tb["quality"])
	}
	return ""
}

// Pricing: USD per 1M tokens.
type Pricing struct {
	InputPerM      float64 `json:"input_per_m"`
	CachedReadPerM float64 `json:"cached_read_per_m"`
	OutputPerM     float64 `json:"output_per_m"`
}

func (p *Pricing) Enabled() bool {
	return p != nil && (p.InputPerM > 0 || p.OutputPerM > 0 || p.CachedReadPerM > 0)
}

func (p *Pricing) Cost(uncached, cacheRead, outTok float64) float64 {
	return (uncached*p.InputPerM + cacheRead*p.CachedReadPerM +
		outTok*p.OutputPerM) / 1e6
}

// ParsePricing 'in,cached,out' USD per 1M tokens; empty -> disabled.
func ParsePricing(text string) (*Pricing, error) {
	p := &Pricing{}
	if strings.TrimSpace(text) == "" {
		return p, nil
	}
	parts := strings.Split(text, ",")
	if len(parts) != 3 {
		return nil, fmt.Errorf("invalid --price value: want 'IN,CACHED,OUT'")
	}
	var err error
	parse := func(s string) float64 {
		f, e := parseFloat(strings.TrimSpace(s))
		if e == false || f < 0 {
			err = fmt.Errorf("invalid --price value %q", s)
		}
		return f
	}
	p.InputPerM = parse(parts[0])
	p.CachedReadPerM = parse(parts[1])
	p.OutputPerM = parse(parts[2])
	if err != nil {
		return nil, err
	}
	return p, nil
}

// Aggregate mirrors model.aggregate.
func Aggregate(rows []Row, windowS float64, pricing *Pricing) *Agg {
	a := &Agg{N: len(rows)}
	if len(rows) == 0 {
		return a
	}
	var ok []Row
	for _, r := range rows {
		if derefI(i64(r["failed"])) == 0 {
			ok = append(ok, r)
		}
	}
	a.Failed = a.N - len(ok)
	var ttfts, lats, tpsVals []float64
	for _, r := range ok {
		if v, ok := parseFloat(r["ttft_ms"]); ok && v != 0 {
			ttfts = append(ttfts, v)
		}
		if v, ok := parseFloat(r["latency_ms"]); ok && v != 0 {
			lats = append(lats, v)
		}
		if t := decodeTPS(r["output_tokens"], r["latency_ms"], r["ttft_ms"]); t != nil {
			tpsVals = append(tpsVals, *t)
		}
	}
	a.TTFTP50, a.TTFTP95 = pctile(ttfts, 0.5), pctile(ttfts, 0.95)
	var e2eVals []float64
	for _, r := range ok {
		if t := e2eTPS(r["output_tokens"], r["latency_ms"]); t != nil {
			e2eVals = append(e2eVals, *t)
		}
	}
	a.E2EP50 = pctile(e2eVals, 0.5)
	for _, r := range ok {
		out, _ := parseFloat(r["output_tokens"])
		lat, ok1 := parseFloat(r["latency_ms"])
		ttft, ok2 := parseFloat(r["ttft_ms"])
		if out >= minDecodeTokens && ok1 && ok2 && lat-ttft < minDecodeMS {
			a.Burst++
		}
	}
	a.LatP50, a.LatP95 = pctile(lats, 0.5), pctile(lats, 0.95)
	a.TPSP50, a.TPSP10, a.TPSP90 =
		pctile(tpsVals, 0.5), pctile(tpsVals, 0.1), pctile(tpsVals, 0.9)
	var first *float64
	for _, r := range rows {
		total, uncached, read, write := tokenParts(r)
		a.InTok += total
		a.Uncached += uncached
		a.CacheRead += read
		a.CacheWrite += write
		if t, ok := parseFloat(r["ts"]); ok && (first == nil || t < *first) {
			v := t
			first = &v
		}
	}
	a.FirstTS = first
	for _, r := range rows {
		a.OutTok += derefI(i64(r["output_tokens"]))
		a.ReasoningTok += derefI(i64(r["reasoning_tokens"]))
		if rowQuality(r) == "complete" {
			a.NComplete++
		}
	}
	if a.InTok > 0 {
		v := float64(a.CacheRead) / float64(a.InTok)
		a.CacheRatio = &v
	}
	var totalLat, outSum float64
	for _, r := range ok {
		if l, ok2 := parseFloat(r["latency_ms"]); ok2 && l != 0 {
			totalLat += l
			o, _ := parseFloat(r["output_tokens"])
			outSum += o
		}
	}
	if totalLat > 0 {
		v := outSum / (totalLat / 1000)
		a.E2ETPS = &v
	}
	if windowS > 0 {
		v := float64(a.N) / (windowS / 60)
		a.RPM = &v
	}
	if pricing.Enabled() {
		v := pricing.Cost(float64(a.Uncached), float64(a.CacheRead), float64(a.OutTok))
		a.CostUSD = &v
	}
	return a
}

// DBTotals: all-time totals over the whole database, computed in SQL.
func DBTotals(db *sql.DB, pricing *Pricing) *Agg {
	a := &Agg{}
	var n, failed, cacheRead, cacheWrite, outTok, reasoning, nComplete interface{}
	var firstTS interface{}
	err := db.QueryRow(`SELECT COUNT(*),
		SUM(failed),
		SUM(COALESCE(cache_read_tokens, 0)),
		SUM(COALESCE(cache_write_tokens, 0)),
		SUM(COALESCE(output_tokens, 0)),
		SUM(COALESCE(reasoning_tokens, 0)),
		SUM(CASE WHEN COALESCE(token_quality,'')='complete' THEN 1 ELSE 0 END),
		MIN(ts)
		FROM requests`).Scan(&n, &failed, &cacheRead, &cacheWrite, &outTok,
		&reasoning, &nComplete, &firstTS)
	if err != nil || derefI(i64(n)) == 0 {
		return a
	}
	a.N = int(derefI(i64(n)))
	a.Failed = int(derefI(i64(failed)))
	var uncached, promptTotal interface{}
	_ = db.QueryRow(`SELECT SUM(CASE
			WHEN COALESCE(input_tokens,0) >= COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)
			  THEN COALESCE(input_tokens,0) - COALESCE(cache_read_tokens,0) - COALESCE(cache_write_tokens,0)
			ELSE MAX(COALESCE(input_tokens,0) - COALESCE(cache_read_tokens,0), 0)
		  END),
		SUM(MAX(COALESCE(input_tokens,0),
				COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)))
		FROM requests`).Scan(&uncached, &promptTotal)
	a.Uncached = derefI(i64(uncached))
	a.InTok = derefI(i64(promptTotal))
	a.CacheRead = derefI(i64(cacheRead))
	a.CacheWrite = derefI(i64(cacheWrite))
	a.OutTok = derefI(i64(outTok))
	a.ReasoningTok = derefI(i64(reasoning))
	a.NComplete = int(derefI(i64(nComplete)))
	if a.InTok > 0 {
		v := float64(a.CacheRead) / float64(a.InTok)
		a.CacheRatio = &v
	}
	a.FirstTS = f64(firstTS)
	if pricing.Enabled() {
		v := pricing.Cost(float64(a.Uncached), float64(a.CacheRead), float64(a.OutTok))
		a.CostUSD = &v
	}
	return a
}

// Prune deletes old rows (both tables) and checkpoints the WAL.
func Prune(db *sql.DB, olderThanS float64) (int, error) {
	cutoff := float64(time.Now().UnixNano())/1e9 - olderThanS
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	res, err := tx.Exec("DELETE FROM requests WHERE ts < ?", cutoff)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	n, _ := res.RowsAffected()
	_, _ = tx.Exec("DELETE FROM quota_history WHERE ts < ?", cutoff)
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	_, _ = db.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	return int(n), nil
}
