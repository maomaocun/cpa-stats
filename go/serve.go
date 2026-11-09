// `cpa-stats serve` — read-only web dashboard + /metrics (views.py serve).
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"
)

type serveFlags struct {
	window  float64
	bind    string
	port    int
	refresh int
	limit   int
	model   string
	session string
	account string
	pricing *Pricing
}

func cmdServe(g *GlobalFlags, argv []string) {
	f := &serveFlags{}
	fs := newFlagSet("serve", g)
	var winStr, priceStr string
	fs.StringVar(&winStr, "window", "1h", "stats window (default 1h)")
	fs.StringVar(&winStr, "w", "1h", "")
	fs.StringVar(&f.bind, "bind", "127.0.0.1", "bind address (default localhost only)")
	fs.IntVar(&f.port, "port", 8399, "")
	fs.IntVar(&f.refresh, "refresh", 5, "page auto-refresh seconds")
	fs.IntVar(&f.refresh, "r", 5, "")
	fs.IntVar(&f.limit, "limit", 30, "recent rows shown (default 30)")
	fs.IntVar(&f.limit, "n", 30, "")
	fs.StringVar(&f.model, "model", "", "filter: substring of model / alias")
	fs.StringVar(&f.model, "m", "", "")
	fs.StringVar(&f.session, "session", "", "filter: session id prefix")
	fs.StringVar(&f.session, "s", "", "")
	fs.StringVar(&f.account, "account", "", "filter: substring of account / auth index")
	fs.StringVar(&f.account, "a", "", "")
	fs.StringVar(&priceStr, "price", os.Getenv("CPA_STATS_PRICE"),
		"USD per 1M tokens IN,CACHED,OUT (env CPA_STATS_PRICE)")
	mustParse(fs, argv)
	var err error
	if f.window, err = parseDuration(winStr); err != nil {
		die("%v", err)
	}
	if f.pricing, err = ParsePricing(priceStr); err != nil {
		die("%v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		text(w, "ok", "text/plain")
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		withDB(g, w, func(db *sql.DB) {
			text(w, metricsText(db, f), "text/plain; version=0.0.4")
		})
	})
	mux.HandleFunc("/api/rows", func(w http.ResponseWriter, r *http.Request) {
		withDB(g, w, func(db *sql.DB) {
			rows := LoadRows(db, nowSecs()-f.window,
				f.model, f.session, 500, f.account)
			var out []map[string]interface{}
			for _, row := range rows {
				d := map[string]interface{}{}
				for _, c := range Columns {
					if c == "raw" {
						continue
					}
					d[c] = row[c]
				}
				out = append(out, d)
			}
			jsonOut(w, out)
		})
	})
	mux.HandleFunc("/api/summary", func(w http.ResponseWriter, r *http.Request) {
		withDB(g, w, func(db *sql.DB) {
			rows := LoadRows(db, nowSecs()-f.window,
				f.model, f.session, 0, f.account)
			agg := Aggregate(rows, f.window, f.pricing)
			jsonOut(w, map[string]interface{}{
				"agg": agg, "quality_ratio": agg.QualityRatio(),
				"window_s": f.window,
				"totals":   DBTotals(db, f.pricing),
			})
		})
	})
	mux.HandleFunc("/api/quota", func(w http.ResponseWriter, r *http.Request) {
		withDB(g, w, func(db *sql.DB) {
			snap := loadQuotaSnapshot(db)
			var fetched interface{}
			if snap != nil {
				fetched = snap["fetched_at"]
			}
			jsonOut(w, map[string]interface{}{
				"fetched_at":  fetched,
				"credentials": ParseCredentials(snap),
			})
		})
	})
	mux.HandleFunc("/api/errors", func(w http.ResponseWriter, r *http.Request) {
		withDB(g, w, func(db *sql.DB) {
			rows := LoadRows(db, nowSecs()-f.window,
				f.model, f.session, 0, f.account)
			type key struct {
				model, code, endpoint string
				status                int64
			}
			groups := map[key]*struct {
				n      int
				lastTS float64
			}{}
			for _, row := range rows {
				if derefI(i64(row["failed"])) == 0 {
					continue
				}
				k := key{
					model:    shortModel(row),
					status:   derefI(i64(row["status_code"])),
					code:     strv(row["fail_code"]),
					endpoint: strv(row["endpoint"]),
				}
				g := groups[k]
				if g == nil {
					g = &struct {
						n      int
						lastTS float64
					}{}
					groups[k] = g
				}
				g.n++
				if ts, ok := parseFloat(row["ts"]); ok && ts > g.lastTS {
					g.lastTS = ts
				}
			}
			type ent struct {
				Model    string  `json:"model"`
				Status   int64   `json:"status"`
				Code     string  `json:"code"`
				Endpoint string  `json:"endpoint"`
				Count    int     `json:"count"`
				LastTS   float64 `json:"last_ts"`
			}
			var out []ent
			for k, v := range groups {
				out = append(out, ent{k.model, k.status, k.code,
					k.endpoint, v.n, v.lastTS})
			}
			sort.Slice(out, func(i, j int) bool { return out[i].Count > out[j].Count })
			jsonOut(w, out)
		})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && r.URL.Path != "/index.html" {
			w.Header().Set("Content-Type", "text/plain")
			w.WriteHeader(404)
			w.Write([]byte("not found"))
			return
		}
		withDB(g, w, func(db *sql.DB) {
			text(w, dashboardHTML(db, f), "text/html; charset=utf-8")
		})
	})

	addr := fmt.Sprintf("%s:%d", f.bind, f.port)
	fmt.Printf("cpa-stats serve: http://%s (refresh %ds · Ctrl-C to quit)\n",
		addr, f.refresh)
	srv := &http.Server{Addr: addr, Handler: mux}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		die("serve: %v", err)
	}
}

func nowSecs() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func withDB(g *GlobalFlags, w http.ResponseWriter, fn func(*sql.DB)) {
	db, err := OpenDBRO(g.DB)
	if err != nil {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(500)
		w.Write([]byte("error: " + err.Error()))
		return
	}
	defer db.Close()
	defer func() {
		if r := recover(); r != nil {
			text(w, fmt.Sprintf("error: %v", r), "text/plain")
		}
	}()
	fn(db)
}

func text(w http.ResponseWriter, body, ctype string) {
	w.Header().Set("Content-Type", ctype)
	w.Write([]byte(body))
}

func jsonOut(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	var b strings.Builder
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
	w.Write([]byte(b.String()))
}

func shortModel(r Row) string {
	name := firstNonEmpty(strv(r["alias"]), strv(r["model"]), "?")
	return strings.TrimPrefix(name, "devin/")
}

func escLabels(v interface{}) string {
	s := strv(v)
	s = strings.ReplaceAll(s, "\\", "\\\\")
	s = strings.ReplaceAll(s, "\"", "\\\"")
	s = strings.ReplaceAll(s, "\n", " ")
	return s
}

func metricLabels(kv ...string) string {
	var parts []string
	for i := 0; i+1 < len(kv); i += 2 {
		parts = append(parts, fmt.Sprintf(`%s="%s"`, kv[i], escLabels(kv[i+1])))
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// metricsText mirrors views.metrics_text (Prometheus exposition).
func metricsText(db *sql.DB, f *serveFlags) string {
	out := []string{
		"# HELP cpa_stats_requests_total Requests stored",
		"# TYPE cpa_stats_requests_total counter",
	}
	tot := DBTotals(db, f.pricing)
	out = append(out,
		fmt.Sprintf("cpa_stats_requests_total %d", tot.N),
		fmt.Sprintf("cpa_stats_request_failures_total %d", tot.Failed))
	for _, kv := range [][2]interface{}{
		{"input", tot.InTok}, {"uncached", tot.Uncached},
		{"cache_read", tot.CacheRead}, {"cache_write", tot.CacheWrite},
		{"output", tot.OutTok}, {"reasoning", tot.ReasoningTok},
	} {
		out = append(out, fmt.Sprintf(
			`cpa_stats_tokens_total{kind="%s"} %d`, kv[0], kv[1]))
	}
	if tot.CacheRatio != nil {
		out = append(out,
			fmt.Sprintf("cpa_stats_cache_hit_ratio %.6f", *tot.CacheRatio))
	}
	var hbAge *float64
	if hf, ok := parseFloat(metaGet(db, "collector_heartbeat")); ok {
		a := nowSecs() - hf
		hbAge = &a
	}
	up := 0
	if hbAge != nil && *hbAge < 15 {
		up = 1
	}
	out = append(out, fmt.Sprintf("cpa_stats_collector_up %d", up))
	if hbAge != nil {
		out = append(out, fmt.Sprintf(
			"cpa_stats_collector_heartbeat_age_seconds %.1f", *hbAge))
	}
	seen := map[string]bool{}
	windows := []int{int(f.window), 3600, 86400, 604800}
	sort.Ints(windows)
	for _, win := range windows {
		tag := windowLabel(float64(win))
		if seen[tag] {
			continue
		}
		seen[tag] = true
		rows := LoadRows(db, nowSecs()-float64(win),
			f.model, f.session, 0, f.account)
		a := Aggregate(rows, float64(win), f.pricing)
		lb := metricLabels("window", tag)
		out = append(out,
			fmt.Sprintf("cpa_stats_window_requests%s %d", lb, a.N),
			fmt.Sprintf("cpa_stats_window_failures%s %d", lb, a.Failed))
		for _, m := range []struct {
			name string
			val  *float64
		}{
			{"ttft_p50_ms", a.TTFTP50}, {"ttft_p95_ms", a.TTFTP95},
			{"latency_p50_ms", a.LatP50}, {"latency_p95_ms", a.LatP95},
			{"tps_p50", a.TPSP50}, {"e2e_tps_p50", a.E2EP50},
		} {
			if m.val != nil {
				out = append(out, fmt.Sprintf(
					"cpa_stats_window_%s%s %.2f", m.name, lb, *m.val))
			}
		}
	}
	if snap := loadQuotaSnapshot(db); snap != nil {
		for _, c := range ParseCredentials(snap) {
			lb := metricLabels("credential", c.Name,
				"account", c.Account, "provider", orLabel(c.Provider, "?"))
			for _, w := range []struct {
				name string
				pct  *int64
			}{
				{orLabel(c.DailyLabel, "daily"), c.DailyPct},
				{orLabel(c.WeeklyLabel, "weekly"), c.WeeklyPct},
			} {
				if w.pct != nil {
					out = append(out, fmt.Sprintf(
						"cpa_stats_quota_remaining_percent%s %d",
						metricLabels("credential", c.Name,
							"account", c.Account, "window", w.name),
						*w.pct))
				}
			}
			out = append(out,
				fmt.Sprintf("cpa_stats_quota_blocked%s %d", lb, b2i(c.Blocked)),
				fmt.Sprintf("cpa_stats_quota_limited%s %d", lb, b2i(c.Limited)))
		}
	}
	return strings.Join(out, "\n") + "\n"
}

// dashboardHTML — a compact self-refreshing status page.
func dashboardHTML(db *sql.DB, f *serveFlags) string {
	rows := LoadRows(db, nowSecs()-f.window, f.model, f.session, 0, f.account)
	agg := Aggregate(rows, f.window, f.pricing)
	tot := DBTotals(db, f.pricing)
	recent := LoadRows(db, nowSecs()-f.window, f.model, f.session,
		f.limit, f.account)
	var b strings.Builder
	e := html.EscapeString
	b.WriteString(`<!doctype html><meta charset="utf-8">`)
	fmt.Fprintf(&b, `<meta http-equiv="refresh" content="%d">`, f.refresh)
	b.WriteString(`<title>cpa-stats</title><style>
body{font:14px -apple-system,monospace;margin:20px;background:#fafafa;color:#222}
h1{font-size:18px} h2{font-size:14px;margin-top:24px;color:#555}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 16px;min-width:110px}
.card .v{font-size:20px;font-weight:600}.card .k{font-size:11px;color:#888}
table{border-collapse:collapse;background:#fff;font-size:12px}
th,td{border:1px solid #ddd;padding:4px 8px;text-align:right;white-space:nowrap}
th{background:#f0f0f0} td.l{text-align:left}
tr.fail td{color:#c00}
.dim{color:#999}
</style>`)
	b.WriteString("<h1>cpa-stats <span class=dim>" + e(windowLabel(f.window)) +
		" window</span></h1>")
	b.WriteString(`<div class="cards">`)
	card := func(k, v string) {
		fmt.Fprintf(&b, `<div class="card"><div class="v">%s</div><div class="k">%s</div></div>`, v, k)
	}
	pctStr := func(p *float64) string {
		if p == nil {
			return "—"
		}
		return fmt.Sprintf("%.0f%%", *p*100)
	}
	card("requests", fmt.Sprint(agg.N))
	card("failed", fmt.Sprint(agg.Failed))
	card("TTFT p50", fmtMs(agg.TTFTP50))
	card("lat p50", fmtMs(agg.LatP50))
	tpsStr := "—"
	if agg.TPSP50 != nil {
		tpsStr = fmt.Sprintf("%.0f t/s", *agg.TPSP50)
	}
	card("TPS p50", tpsStr)
	card("cache hit", pctStr(agg.CacheRatio))
	card("in tok", fmtTok(agg.InTok))
	card("out tok", fmtTok(agg.OutTok))
	if agg.CostUSD != nil {
		card("cost", fmt.Sprintf("$%.2f", *agg.CostUSD))
	}
	card("total stored", fmtTok(tot.N))
	b.WriteString(`</div>`)

	// quota cards
	if snap := loadQuotaSnapshot(db); snap != nil {
		creds := ParseCredentials(snap)
		if len(creds) > 0 {
			b.WriteString("<h2>quota</h2><table><tr><th>credential</th>" +
				"<th>provider</th><th>status</th><th>daily</th><th>weekly</th>" +
				"<th>ok/fail</th></tr>")
			for _, c := range creds {
				pctCell := func(p *int64) string {
					return quotaBar(p)
				}
				fmt.Fprintf(&b,
					"<tr><td class=l>%s</td><td>%s</td><td>%s%s</td><td>%s</td>"+
						"<td>%s</td><td>%d/%d</td></tr>",
					e(c.Name), e(c.Provider), e(c.Status),
					map[bool]string{true: " 🚫", false: ""}[c.Blocked || c.Limited],
					pctCell(c.DailyPct), pctCell(c.WeeklyPct), c.OK, c.Fail)
			}
			b.WriteString("</table>")
		}
	}

	b.WriteString("<h2>recent requests</h2><table><tr><th>time</th><th>model</th>" +
		"<th>ttft</th><th>latency</th><th>in</th><th>out</th><th>hit</th>" +
		"<th>status</th></tr>")
	for _, r := range recent {
		ts, _ := parseFloat(r["ts"])
		t := time.Unix(int64(ts), 0).Format("15:04:05")
		_, _, read, _ := tokenParts(r)
		inp := derefI(i64(r["input_tokens"]))
		hit := "—"
		if inp+read > 0 {
			total, _, _, _ := tokenParts(r)
			if total > 0 {
				hit = fmt.Sprintf("%.0f%%", float64(read)/float64(total)*100)
			}
		}
		failed := derefI(i64(r["failed"])) == 1
		status := "200"
		if sc := i64(r["status_code"]); sc != nil {
			status = fmt.Sprint(*sc)
		} else if failed {
			status = "fail"
		}
		cls := ""
		if failed {
			cls = ` class="fail"`
		}
		fmt.Fprintf(&b,
			"<tr%s><td>%s</td><td class=l>%s</td><td>%s</td><td>%s</td>"+
				"<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>",
			cls, t, e(shortModel(r)),
			fmtMs(f64(r["ttft_ms"])), fmtMs(f64(r["latency_ms"])),
			fmtTok(r["input_tokens"]), fmtTok(r["output_tokens"]),
			hit, e(status))
	}
	b.WriteString("</table>")
	fmt.Fprintf(&b, "<p class=dim>generated %s</p>",
		time.Now().Format("15:04:05"))
	return b.String()
}
