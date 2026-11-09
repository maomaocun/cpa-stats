// Read-side viewer commands (plain-text equivalents of the rich views).
package main

import (
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"
)

type filters struct {
	model, session, account string
}

func addFilters(fs *flagSetLike, f *filters) {
	fs.StringVar(&f.model, "model", "", "filter: substring of model / alias")
	fs.StringVar(&f.model, "m", "", "")
	fs.StringVar(&f.session, "session", "", "filter: session id prefix")
	fs.StringVar(&f.session, "s", "", "")
	fs.StringVar(&f.account, "account", "", "filter: substring of account / auth index")
	fs.StringVar(&f.account, "a", "", "")
}

func addSince(fs *flagSetLike, p *float64, dflt string) {
	var tmp string
	fs.StringVar(&tmp, "since", dflt, "window (default "+dflt+")")
	fs.postParse = append(fs.postParse, func() error {
		v, err := parseDuration(tmp)
		if err != nil {
			return err
		}
		*p = v
		return nil
	})
}

func addPrice(fs *flagSetLike, p **Pricing) {
	var tmp string
	fs.StringVar(&tmp, "price", os.Getenv("CPA_STATS_PRICE"),
		"USD per 1M tokens for input / cache-read / output; "+
			"enables the est. cost (e.g. '2,0.2,8'; env CPA_STATS_PRICE)")
	fs.postParse = append(fs.postParse, func() error {
		v, err := ParsePricing(tmp)
		if err != nil {
			return err
		}
		*p = v
		return nil
	})
}

// ---------------------------------------------------------------------- sql

func cmdSQL(g *GlobalFlags, argv []string) {
	var format string
	var limit int
	fs := newFlagSet("sql", g)
	fs.StringVar(&format, "format", "table", "table|csv|json")
	fs.IntVar(&limit, "limit", 500, "max rows displayed (default 500)")
	mustParse(fs, argv)
	if fs.NArg() < 1 {
		die("usage: cpa-stats sql <query>")
	}
	stmt := strings.TrimSpace(strings.TrimSuffix(strings.Join(fs.Args(), " "), ";"))
	first := ""
	if stmt != "" {
		first = strings.ToUpper(strings.Fields(stmt)[0])
	}
	switch first {
	case "SELECT", "WITH", "EXPLAIN", "PRAGMA":
	default:
		die("only read-only queries are allowed (SELECT / WITH / EXPLAIN / PRAGMA)")
	}
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	rows, err := db.Query(stmt)
	if err != nil {
		die("query failed: %v", err)
	}
	defer rows.Close()
	cols, _ := rows.Columns()
	var data [][]interface{}
	for rows.Next() && len(data) < limit {
		vals := make([]interface{}, len(cols))
		ptrs := make([]interface{}, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if rows.Scan(ptrs...) != nil {
			continue
		}
		for i, v := range vals {
			if b, ok := v.([]byte); ok {
				vals[i] = string(b)
			}
		}
		data = append(data, vals)
	}
	switch format {
	case "json":
		var out []map[string]interface{}
		for _, r := range data {
			m := map[string]interface{}{}
			for i, c := range cols {
				m[c] = r[i]
			}
			out = append(out, m)
		}
		fmt.Println(marshalNoEscape(out))
	case "csv":
		w := csv.NewWriter(os.Stdout)
		w.Write(cols)
		for _, r := range data {
			var rec []string
			for _, v := range r {
				rec = append(rec, strv(v))
			}
			w.Write(rec)
		}
		w.Flush()
	default:
		var tab [][]string
		for _, r := range data {
			var rec []string
			for _, v := range r {
				rec = append(rec, strv(v))
			}
			tab = append(tab, rec)
		}
		printTable(os.Stdout, cols, tab, nil)
		if len(data) == limit {
			fmt.Printf("… truncated at --limit %d\n", limit)
		}
	}
}

// ------------------------------------------------------------------ export

func cmdExport(g *GlobalFlags, argv []string) {
	var since float64
	var format, output string
	f := &filters{}
	fs := newFlagSet("export", g)
	addSince(fs, &since, "24h")
	fs.StringVar(&format, "format", "csv", "csv|jsonl")
	fs.StringVar(&output, "output", "", "output file (default stdout)")
	fs.StringVar(&output, "o", "", "")
	addFilters(fs, f)
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	rows := LoadRows(db, nowSecs()-since, f.model, f.session, 0, f.account)
	// oldest first
	for i, j := 0, len(rows)-1; i < j; i, j = i+1, j-1 {
		rows[i], rows[j] = rows[j], rows[i]
	}
	out := os.Stdout
	if output != "" {
		fh, err := os.Create(output)
		if err != nil {
			die("create %s: %v", output, err)
		}
		defer fh.Close()
		out = fh
	}
	var fields []string
	for _, c := range Columns {
		if c != "raw" {
			fields = append(fields, c)
		}
	}
	fields = append(fields, "prompt_tokens_total", "uncached_tokens",
		"cache_hit_ratio", "decode_tps", "e2e_tps", "iso_time")
	if format == "csv" {
		w := csv.NewWriter(out)
		w.Write(fields)
		for _, r := range rows {
			w.Write(exportRecord(r, fields))
		}
		w.Flush()
	} else {
		for _, r := range rows {
			d := exportDict(r)
			var buf strings.Builder
			enc := json.NewEncoder(&buf)
			enc.SetEscapeHTML(false)
			_ = enc.Encode(d)
			out.WriteString(strings.TrimRight(buf.String(), "\n") + "\n")
		}
	}
	if output != "" {
		fmt.Fprintf(os.Stderr, "wrote %d rows to %s\n", len(rows), output)
	}
}

func exportDict(r Row) map[string]interface{} {
	d := map[string]interface{}{}
	for _, c := range Columns {
		if c != "raw" {
			d[c] = r[c]
		}
	}
	total, uncached, read, _ := tokenParts(r)
	d["prompt_tokens_total"] = total
	d["uncached_tokens"] = uncached
	if total > 0 {
		d["cache_hit_ratio"] = round4(float64(read) / float64(total))
	} else {
		d["cache_hit_ratio"] = nil
	}
	d["decode_tps"] = decodeTPS(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
	d["e2e_tps"] = e2eTPS(r["output_tokens"], r["latency_ms"])
	if ts, ok := parseFloat(r["ts"]); ok {
		d["iso_time"] = time.Unix(int64(ts), int64((ts-float64(int64(ts)))*1e9)).
			Format("2006-01-02T15:04:05.999999-07:00")
	}
	return d
}

func round4(f float64) float64 { return float64(int(f*10000+0.5)) / 10000 }

func exportRecord(r Row, fields []string) []string {
	d := exportDict(r)
	var out []string
	for _, f := range fields {
		out = append(out, strv(d[f]))
	}
	return out
}

// -------------------------------------------------------------------- show

func cmdShow(g *GlobalFlags, argv []string) {
	var raw, asJSON bool
	fs := newFlagSet("show", g)
	fs.BoolVar(&raw, "raw", false, "also dump the raw upstream record JSON")
	fs.BoolVar(&asJSON, "json", false, "print the stored row as JSON")
	mustParse(fs, argv)
	if fs.NArg() < 1 {
		die("usage: cpa-stats show <request_id>")
	}
	rid := fs.Arg(0)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	hasTrace := false
	tr, err := db.Query("PRAGMA table_info(requests)")
	if err == nil {
		for tr.Next() {
			var cid int
			var name, ctype string
			var nn, pk int
			var df interface{}
			if tr.Scan(&cid, &name, &ctype, &nn, &df, &pk) == nil && name == "trace_id" {
				hasTrace = true
			}
		}
		tr.Close()
	}
	where := "id LIKE ? OR request_id LIKE ?"
	args := []interface{}{rid + "%", rid + "%"}
	if hasTrace {
		where += " OR trace_id LIKE ?"
		args = append(args, rid+"%")
	}
	rows, err := db.Query(
		"SELECT * FROM requests WHERE "+where+" ORDER BY ts DESC LIMIT 1", args...)
	if err != nil {
		die("query failed: %v", err)
	}
	found := scanRows(rows)
	rows.Close()
	if len(found) == 0 {
		fmt.Fprintf(os.Stderr, "no request matching '%s'\n", rid)
		os.Exit(1)
	}
	row := found[0]
	if asJSON {
		d := map[string]interface{}{}
		for k, v := range row {
			d[k] = v
		}
		if rw, ok := d["raw"].(string); ok && rw != "" {
			var m interface{}
			dec := json.NewDecoder(strings.NewReader(rw))
			dec.UseNumber()
			if dec.Decode(&m) == nil {
				d["raw"] = m
			}
		}
		fmt.Println(marshalNoEscape(d))
		return
	}
	var tab [][]string
	for _, c := range Columns {
		v := row[c]
		if c == "raw" {
			continue
		}
		vs := strv(v)
		if c == "ts" {
			if t, ok := parseFloat(v); ok {
				vs = fmt.Sprintf("%s (%.3f)",
					time.Unix(int64(t), 0).Format("2006-01-02 15:04:05"), t)
			}
		}
		tab = append(tab, []string{c, vs})
	}
	printTable(os.Stdout, []string{"field", "value"}, tab, nil)
	if raw {
		if rw := strv(row["raw"]); rw != "" {
			var m interface{}
			dec := json.NewDecoder(strings.NewReader(rw))
			dec.UseNumber()
			if dec.Decode(&m) == nil {
				pretty, _ := json.MarshalIndent(m, "", "  ")
				fmt.Println(string(pretty))
			} else {
				fmt.Println(rw)
			}
		}
	}
}

// ------------------------------------------------------------------- quota

func cmdQuota(g *GlobalFlags, argv []string) {
	var refresh, asJSON bool
	fs := newFlagSet("quota", g)
	fs.BoolVar(&refresh, "refresh", false,
		"force CPA to re-observe credentials upstream first, then store + show")
	fs.BoolVar(&refresh, "r", false, "")
	fs.BoolVar(&asJSON, "json", false, "")
	mustParse(fs, argv)

	var db *sql.DB
	var err error
	var snap map[string]interface{}
	if refresh {
		db, err = OpenDB(g.DB)
		if err != nil {
			die("open %s: %v", g.DB, err)
		}
		cfgs := loadCPAConfigs(g)
		var allFiles []interface{}
		var errsAll []error
		for _, cfg := range cfgs {
			s, err := fetchCredentials(cfg, 10)
			if err != nil {
				errsAll = append(errsAll, err)
				continue
			}
			names := staleQuotaCredentials(s, 0, nowSecs())
			for _, n := range names {
				if e := refreshCredential(cfg, n, 15); e != nil {
					errsAll = append(errsAll, e)
				}
			}
			if len(names) > 0 {
				if s2, err := fetchCredentials(cfg, 10); err == nil {
					s = s2
				}
			}
			files, _ := s["files"].([]interface{})
			for _, ff := range files {
				if fm, ok := ff.(map[string]interface{}); ok {
					if len(cfgs) > 1 {
						cp := map[string]interface{}{}
						for k, v := range fm {
							cp[k] = v
						}
						cp["_instance"] = instanceTag(cfg.URL)
						allFiles = append(allFiles, cp)
					} else {
						allFiles = append(allFiles, fm)
					}
				}
			}
		}
		if len(errsAll) > 0 {
			fmt.Fprintf(os.Stderr,
				"quota refresh failed for %d credential(s): %v\n",
				len(errsAll), errsAll[0])
		}
		now := nowSecs()
		payload := map[string]interface{}{"fetched_at": now, "files": allFiles}
		tx, _ := db.Begin()
		saveQuotaSnapshotTx(tx, payload)
		saveQuotaHistory(tx, ParseCredentials(payload), now)
		tx.Commit()
		snap = payload
	} else {
		db = OpenDBROOrDie(g.DB)
	}
	defer db.Close()

	if snap == nil {
		snap = loadQuotaSnapshot(db)
	}
	creds := ParseCredentials(snap)
	if asJSON {
		var fetched interface{}
		if snap != nil {
			fetched = snap["fetched_at"]
		}
		out := map[string]interface{}{
			"fetched_at":  fetched,
			"credentials": creds,
		}
		fmt.Println(marshalNoEscape(out))
		return
	}
	if len(creds) == 0 {
		fmt.Println("no credential snapshot yet — restart `cpa-stats collect` " +
			"(it stores /management/credentials every --quota-interval seconds)")
		return
	}
	fetched, _ := parseFloat(snap["fetched_at"])
	var ageStr string
	if fetched > 0 {
		age := nowSecs() - fetched
		ageStr = " · snapshot " + fmtAge(&age)
	}
	fmt.Printf("cpa-stats quota · %d credential(s)%s\n\n", len(creds), ageStr)
	sort.Slice(creds, func(i, j int) bool {
		bi, bj := CredSortKey(&creds[i]), CredSortKey(&creds[j])
		if equalKeys(bi, bj) {
			return bi[6].(string) < bj[6].(string)
		}
		return credLess(bi, bj)
	})
	hist := map[string][]QuotaHistRow{}
	for _, r := range loadQuotaHistory(db, 0, "") {
		hist[r.Name] = append(hist[r.Name], r)
	}
	var tab [][]string
	for _, c := range creds {
		reset := func(t *float64) string {
			if t == nil {
				return ""
			}
			return " " + fmtCountdown(*t-nowSecs())
		}
		status := c.Status
		if c.Blocked {
			status += " BLOCKED"
		} else if c.Limited {
			status += " limited"
		}
		var forecast string
		if fc := QuotaForecast(hist[c.Name], "weekly_pct", 1800, 0.1); fc != nil {
			forecast = fmt.Sprintf(" −%.0f%%/day → 0%% in %s",
				fc[1], fmtCountdown(fc[2]-nowSecs()))
		}
		tab = append(tab, []string{
			c.Name, c.Provider, c.Account, status,
			quotaBar(c.DailyPct) + reset(c.DailyReset),
			quotaBar(c.WeeklyPct) + reset(c.WeeklyReset) + forecast,
			fmt.Sprintf("%d/%d", c.OK, c.Fail),
		})
	}
	printTable(os.Stdout,
		[]string{"credential", "provider", "account", "status",
			"daily", "weekly", "ok/fail"},
		tab, nil)
}

func credLess(a, b [7]interface{}) bool {
	for i := 0; i < 6; i++ {
		av, bv := a[i], b[i]
		switch x := av.(type) {
		case bool:
			if x != bv.(bool) {
				return bv.(bool) // false sorts first (not-blocked first)
			}
		case int64:
			if x != bv.(int64) {
				return x < bv.(int64)
			}
		}
	}
	return false
}

// ----------------------------------------------------------------- summary

var summaryKeyFns = map[string]func(Row) string{
	"model":   shortModel,
	"session": func(r Row) string { return orLabel(strv(r["session_id"]), "—") },
	"account": func(r Row) string {
		return orLabel(firstNonEmpty(strv(r["source"]), strv(r["auth_index"])), "—")
	},
	"effort": func(r Row) string {
		return shortModel(r) + " · " + orLabel(strv(r["effort"]), "—")
	},
	"endpoint":  func(r Row) string { return orLabel(strv(r["endpoint"]), "—") },
	"provider":  func(r Row) string { return orLabel(strv(r["provider"]), "—") },
	"upstream":  func(r Row) string { return orLabel(strv(r["response_model"]), "—") },
	"node_kind": func(r Row) string { return orLabel(strv(r["node_kind"]), "—") },
	"hour": func(r Row) string {
		ts, _ := parseFloat(r["ts"])
		return time.Unix(int64(ts), 0).Format("01-02 15:00")
	},
	"day": func(r Row) string {
		ts, _ := parseFloat(r["ts"])
		return time.Unix(int64(ts), 0).Format("2006-01-02")
	},
}

func cmdSummary(g *GlobalFlags, argv []string) {
	var since float64
	var by string
	var asJSON bool
	var pricing *Pricing
	f := &filters{}
	fs := newFlagSet("summary", g)
	addSince(fs, &since, "24h")
	fs.StringVar(&by, "by", "model",
		"model|effort|session|account|endpoint|provider|upstream|node_kind|hour|day")
	fs.BoolVar(&asJSON, "json", false, "")
	addPrice(fs, &pricing)
	addFilters(fs, f)
	mustParse(fs, argv)
	keyfn, ok := summaryKeyFns[by]
	if !ok {
		die("invalid --by %q", by)
	}
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	now := nowSecs()
	rows := LoadRows(db, now-since, f.model, f.session, 0, f.account)
	agg := Aggregate(rows, since, pricing)
	var prevRows []Row
	for _, r := range LoadRows(db, now-2*since, f.model, f.session, 0, f.account) {
		if ts, ok := parseFloat(r["ts"]); ok && ts < now-since {
			prevRows = append(prevRows, r)
		}
	}
	groups := map[string][]Row{}
	var order []string
	for _, r := range rows {
		k := keyfn(r)
		if _, seen := groups[k]; !seen {
			order = append(order, k)
		}
		groups[k] = append(groups[k], r)
	}
	if asJSON {
		gmap := map[string]*Agg{}
		for name, grp := range groups {
			gmap[name] = Aggregate(grp, 0, nil)
		}
		var prev *Agg
		if len(prevRows) > 0 {
			prev = Aggregate(prevRows, since, nil)
		}
		out := map[string]interface{}{
			"since_s": since, "aggregate": agg, "previous": prev,
			"by": by, "groups": gmap,
		}
		fmt.Println(marshalNoEscape(out))
		return
	}
	fmt.Printf("cpa-stats summary · last %s\n", windowLabel(since))
	printAggLine(agg)
	if len(prevRows) > 0 {
		prev := Aggregate(prevRows, since, nil)
		diff := func(cur, old float64) string {
			if old == 0 {
				return "n/a"
			}
			d := (cur - old) / old * 100
			if d >= 0 {
				return fmt.Sprintf("+%.0f%%", d)
			}
			return fmt.Sprintf("−%.0f%%", -d)
		}
		failNow, failPrev := 0.0, 0.0
		if agg.N > 0 {
			failNow = float64(agg.Failed) / float64(agg.N) * 100
		}
		if prev.N > 0 {
			failPrev = float64(prev.Failed) / float64(prev.N) * 100
		}
		fd := failNow - failPrev
		fdSign := "+"
		if fd < 0 {
			fdSign = "−"
			fd = -fd
		}
		fmt.Printf("vs previous %s:  req %d (%s)  fail %.1f%% (%s%.1fpt)  "+
			"in %s (%s)  out %s (%s)  ttft p50 %s (%s)  lat p50 %s (%s)\n",
			windowLabel(since), agg.N, diff(float64(agg.N), float64(prev.N)),
			failNow, fdSign, fd,
			fmtTok(agg.InTok), diff(float64(agg.InTok), float64(prev.InTok)),
			fmtTok(agg.OutTok), diff(float64(agg.OutTok), float64(prev.OutTok)),
			fmtMs(agg.TTFTP50), diff(derefF(agg.TTFTP50), derefF(prev.TTFTP50)),
			fmtMs(agg.LatP50), diff(derefF(agg.LatP50), derefF(prev.LatP50)))
	}
	fmt.Printf("\nby %s\n", by)
	// order: hour/day chronological, others by count desc
	sort.Slice(order, func(i, j int) bool {
		if by == "hour" || by == "day" {
			return order[i] < order[j]
		}
		return len(groups[order[i]]) > len(groups[order[j]])
	})
	var tab [][]string
	right := map[int]bool{}
	for i := 1; i < 16; i++ {
		right[i] = true
	}
	for _, name := range order {
		a := Aggregate(groups[name], 0, nil)
		tpsRange := "—"
		if a.TPSP10 != nil || a.TPSP90 != nil {
			tpsRange = fmt.Sprintf("%.0f–%.0f",
				derefF(a.TPSP10), derefF(a.TPSP90))
		}
		hit := "—"
		if a.CacheRatio != nil {
			hit = fmt.Sprintf("%.0f%%", *a.CacheRatio*100)
		}
		failStr := fmt.Sprint(a.Failed)
		if a.Failed > 0 {
			failStr = "\033[31m" + failStr + "\033[0m"
		}
		tab = append(tab, []string{
			name, fmt.Sprint(a.N), failStr,
			fmtMs(a.TTFTP50), fmtMs(a.TTFTP95),
			fmt.Sprintf("%.0f", derefF(a.TPSP50)), tpsRange,
			fmt.Sprintf("%.0f", derefF(a.E2EP50)),
			fmtMs(a.LatP50), fmtMs(a.LatP95),
			fmtTok(a.InTok), fmtTok(a.Uncached),
			fmtTok(a.CacheRead), fmtTok(a.CacheWrite),
			hit, fmtTok(a.OutTok),
		})
	}
	if len(tab) == 0 {
		tab = append(tab, []string{"no requests in window"})
	}
	printTable(os.Stdout, []string{
		by, "req", "fail", "TTFT p50", "TTFT p95", "TPS p50", "TPS p10–p90",
		"e2e p50", "lat p50", "lat p95", "in", "new", "c.read", "c.write",
		"hit", "out",
	}, tab, right)
	fails := 0
	for _, r := range rows {
		if derefI(i64(r["failed"])) == 1 {
			fails++
		}
	}
	if fails > 0 {
		fmt.Println("\nrecent failures")
		var ftab [][]string
		count := 0
		for _, r := range rows {
			if derefI(i64(r["failed"])) != 1 {
				continue
			}
			ts, _ := parseFloat(r["ts"])
			errStr := strv(r["error"])
			if len([]rune(errStr)) > 300 {
				errStr = string([]rune(errStr)[:300])
			}
			status := ""
			if sc := i64(r["status_code"]); sc != nil {
				status = fmt.Sprint(*sc)
			}
			ftab = append(ftab, []string{
				time.Unix(int64(ts), 0).Format("01-02 15:04:05"),
				shortModel(r), status, errStr,
			})
			if count++; count >= 10 {
				break
			}
		}
		printTable(os.Stdout, []string{"time", "model", "status", "error"},
			ftab, nil)
	}
	if snap := loadQuotaSnapshot(db); snap != nil {
		if creds := ParseCredentials(snap); len(creds) > 0 {
			fmt.Println()
			printQuotaBrief(creds)
		}
	}
}

func printAggLine(a *Agg) {
	hit := "—"
	if a.CacheRatio != nil {
		hit = fmt.Sprintf("%.0f%%", *a.CacheRatio*100)
	}
	parts := []string{
		fmt.Sprintf("req %d", a.N),
		fmt.Sprintf("fail %d", a.Failed),
		"TTFT " + fmtMs(a.TTFTP50),
		"lat " + fmtMs(a.LatP50),
	}
	if a.TPSP50 != nil {
		parts = append(parts, fmt.Sprintf("TPS %.0f", *a.TPSP50))
	}
	parts = append(parts,
		"in "+fmtTok(a.InTok), "out "+fmtTok(a.OutTok), "hit "+hit)
	if a.CostUSD != nil {
		parts = append(parts, fmt.Sprintf("cost $%.2f", *a.CostUSD))
	}
	fmt.Println(strings.Join(parts, "  ·  "))
}

func printQuotaBrief(creds []CredInfo) {
	sort.Slice(creds, func(i, j int) bool {
		bi, bj := CredSortKey(&creds[i]), CredSortKey(&creds[j])
		return credLess(bi, bj) || (equalKeys(bi, bj) && bi[6].(string) < bj[6].(string))
	})
	var tab [][]string
	for _, c := range creds {
		status := c.Status
		if c.Blocked {
			status += " BLOCKED"
		} else if c.Limited {
			status += " limited"
		}
		tab = append(tab, []string{c.Name, c.Provider, status,
			quotaBar(c.DailyPct), quotaBar(c.WeeklyPct)})
	}
	printTable(os.Stdout, []string{"credential", "provider", "status",
		"daily", "weekly"}, tab, nil)
}

func equalKeys(a, b [7]interface{}) bool {
	for i := 0; i < 6; i++ {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ------------------------------------------------------------------ errors

func cmdErrors(g *GlobalFlags, argv []string) {
	var since float64
	var asJSON bool
	f := &filters{}
	fs := newFlagSet("errors", g)
	addSince(fs, &since, "24h")
	fs.BoolVar(&asJSON, "json", false, "")
	addFilters(fs, f)
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	rows := LoadRows(db, nowSecs()-since, f.model, f.session, 0, f.account)
	type key struct {
		model, code, ep string
		status          int64
	}
	groups := map[key][]Row{}
	for _, r := range rows {
		if derefI(i64(r["failed"])) == 0 {
			continue
		}
		k := key{shortModel(r), strv(r["fail_code"]), strv(r["endpoint"]),
			derefI(i64(r["status_code"]))}
		groups[k] = append(groups[k], r)
	}
	type ent struct {
		k      key
		n      int
		last   float64
		sample string
	}
	var list []ent
	for k, grp := range groups {
		e := ent{k: k, n: len(grp)}
		for _, r := range grp {
			if ts, ok := parseFloat(r["ts"]); ok && ts > e.last {
				e.last = ts
			}
			if e.sample == "" {
				if s := strv(r["error"]); s != "" {
					e.sample = s
				}
			}
		}
		list = append(list, e)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].n > list[j].n })
	fails := 0
	for _, grp := range groups {
		fails += len(grp)
	}
	if asJSON {
		var out []map[string]interface{}
		for _, e := range list {
			sample := e.sample
			if len([]rune(sample)) > 300 {
				sample = string([]rune(sample)[:300])
			}
			out = append(out, map[string]interface{}{
				"model": e.k.model, "status": e.k.status,
				"code": e.k.code, "endpoint": e.k.ep,
				"count": e.n, "last_ts": e.last, "sample": sample,
			})
		}
		fmt.Println(marshalNoEscape(map[string]interface{}{
			"since_s": since, "requests": len(rows), "failed": fails,
			"groups": out,
		}))
		return
	}
	pctS := ""
	if len(rows) > 0 {
		pctS = fmt.Sprintf(" (%.1f%%)", float64(fails)/float64(len(rows))*100)
	}
	fmt.Printf("cpa-stats errors · last %s · %d/%d failed%s\n",
		windowLabel(since), fails, len(rows), pctS)
	if fails == 0 {
		fmt.Println("no failures in window")
		return
	}
	var tab [][]string
	for _, e := range list {
		sample := e.sample
		if len([]rune(sample)) > 100 {
			sample = string([]rune(sample)[:100])
		}
		ep := e.k.ep
		if i := strings.LastIndex(ep, "/"); i >= 0 {
			ep = ep[i+1:]
		}
		code := e.k.code
		if code == "" {
			code = "—"
		}
		tab = append(tab, []string{
			e.k.model, fmt.Sprint(e.k.status), code, ep,
			fmt.Sprint(e.n), time.Unix(int64(e.last), 0).Format("01-02 15:04:05"),
			sample,
		})
	}
	printTable(os.Stdout, []string{"model", "status", "code", "endpoint",
		"count", "last", "sample error"}, tab,
		map[int]bool{1: true, 4: true})
}

// --------------------------------------------------------------------- top

func cmdTop(g *GlobalFlags, argv []string) {
	var since float64
	var by string
	var limit int
	var asJSON bool
	var pricing *Pricing
	f := &filters{}
	fs := newFlagSet("top", g)
	addSince(fs, &since, "24h")
	fs.StringVar(&by, "by", "tokens", "tokens|output|cost|latency")
	fs.IntVar(&limit, "limit", 15, "")
	fs.IntVar(&limit, "n", 15, "")
	fs.BoolVar(&asJSON, "json", false, "")
	addPrice(fs, &pricing)
	addFilters(fs, f)
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	rows := LoadRows(db, nowSecs()-since, f.model, f.session, 0, f.account)
	type scored struct {
		r                   Row
		tokens, output, lat float64
		cost                *float64
	}
	var list []scored
	for _, r := range rows {
		total, uncached, read, _ := tokenParts(r)
		outTok, _ := parseFloat(r["output_tokens"])
		lat, _ := parseFloat(r["latency_ms"])
		var cost *float64
		if pricing.Enabled() {
			c := pricing.Cost(float64(uncached), float64(read), outTok)
			cost = &c
		}
		list = append(list, scored{r, float64(total) + outTok, outTok, lat, cost})
	}
	keyOf := func(s scored) (bool, float64) {
		switch by {
		case "tokens":
			return false, s.tokens
		case "output":
			return false, s.output
		case "cost":
			if s.cost == nil {
				return true, 0
			}
			return false, *s.cost
		case "latency":
			return false, s.lat
		}
		return false, s.tokens
	}
	sort.Slice(list, func(i, j int) bool {
		ni, vi := keyOf(list[i])
		nj, vj := keyOf(list[j])
		if ni != nj {
			return nj // non-nil first
		}
		return vi > vj
	})
	if len(list) > limit {
		list = list[:limit]
	}
	if asJSON {
		var out []map[string]interface{}
		for _, s := range list {
			d := exportDict(s.r)
			if s.cost != nil {
				d["cost_usd"] = *s.cost
			}
			out = append(out, d)
		}
		fmt.Println(marshalNoEscape(map[string]interface{}{
			"since_s": since, "by": by, "rows": out}))
		return
	}
	if by == "cost" && !pricing.Enabled() {
		fmt.Println("hint: pass --price in,cached,out to sort by estimated cost")
	}
	if len(list) == 0 {
		fmt.Printf("cpa-stats top · last %s · no requests in window\n",
			windowLabel(since))
		return
	}
	fmt.Printf("cpa-stats top · last %s · by %s\n", windowLabel(since), by)
	var tab [][]string
	headers := []string{"time", "model", "endpoint", "account", "session",
		"in", "hit", "out", "TTFT", "lat", "TPS"}
	if pricing.Enabled() {
		headers = append(headers, "cost")
	}
	for _, s := range list {
		r := s.r
		total, _, read, _ := tokenParts(r)
		hit := "—"
		if total > 0 {
			hit = fmt.Sprintf("%.0f%%", float64(read)/float64(total)*100)
		}
		ts, _ := parseFloat(r["ts"])
		ep := strv(r["endpoint"])
		if i := strings.LastIndex(ep, "/"); i >= 0 {
			ep = ep[i+1:]
		}
		sess := strv(r["session_id"])
		if len(sess) > 8 {
			sess = sess[len(sess)-8:]
		}
		if sess == "" {
			sess = "—"
		}
		tps := "—"
		if t := decodeTPS(r["output_tokens"], r["latency_ms"], r["ttft_ms"]); t != nil {
			tps = fmt.Sprintf("%.0f", *t)
		}
		row := []string{
			time.Unix(int64(ts), 0).Format("01-02 15:04:05"),
			shortModel(r), orLabel(ep, "—"),
			orLabel(strv(r["source"]), "—"), sess,
			fmtTok(total), hit, fmtTok(r["output_tokens"]),
			fmtMs(f64(r["ttft_ms"])), fmtMs(f64(r["latency_ms"])), tps,
		}
		if pricing.Enabled() {
			cost := "—"
			if s.cost != nil {
				cost = fmt.Sprintf("$%.3f", *s.cost)
			}
			row = append(row, cost)
		}
		tab = append(tab, row)
	}
	printTable(os.Stdout, headers, tab,
		map[int]bool{5: true, 6: true, 7: true, 8: true, 9: true, 10: true})
}

// ------------------------------------------------------------------ report

func reportLines(db *sql.DB, since float64, f *filters, pricing *Pricing) []string {
	rows := LoadRows(db, nowSecs()-since, f.model, f.session, 0, f.account)
	agg := Aggregate(rows, since, pricing)
	okN := agg.N - agg.Failed
	lines := []string{
		fmt.Sprintf("cpa-stats report · last %s · %s",
			windowLabel(since), time.Now().Format("2006-01-02 15:04")),
	}
	s := fmt.Sprintf("requests  %d · %d ok · %d failed", agg.N, okN, agg.Failed)
	if agg.N > 0 {
		s += fmt.Sprintf(" (%.1f%%)", float64(agg.Failed)/float64(agg.N)*100)
	}
	lines = append(lines, s)
	hit := "—"
	if agg.CacheRatio != nil {
		hit = fmt.Sprintf("%.0f%%", *agg.CacheRatio*100)
	}
	lines = append(lines, fmt.Sprintf(
		"tokens    in %s (new %s · cached %s · hit %s) · out %s",
		fmtTok(agg.InTok), fmtTok(agg.Uncached),
		fmtTok(agg.CacheRead+agg.CacheWrite), hit, fmtTok(agg.OutTok)))
	lines = append(lines, fmt.Sprintf(
		"speed     TTFT p50 %s · lat p50 %s · p95 %s · TPS p50 %s · e2e %s",
		fmtMs(agg.TTFTP50), fmtMs(agg.LatP50), fmtMs(agg.LatP95),
		fmtTps(agg.TPSP50), fmtTps(agg.E2EP50)))
	if agg.CostUSD != nil {
		lines = append(lines, fmt.Sprintf("est. cost $%.2f", *agg.CostUSD))
	}
	models := map[string]int{}
	for _, r := range rows {
		models[shortModel(r)]++
	}
	type mc struct {
		m string
		n int
	}
	var ml []mc
	for m, n := range models {
		ml = append(ml, mc{m, n})
	}
	sort.Slice(ml, func(i, j int) bool { return ml[i].n > ml[j].n })
	if len(ml) > 4 {
		ml = ml[:4]
	}
	if len(ml) > 0 {
		var parts []string
		for _, x := range ml {
			parts = append(parts, fmt.Sprintf("%s %d", x.m, x.n))
		}
		lines = append(lines, "models    "+strings.Join(parts, " · "))
	}
	if agg.Failed > 0 {
		codes := map[string]int{}
		for _, r := range rows {
			if derefI(i64(r["failed"])) == 0 {
				continue
			}
			c := "err"
			if sc := i64(r["status_code"]); sc != nil {
				c = fmt.Sprint(*sc)
			}
			if fc := strv(r["fail_code"]); fc != "" {
				c += "/" + fc
			}
			codes[c]++
		}
		var cl []mc
		for c, n := range codes {
			cl = append(cl, mc{c, n})
		}
		sort.Slice(cl, func(i, j int) bool { return cl[i].n > cl[j].n })
		if len(cl) > 4 {
			cl = cl[:4]
		}
		var parts []string
		for _, x := range cl {
			parts = append(parts, fmt.Sprintf("%s×%d", x.m, x.n))
		}
		lines = append(lines, "errors    "+strings.Join(parts, " · "))
	}
	if snap := loadQuotaSnapshot(db); snap != nil {
		if creds := ParseCredentials(snap); len(creds) > 0 {
			sort.Slice(creds, func(i, j int) bool {
				bi, bj := CredSortKey(&creds[i]), CredSortKey(&creds[j])
				return credLess(bi, bj)
			})
			hist := map[string][]QuotaHistRow{}
			for _, r := range loadQuotaHistory(db, 0, "") {
				hist[r.Name] = append(hist[r.Name], r)
			}
			var bits []string
			for i, c := range creds {
				if i >= 4 {
					break
				}
				b := c.Account
				if c.DailyPct != nil {
					b += fmt.Sprintf(" %s %d%%",
						orLabel(c.DailyLabel, "daily"), *c.DailyPct)
				}
				if c.WeeklyPct != nil {
					b += fmt.Sprintf(" %s %d%%",
						orLabel(c.WeeklyLabel, "weekly"), *c.WeeklyPct)
				}
				if c.Blocked {
					b += " BLOCKED"
				} else if c.Limited {
					b += " limited"
				}
				if fc := QuotaForecast(hist[c.Name], "weekly_pct", 1800, 0.1); fc != nil {
					b += fmt.Sprintf(" (→0%% in %s)",
						fmtCountdown(fc[2]-nowSecs()))
				}
				bits = append(bits, b)
			}
			lines = append(lines, "quota     "+strings.Join(bits, " · "))
		}
	}
	return lines
}

func fmtTps(p *float64) string {
	if p == nil {
		return "—"
	}
	return fmt.Sprintf("%.0f", *p)
}

func cmdReport(g *GlobalFlags, argv []string) {
	var since float64
	var webhook string
	var asJSON bool
	var pricing *Pricing
	f := &filters{}
	fs := newFlagSet("report", g)
	addSince(fs, &since, "24h")
	fs.StringVar(&webhook, "webhook", os.Getenv("CPA_STATS_ALERT_WEBHOOK"),
		"POST the digest to this URL (env CPA_STATS_ALERT_WEBHOOK)")
	fs.BoolVar(&asJSON, "json", false, "")
	addPrice(fs, &pricing)
	addFilters(fs, f)
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	text := strings.Join(reportLines(db, since, f, pricing), "\n")
	if asJSON {
		fmt.Println(marshalNoEscape(map[string]interface{}{"report": text}))
		return
	}
	if webhook != "" {
		if err := sendWebhook(webhook, text, 10); err != nil {
			die("webhook failed: %v", err)
		}
		fmt.Printf("posted report to %s…\n", webhook[:minInt(60, len(webhook))])
		return
	}
	fmt.Println(text)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// ------------------------------------------------------------------- alert

func cmdAlert(g *GlobalFlags, argv []string) {
	var webhook string
	fs := newFlagSet("alert", g)
	fs.StringVar(&webhook, "webhook", "", "POST a test alert to this URL")
	mustParse(fs, argv)
	if webhook != "" {
		if err := sendWebhook(webhook, "cpa-stats: test alert ✅", 10); err != nil {
			die("webhook failed: %v", err)
		}
		fmt.Println("sent test alert")
		return
	}
	db := OpenDBROOrDie(g.DB)
	defer db.Close()
	raw := metaGet(db, "alert_state")
	if raw == "" {
		fmt.Println("no alert state yet (collector writes it once " +
			"--alert-webhook is set and a threshold is crossed)")
		return
	}
	var state map[string]map[string]bool
	if json.Unmarshal([]byte(raw), &state) != nil {
		fmt.Println(raw)
		return
	}
	var names []string
	for n := range state {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		var active []string
		for k, v := range state[n] {
			if v {
				active = append(active, k)
			}
		}
		sort.Strings(active)
		if len(active) == 0 {
			fmt.Printf("%s: clear\n", n)
		} else {
			fmt.Printf("%s: %s\n", n, strings.Join(active, ", "))
		}
	}
}

// ------------------------------------------------------------------- prune

func cmdPrune(g *GlobalFlags, argv []string) {
	var olderStr string
	var vacuum bool
	fs := newFlagSet("prune", g)
	fs.StringVar(&olderStr, "older-than", "", "e.g. 30d (required)")
	fs.BoolVar(&vacuum, "vacuum", false,
		"rebuild the db file to reclaim space (locks the db; omit for routine pruning)")
	mustParse(fs, argv)
	if olderStr == "" {
		die("--older-than is required (e.g. 30d)")
	}
	older, err := parseDuration(olderStr)
	if err != nil {
		die("%v", err)
	}
	db, err := OpenDB(g.DB)
	if err != nil {
		die("open %s: %v", g.DB, err)
	}
	defer db.Close()
	n, err := Prune(db, older)
	if err != nil {
		die("prune failed: %v", err)
	}
	if vacuum {
		if _, err := db.Exec("VACUUM"); err != nil {
			fmt.Fprintf(os.Stderr, "vacuum failed: %v\n", err)
		} else {
			fmt.Println("vacuumed")
		}
	}
	fmt.Printf("deleted %d records older than %s\n", n, windowLabel(older))
}
