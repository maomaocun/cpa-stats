// `cpa-stats tui` — interactive dashboard (tcell).
//
// Layout
//   header    collector status · window · totals
//   cards     all-time + per-window stat cards
//   body      per-model table  |  right-hand tabs (recent/sessions/trend/
//             heatmap/quota/totals)
//   footer    keybindings
//
// Keys
//   1-7       window 15m / 1h / 6h / 24h / 7d / 30d / all
//   ↑↓/j/k    select a row (mouse wheel scrolls; click selects)
//   ←→/h/l    switch right-hand tabs
//   tab       cycle focus between panels
//   enter     detail modal (request or credential); on models/sessions rows
//             filters everything to that value (again to clear)
//   f         filter (model substring)
//   s         cycle model-table sort (req→in→out→tps→lat)
//   e         export the current window rows to a CSV file in cwd
//   p         pause / resume refresh
//   r         force refresh
//   q         quit
package main

import (
	"database/sql"
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/gdamore/tcell/v2"
)

var tuiWindows = []struct {
	key, label string
	secs       float64 // 0 = all
}{
	{"1", "15m", 15 * 60},
	{"2", "1h", 3600},
	{"3", "6h", 6 * 3600},
	{"4", "24h", 24 * 3600},
	{"5", "7d", 7 * 86400},
	{"6", "30d", 30 * 86400},
	{"7", "all", 0},
}

var tuiTabs = []string{"recent", "sessions", "trend", "heatmap", "quota", "totals"}

// segT is a statusbar segment: text + style.
type segT struct {
	t  string
	st tcell.Style
}
var modelSorts = []string{"req", "in", "out", "tps", "lat"}

type tuiApp struct {
	screen    tcell.Screen
	db        *sql.DB
	pricing   *Pricing
	refreshS  float64

	// state
	winIdx      int
	tabIdx      int
	focusPanel  int // 0=models,1=recent,2=sessions,3=quota
	paused      bool
	modelFilter string
	sessFilter  string
	modelSort   int
	filterInput bool
	filterText  string
	modal       *tuiModal

	// data
	rows     []Row
	creds    []CredInfo
	credsHist map[string][]QuotaHistRow
	hmRows   []Row
	hmTS     float64
}

type tuiModal struct {
	title string
	lines []string
	scroll int
}

func cmdTUI(g *GlobalFlags, argv []string) {
	var refresh float64 = 2.0
	var priceStr string
	fs := newFlagSet("tui", g)
	fs.Float64Var(&refresh, "refresh", 2.0, "")
	fs.Float64Var(&refresh, "r", 2.0, "")
	fs.StringVar(&priceStr, "price", os.Getenv("CPA_STATS_PRICE"), "")
	mustParse(fs, argv)
	if refresh <= 0 {
		die("--refresh must be > 0")
	}
	pricing, err := ParsePricing(priceStr)
	if err != nil {
		die("%v", err)
	}

	db := OpenDBROOrDie(g.DB)
	defer db.Close()

	s, err := tcell.NewScreen()
	if err != nil {
		die("tcell: %v", err)
	}
	if err := s.Init(); err != nil {
		die("tcell init: %v", err)
	}
	defer s.Fini()
	s.EnableMouse()
	s.EnablePaste()

	app := &tuiApp{
		screen: s, db: db, pricing: pricing,
		refreshS: refresh, winIdx: 1,
	}
	app.refreshData()

	ev := make(chan tcell.Event, 64)
	go func() {
		for {
			if e := s.PollEvent(); e != nil {
				ev <- e
			}
		}
	}()
	tick := time.NewTicker(time.Duration(refresh * float64(time.Second)))
	defer tick.Stop()

	for {
		app.draw()
		s.Show()
		select {
		case <-tick.C:
			if !app.paused && app.modal == nil && !app.filterInput {
				app.refreshData()
			}
		case e := <-ev:
			if app.handleEvent(e) {
				return
			}
		}
	}
}

func (a *tuiApp) windowS() float64 { return tuiWindows[a.winIdx].secs }

func (a *tuiApp) refreshData() {
	now := nowSecs()
	win := a.windowS()
	var since float64
	if win > 0 {
		since = now - win
	}
	a.rows = LoadRows(a.db, since, a.modelFilter, a.sessFilter, 0, "")
	if snap := loadQuotaSnapshot(a.db); snap != nil {
		a.creds = ParseCredentials(snap)
	} else {
		a.creds = nil
	}
	a.credsHist = map[string][]QuotaHistRow{}
	for _, r := range loadQuotaHistory(a.db, 0, "") {
		a.credsHist[r.Name] = append(a.credsHist[r.Name], r)
	}
}

func (a *tuiApp) handleEvent(e tcell.Event) bool {
	if a.modal != nil {
		if ke, ok := e.(*tcell.EventKey); ok {
			switch ke.Key() {
			case tcell.KeyEscape, tcell.KeyEnter:
				a.modal = nil
			case tcell.KeyUp:
				if a.modal.scroll > 0 {
					a.modal.scroll--
				}
			case tcell.KeyDown:
				if a.modal.scroll < len(a.modal.lines)-1 {
					a.modal.scroll++
				}
			default:
				if ke.Rune() == 'q' {
					a.modal = nil
				}
			}
		}
		return false
	}
	if a.filterInput {
		if ke, ok := e.(*tcell.EventKey); ok {
			switch ke.Key() {
			case tcell.KeyEscape:
				a.filterInput = false
				a.filterText = ""
				a.modelFilter = ""
				a.refreshData()
			case tcell.KeyEnter:
				a.filterInput = false
				a.modelFilter = a.filterText
				a.refreshData()
			case tcell.KeyBackspace, tcell.KeyBackspace2:
				if len(a.filterText) > 0 {
					a.filterText = a.filterText[:len(a.filterText)-1]
				}
			default:
				if ke.Rune() != 0 {
					a.filterText += string(ke.Rune())
				}
			}
		}
		return false
	}
	switch ev := e.(type) {
	case *tcell.EventKey:
		switch ev.Key() {
		case tcell.KeyEscape:
			if a.modelFilter != "" || a.sessFilter != "" {
				a.modelFilter, a.sessFilter = "", ""
				a.refreshData()
			}
		case tcell.KeyRight:
			a.tabIdx = (a.tabIdx + 1) % len(tuiTabs)
		case tcell.KeyLeft:
			a.tabIdx = (a.tabIdx - 1 + len(tuiTabs)) % len(tuiTabs)
		case tcell.KeyTab:
			a.focusPanel = (a.focusPanel + 1) % 4
		case tcell.KeyEnter:
			a.detailOrToggle()
		case tcell.KeyUp:
			a.moveSel(-1)
		case tcell.KeyDown:
			a.moveSel(1)
		default:
			switch ev.Rune() {
			case 'q':
				return true
			case '1', '2', '3', '4', '5', '6', '7':
				a.winIdx = int(ev.Rune()-'0') - 1
				a.refreshData()
			case 'f':
				a.filterInput = true
				a.filterText = a.modelFilter
			case 's':
				a.modelSort = (a.modelSort + 1) % len(modelSorts)
			case 'e':
				a.exportRows()
			case 'p':
				a.paused = !a.paused
			case 'r':
				a.refreshData()
			case 'j':
				a.moveSel(1)
			case 'k':
				a.moveSel(-1)
			}
		}
	case *tcell.EventResize:
		a.screen.Sync()
	case *tcell.EventMouse:
		// wheel scroll on the focused table
		b := ev.Buttons()
		if b&tcell.WheelUp != 0 {
			a.moveSel(-1)
		}
		if b&tcell.WheelDown != 0 {
			a.moveSel(1)
		}
	}
	return false
}

// ------------------------------------------------------------------ select

var selIdx = map[int]*int{0: new(int), 1: new(int), 2: new(int), 3: new(int)}

func (a *tuiApp) sel() *int { return selIdx[a.focusPanel] }

func (a *tuiApp) moveSel(d int) {
	s := a.sel()
	*s += d
	if *s < 0 {
		*s = 0
	}
	if *s > 500 {
		*s = 500
	}
}

func (a *tuiApp) detailOrToggle() {
	switch a.focusPanel {
	case 0: // models
		if name := a.selectedModel(); name != "" {
			if a.modelFilter == name {
				a.modelFilter = ""
			} else {
				a.modelFilter = name
			}
			a.refreshData()
		}
	case 1: // recent
		if r := a.selectedRow(); r != nil {
			a.modal = &tuiModal{title: shortModel(r) + " · " + strv(r["id"]),
				lines: detailLines(r)}
		}
	case 2: // sessions
		if sid := a.selectedSession(); sid != "" {
			if a.sessFilter == sid {
				a.sessFilter = ""
			} else {
				a.sessFilter = sid
			}
			a.refreshData()
		}
	case 3: // quota
		if c := a.selectedCred(); c != nil {
			a.modal = &tuiModal{title: c.Account + " · " + c.Provider,
				lines: credLines(*c, a.credsHist[c.Name])}
		}
	}
}

func (a *tuiApp) selectedModel() string {
	groups := a.modelGroups()
	i := *a.sel()
	if i < 0 || i >= len(groups) {
		return ""
	}
	return groups[i].name
}

func (a *tuiApp) selectedRow() Row {
	i := *a.sel()
	if i < 0 || i >= len(a.rows) || i >= 200 {
		return nil
	}
	return a.rows[i]
}

func (a *tuiApp) selectedSession() string {
	groups := a.sessionGroups()
	i := *a.sel()
	if i < 0 || i >= len(groups) {
		return ""
	}
	return groups[i].name
}

func (a *tuiApp) selectedCred() *CredInfo {
	i := *a.sel()
	if i < 0 || i >= len(a.creds) {
		return nil
	}
	return &a.creds[i]
}

type tuiGroup struct {
	name string
	rows []Row
}

func (a *tuiApp) modelGroups() []tuiGroup {
	m := map[string][]Row{}
	for _, r := range a.rows {
		m[shortModel(r)] = append(m[shortModel(r)], r)
	}
	var gs []tuiGroup
	for n, rs := range m {
		gs = append(gs, tuiGroup{n, rs})
	}
	keyfns := map[int]func(*Agg) float64{
		0: func(x *Agg) float64 { return float64(x.N) },
		1: func(x *Agg) float64 { return float64(x.InTok) },
		2: func(x *Agg) float64 { return float64(x.OutTok) },
		3: func(x *Agg) float64 {
			if x.TPSP50 != nil {
				return *x.TPSP50
			}
			return -1
		},
		4: func(x *Agg) float64 {
			if x.LatP50 != nil {
				return *x.LatP50
			}
			return -1
		},
	}
	kf := keyfns[a.modelSort]
	sort.Slice(gs, func(i, j int) bool {
		return kf(Aggregate(gs[i].rows, a.windowS(), a.pricing)) >
			kf(Aggregate(gs[j].rows, a.windowS(), a.pricing))
	})
	return gs
}

func (a *tuiApp) sessionGroups() []tuiGroup {
	m := map[string][]Row{}
	for _, r := range a.rows {
		sid := orLabel(strv(r["session_id"]), "—")
		m[sid] = append(m[sid], r)
	}
	var gs []tuiGroup
	for n, rs := range m {
		gs = append(gs, tuiGroup{n, rs})
	}
	sort.Slice(gs, func(i, j int) bool {
		var ti, tj float64
		for _, r := range gs[i].rows {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			ti += float64(t) + o
		}
		for _, r := range gs[j].rows {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			tj += float64(t) + o
		}
		return ti > tj
	})
	if len(gs) > 30 {
		gs = gs[:30]
	}
	return gs
}

func (a *tuiApp) exportRows() {
	if len(a.rows) == 0 {
		return
	}
	name := fmt.Sprintf("cpa-stats-%s-%d.csv",
		tuiWindows[a.winIdx].label, int64(nowSecs()))
	path, _ := filepath.Abs(name)
	fh, err := os.Create(path)
	if err != nil {
		return
	}
	defer fh.Close()
	w := csv.NewWriter(fh)
	var fields []string
	for _, c := range Columns {
		if c != "raw" {
			fields = append(fields, c)
		}
	}
	fields = append(fields, "prompt_tokens_total", "uncached_tokens",
		"cache_hit_ratio", "decode_tps", "e2e_tps", "iso_time")
	w.Write(fields)
	for i := len(a.rows) - 1; i >= 0; i-- {
		w.Write(exportRecord(a.rows[i], fields))
	}
	w.Flush()
}

// -------------------------------------------------------------------- draw

func (a *tuiApp) draw() {
	s := a.screen
	sw, sh := s.Size()
	s.Clear()
	y := 0

	// statusbar
	status := collectorStatus(a.db)
	left := fmt.Sprintf(" cpa-stats   window %s",
		tuiWindows[a.winIdx].label)
	put(s, 0, y, stCyanB, left)
	var segs []segT
	collSt := stRed
	if strings.Contains(status, "live") {
		collSt = stGreen
	}
	segs = append(segs, segT{status, collSt})
	if upd := updateNotice(a.db); upd != "" {
		segs = append(segs, segT{"  ↑ " + upd, stYellow})
	}
	segs = append(segs, quotaChipSegs(a.creds)...)
	if a.modelSort != 0 {
		segs = append(segs, segT{fmt.Sprintf("   sort %s",
			modelSorts[a.modelSort]), stDim})
	}
	if a.sessFilter != "" {
		sf := a.sessFilter
		if len(sf) > 8 {
			sf = sf[len(sf)-8:]
		}
		segs = append(segs, segT{fmt.Sprintf("   sess %s", sf), stPink})
	}
	if a.modelFilter != "" {
		segs = append(segs, segT{fmt.Sprintf("   filter %s",
			a.modelFilter), stPink})
	}
	if a.paused {
		segs = append(segs, segT{"   PAUSED", stYellow})
	}
	totals := DBTotals(a.db, a.pricing)
	segs = append(segs, segT{fmt.Sprintf("   %d stored · %s", totals.N,
		time.Now().Format("15:04:05")), stDim})
	totalW := 0
	for _, sg := range segs {
		totalW += cellWidth(sg.t)
	}
	rx := sw - totalW - 1
	if rx < 0 {
		rx = 0
	}
	for _, sg := range segs {
		rx = put(s, rx, y, sg.st, sg.t)
	}
	y += 2

	// cards
	now := nowSecs()
	win := a.windowS()
	span := win
	if span <= 0 && len(a.rows) > 0 {
		var minT float64
		for _, r := range a.rows {
			if ts, ok := parseFloat(r["ts"]); ok && (minT == 0 || ts < minT) {
				minT = ts
			}
		}
		span = now - minT
		if span < 60 {
			span = 60
		}
	}
	agg := Aggregate(a.rows, win, a.pricing)
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
		{fmt.Sprintf("req · %s", tuiWindows[a.winIdx].label),
			fmt.Sprint(agg.N), succ + rpm + qual},
		{"TTFT p50", fmtMs(agg.TTFTP50), "p95 " + fmtMs(agg.TTFTP95)},
		{"TPS decode/e2e", fmt.Sprintf("%s / %s",
			fmtTps(agg.TPSP50), fmtTps(agg.E2EP50)),
			fmt.Sprintf("p10 %s · p90 %s", fmtTps(agg.TPSP10),
				fmtTps(agg.TPSP90))},
		{"latency p50", fmtMs(agg.LatP50), "p95 " + fmtMs(agg.LatP95)},
		{"input", fmtTok(agg.InTok),
			fmt.Sprintf("%s new · %s cached", fmtTok(agg.Uncached),
				fmtTok(agg.CacheRead+agg.CacheWrite))},
		{"cache", "hit " + fmtPct(agg.CacheRatio),
			fmt.Sprintf("read %s · write %s",
				fmtTok(agg.CacheRead), fmtTok(agg.CacheWrite))},
		{"output", fmtTok(agg.OutTok),
			func() string {
				if agg.Burst > 0 {
					return fmt.Sprintf("%d× burst", agg.Burst)
				}
				return ""
			}()},
	}
	if agg.CostUSD != nil {
		cards = append(cards, [3]string{"cost",
			fmt.Sprintf("$%.2f", *agg.CostUSD),
			"all-time " + fmt.Sprintf("$%.2f", derefF(totals.CostUSD))})
	}
	colW := (sw - 2*(len(cards)-1)) / len(cards)
	for i, c := range cards {
		x := i * (colW + 2)
		putClipped(s, x, y, sw, stDim, c[0])
		putClipped(s, x, y+1, sw, stBold, c[1])
		putClipped(s, x, y+2, sw, stDim, c[2])
	}
	y += 4

	// activity sparkline
	if len(a.rows) > 0 && span > 0 {
		counts := bucketCounts(a.rows, now-span, span/40, 40)
		var tok float64
		for _, r := range a.rows {
			t, _, _, _ := tokenParts(r)
			o, _ := parseFloat(r["output_tokens"])
			tok += float64(t) + o
		}
		peak := 0
		total := 0
		for _, c := range counts {
			total += c
			if c > peak {
				peak = c
			}
		}
		tokH := tok / (span / 3600)
		put(s, 0, y, stCyan, "activity "+sparklineF(counts))
		put(s, 44, y, stDim, fmt.Sprintf("%d req · peak %d/bucket · %s tok/h",
			total, peak, fmtTok(tokH)))
	}
	y += 2

	// body: left models 46%, right tabs 54%
	leftW := sw * 46 / 100
	rightX := leftW + 2
	rightW := sw - rightX
	if rightW < 0 {
		rightW = 0
	}
	bodyH := sh - y - 3
	if bodyH < 0 {
		bodyH = 0
	}

	// models table
	var mrows []tuiRow
	for _, grp := range a.modelGroups() {
		ag := Aggregate(grp.rows, win, a.pricing)
		var mstyles []tcell.Style
		if ag.Failed > 0 {
			mstyles = make([]tcell.Style, 10)
			mstyles[2] = stRed
		}
		mrows = append(mrows, tuiRow{cells: []string{
			grp.name, fmt.Sprint(ag.N), fmt.Sprint(ag.Failed),
			fmtMs(ag.TTFTP50), fmtTps(ag.TPSP50), fmtTps(ag.E2EP50),
			fmtTok(ag.InTok), fmtTok(ag.Uncached), fmtPct(ag.CacheRatio),
			fmtTok(ag.OutTok),
		}, right: []bool{false, true, true, true, true, true, true, true,
			true, true}, styles: mstyles})
	}
	mh := "model"
	if a.focusPanel == 0 {
		mh = "▶ model"
	}
	put(s, 0, y, stBold, mh)
	drawTable(s, 0, y+1, leftW, bodyH-1,
		[]string{"model", "req", "fail", "TTFT p50", "TPS p50", "e2e p50",
			"in", "new", "hit", "out"},
		mrows, selOrZero(a.focusPanel == 0, a.sel()))

	// tab bar
	tx := rightX
	for i, t := range tuiTabs {
		st := stDim
		if i == a.tabIdx {
			st = stBold.Foreground(tcell.ColorTeal)
		}
		tx = put(s, tx, y, st, " "+t+" ")
		tx++
	}
	put(s, tx, y, stDim, "(←→)")
	tabY := y + 2
	tabH := sh - tabY - 2
	if tabH < 0 {
		tabH = 0
	}

	switch tuiTabs[a.tabIdx] {
	case "recent":
		var rows []tuiRow
		for i, r := range a.rows {
			if i >= 200 {
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
			statusSt := stGreen
			if failed {
				status = "✗"
				if sc := strv(r["status_code"]); sc != "" {
					status += " " + sc
				}
				if fc := strv(r["fail_code"]); fc != "" {
					status += " " + fc
				}
				statusSt = stRed
			}
			tpsCell := fmtTps(tps)
			outT, _ := parseFloat(r["output_tokens"])
			latV, _ := parseFloat(r["latency_ms"])
			ttV, _ := parseFloat(r["ttft_ms"])
			if !failed && tps == nil && outT >= minDecodeTokens &&
				latV > 0 && ttV > 0 {
				tpsCell = "burst"
			}
			sess := strv(r["session_id"])
			if len(sess) > 8 {
				sess = sess[len(sess)-8:]
			}
			rstyles := make([]tcell.Style, 13)
			rstyles[12] = statusSt
			rows = append(rows, tuiRow{cells: []string{
				time.Unix(int64(ts), 0).Format("15:04:05"), shortModel(r),
				shortEndpoint(strv(r["endpoint"])), strv(r["effort"]),
				fmtTok(total), hit, fmtTok(r["output_tokens"]),
				fmtMs(f64(r["ttft_ms"])), fmtMs(f64(r["latency_ms"])),
				tpsCell, fmtTps(e2e), sess, status,
			}, right: []bool{false, false, false, false, true, true, true,
				true, true, true, true, false, false}, styles: rstyles})
		}
		drawTable(s, rightX, tabY, rightW, tabH,
			[]string{"time", "model", "endpoint", "effort", "in", "hit",
				"out", "TTFT", "lat", "TPS", "e2e", "sess", ""},
			rows, selOrZero(a.focusPanel == 1, a.sel()))

	case "sessions":
		var rows []tuiRow
		for _, grp := range a.sessionGroups() {
			ag := Aggregate(grp.rows, 0, nil)
			var last float64
			for _, r := range grp.rows {
				if ts, ok := parseFloat(r["ts"]); ok && ts > last {
					last = ts
				}
			}
			var sstyles []tcell.Style
			if ag.Failed > 0 {
				sstyles = make([]tcell.Style, 8)
				sstyles[2] = stRed
			}
			rows = append(rows, tuiRow{cells: []string{
				truncateRunes(grp.name, 16), fmt.Sprint(ag.N),
				fmt.Sprint(ag.Failed), fmtTok(ag.InTok), fmtTok(ag.Uncached),
				fmtTok(ag.OutTok), fmtMs(ag.LatP50),
				time.Unix(int64(last), 0).Format("15:04:05"),
			}, right: []bool{false, true, true, true, true, true, true,
				true}, styles: sstyles})
		}
		drawTable(s, rightX, tabY, rightW, tabH,
			[]string{"session", "req", "fail", "in", "new", "out",
				"lat p50", "last"},
			rows, selOrZero(a.focusPanel == 2, a.sel()))

	case "trend":
		bS, nb := pickBucket(span, 48)
		aggs := bucketAggs(a.rows, now-span, bS, nb, a.pricing)
		put(s, rightX, tabY, stDim,
			fmt.Sprintf("%s buckets · %d active", windowLabel(bS),
				countNonNil(aggs)))
		cy := tabY + 2
		for _, line := range trendLinesText(aggs) {
			if cy >= tabY+tabH {
				break
			}
			put(s, rightX, cy, stDim, line)
			cy++
		}

	case "heatmap":
		var hmRows []Row
		if win <= 0 || win >= 7*86400 {
			hmRows = a.rows
		} else {
			if now-a.hmTS > 30 {
				a.hmRows = LoadRows(a.db, now-7*86400,
					a.modelFilter, a.sessFilter, 0, "")
				a.hmTS = now
			}
			hmRows = a.hmRows
		}
		for i, line := range strings.Split(heatmapText(hmRows), "\n") {
			if i >= tabH {
				break
			}
			if j := strings.Index(line, "✗"); j >= 0 {
				put(s, rightX, tabY+i, stDim, line[:j])
				put(s, rightX+cellWidth(line[:j]), tabY+i, stRed, line[j:])
			} else {
				put(s, rightX, tabY+i, stDim, line)
			}
		}

	case "quota":
		var rows []tuiRow
		creds := append([]CredInfo{}, a.creds...)
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
			resets := fmtReset(c.DailyReset) + "/" + fmtReset(c.WeeklyReset)
			if resets == "—/—" {
				resets = "—"
			}
			var extra []string
			for k, v := range c.Extra {
				extra = append(extra, fmt.Sprintf("%s=%v", k, v))
				if len(extra) >= 8 {
					break
				}
			}
			obs := "—"
			if t, ok := parseISOTS(c.ObservedAt); ok {
				age := now - t
				obs = strings.TrimSuffix(fmtAge(&age), " ago")
			}
			eta := "—"
			for _, k := range []string{"weekly_pct", "daily_pct"} {
				if fc := QuotaForecast(a.credsHist[c.Name], k, 1800, 0.1); fc != nil {
					eta = fmt.Sprintf("%s→0 in %s",
						map[string]string{"weekly_pct": "w",
							"daily_pct": "d"}[k],
						fmtCountdown(fc[2]-now))
					break
				}
			}
			daily := "—"
			if c.DailyPct != nil {
				daily = quotaBar(c.DailyPct)
				if c.DailyLabel != "" && c.DailyLabel != "daily" {
					daily += " " + c.DailyLabel
				}
			}
			weekly := "—"
			if c.WeeklyPct != nil {
				weekly = quotaBar(c.WeeklyPct)
				if c.WeeklyLabel != "" && c.WeeklyLabel != "weekly" {
					weekly += " " + c.WeeklyLabel
				}
			}
			qstyles := make([]tcell.Style, 10)
			qstyles[1] = credStatusStyle(c.Blocked, c.Limited)
			if c.Fail > 0 {
				qstyles[2] = stYellow
			}
			qstyles[4] = pctStyle(c.DailyPct)
			qstyles[5] = pctStyle(c.WeeklyPct)
			rows = append(rows, tuiRow{cells: []string{
				c.Account + orLabel("·"+c.Provider, ""),
				state, fmt.Sprintf("%d/%d", c.OK, c.Fail),
				orLabel(c.Plan, "—"), daily, weekly, obs, resets, eta,
				orLabel(strings.Join(extra, " "), "—"),
			}, styles: qstyles})
		}
		drawTable(s, rightX, tabY, rightW, tabH,
			[]string{"account", "status", "ok/fail", "plan", "daily",
				"weekly", "data", "resets d/w", "0% eta", "signals"},
			rows, selOrZero(a.focusPanel == 3, a.sel()))

	case "totals":
		cy := tabY
		put(s, rightX, cy, stBold, "all time")
		cy += 2
		lines := []string{
			fmt.Sprintf("  requests    %d  (%d ok, %d failed)",
				totals.N, totals.N-totals.Failed, totals.Failed),
			"  since       " + func() string {
				if totals.FirstTS == nil {
					return "—"
				}
				return time.Unix(int64(*totals.FirstTS), 0).
					Format("2006-01-02 15:04:05")
			}(),
			fmt.Sprintf("  input       %s   (%s new · %s cached)",
				fmtTok(totals.InTok), fmtTok(totals.Uncached),
				fmtTok(totals.CacheRead+totals.CacheWrite)),
			fmt.Sprintf("  cache       read %s · write %s · hit %s",
				fmtTok(totals.CacheRead), fmtTok(totals.CacheWrite),
				fmtPct(totals.CacheRatio)),
			"  output      " + fmtTok(totals.OutTok) +
				func() string {
					if totals.ReasoningTok > 0 {
						return "  reasoning " + fmtTok(totals.ReasoningTok)
					}
					return ""
				}(),
		}
		if totals.CostUSD != nil {
			lines = append(lines,
				fmt.Sprintf("  est. cost   $%.2f", *totals.CostUSD))
		}
		qr := totals.QualityRatio()
		qrStr := "—"
		if qr != nil {
			qrStr = fmt.Sprintf("%.0f%%", *qr*100)
		}
		lines = append(lines, fmt.Sprintf("  token data  %s clean (%d/%d)",
			qrStr, totals.NComplete, totals.N))
		for _, l := range lines {
			if cy >= tabY+tabH {
				break
			}
			put(s, rightX, cy, tcell.StyleDefault, l)
			cy++
		}
	}

	// footer / filter input
	fy := sh - 2
	if a.filterInput {
		put(s, 0, fy, stBold, "filter: "+a.filterText+"▌")
	}
	put(s, 0, sh-1, stDim,
		"1-7 window · ←→ tab · tab focus · ↑↓ select · enter detail/filter · "+
			"f filter · s sort · e export · p pause · r refresh · q quit")

	// detail modal overlays everything
	if a.modal != nil {
		mx, my, mw, mh := modalBox(s, a.modal.title, 0.8, 0.7)
		maxScroll := len(a.modal.lines) - mh
		if maxScroll < 0 {
			maxScroll = 0
		}
		if a.modal.scroll > maxScroll {
			a.modal.scroll = maxScroll
		}
		blank := repeats(' ', mw)
		for i := 0; i < mh; i++ {
			put(s, mx, my+i, stDim, blank)
		}
		for i, line := range a.modal.lines[a.modal.scroll:] {
			if i >= mh {
				break
			}
			if len(line) > 26 && strings.TrimSpace(line[:24]) != "" {
				vst := tcell.StyleDefault
				if strings.Contains(line[26:], "✗ FAILED") {
					vst = stRed
				} else if strings.Contains(line[26:], "✓ ok") {
					vst = stGreen
				}
				putClipped(s, mx, my+i, mx+mw, stDim, line[:26])
				putClipped(s, mx+26, my+i, mx+mw, vst, line[26:])
			} else {
				putClipped(s, mx, my+i, mx+mw, stDim, line)
			}
		}
	}
}

func selOrZero(focused bool, sel *int) int {
	if !focused || sel == nil {
		return -1
	}
	return *sel
}

func countNonNil(aggs []*Agg) int {
	n := 0
	for _, a := range aggs {
		if a != nil {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------- helpers

func detailLines(r Row) []string {
	var out []string
	for _, kv := range requestDetailPairs(r) {
		out = append(out, fmt.Sprintf("%24s  %s", kv[0], kv[1]))
	}
	out = append(out, "", "esc / enter to close")
	return out
}

func credLines(c CredInfo, hist []QuotaHistRow) []string {
	var lines []string
	state := c.Status
	if c.Blocked {
		state += " ·blocked"
	}
	if c.Limited {
		state += " ·limited"
	}
	add := func(k, v string) {
		lines = append(lines, fmt.Sprintf("%16s  %s", k, v))
	}
	add("status", state)
	if c.StatusMessage != "" {
		add("message", c.StatusMessage)
	}
	add("requests", fmt.Sprintf("%d ok · %d failed", c.OK, c.Fail))
	now := nowSecs()
	for _, kv := range []struct {
		key, tag string
		pct      *int64
		reset    *float64
	}{
		{"daily_pct", orLabel(c.DailyLabel, "daily"), c.DailyPct, c.DailyReset},
		{"weekly_pct", orLabel(c.WeeklyLabel, "weekly"), c.WeeklyPct, c.WeeklyReset},
	} {
		series := QuotaPctSeries(hist, kv.key)
		if len(series) >= 2 {
			var eta string
			fc := QuotaForecast(hist, kv.key, 1800, 0.1)
			if fc != nil {
				eta = fmt.Sprintf(" −%.0f%%/day → 0%% in %s",
					fc[1], fmtCountdown(fc[2]-now))
			} else {
				eta = " · steady"
			}
			add(kv.tag, fmt.Sprintf("%s  %s%s",
				quotaBar(kv.pct),
				sparkline(series[maxInt(0, len(series)-40):]),
				eta))
			if p := QuotaPace(kv.pct, kv.reset,
				func() *float64 {
					if fc != nil {
						return &fc[1]
					}
					return nil
				}(), now); p != nil {
				pace := fmt.Sprintf("≤%.0f%%/day", p[0])
				if p[1] >= 0 {
					pace += fmt.Sprintf(" · burning %.0f%%/day → %s",
						p[1], map[bool]string{true: "over pace",
							false: "on pace"}[p[2] == 1])
				}
				add("pace", pace)
			}
		}
	}
	if c.RetryAfterTS != nil {
		add("retry-after", fmtReset(c.RetryAfterTS))
	}
	for _, lim := range c.Limits {
		var bits []string
		for _, tag := range []string{"short", "long"} {
			w, _ := lim[tag].(map[string]interface{})
			if w == nil {
				continue
			}
			s_ := tag
			if p := w["pct"]; p != nil {
				if iv, ok := parseFloat(p); ok {
					s_ += fmt.Sprintf(" %.0f%%", iv)
				}
			} else {
				s_ += " —"
			}
			if v := strv(w["window"]); v != "" {
				s_ += "/" + v
			}
			if rs := w["reset"]; rs != nil {
				if rf, ok := parseFloat(rs); ok {
					s_ += " resets " + fmtReset(&rf)
				}
			}
			bits = append(bits, s_)
		}
		if lim["limited"] == true {
			bits = append(bits, "LIMITED")
		}
		add("limit", fmt.Sprintf("%v: %s", lim["label"],
			strings.Join(bits, " · ")))
	}
	if len(c.Credits) > 0 {
		var keys []string
		for k := range c.Credits {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var parts []string
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%s=%v", k, c.Credits[k]))
		}
		add("credits", strings.Join(parts, " "))
	}
	var sigKeys []string
	for k := range c.Signals {
		sigKeys = append(sigKeys, k)
	}
	sort.Strings(sigKeys)
	for _, k := range sigKeys {
		add("signal", fmt.Sprintf("%s = %v", k, c.Signals[k]))
	}
	for _, cd := range c.Cooldowns {
		cdm, ok := cd.(map[string]interface{})
		if !ok {
			continue
		}
		var parts []string
		scope := strv(cdm["scope"])
		mk := strv(cdm["model_key"])
		var bits []string
		var ckeys []string
		for k := range cdm {
			ckeys = append(ckeys, k)
		}
		sort.Strings(ckeys)
		for _, k := range ckeys {
			if v := cdm[k]; v != nil && v != "" {
				bits = append(bits, fmt.Sprintf("%s=%v", k, v))
			}
		}
		line := scope
		if mk != "" {
			line += "/" + mk
		}
		line += "  " + strings.Join(bits, " ")
		parts = append(parts, line)
		for _, p := range parts {
			add("cooldown", p)
		}
	}
	var mqKeys []string
	for k := range c.ModelQuotas {
		mqKeys = append(mqKeys, k)
	}
	sort.Strings(mqKeys)
	for _, mn := range mqKeys {
		q, _ := c.ModelQuotas[mn].(map[string]interface{})
		sig, _ := q["signals"].(map[string]interface{})
		var keys []string
		for k := range sig {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var parts []string
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%s=%v", k, sig[k]))
		}
		rendered := strings.Join(parts, " ")
		if rendered == "" {
			rendered = "—"
		}
		add("model quota", fmt.Sprintf("%s: %s", mn, rendered))
	}
	if c.ObservedAt != "" {
		add("observed", c.ObservedAt)
	}
	lines = append(lines, "", "esc / enter to close")
	return lines
}

// quotaChipSegs: the lowest remaining quota across credentials, colored.
func quotaChipSegs(creds []CredInfo) []segT {
	var segs []segT
	var daily, weekly *int64
	for _, c := range creds {
		if c.DailyPct != nil && (daily == nil || *c.DailyPct < *daily) {
			daily = c.DailyPct
		}
		if c.WeeklyPct != nil && (weekly == nil || *c.WeeklyPct < *weekly) {
			weekly = c.WeeklyPct
		}
	}
	if daily == nil && weekly == nil {
		return nil
	}
	dl, wl := "d", "w"
	for _, c := range creds {
		if c.DailyPct != nil && daily != nil && *c.DailyPct == *daily {
			dl = orLabel(c.DailyLabel, "daily")
			if dl == "daily" {
				dl = "d"
			}
		}
		if c.WeeklyPct != nil && weekly != nil && *c.WeeklyPct == *weekly {
			wl = orLabel(c.WeeklyLabel, "weekly")
			if wl == "weekly" {
				wl = "w"
			}
		}
	}
	segs = append(segs, segT{"   quota", stDim})
	if daily != nil {
		segs = append(segs,
			segT{fmt.Sprintf(" %s%d%%", dl, *daily), pctStyle(daily)})
	}
	if weekly != nil {
		segs = append(segs,
			segT{fmt.Sprintf(" %s%d%%", wl, *weekly), pctStyle(weekly)})
	}
	var blocked, limited bool
	for _, c := range creds {
		blocked = blocked || c.Blocked
		limited = limited || c.Limited
	}
	if blocked {
		segs = append(segs, segT{" blocked", stRed})
	} else if limited {
		segs = append(segs, segT{" limited", stYellow})
	}
	return segs
}

func trendLinesText(aggs []*Agg) []string {
	type def struct {
		name string
		get  func(*Agg) *float64
		fmt  func(*float64) string
	}
	defs := []def{
		{"req", func(a *Agg) *float64 { v := float64(a.N); return &v }, fmtTps},
		{"fail%", func(a *Agg) *float64 {
			if a.N == 0 {
				return nil
			}
			v := float64(a.Failed) / float64(a.N) * 100
			return &v
		}, func(p *float64) string {
			if p == nil {
				return "—"
			}
			return fmt.Sprintf("%.1f%%", *p)
		}},
		{"ttft", func(a *Agg) *float64 { return a.TTFTP50 }, fmtMs},
		{"lat", func(a *Agg) *float64 { return a.LatP50 }, fmtMs},
		{"tps", func(a *Agg) *float64 { return a.TPSP50 }, fmtTps},
		{"e2e", func(a *Agg) *float64 { return a.E2EP50 }, fmtTps},
		{"in", func(a *Agg) *float64 { v := float64(a.InTok); return &v },
			func(p *float64) string { return fmtTok(*p) }},
		{"out", func(a *Agg) *float64 { v := float64(a.OutTok); return &v },
			func(p *float64) string { return fmtTok(*p) }},
		{"hit%", func(a *Agg) *float64 { return a.CacheRatio },
			func(p *float64) string {
				if p == nil {
					return "—"
				}
				return fmt.Sprintf("%.0f%%", *p*100)
			}},
		{"cost", func(a *Agg) *float64 { return a.CostUSD },
			func(p *float64) string {
				if p == nil {
					return "—"
				}
				return fmt.Sprintf("$%.2f", *p)
			}},
	}
	var lines []string
	for _, d := range defs {
		var vals []float64
		var lastV *float64
		any := false
		for _, a := range aggs {
			if a == nil {
				vals = append(vals, 0)
				continue
			}
			if v := d.get(a); v != nil {
				vals = append(vals, *v)
				lastV = v
				any = true
			} else {
				vals = append(vals, 0)
			}
		}
		if !any {
			continue
		}
		lines = append(lines, fmt.Sprintf("%8s  %s  %s", d.name,
			sparkline(vals), d.fmt(lastV)))
	}
	return lines
}

func heatmapText(rows []Row) string {
	if len(rows) == 0 {
		return "no requests in the last 7d"
	}
	type key struct{ y, d, h int }
	grid := map[key]int{}
	fails := map[key]int{}
	dayNames := map[[2]int]string{}
	for _, r := range rows {
		ts, ok := parseFloat(r["ts"])
		if !ok {
			continue
		}
		t := time.Unix(int64(ts), 0)
		k := key{t.Year(), t.YearDay(), t.Hour()}
		dayNames[[2]int{t.Year(), t.YearDay()}] = t.Format("01-02")
		grid[k]++
		if derefI(i64(r["failed"])) == 1 {
			fails[k]++
		}
	}
	if len(grid) == 0 {
		return "no requests in the last 7d"
	}
	hi := 0
	for _, n := range grid {
		if n > hi {
			hi = n
		}
	}
	blocks := []rune(" ▁▂▃▄▅▆▇█")
	var dayKeys [][2]int
	for k := range grid {
		dk := [2]int{k.y, k.d}
		found := false
		for _, d := range dayKeys {
			if d == dk {
				found = true
				break
			}
		}
		if !found {
			dayKeys = append(dayKeys, dk)
		}
	}
	sort.Slice(dayKeys, func(i, j int) bool {
		if dayKeys[i][0] != dayKeys[j][0] {
			return dayKeys[i][0] < dayKeys[j][0]
		}
		return dayKeys[i][1] < dayKeys[j][1]
	})
	if len(dayKeys) > 7 {
		dayKeys = dayKeys[len(dayKeys)-7:]
	}
	lines := []string{"        0     6     12    18    hour →"}
	for _, dk := range dayKeys {
		var cells []string
		total, failsN := 0, 0
		for h := 0; h < 24; h++ {
			n := grid[key{dk[0], dk[1], h}]
			f := fails[key{dk[0], dk[1], h}]
			total += n
			failsN += f
			idx := 0
			if n > 0 {
				idx = int(float64(n) / float64(hi) * float64(len(blocks)-1))
				if idx > len(blocks)-1 {
					idx = len(blocks) - 1
				}
			}
			cells = append(cells, string(blocks[idx]))
		}
		date := dayNames[dk]
		line := fmt.Sprintf("%s  %s  %d", date, strings.Join(cells, ""), total)
		if failsN > 0 {
			line += fmt.Sprintf("  %d✗", failsN)
		}
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n")
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
