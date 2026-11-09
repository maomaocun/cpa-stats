// The collector daemon: drains the CPA usage queue into SQLite.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"time"
)

type collectFlags struct {
	g             *GlobalFlags
	interval      float64
	batch         int
	quotaInterval float64
	quotaRefresh  float64
	retentionDays float64
	alerts        *AlertOpts
	once          bool
	verbose       bool
}

func cmdCollect(g *GlobalFlags, argv []string) {
	f := &collectFlags{g: g, alerts: &AlertOpts{}}
	fs := newFlagSet("collect", g)
	fs.Float64Var(&f.interval, "interval", 2.0, "poll interval seconds")
	fs.IntVar(&f.batch, "batch", 500, "")
	fs.Float64Var(&f.quotaInterval, "quota-interval", 60.0,
		"also snapshot /credentials quota+status every N seconds (0 = off)")
	fs.Float64Var(&f.quotaRefresh, "quota-refresh", 300.0,
		"force-refresh quota-observable credentials whose data is older than N seconds before snapshotting (0 = never refresh)")
	fs.Float64Var(&f.retentionDays, "retention-days", 90,
		"delete records older than this, checked hourly (0 = keep forever)")
	fs.StringVar(&f.alerts.Webhook, "alert-webhook",
		os.Getenv("CPA_STATS_ALERT_WEBHOOK"),
		"POST text alerts to this URL on quota transitions (low %, blocked/limited, recovery, failure spikes; env CPA_STATS_ALERT_WEBHOOK; Lark bot webhooks work)")
	fs.Float64Var(&f.alerts.Below, "alert-below", 20.0,
		"alert when a quota window drops below this % (default 20)")
	fs.Float64Var(&f.alerts.EtaHours, "alert-eta-hours", 0.0,
		"also alert when the burn forecast reaches 0% within N hours (0 = off)")
	fs.Float64Var(&f.alerts.FailPct, "alert-fail-pct", 25.0,
		"alert when the rolling failure share reaches this % (default 25)")
	fs.Float64Var(&f.alerts.FailWindow, "alert-fail-window", 600.0,
		"rolling window seconds for failure-share alerts (default 600)")
	fs.IntVar(&f.alerts.FailMin, "alert-fail-min", 10,
		"minimum requests in the rolling window before failure-share alerts can fire (default 10)")
	fs.BoolVar(&f.once, "once", false, "drain once and exit")
	fs.BoolVar(&f.verbose, "verbose", false, "")
	fs.BoolVar(&f.verbose, "v", false, "")
	mustParse(fs, argv)
	if f.interval <= 0 {
		die("--interval must be > 0")
	}

	cfgs := loadCPAConfigs(g)
	db, err := OpenDB(g.DB)
	if err != nil {
		die("open %s: %v", g.DB, err)
	}
	defer db.Close()
	spool := filepath.Join(filepath.Dir(g.DB), "spool.jsonl")

	var stop atomic.Bool
	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sigCh
		stop.Store(true)
	}()

	logf := func(format string, a ...interface{}) { nowLog(format, a...) }

	// Replay records that were popped but could not be written last time.
	if fh, err := os.Open(spool); err == nil {
		var recs []map[string]interface{}
		sc := bufio.NewScanner(fh)
		sc.Buffer(make([]byte, 1<<20), 16<<20)
		for sc.Scan() {
			line := sc.Bytes()
			if len(line) == 0 {
				continue
			}
			var r map[string]interface{}
			dec := json.NewDecoder(bytes.NewReader(line))
			dec.UseNumber()
			if dec.Decode(&r) == nil {
				recs = append(recs, r)
			}
		}
		fh.Close()
		for _, r := range recs {
			if _, err := InsertRecords(db, []map[string]interface{}{r}); err != nil {
				logf("spool replay insert failed: %v", err)
			}
		}
		os.Remove(spool)
		if len(recs) > 0 {
			logf("replayed %d spooled records", len(recs))
		}
	}

	var urls string
	for i, c := range cfgs {
		if i > 0 {
			urls += ", "
		}
		urls += c.URL
	}
	logf("collector v%s started: %s -> %s (interval %.0fs)",
		Version, urls, g.DB, f.interval)
	metaSet(db, "collector_pid", os.Getpid())
	metaSet(db, "collector_started", float64(time.Now().UnixNano())/1e9)

	backoff := f.interval
	var lastError, quotaErr string
	total := 0
	var lastPrune, lastQuota, lastReleaseCheck float64
	var recent []RecentReq
	multi := len(cfgs) > 1

	for !stop.Load() {
		got := 0
		var errs []string
		for _, cfg := range cfgs {
			for {
				recs, err := fetchQueue(cfg, f.batch, 10)
				if err != nil {
					errs = append(errs, fmt.Sprintf("%s: %s",
						instanceTag(cfg.URL), queueErrMsg(err, "usage queue")))
					break
				}
				if len(recs) == 0 {
					break
				}
				n, ierr := InsertRecords(db, recs)
				if ierr != nil {
					// never lose popped data — spool the batch
					fh, serr := os.OpenFile(spool,
						os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
					if serr == nil {
						for _, r := range recs {
							fh.WriteString(marshalNoEscape(r) + "\n")
						}
						fh.Close()
					}
					errs = append(errs, fmt.Sprintf(
						"%s: db write failed, spooled %d records: %v",
						instanceTag(cfg.URL), len(recs), ierr))
					break
				}
				got += n
				if f.alerts.Webhook != "" {
					nowR := float64(time.Now().UnixNano()) / 1e9
					for _, r := range recs {
						recent = append(recent, RecentReq{
							TS:     nowR,
							Failed: pyBool(r["failed"]) == 1,
							Model: firstNonEmpty(
								strv(r["model"]), strv(r["alias"]), "?"),
						})
					}
				}
				if len(recs) < f.batch {
					break
				}
			}
		}
		total += got
		if got > 0 && f.verbose {
			logf("stored %d records (total %d)", got, total)
		}

		var tag string
		nowF := float64(time.Now().UnixNano()) / 1e9
		if nowF-lastReleaseCheck > 86400 {
			lastReleaseCheck = nowF
			tag = latestReleaseTag("", 5) // failures fine — offline
		}
		func() {
			tx, err := db.Begin()
			if err != nil {
				return
			}
			defer tx.Rollback()
			metaSetTx(tx, "collector_heartbeat", nowF)
			last := ""
			if len(errs) > 0 {
				last = fmt.Sprintf("%v|%s", nowF, errs[0])
			}
			metaSetTx(tx, "collector_last_error", last)
			if tag != "" {
				metaSetTx(tx, "latest_release", tag)
			}
			tx.Commit()
		}()

		if len(errs) > 0 {
			msg := ""
			for i, e := range errs {
				if i > 0 {
					msg += "; "
				}
				msg += e
			}
			if msg != lastError {
				logf("error: %s", msg)
			}
			lastError = msg
			backoff = minF(maxF(backoff*2, f.interval), 30)
		} else {
			if lastError != "" {
				logf("recovered")
				lastError = ""
			}
			backoff = f.interval
		}

		if f.alerts.Webhook != "" {
			func() {
				defer func() {
					if r := recover(); r != nil {
						logf("request alert check failed: %v", r)
					}
				}()
				checkRequestAlerts(db, recent, f.alerts, logf)
			}()
		}

		if f.quotaInterval > 0 && nowF-lastQuota >= f.quotaInterval {
			func() {
				defer func() {
					if r := recover(); r != nil {
						err, _ := r.(error)
						if err == nil {
							err = fmt.Errorf("%v", r)
						}
						msg := queueErrMsg(err, "credentials endpoint")
						if msg != quotaErr {
							logf("quota snapshot error: %s", msg)
						}
						quotaErr = msg
					}
				}()
				now := float64(time.Now().UnixNano()) / 1e9
				var allFiles []interface{}
				var refreshErrs []error
				for _, cfg := range cfgs {
					snap, err := fetchCredentials(cfg, 10)
					if err != nil {
						panic(err)
					}
					if f.quotaRefresh > 0 {
						stale := staleQuotaCredentials(snap, f.quotaRefresh, now)
						if len(stale) > 0 {
							for _, n := range stale {
								if e := refreshCredential(cfg, n, 15); e != nil {
									refreshErrs = append(refreshErrs, e)
								}
							}
							snap, err = fetchCredentials(cfg, 10)
							if err != nil {
								panic(err)
							}
						}
					}
					files, _ := snap["files"].([]interface{})
					for _, ff := range files {
						fm, ok := ff.(map[string]interface{})
						if !ok {
							continue
						}
						if multi {
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
				if len(refreshErrs) > 0 {
					logf("quota refresh: %d credential(s) failed (%v)",
						len(refreshErrs), refreshErrs[0])
				}
				payload := map[string]interface{}{
					"fetched_at": now,
					"files":      allFiles,
				}
				tx, err := db.Begin()
				if err != nil {
					panic(err)
				}
				saveQuotaSnapshotTx(tx, payload)
				creds := ParseCredentials(payload)
				saveQuotaHistory(tx, creds, now)
				if err := tx.Commit(); err != nil {
					panic(err)
				}
				checkQuotaAlerts(db, creds, f.alerts, logf)
				if quotaErr != "" {
					logf("quota snapshot recovered")
					quotaErr = ""
				}
			}()
			lastQuota = float64(time.Now().UnixNano()) / 1e9
		}

		if f.retentionDays > 0 && nowF-lastPrune > 3600 {
			if n, err := Prune(db, f.retentionDays*86400); err != nil {
				logf("prune failed: %v", err)
			} else if n > 0 {
				logf("pruned %d records older than %gd", n, f.retentionDays)
			}
			lastPrune = float64(time.Now().UnixNano()) / 1e9
		}

		if f.once {
			break
		}
		slept := 0.0
		for slept < backoff && !stop.Load() {
			time.Sleep(200 * time.Millisecond)
			slept += 0.2
		}
	}
	logf("collector stopped (stored %d records this run)", total)
}

// queueErrMsg renders an error like the Python collector does.
func queueErrMsg(err error, what string) string {
	if he, ok := err.(*HTTPError); ok {
		msg := fmt.Sprintf("HTTP %d from %s", he.Code, what)
		switch he.Code {
		case 401, 403:
			msg += " (management key rejected)"
		case 404:
			msg += " (management API disabled? set management.secret-key)"
		}
		return msg
	}
	if ue, ok := err.(interface{ Unwrap() error }); ok {
		if inner := ue.Unwrap(); inner != nil {
			return inner.Error()
		}
	}
	return err.Error()
}

func minF(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func maxF(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
