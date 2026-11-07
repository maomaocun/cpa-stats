// Credential quota normalization + burn forecast (mirrors model.py quota part).
package main

import (
	"encoding/json"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// CredInfo mirrors model.CredInfo — one managed credential snapshot.
type CredInfo struct {
	Name          string                   `json:"name"`
	Provider      string                   `json:"provider"`
	Account       string                   `json:"account"`
	Status        string                   `json:"status"`
	StatusMessage string                   `json:"status_message"`
	Disabled      bool                     `json:"disabled"`
	Blocked       bool                     `json:"blocked"`
	OK            int64                    `json:"ok"`
	Fail          int64                    `json:"fail"`
	Plan          string                   `json:"plan"`
	DailyPct      *int64                   `json:"daily_pct"`
	WeeklyPct     *int64                   `json:"weekly_pct"`
	DailyLabel    string                   `json:"daily_label"`
	WeeklyLabel   string                   `json:"weekly_label"`
	DailyReset    *float64                 `json:"daily_reset"`
	WeeklyReset   *float64                 `json:"weekly_reset"`
	PlanStart     *float64                 `json:"plan_start"`
	PlanEnd       *float64                 `json:"plan_end"`
	Limited       bool                     `json:"limited"`
	RetryAfterTS  *float64                 `json:"retry_after_ts"`
	Credits       map[string]interface{}   `json:"credits"`
	Limits        []map[string]interface{} `json:"limits"`
	ObservedAt    string                   `json:"observed_at"`
	Instance      string                   `json:"instance"`
	Signals       map[string]interface{}   `json:"signals"`
	Extra         map[string]interface{}   `json:"extra"`
	Cooldowns     []interface{}            `json:"cooldowns"`
	ModelQuotas   map[string]interface{}   `json:"model_quotas"`
}

// DEVIN_QUOTA_KEYS — signal keys CPA writes for Devin credentials.
var devinQuotaKeys = map[string]bool{
	"plan": true, "daily_quota_remaining_percent": true,
	"weekly_quota_remaining_percent": true,
	"daily_quota_reset_at":           true, "weekly_quota_reset_at": true,
	"plan_start": true, "plan_end": true,
}

var codexWinSuffixes = []string{"used-percent", "window-minutes",
	"reset-after-seconds", "reset-at", "over-secondary-limit-percent"}

var codexWinRe = regexp.MustCompile(
	`^(?:(?P<slug>.+)-)?(?P<win>primary|secondary)-(?P<fld>` +
		strings.Join(codexWinSuffixes, "|") + `)$`)

var codexFlagRe = regexp.MustCompile(
	`^(?:(?P<slug>.+)-)?(?P<fld>allowed|limit-reached|limit-name|status)$`)

var claudeStatusKeys = []string{
	"anthropic-ratelimit-unified-status",
	"anthropic-ratelimit-unified-5h-status",
	"anthropic-ratelimit-unified-7d-status",
}

func retryAfterTS(value interface{}, base float64) *float64 {
	if value == nil {
		return nil
	}
	if secs, ok := parseFloat(value); ok {
		if secs < 0 {
			secs = 0
		}
		v := base + secs
		return &v
	}
	// HTTP-date
	if t, err := http.ParseTime(strv(value)); err == nil {
		v := float64(t.UnixNano()) / 1e9
		return &v
	}
	return nil
}

func windowMinutesLabel(minutes interface{}) string {
	m, ok := parseFloat(minutes)
	if !ok || m <= 0 {
		return ""
	}
	im := int64(m)
	if im%1440 == 0 {
		return strconv.FormatInt(im/1440, 10) + "d"
	}
	if im%60 == 0 {
		return strconv.FormatInt(im/60, 10) + "h"
	}
	return strconv.FormatInt(im, 10) + "m"
}

func remainingFromUsed(used interface{}) *int64 {
	u, ok := parseFloat(used)
	if !ok {
		return nil
	}
	v := int64(round(clamp(100-u, 0, 100)))
	return &v
}

func remainingFromUtil(util interface{}) *int64 {
	u, ok := parseFloat(util)
	if !ok {
		return nil
	}
	rem := (1 - u) * 100
	if u > 1.0 {
		rem = 100 - u
	}
	v := int64(round(clamp(rem, 0, 100)))
	return &v
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func round(f float64) float64 {
	if f < 0 {
		return float64(int(f - 0.5))
	}
	return float64(int(f + 0.5))
}

func codexWindowReset(sig map[string]interface{}, prefix string,
	used map[string]bool, base float64) *float64 {
	keyAt := prefix + "reset-at"
	keyAfter := prefix + "reset-after-seconds"
	var t *float64
	if v, ok := sig[keyAt]; ok {
		used[keyAt] = true
		if ts, ok2 := parseEpoch(v); ok2 {
			t = &ts
		}
	}
	if v, ok := sig[keyAfter]; ok {
		used[keyAfter] = true
		if t == nil {
			if secs, ok2 := parseFloat(v); ok2 {
				ts := base + secs
				t = &ts
			}
		}
	}
	return t
}

func normalizeCodex(c *CredInfo, sig map[string]interface{},
	used map[string]bool, base float64) {
	if v, ok := sig["x-codex-plan-type"]; ok {
		used["x-codex-plan-type"] = true
		if c.Plan == "" {
			c.Plan = strv(v)
		}
	}
	for _, win := range []struct {
		name, pctAttr, resetAttr, labelAttr, dflt string
	}{
		{"primary", "daily", "dailyReset", "dailyLabel", "5h"},
		{"secondary", "weekly", "weeklyReset", "weeklyLabel", "7d"},
	} {
		ukey := "x-codex-" + win.name + "-used-percent"
		if v, ok := sig[ukey]; ok {
			used[ukey] = true
			if rem := remainingFromUsed(v); rem != nil {
				if win.name == "primary" {
					c.DailyPct = rem
				} else {
					c.WeeklyPct = rem
				}
			}
		}
		rt := codexWindowReset(sig, "x-codex-"+win.name+"-", used, base)
		if rt != nil {
			if win.name == "primary" {
				c.DailyReset = rt
			} else {
				c.WeeklyReset = rt
			}
		}
		mkey := "x-codex-" + win.name + "-window-minutes"
		label := ""
		if v, ok := sig[mkey]; ok {
			used[mkey] = true
			label = windowMinutesLabel(v)
		}
		if label == "" {
			label = win.dflt
		}
		if win.name == "primary" {
			c.DailyLabel = label
		} else {
			c.WeeklyLabel = label
		}
	}
	for _, key := range []string{"x-codex-active-limit", "x-codex-allowed",
		"x-codex-limit-reached"} {
		if _, ok := sig[key]; ok {
			used[key] = true
		}
	}
	if truthy(sig["x-codex-limit-reached"]) || falsy(sig["x-codex-allowed"]) {
		c.Limited = true
	}
	credits := map[string]interface{}{}
	for k, v := range sig {
		if strings.HasPrefix(k, "x-codex-credits-") {
			used[k] = true
			credits[k[len("x-codex-credits-"):]] = v
		}
	}
	if len(credits) > 0 {
		c.Credits = credits
	}
	// Named extra windows (additional limits, code review, short-name slugs).
	type winEnt struct {
		name, ns string
		windows  map[string]map[string]interface{}
		scalars  map[string]interface{}
	}
	groups := map[string]*winEnt{}
	var order []string
	getEnt := func(name, ns string) *winEnt {
		e, ok := groups[name]
		if !ok {
			e = &winEnt{name: name, ns: ns,
				windows: map[string]map[string]interface{}{},
				scalars: map[string]interface{}{}}
			groups[name] = e
			order = append(order, name)
		}
		return e
	}
	for k, v := range sig {
		if !strings.HasPrefix(k, "x-codex-") || used[k] {
			continue
		}
		body := k[len("x-codex-"):]
		ns, rest := "", body
		switch {
		case strings.HasPrefix(body, "additional-"):
			ns, rest = "additional", body[len("additional-"):]
		case strings.HasPrefix(body, "code-review-"):
			ns, rest = "code review", body[len("code-review-"):]
		case strings.HasPrefix(body, "primary-"), strings.HasPrefix(body, "secondary-"):
			continue // main window handled above
		}
		if m := codexWinRe.FindStringSubmatch(rest); m != nil {
			name := m[1]
			if name == "" {
				name = ns
			}
			if name == "" {
				continue
			}
			e := getEnt(name, ns)
			win, fld := m[2], m[3]
			if e.windows[win] == nil {
				e.windows[win] = map[string]interface{}{}
			}
			e.windows[win][fld] = v
			used[k] = true
			continue
		}
		if m := codexFlagRe.FindStringSubmatch(rest); m != nil {
			name := m[1]
			if name == "" {
				name = ns
			}
			if name == "" {
				continue // bare 'allowed' etc. = main-window scalar
			}
			e := getEnt(name, ns)
			e.scalars[m[2]] = v
			used[k] = true
		}
	}
	c.Limits = nil
	sort.Strings(order)
	for _, name := range order {
		ent := groups[name]
		prefix := "x-codex-" + name
		if ent.ns == "code review" {
			prefix = "x-codex-code-review"
		} else if ent.ns == "additional" {
			prefix = "x-codex-additional-" + name
		}
		item := map[string]interface{}{
			"name":       name,
			"label":      firstNonEmpty(strv(ent.scalars["limit-name"]), name),
			"sig_prefix": prefix,
		}
		if truthy(ent.scalars["limit-reached"]) || falsy(ent.scalars["allowed"]) {
			item["limited"] = true
		}
		for _, wt := range []struct{ win, tag string }{
			{"primary", "short"}, {"secondary", "long"},
		} {
			w := ent.windows[wt.win]
			if w == nil {
				continue
			}
			var rt *float64
			if t, ok := parseEpoch(w["reset-at"]); ok {
				rt = &t
			} else if secs, ok := parseFloat(w["reset-after-seconds"]); ok {
				t := base + secs
				rt = &t
			}
			entry := map[string]interface{}{
				"pct":    remainingFromUsed(w["used-percent"]),
				"window": windowMinutesLabel(w["window-minutes"]),
			}
			if rt != nil {
				entry["reset"] = *rt
			} else {
				entry["reset"] = nil
			}
			if over, ok := parseFloat(w["over-secondary-limit-percent"]); ok {
				entry["over_pct"] = over
			}
			item[wt.tag] = entry
		}
		if item["short"] == nil && item["long"] == nil && item["limited"] == nil {
			continue // limit-name only, no window data — noise
		}
		c.Limits = append(c.Limits, item)
	}
}

func normalizeClaude(c *CredInfo, sig map[string]interface{},
	used map[string]bool, base float64) {
	c.DailyLabel, c.WeeklyLabel = "5h", "7d"
	for _, win := range []struct {
		name, pctAttr, resetAttr string
	}{
		{"5h", "daily", "dailyReset"},
		{"7d", "weekly", "weeklyReset"},
	} {
		ukey := "anthropic-ratelimit-unified-" + win.name + "-utilization"
		if v, ok := sig[ukey]; ok {
			used[ukey] = true
			if rem := remainingFromUtil(v); rem != nil {
				if win.name == "5h" {
					c.DailyPct = rem
				} else {
					c.WeeklyPct = rem
				}
			}
		}
		for _, suffix := range []string{"-" + win.name + "-reset", "-" + win.name + "-status"} {
			key := "anthropic-ratelimit-unified" + suffix
			if _, ok := sig[key]; ok {
				used[key] = true
			}
		}
		rkey := "anthropic-ratelimit-unified-" + win.name + "-reset"
		if t, ok := parseEpoch(sig[rkey]); ok {
			if win.name == "5h" {
				c.DailyReset = &t
			} else {
				c.WeeklyReset = &t
			}
		}
	}
	if v, ok := sig["anthropic-ratelimit-unified-reset"]; ok {
		used["anthropic-ratelimit-unified-reset"] = true
		if t, ok2 := parseEpoch(v); ok2 && c.DailyReset == nil {
			c.DailyReset = &t
		}
	}
	if _, ok := sig["anthropic-ratelimit-unified-status"]; ok {
		used["anthropic-ratelimit-unified-status"] = true
	}
	for _, k := range claudeStatusKeys {
		if strings.ToLower(strings.TrimSpace(strv(sig[k]))) == "rejected" {
			c.Limited = true
		}
	}
}

func normalizeSignals(c *CredInfo, observed *float64) map[string]bool {
	used := map[string]bool{}
	sig := map[string]interface{}{}
	for k, v := range c.Signals {
		sig[strings.ToLower(k)] = v
	}
	base := float64(time.Now().UnixNano()) / 1e9
	if observed != nil {
		base = *observed
	}
	baseF := base
	switch strings.ToLower(c.Provider) {
	case "codex":
		normalizeCodex(c, sig, used, baseF)
	case "claude":
		normalizeClaude(c, sig, used, baseF)
	}
	if v, ok := sig["retry-after"]; ok {
		used["retry-after"] = true
		c.RetryAfterTS = retryAfterTS(v, baseF)
	}
	return used
}

// ParseCredentials normalizes a {files:[...]} snapshot into CredInfo rows.
func ParseCredentials(snap map[string]interface{}) []CredInfo {
	if snap == nil {
		return nil
	}
	var files []interface{}
	if fl, ok := snap["files"].([]interface{}); ok {
		files = fl
	}
	var out []CredInfo
	for _, f := range files {
		fm, ok := f.(map[string]interface{})
		if !ok {
			continue
		}
		quota, _ := fm["quota"].(map[string]interface{})
		signals := map[string]interface{}{}
		if s_, ok := quota["signals"].(map[string]interface{}); ok {
			for k, v := range s_ {
				signals[k] = v
			}
		}
		status := strings.ToLower(firstNonEmpty(strv(fm["status"]), "?"))
		disabled := truthy(fm["disabled"])
		if disabled {
			status = "disabled"
		}
		account := firstNonEmpty(strv(fm["email"]), strv(fm["account"]),
			strv(fm["label"]), strv(fm["name"]), strv(fm["id"]), "?")
		var okN, failN int64
		if v, ok := parseFloat(fm["success"]); ok {
			okN = int64(v)
		}
		if v, ok := parseFloat(fm["failed"]); ok {
			failN = int64(v)
		}
		c := CredInfo{
			Name:          firstNonEmpty(strv(fm["name"]), strv(fm["id"]), "?"),
			Provider:      firstNonEmpty(strv(fm["provider"]), strv(fm["type"])),
			Account:       account,
			Status:        status,
			StatusMessage: strv(fm["status_message"]),
			Disabled:      disabled,
			Blocked:       truthy(fm["quota_blocked"]) || truthy(fm["unavailable"]),
			OK:            okN,
			Fail:          failN,
			Plan:          strv(signals["plan"]),
			DailyPct:      parsePct(signals["daily_quota_remaining_percent"]),
			WeeklyPct:     parsePct(signals["weekly_quota_remaining_percent"]),
			DailyLabel:    "daily", WeeklyLabel: "weekly",
			ObservedAt: strv(quota["observed_at"]),
			Signals:    signals,
		}
		if t, ok := parseISOTS(signals["daily_quota_reset_at"]); ok {
			c.DailyReset = &t
		}
		if t, ok := parseISOTS(signals["weekly_quota_reset_at"]); ok {
			c.WeeklyReset = &t
		}
		if t, ok := parseISOTS(signals["plan_start"]); ok {
			c.PlanStart = &t
		}
		if t, ok := parseISOTS(signals["plan_end"]); ok {
			c.PlanEnd = &t
		}
		if cl, ok := fm["cooldowns"].([]interface{}); ok {
			c.Cooldowns = cl
		} else {
			c.Cooldowns = []interface{}{}
		}
		if mq, ok := fm["model_quotas"].(map[string]interface{}); ok {
			c.ModelQuotas = mq
		} else {
			c.ModelQuotas = map[string]interface{}{}
		}
		instance := firstNonEmpty(strv(fm["_instance"]), strv(fm["instance"]))
		if instance != "" {
			c.Instance = instance
			c.Name = c.Name + "@" + instance // unique history names
		}
		var observed *float64
		if t, ok := parseISOTS(c.ObservedAt); ok {
			observed = &t
		}
		used := normalizeSignals(&c, observed)
		extra := map[string]interface{}{}
		for k, v := range signals {
			lk := strings.ToLower(k)
			if !devinQuotaKeys[lk] && !used[lk] {
				extra[lk] = v
			}
		}
		c.Extra = extra
		out = append(out, c)
	}
	return out
}

// CredSortKey orders worst-first for quota tables.
func CredSortKey(c *CredInfo) [7]interface{} {
	var wk, dly int64
	if c.WeeklyPct != nil {
		wk = *c.WeeklyPct
	}
	if c.DailyPct != nil {
		dly = *c.DailyPct
	}
	return [7]interface{}{!c.Blocked, !c.Limited, c.WeeklyPct == nil, wk,
		c.DailyPct == nil, dly, c.Account}
}

// QuotaForecast mirrors model.quota_forecast: burn rate + ETA for one history
// column. Returns (currentPct, dropPerDay, etaTS) or nil.
func QuotaForecast(rows []QuotaHistRow, key string,
	minSpanS, maxStepRate float64) *[3]float64 {
	type pt struct{ t, p float64 }
	var pts []pt
	for _, r := range rows {
		var v *int64
		switch key {
		case "weekly_pct":
			v = r.WeeklyPct
		case "daily_pct":
			v = r.DailyPct
		}
		if r.TS != 0 && v != nil {
			pts = append(pts, pt{r.TS, float64(*v)})
		}
	}
	for i := 1; i < len(pts); i++ { // insertion-sort by ts
		for j := i; j > 0 && pts[j].t < pts[j-1].t; j-- {
			pts[j], pts[j-1] = pts[j-1], pts[j]
		}
	}
	if len(pts) < 3 {
		return nil
	}
	end := len(pts) - 1
	start := end
	for start > 0 {
		tPrev, pPrev := pts[start-1].t, pts[start-1].p
		tCur, pCur := pts[start].t, pts[start].p
		if pPrev < pCur {
			break // window reset / refill
		}
		step := pPrev - pCur
		dt := tCur - tPrev
		if dt < 1 {
			dt = 1
		}
		limit := 3.0
		if x := maxStepRate * dt; x > limit {
			limit = x
		}
		if step > limit {
			break // corrected watermark, not burn
		}
		start--
	}
	run := pts[start:]
	if len(run) < 3 {
		return nil
	}
	t0, p0 := run[0].t, run[0].p
	t1, p1 := run[len(run)-1].t, run[len(run)-1].p
	if t1-t0 < minSpanS || p1 >= p0 {
		return nil
	}
	n := float64(len(run))
	var sx, sy, sxx, sxy float64
	for _, p := range run {
		sx += p.t
		sy += p.p
		sxx += p.t * p.t
		sxy += p.t * p.p
	}
	denom := n*sxx - sx*sx
	if denom <= 0 {
		return nil
	}
	slope := (n*sxy - sx*sy) / denom // pct per second, negative = draining
	if slope >= 0 {
		return nil
	}
	return &[3]float64{p1, -slope * 86400, t1 + p1/(-slope)}
}

// QuotaPctSeries: pct values over time for sparklines (None-safe).
func QuotaPctSeries(rows []QuotaHistRow, key string) []float64 {
	var out []float64
	for _, r := range rows {
		var v *int64
		switch key {
		case "weekly_pct":
			v = r.WeeklyPct
		case "daily_pct":
			v = r.DailyPct
		}
		if v != nil {
			out = append(out, float64(*v))
		}
	}
	return out
}

// SignalPctSeries: remaining-% series for a codex used-percent key read back
// from the raw signals JSON stored in quota_history (case-insensitive).
func SignalPctSeries(rows []QuotaHistRow, sigKey string) []float64 {
	var out []float64
	want := strings.ToLower(sigKey)
	for _, r := range rows {
		var sig map[string]interface{}
		if json.Unmarshal([]byte(r.Signals), &sig) != nil || sig == nil {
			continue
		}
		v, ok := sig[sigKey]
		if !ok || v == nil {
			for k, vv := range sig {
				if strings.ToLower(k) == want {
					v = vv
					ok = true
					break
				}
			}
		}
		if !ok {
			continue
		}
		if rem := remainingFromUsed(v); rem != nil {
			out = append(out, float64(*rem))
		}
	}
	return out
}

// QuotaPace: (budget %/day, burn %/day, overPace) — the %/day drain landing on
// 0 exactly at the window reset vs current burn. nil when unknown.
func QuotaPace(pct *int64, resetTS *float64, burnPerDay *float64,
	now float64) *[3]float64 {
	if pct == nil || resetTS == nil || *resetTS <= now {
		return nil
	}
	budget := float64(*pct) * 86400 / (*resetTS - now)
	over := burnPerDay != nil && *burnPerDay > budget*1.05
	var burn float64
	if burnPerDay != nil {
		burn = *burnPerDay
	} else {
		burn = -1 // sentinel: unknown
	}
	if over {
		return &[3]float64{budget, burn, 1}
	}
	return &[3]float64{budget, burn, 0}
}
