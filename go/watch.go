// `cpa-stats watch` — live dashboard: re-rendered full-screen every refresh.
package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"

	"golang.org/x/term"
)

type watchFlags struct {
	window  float64
	limit   int
	refresh float64
	once    bool
	model, session, account string
	pricing *Pricing
}

func cmdWatch(g *GlobalFlags, argv []string) {
	f := &watchFlags{refresh: 1.0}
	fs := newFlagSet("watch", g)
	var winStr, priceStr string
	fs.StringVar(&winStr, "window", "1h", "stats window (default 1h)")
	fs.StringVar(&winStr, "w", "1h", "")
	fs.IntVar(&f.limit, "limit", 0, "recent rows to show (default: fit terminal)")
	fs.IntVar(&f.limit, "n", 0, "")
	fs.Float64Var(&f.refresh, "refresh", 1.0, "refresh seconds")
	fs.Float64Var(&f.refresh, "r", 1.0, "")
	fs.BoolVar(&f.once, "once", false, "render once and exit")
	fs.StringVar(&f.model, "model", "", "")
	fs.StringVar(&f.model, "m", "", "")
	fs.StringVar(&f.session, "session", "", "")
	fs.StringVar(&f.session, "s", "", "")
	fs.StringVar(&f.account, "account", "", "")
	fs.StringVar(&f.account, "a", "", "")
	fs.StringVar(&priceStr, "price", os.Getenv("CPA_STATS_PRICE"), "")
	mustParse(fs, argv)
	var err error
	if f.window, err = parseDuration(winStr); err != nil {
		die("%v", err)
	}
	if f.pricing, err = ParsePricing(priceStr); err != nil {
		die("%v", err)
	}

	db := OpenDBROOrDie(g.DB)
	defer db.Close()

	if f.once || !term.IsTerminal(int(os.Stdout.Fd())) {
		fmt.Print(dashboardText(db, f))
		return
	}

	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	for {
		fmt.Print("\033[2J\033[H") // clear + home
		fmt.Print(dashboardText(db, f))
		select {
		case <-sigCh:
			fmt.Print("\033[?25h\n") // show cursor
			return
		case <-time.After(time.Duration(f.refresh * float64(time.Second))):
		}
	}
}

// dashboardText mirrors render_dashboard in plain text.
func dashboardText(db *sql.DB, f *watchFlags) string {
	now := nowSecs()
	rows := LoadRows(db, now-f.window, f.model, f.session, 0, f.account)
	agg := Aggregate(rows, f.window, f.pricing)
	totals := DBTotals(db, f.pricing)
	snap := loadQuotaSnapshot(db)
	creds := ParseCredentials(snap)
	hist := map[string][]QuotaHistRow{}
	for _, r := range loadQuotaHistory(db, 0, "") {
		hist[r.Name] = append(hist[r.Name], r)
	}

	var b strings.Builder
	// header
	title := fmt.Sprintf(" cpa-stats   window %s", windowLabel(f.window))
	if f.model != "" {
		title += " · model~" + f.model
	}
	if f.session != "" {
		title += " · session " + f.session
	}
	if f.account != "" {
		title += " · acct~" + f.account
	}
	status := collectorStatus(db)
	b.WriteString(title)
	// right-align status
	w, _, _ := term.GetSize(int(os.Stdout.Fd()))
	if w <= 0 {
		w = 120
	}
	padN := w - cellWidth(title) - cellWidth(stripANSI(status)) - 14
	if padN < 1 {
		padN = 1
	}
	b.WriteString(spaces(padN) + status)
	if upd := updateNotice(db); upd != "" {
		b.WriteString("  ↑ " + upd)
	}
	fmt.Fprintf(&b, "   %d stored · %s\n\n", totals.N,
		time.Now().Format("15:04:05"))

	// totals cards
	b.WriteString(cardLine([][3]string{
		{"all time · requests", fmt.Sprint(totals.N),
			fmt.Sprintf("%s · since %s",
				func() string {
					if totals.N == 0 {
						return "no requests"
					}
					return fmt.Sprintf("%d/%d ok",
						totals.N-totals.Failed, totals.N)
				}(),
				func() string {
					if totals.FirstTS == nil {
						return "—"
					}
					return time.Unix(int64(*totals.FirstTS), 0).
						Format("01-02 15:04")
				}())},
		{"all time · input", fmtTok(totals.InTok),
			fmt.Sprintf("%s new · %s cached",
				fmtTok(totals.Uncached),
				fmtTok(totals.CacheRead+totals.CacheWrite))},
		{"all time · cache",
			"hit " + fmtPct(totals.CacheRatio),
			fmt.Sprintf("read %s · write %s",
				fmtTok(totals.CacheRead), fmtTok(totals.CacheWrite))},
		{"all time · output", fmtTok(totals.OutTok),
			func() string {
				if totals.ReasoningTok > 0 {
					return "reasoning " + fmtTok(totals.ReasoningTok)
				}
				return ""
			}()},
	}, w))
	b.WriteString("\n")

	// per-window cards
	okN := agg.N - agg.Failed
	succ := "no requests"
	if agg.N > 0 {
		succ = fmt.Sprintf("%.1f%% ok", float64(okN)/float64(agg.N)*100)
	}
	rpm := ""
	if agg.RPM != nil {
		rpm = fmt.Sprintf(" · %.1f/min", *agg.RPM)
	}
	qual := ""
	if qr := agg.QualityRatio(); qr != nil && *qr < 1 {
		qual = fmt.Sprintf(" · %.0f%% clean", *qr*100)
	}
	cards := [][3]string{
		{fmt.Sprintf("requests · %s", windowLabel(f.window)),
			fmt.Sprint(agg.N), succ + rpm + qual},
		{"TTFT p50", fmtMs(agg.TTFTP50),
			"p95 " + fmtMs(agg.TTFTP95)},
		{"TPS p50  decode / e2e",
			fmt.Sprintf("%s / %s tok/s", fmtTps(agg.TPSP50), fmtTps(agg.E2EP50)),
			fmt.Sprintf("p10 %s · p90 %s%s", fmtTps(agg.TPSP10),
				fmtTps(agg.TPSP90),
				func() string {
					if agg.Burst > 0 {
						return fmt.Sprintf(" · %d× burst", agg.Burst)
					}
					return ""
				}())},
		{"latency p50", fmtMs(agg.LatP50), "p95 " + fmtMs(agg.LatP95)},
		{"input tokens", fmtTok(agg.InTok),
			fmt.Sprintf("%s new · %s cached", fmtTok(agg.Uncached),
				fmtTok(agg.CacheRead+agg.CacheWrite))},
		{"cache", "hit " + fmtPct(agg.CacheRatio),
			fmt.Sprintf("read %s · write %s",
				fmtTok(agg.CacheRead), fmtTok(agg.CacheWrite))},
		{"output tokens", fmtTok(agg.OutTok),
			func() string {
				if agg.ReasoningTok > 0 {
					return "reasoning " + fmtTok(agg.ReasoningTok)
				}
				return "reasoning not reported"
			}()},
	}
	if agg.CostUSD != nil {
		cards = append(cards, [3]string{"est. cost",
			fmt.Sprintf("$%.2f", *agg.CostUSD), "tokens × price"})
	}
	b.WriteString(cardLine(cards, w))
	b.WriteString("\n")

	// activity sparkline
	if len(rows) > 0 {
		span := f.window
		if span <= 0 {
			var minT float64
			for _, r := range rows {
				if ts, ok := parseFloat(r["ts"]); ok && (minT == 0 || ts < minT) {
					minT = ts
				}
			}
			span = now - minT
			if span < 60 {
				span = 60
			}
		}
		counts := bucketCounts(rows, now-span, span/40, 40)
		var tok float64
		for _, r := range rows {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			tok += float64(t) + o
		}
		tokH := tok / (span / 3600)
		var peak int
		total := 0
		for _, c := range counts {
			total += c
			if c > peak {
				peak = c
			}
		}
		fmt.Fprintf(&b, "activity %s  %d req · peak %d/bucket · %s tok/h\n",
			sparklineF(counts), total, peak, fmtTok(tokH))
	}

	// quota table
	if len(creds) > 0 {
		b.WriteString("\n")
		var tab []tuiRow
		sort.Slice(creds, func(i, j int) bool {
			bi, bj := CredSortKey(&creds[i]), CredSortKey(&creds[j])
			return credLess(bi, bj) ||
				(equalKeys(bi, bj) && bi[6].(string) < bj[6].(string))
		})
		for _, c := range creds {
			state := c.Status
			if c.Blocked {
				state += " ·blocked"
			}
			if c.Limited {
				state += " ·limited"
			}
			pct := func(p *int64, label, dflt string) string {
				if p == nil {
					return "—"
				}
				l := label
				if l == "" || l == dflt {
					l = dflt
				}
				return quotaBar(p) + " " + l
			}
			var eta string
			for _, k := range []string{"weekly_pct", "daily_pct"} {
				if fc := QuotaForecast(hist[c.Name], k, 1800, 0.1); fc != nil {
					eta = fmt.Sprintf("%s→0 in %s",
						map[string]string{"weekly_pct": "w", "daily_pct": "d"}[k],
						fmtCountdown(fc[2]-now))
					break
				}
			}
			tab = append(tab, tuiRow{cells: []string{
				c.Account + orLabel("·"+c.Provider, ""),
				state, fmt.Sprintf("%d/%d", c.OK, c.Fail),
				orLabel(c.Plan, "—"),
				pct(c.DailyPct, c.DailyLabel, "daily"),
				pct(c.WeeklyPct, c.WeeklyLabel, "weekly"),
				orLabel(fmtReset(c.DailyReset)+"/"+fmtReset(c.WeeklyReset), "—"),
				orLabel(eta, "—"),
			}})
		}
		b.WriteString(renderTableText(
			[]string{"account", "status", "ok/fail", "plan",
				"daily", "weekly", "resets d/w", "0% eta"},
			tab))
	} else {
		b.WriteString("\nquota: no credentials snapshot yet — restart " +
			"`cpa-stats collect` to populate (every --quota-interval s)\n")
	}

	// per-model table
	b.WriteString("\nby model\n")
	models := map[string][]Row{}
	for _, r := range rows {
		m := shortModel(r)
		models[m] = append(models[m], r)
	}
	var order []string
	for m := range models {
		order = append(order, m)
	}
	sort.Slice(order, func(i, j int) bool {
		return len(models[order[i]]) > len(models[order[j]])
	})
	var mtab []tuiRow
	for _, name := range order {
		a := Aggregate(models[name], 0, nil)
		mtab = append(mtab, tuiRow{cells: []string{
			name, fmt.Sprint(a.N), fmt.Sprint(a.Failed),
			fmtMs(a.TTFTP50), fmtTps(a.TPSP50), fmtTps(a.E2EP50),
			fmtTok(a.InTok), fmtTok(a.Uncached), fmtPct(a.CacheRatio),
			fmtTok(a.OutTok),
		}, right: []bool{false, true, true, true, true, true, true, true,
			true, true}})
	}
	b.WriteString(renderTableText([]string{"model", "req", "fail", "TTFT p50",
		"TPS p50", "e2e p50", "in", "new", "hit", "out"}, mtab))

	// sessions
	b.WriteString("\ntop sessions\n")
	sessions := map[string][]Row{}
	for _, r := range rows {
		s := orLabel(strv(r["session_id"]), "—")
		sessions[s] = append(sessions[s], r)
	}
	var sorder []string
	for s := range sessions {
		sorder = append(sorder, s)
	}
	sort.Slice(sorder, func(i, j int) bool {
		var ti, tj float64
		for _, r := range sessions[sorder[i]] {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			ti += float64(t) + o
		}
		for _, r := range sessions[sorder[j]] {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			tj += float64(t) + o
		}
		return ti > tj
	})
	var stab []tuiRow
	for i, sid := range sorder {
		if i >= 10 {
			break
		}
		a := Aggregate(sessions[sid], 0, nil)
		var last float64
		for _, r := range sessions[sid] {
			if ts, ok := parseFloat(r["ts"]); ok && ts > last {
				last = ts
			}
		}
		stab = append(stab, tuiRow{cells: []string{
			truncateRunes(sid, 16), fmt.Sprint(a.N), fmt.Sprint(a.Failed),
			fmtTok(a.InTok), fmtTok(a.Uncached), fmtTok(a.OutTok),
			fmtMs(a.LatP50), time.Unix(int64(last), 0).Format("15:04:05"),
		}, right: []bool{false, true, true, true, true, true, true, true}})
	}
	b.WriteString(renderTableText([]string{"session", "req", "fail", "in",
		"new", "out", "lat p50", "last"}, stab))

	// recent requests
	b.WriteString("\nrecent\n")
	var rtab []tuiRow
	limit := f.limit
	if limit <= 0 {
		limit = 20
	}
	for i, r := range rows {
		if i >= limit {
			break
		}
		failed := derefI(i64(r["failed"])) == 1
		ts, _ := parseFloat(r["ts"])
		total, _, read, _ := tokenParts(r)
		hit := "—"
		if total > 0 {
			hit = fmt.Sprintf("%.0f%%", float64(read)/float64(total)*100)
		}
		tps := decodeTPS(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
		var e2e *float64
		if !failed {
			e2e = e2eTPS(r["output_tokens"], r["latency_ms"])
		}
		status := "✓"
		if failed {
			status = "✗"
		}
		tpsCell := fmtTps(tps)
		if !failed && tps == nil && derefI(i64(r["output_tokens"])) >=
			minDecodeTokens && r["latency_ms"] != nil && r["ttft_ms"] != nil {
			tpsCell = "burst"
		}
		sess := strv(r["session_id"])
		if len(sess) > 8 {
			sess = sess[len(sess)-8:]
		}
		rtab = append(rtab, tuiRow{cells: []string{
			time.Unix(int64(ts), 0).Format("15:04:05"), shortModel(r),
			shortEndpoint(strv(r["endpoint"])), strv(r["effort"]),
			fmtTok(total), hit, fmtTok(r["output_tokens"]),
			fmtMs(f64(r["ttft_ms"])), fmtMs(f64(r["latency_ms"])),
			tpsCell, fmtTps(e2e), sess, status,
		}, right: []bool{false, false, false, false, true, true, true, true,
			true, true, true, false, false}})
	}
	b.WriteString(renderTableText([]string{"time", "model", "endpoint",
		"effort", "in", "hit", "out", "TTFT", "lat", "TPS", "e2e", "session",
		""}, rtab))

	b.WriteString("\nTTFT = time to first token · TPS = out/(latency−TTFT) · " +
		"e2e = out/latency · burst = response arrived in one piece · " +
		"in = new+c.read+c.write · hit = c.read/in\nCtrl-C to quit\n")
	return b.String()
}

func truncateRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

func fmtPct(p *float64) string {
	if p == nil {
		return "—"
	}
	return fmt.Sprintf("%.0f%%", *p*100)
}

func fmtReset(ts *float64) string {
	if ts == nil {
		return "—"
	}
	d := *ts - nowSecs()
	if d > 0 {
		return fmtCountdown(d)
	}
	return time.Unix(int64(*ts), 0).Format("01-02 15:04")
}

func collectorStatus(db *sql.DB) string {
	hb := metaGet(db, "collector_heartbeat")
	err := metaGet(db, "collector_last_error")
	var age *float64
	if hf, ok := parseFloat(hb); ok {
		a := nowSecs() - hf
		age = &a
	}
	if age != nil && *age < 15 && err == "" {
		return "● collector live"
	}
	if err != "" {
		msg := err
		if parts := strings.SplitN(err, "|", 2); len(parts) == 2 {
			msg = parts[1]
		}
		return "● collector error: " + truncateRunes(msg, 70)
	}
	if age == nil {
		return "● collector never ran"
	}
	return "● collector stale (" + fmtAge(age) + ")"
}

func updateNotice(db *sql.DB) string {
	tag := metaGet(db, "latest_release")
	if tag == "" {
		return ""
	}
	cur, _ := parseVersion(Version)
	if nv, ok := parseVersion(tag); ok && versionLess(cur, nv) {
		return tag
	}
	return ""
}

func bucketCounts(rows []Row, sinceTS, bucketS float64, buckets int) []int {
	counts := make([]int, buckets)
	for _, r := range rows {
		ts, ok := parseFloat(r["ts"])
		if !ok {
			continue
		}
		idx := int((ts - sinceTS) / bucketS)
		if idx >= 0 && idx < buckets {
			counts[idx]++
		}
	}
	return counts
}

func sparklineF(counts []int) string {
	vals := make([]float64, len(counts))
	for i, c := range counts {
		vals[i] = float64(c)
	}
	return sparkline(vals)
}

// cardLine renders the stat cards as aligned text blocks.
func cardLine(cards [][3]string, width int) string {
	if len(cards) == 0 {
		return ""
	}
	colW := (width - 2*(len(cards)-1)) / len(cards)
	if colW < 14 {
		colW = 14
	}
	var out strings.Builder
	// 3 lines per card: title / value / sub
	for li := 0; li < 3; li++ {
		for i, c := range cards {
			if i > 0 {
				out.WriteString("  ")
			}
			var s string
			switch li {
			case 0:
				s = c[0]
			case 1:
				s = c[1]
			case 2:
				s = c[2]
			}
			s = clip(s, colW)
			out.WriteString(s + spaces(colW-cellWidth(s)))
		}
		out.WriteString("\n")
	}
	return out.String()
}

// renderTableText renders a plain-text table (headers + rows).
func renderTableText(headers []string, rows []tuiRow) string {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = cellWidth(h)
	}
	for _, r := range rows {
		for i, c := range r.cells {
			if i < len(widths) && cellWidth(c) > widths[i] {
				widths[i] = cellWidth(c)
			}
		}
	}
	var b strings.Builder
	var sep []string
	for i, h := range headers {
		b.WriteString(padCell(h, widths[i], rAlign(h, i, rows, true)))
		if i < len(headers)-1 {
			b.WriteString("  ")
		}
		sep = append(sep, strings.Repeat("-", widths[i]))
	}
	b.WriteString("\n" + strings.Join(sep, "  ") + "\n")
	for _, r := range rows {
		for i := range headers {
			c := ""
			if i < len(r.cells) {
				c = r.cells[i]
			}
			right := false
			if i < len(r.right) {
				right = r.right[i]
			}
			b.WriteString(padCell(c, widths[i], right))
			if i < len(headers)-1 {
				b.WriteString("  ")
			}
		}
		b.WriteString("\n")
	}
	return b.String()
}
