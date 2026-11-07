// Webhook alerts: quota transitions + rolling failure spikes (collector.py).
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

type AlertOpts struct {
	Webhook    string
	Below      float64
	EtaHours   float64
	FailPct    float64
	FailWindow float64
	FailMin    int
}

func loadAlertState(db *sql.DB) map[string]map[string]bool {
	state := map[string]map[string]bool{}
	raw := metaGet(db, "alert_state")
	if raw == "" {
		return state
	}
	var m map[string]map[string]bool
	if json.Unmarshal([]byte(raw), &m) == nil && m != nil {
		return m
	}
	return state
}

func saveAlertState(db *sql.DB, state map[string]map[string]bool) {
	metaSet(db, "alert_state", marshalNoEscape(state))
}

func fireAlert(db *sql.DB, webhook string, events []string,
	state map[string]map[string]bool, logf func(string, ...interface{})) {
	if len(events) == 0 {
		return
	}
	saveAlertState(db, state)
	for _, e := range events {
		if err := sendWebhook(webhook, e, 10); err != nil {
			logf("alert webhook failed: %v", err)
		} else {
			rs := []rune(e)
			if len(rs) > 100 {
				rs = rs[:100]
			}
			logf("alert sent: %s", string(rs))
		}
	}
}

func orLabel(l, dflt string) string {
	if l != "" {
		return l
	}
	return dflt
}

// checkQuotaAlerts fires on quota state transitions — once per crossing, not
// every poll. State lives in meta.alert_state.
func checkQuotaAlerts(db *sql.DB, creds []CredInfo, opts *AlertOpts,
	logf func(string, ...interface{})) {
	if opts.Webhook == "" {
		return
	}
	below := opts.Below
	etaS := opts.EtaHours * 3600
	state := loadAlertState(db)
	now := float64(time.Now().UnixNano()) / 1e9
	for _, c := range creds {
		cc := c
		st := state[c.Name]
		if st == nil {
			st = map[string]bool{}
			state[c.Name] = st
		}
		var events []string
		for _, w := range []struct {
			pct  *int64
			win  string
			flag string
		}{
			{cc.DailyPct, orLabel(cc.DailyLabel, "daily"), "low_daily_pct"},
			{cc.WeeklyPct, orLabel(cc.WeeklyLabel, "weekly"), "low_weekly_pct"},
		} {
			if w.pct == nil {
				continue
			}
			p := float64(*w.pct)
			if p < below && !st[w.flag] {
				events = append(events, fmt.Sprintf(
					"⚠ cpa-stats: %s %s quota %d%% (< %g%%)",
					cc.Account, w.win, *w.pct, below))
				st[w.flag] = true
			} else if p >= below && st[w.flag] {
				events = append(events, fmt.Sprintf(
					"✅ cpa-stats: %s %s quota recovered to %d%%",
					cc.Account, w.win, *w.pct))
				st[w.flag] = false
			}
		}
		for _, b := range []struct {
			flag  string
			cond  bool
			label string
		}{
			{"blocked", cc.Blocked, "blocked"},
			{"limited", cc.Limited, "upstream limit reached"},
		} {
			if b.cond && !st[b.flag] {
				events = append(events,
					fmt.Sprintf("🚫 cpa-stats: %s %s", cc.Account, b.label))
				st[b.flag] = true
			} else if !b.cond && st[b.flag] {
				events = append(events, fmt.Sprintf(
					"✅ cpa-stats: %s no longer %s", cc.Account, b.label))
				st[b.flag] = false
			}
		}
		if etaS > 0 {
			hist := loadQuotaHistory(db, 0, cc.Name)
			for _, w := range []struct {
				col, win, flag string
			}{
				{"weekly_pct", orLabel(cc.WeeklyLabel, "weekly"), "eta_weekly_pct"},
				{"daily_pct", orLabel(cc.DailyLabel, "daily"), "eta_daily_pct"},
			} {
				fc := QuotaForecast(hist, w.col, 1800, 0.1)
				soon := fc != nil && fc[2]-now < etaS
				if soon && !st[w.flag] {
					mins := int(fc[2]-now) / 60
					if mins < 0 {
						mins = 0
					}
					events = append(events, fmt.Sprintf(
						"⏳ cpa-stats: %s %s quota → 0%% in %dm",
						cc.Account, w.win, mins))
					st[w.flag] = true
				} else if !soon && st[w.flag] {
					st[w.flag] = false
				}
			}
		}
		fireAlert(db, opts.Webhook, events, state, logf)
	}
}

// RecentReq is one recent request for the rolling fail-spike window.
type RecentReq struct {
	TS     float64
	Failed bool
	Model  string
}

// checkRequestAlerts: rolling failure-rate spike alert over the request stream.
func checkRequestAlerts(db *sql.DB, recent []RecentReq, opts *AlertOpts,
	logf func(string, ...interface{})) {
	if opts.Webhook == "" {
		return
	}
	winS := opts.FailWindow
	minN := opts.FailMin
	limit := opts.FailPct
	now := float64(time.Now().UnixNano()) / 1e9
	for len(recent) > 0 && recent[0].TS < now-winS {
		recent = recent[1:]
	}
	n := len(recent)
	fails := 0
	for _, r := range recent {
		if r.Failed {
			fails++
		}
	}
	var ratio float64
	if n > 0 {
		ratio = float64(fails) / float64(n) * 100
	}
	state := loadAlertState(db)
	st := state["requests"]
	if st == nil {
		st = map[string]bool{}
		state["requests"] = st
	}
	spiking := n >= minN && ratio >= limit
	mins := int(winS / 60)
	if mins < 1 {
		mins = 1
	}
	var events []string
	if spiking && !st["fail_spike"] {
		type mc struct {
			m string
			k int
		}
		counts := map[string]int{}
		for _, r := range recent {
			if r.Failed {
				counts[r.Model]++
			}
		}
		var top []mc
		for m, k := range counts {
			top = append(top, mc{m, k})
		}
		sort.Slice(top, func(i, j int) bool {
			if top[i].k != top[j].k {
				return top[i].k > top[j].k
			}
			return top[i].m < top[j].m
		})
		var tops string
		if len(top) > 0 {
			if len(top) > 3 {
				top = top[:3]
			}
			s := ""
			for _, t := range top {
				if s != "" {
					s += ", "
				}
				s += fmt.Sprintf("%s×%d", t.m, t.k)
			}
			tops = fmt.Sprintf(" (top: %s)", s)
		}
		events = append(events, fmt.Sprintf(
			"🔥 cpa-stats: %.0f%% of %d requests failed in %dm%s",
			ratio, n, mins, tops))
		st["fail_spike"] = true
	} else if !spiking && st["fail_spike"] {
		events = append(events, fmt.Sprintf(
			"✅ cpa-stats: failure rate back to %.0f%% (%d/%d in %dm)",
			ratio, fails, n, mins))
		st["fail_spike"] = false
	}
	fireAlert(db, opts.Webhook, events, state, logf)
}
