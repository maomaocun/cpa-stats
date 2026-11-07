// `cpa-stats tail` — stream one line per request.
package main

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func shortEndpoint(ep string) string {
	if ep == "" {
		return ""
	}
	if i := strings.LastIndex(ep, " "); i >= 0 {
		ep = ep[i+1:] // drop the HTTP method
	}
	if i := strings.LastIndex(ep, "/"); i >= 0 {
		if t := ep[i+1:]; t != "" {
			return t
		}
	}
	return ep
}

func cmdTail(g *GlobalFlags, argv []string) {
	var since float64
	var refresh float64
	var verbose bool
	f := &filters{}
	fs := newFlagSet("tail", g)
	addSince(fs, &since, "5m")
	fs.Float64Var(&refresh, "refresh", 0.5, "")
	fs.Float64Var(&refresh, "r", 0.5, "")
	fs.BoolVar(&verbose, "verbose", false,
		"print a second line per request: account, endpoint, tiers, "+
			"node/fork/compaction, exec/trace ids, client ip")
	fs.BoolVar(&verbose, "v", false, "")
	addFilters(fs, f)
	mustParse(fs, argv)
	db := OpenDBROOrDie(g.DB)
	defer db.Close()

	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("tailing requests (Ctrl-C to quit)")
	lastTS := nowSecs() - since
	seen := map[string]bool{}
	for {
		rows := LoadRows(db, lastTS-1, f.model, f.session, 0, f.account)
		// oldest first
		for i, j := 0, len(rows)-1; i < j; i, j = i+1, j-1 {
			rows[i], rows[j] = rows[j], rows[i]
		}
		for _, r := range rows {
			id := strv(r["id"])
			if seen[id] {
				continue
			}
			seen[id] = true
			if ts, ok := parseFloat(r["ts"]); ok && ts > lastTS {
				lastTS = ts
			}
			printTailRow(r, verbose)
		}
		select {
		case <-sigCh:
			return
		case <-time.After(time.Duration(refresh * float64(time.Second))):
		}
	}
}

func printTailRow(r Row, verbose bool) {
	ts, _ := parseFloat(r["ts"])
	failed := derefI(i64(r["failed"])) == 1
	var b strings.Builder
	b.WriteString(time.Unix(int64(ts), 0).Format("15:04:05 "))
	if failed {
		b.WriteString("\033[31m✗\033[0m ")
	} else {
		b.WriteString("\033[32m✓\033[0m ")
	}
	b.WriteString(pad(shortModel(r), 18, false))
	ep := shortEndpoint(strv(r["endpoint"]))
	if ep != "" {
		b.WriteString("\033[2m" + pad(ep, 12, false) + "\033[0m")
	}
	resp := strv(r["response_model"])
	if resp != "" && resp != strv(r["model"]) && resp != strv(r["alias"]) {
		b.WriteString("\033[2m→" + resp + " \033[0m")
	}
	b.WriteString("\033[2m" + pad(strv(r["effort"]), 7, false) + "\033[0m")
	total, uncached, read, write := tokenParts(r)
	fmt.Fprintf(&b, " in %6s", fmtTok(total))
	hit := "—"
	if total > 0 {
		hit = fmt.Sprintf("%.0f%%", float64(read)/float64(total)*100)
	}
	fmt.Fprintf(&b, " \033[2m(new %5s · read %5s · write %5s · hit %4s)\033[0m",
		fmtTok(uncached), fmtTok(read), fmtTok(write), hit)
	fmt.Fprintf(&b, "  out %6s", fmtTok(r["output_tokens"]))
	if rt := i64(r["reasoning_tokens"]); rt != nil && *rt > 0 {
		fmt.Fprintf(&b, " (reason %s)", fmtTok(*rt))
	}
	fmt.Fprintf(&b, "  TTFT %7s", fmtMs(f64(r["ttft_ms"])))
	fmt.Fprintf(&b, "  lat %7s", fmtMs(f64(r["latency_ms"])))
	b.WriteString("  TPS ")
	b.WriteString(pad(fmtTps(decodeTPS(r["output_tokens"], r["latency_ms"], r["ttft_ms"])), 4, true))
	var e2e *float64
	if !failed {
		e2e = e2eTPS(r["output_tokens"], r["latency_ms"])
	}
	b.WriteString("  e2e ")
	b.WriteString(pad(fmtTps(e2e), 4, true))
	if sess := strv(r["session_id"]); sess != "" {
		if len(sess) > 8 {
			sess = sess[len(sess)-8:]
		}
		fmt.Fprintf(&b, "  \033[2msess %s\033[0m", sess)
	}
	if failed {
		errStr := strv(r["error"])
		if errStr != "" {
			code := ""
			if fc := strv(r["fail_code"]); fc != "" {
				code = " (" + fc + ")"
			}
			if len([]rune(errStr)) > 120 {
				errStr = string([]rune(errStr)[:120])
			}
			sc := strv(r["status_code"])
			fmt.Fprintf(&b, "  \033[31m%s%s: %s\033[0m", sc, code, errStr)
		}
	}
	fmt.Println(b.String())
	if !verbose {
		return
	}
	var d strings.Builder
	d.WriteString("      \033[2m")
	d.WriteString("acct " + orLabel(strv(r["source"]), "—"))
	d.WriteString(" · auth " + orLabel(strv(r["auth_type"]), "—"))
	d.WriteString(" via " + orLabel(strv(r["access_provider"]), "—"))
	d.WriteString(" · endpoint " + orLabel(strv(r["endpoint"]), "—"))
	var node []string
	if v := strv(r["node_kind"]); v != "" {
		node = append(node, v)
	}
	if derefI(i64(r["is_fork"])) == 1 {
		node = append(node, "fork")
	}
	if derefI(i64(r["is_compaction"])) == 1 {
		node = append(node, "compaction")
	}
	if len(node) > 0 {
		d.WriteString(" · node " + strings.Join(node, "/"))
	}
	if v := strv(r["parent_session_id"]); v != "" {
		d.WriteString(" · parent " + v)
	}
	var tiers []string
	for _, k := range []string{"service_tier", "effective_service_tier",
		"response_service_tier", "upstream_service_tier"} {
		if v := strv(r[k]); v != "" {
			tiers = append(tiers, v)
		}
	}
	if len(tiers) > 0 {
		d.WriteString(" · tier " + strings.Join(tiers, "/"))
	}
	if v := strv(r["trace_id"]); v != "" {
		d.WriteString(" · trace " + v)
	}
	if v := strv(r["client_ip"]); v != "" {
		d.WriteString(" · ip " + v)
	}
	d.WriteString("\033[0m")
	fmt.Println(d.String())
}
