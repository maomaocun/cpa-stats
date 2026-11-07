// `cpa-stats trend` — metric trends over time buckets (sparklines).
package main

import (
	"fmt"
	"math"
	"strings"
	"time"
)

var bucketLadder = []float64{60, 120, 300, 600, 900, 1800, 3600, 7200, 10800,
	21600, 43200, 86400}

func pickBucket(windowS float64, target int) (float64, int) {
	if windowS <= 0 {
		windowS = 3600
	}
	for _, b := range bucketLadder {
		if windowS/b <= float64(target) {
			return b, int(math.Max(1, math.Ceil(windowS/b)))
		}
	}
	b := math.Ceil(windowS/float64(target)/86400) * 86400
	return b, int(math.Max(1, math.Ceil(windowS/b)))
}

func bucketAggs(rows []Row, sinceTS, bucketS float64, n int,
	pricing *Pricing) []*Agg {
	buckets := make([][]Row, n)
	for _, r := range rows {
		ts, ok := parseFloat(r["ts"])
		if !ok {
			continue
		}
		idx := int((ts - sinceTS) / bucketS)
		if idx >= 0 && idx < n {
			buckets[idx] = append(buckets[idx], r)
		}
	}
	out := make([]*Agg, n)
	for i, b := range buckets {
		if len(b) > 0 {
			out[i] = Aggregate(b, bucketS, pricing)
		}
	}
	return out
}

func cmdTrend(g *GlobalFlags, argv []string) {
	var since, bucketS float64
	var metricList string
	var asJSON bool
	var pricing *Pricing
	var bucketStr string
	f := &filters{}
	fs := newFlagSet("trend", g)
	addSince(fs, &since, "24h")
	fs.StringVar(&bucketStr, "bucket", "",
		"bucket size, e.g. 15m/1h (default: auto, ~40 buckets)")
	fs.StringVar(&metricList, "metric", "",
		"comma list: req,fail,ttft,lat,tps,e2e,in,out,hit,cost (default all)")
	fs.BoolVar(&asJSON, "json", false, "")
	addPrice(fs, &pricing)
	addFilters(fs, f)
	fs.postParse = append(fs.postParse, func() error {
		if bucketStr == "" {
			return nil
		}
		v, err := parseDuration(bucketStr)
		if err != nil {
			return err
		}
		bucketS = v
		return nil
	})
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()

	now := nowSecs()
	rows := LoadRows(db, now-since, f.model, f.session, 0, f.account)
	var nB int
	if bucketS <= 0 {
		bucketS, nB = pickBucket(since, 40)
	} else {
		nB = int(math.Max(1, math.Ceil(since/bucketS)))
	}
	sinceTS := now - since
	aggs := bucketAggs(rows, sinceTS, bucketS, nB, pricing)

	type metricDef struct {
		name string
		get  func(*Agg) *float64
		fmt  func(*float64) string
	}
	defs := []metricDef{
		{"req", func(a *Agg) *float64 { v := float64(a.N); return &v }, fmtTps},
		{"fail", func(a *Agg) *float64 {
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
		{"in", func(a *Agg) *float64 {
			v := float64(a.InTok)
			return &v
		}, func(p *float64) string { return fmtTok(*p) }},
		{"out", func(a *Agg) *float64 {
			v := float64(a.OutTok)
			return &v
		}, func(p *float64) string { return fmtTok(*p) }},
		{"hit", func(a *Agg) *float64 { return a.CacheRatio },
			func(p *float64) string {
				if p == nil {
					return "—"
				}
				return fmt.Sprintf("%.0f%%", *p*100)
			}},
	}
	if pricing.Enabled() {
		defs = append(defs, metricDef{"cost",
			func(a *Agg) *float64 { return a.CostUSD },
			func(p *float64) string {
				if p == nil {
					return "—"
				}
				return fmt.Sprintf("$%.2f", *p)
			}})
	}
	if metricList != "" {
		want := map[string]bool{}
		for _, m := range strings.Split(metricList, ",") {
			want[strings.TrimSpace(m)] = true
		}
		var kept []metricDef
		for _, d := range defs {
			if want[d.name] {
				kept = append(kept, d)
			}
		}
		defs = kept
	}

	if asJSON {
		out := map[string]interface{}{
			"since_s": since, "bucket_s": bucketS, "buckets": nB,
		}
		for _, d := range defs {
			var series []interface{}
			for _, a := range aggs {
				if a == nil {
					series = append(series, nil)
					continue
				}
				if v := d.get(a); v != nil {
					series = append(series, *v)
				} else {
					series = append(series, nil)
				}
			}
			out[d.name] = series
		}
		fmt.Println(marshalNoEscape(out))
		return
	}

	fmt.Printf("cpa-stats trend · last %s · %d × %s buckets\n",
		windowLabel(since), nB, windowLabel(bucketS))
	for _, d := range defs {
		var vals []float64
		var lastV *float64
		var mn, mx *float64
		for _, a := range aggs {
			if a == nil {
				vals = append(vals, 0)
				continue
			}
			if v := d.get(a); v != nil {
				vals = append(vals, *v)
				lastV = v
				if mn == nil || *v < *mn {
					cp := *v
					mn = &cp
				}
				if mx == nil || *v > *mx {
					cp := *v
					mx = &cp
				}
			} else {
				vals = append(vals, 0)
			}
		}
		line := fmt.Sprintf("%-5s %s  last %s", d.name,
			sparkline(vals), d.fmt(lastV))
		if mn != nil && mx != nil && *mx > 0 {
			line += fmt.Sprintf("  (%s–%s)", d.fmt(mn), d.fmt(mx))
		}
		fmt.Println(line)
	}
	// bucket time axis
	fmt.Println("\nbucket times:")
	var axis []string
	for i := 0; i < nB; i++ {
		t := time.Unix(int64(sinceTS)+int64(float64(i+1)*bucketS), 0)
		axis = append(axis, t.Format("15:04"))
	}
	// compact: print first/mid/last
	if len(axis) > 6 {
		fmt.Printf("%s … %s\n", axis[0], axis[len(axis)-1])
	} else {
		fmt.Println(strings.Join(axis, "  "))
	}
}
