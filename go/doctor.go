// `cpa-stats doctor` — check CPA, collector and database (mirrors cli.py).
package main

import (
	"fmt"
	"os"
	"regexp"
	"strings"
	"time"
)

func cmdDoctor(g *GlobalFlags, argv []string) {
	fs := newFlagSet("doctor", g)
	mustParse(fs, argv)

	ok := true
	line := func(good bool, msg string) {
		if !good {
			ok = false
		}
		mark := "\033[32m✓\033[0m "
		if !good {
			mark = "\033[31m✗\033[0m "
		}
		fmt.Println(mark + msg)
	}

	cfgs, cfgErr := loadCPAConfigsE(g)
	if cfgErr != nil || len(cfgs) == 0 {
		line(false, "cannot load CPA config")
		os.Exit(1)
	}
	more := ""
	if len(cfgs) > 1 {
		more = fmt.Sprintf(" (+%d more)", len(cfgs)-1)
	}
	line(true, fmt.Sprintf("CPA endpoint %s%s", cfgs[0].URL, more))

	nowF := float64(time.Now().UnixNano()) / 1e9
	for _, cfg := range cfgs {
		var body interface{}
		var lastCode int
		var lastErr error
		for _, path := range configPaths {
			data, err := mgmtGet(cfg, path, 5)
			if err == nil {
				body = data
				break
			}
			lastErr = err
			if he, isHE := err.(*HTTPError); isHE {
				lastCode = he.Code
				if he.Code != 404 {
					break
				}
			} else {
				break
			}
		}
		if body == nil {
			if lastCode != 0 {
				line(false, fmt.Sprintf("%s: management API returned HTTP %d",
					cfg.URL, lastCode))
			} else {
				line(false, fmt.Sprintf("%s: CPA not reachable: %v",
					cfg.URL, lastErr))
			}
			continue
		}
		line(true, fmt.Sprintf("%s: management API reachable, key accepted",
			cfg.URL))
		if ver, err := mgmtGet(cfg, "/v8/management/server/latest-version", 5); err == nil {
			if m, isM := ver.(map[string]interface{}); isM {
				latest := firstNonEmpty(strv(m["latest-version"]),
					strv(m["latest_version"]))
				if latest != "" {
					line(true, fmt.Sprintf("%s: latest upstream release %s",
						cfg.URL, latest))
				}
			}
		}
		flat := marshalNoEscape(body)
		if m := regexp.MustCompile(`"usage-statistics-enabled":\s*(true|false)`).
			FindStringSubmatch(flat); m != nil {
			line(m[1] == "true",
				fmt.Sprintf("usage-statistics-enabled = %s", m[1]))
		}
		if m := regexp.MustCompile(`"redis-usage-queue-retention-seconds":\s*(\d+)`).
			FindStringSubmatch(flat); m != nil {
			line(true, fmt.Sprintf("usage queue retention = %ss", m[1]))
		}
	}

	if fileExists(g.DB) {
		db, err := OpenDBRO(g.DB)
		if err != nil {
			line(false, fmt.Sprintf("database %s: %v", g.DB, err))
		} else {
			defer db.Close()
			var n int64
			var last interface{}
			_ = db.QueryRow("SELECT COUNT(*) FROM requests").Scan(&n)
			_ = db.QueryRow("SELECT MAX(ts) FROM requests").Scan(&last)
			var age *float64
			if lf, isF := parseFloat(last); isF {
				a := nowF - lf
				age = &a
			}
			line(true, fmt.Sprintf("database %s (%d records, last %s)",
				g.DB, n, fmtAge(age)))
			ver := metaGet(db, "schema_version")
			if ver == "" {
				ver = "pre-v2"
			}
			line(true, fmt.Sprintf("schema version %s", ver))
			snap := loadQuotaSnapshot(db)
			quotaOK := false
			if snap != nil {
				if files, isL := snap["files"].([]interface{}); isL {
					fetched, _ := parseFloat(snap["fetched_at"])
					age := nowF - fetched
					line(age < 3*3600, fmt.Sprintf(
						"quota snapshot: %d credential(s), %s",
						len(files), fmtAge(&age)))
					quotaOK = true
				}
			}
			if !quotaOK {
				line(false, "no quota snapshot yet (restart collect >=2.1; "+
					"--quota-interval)")
			}
			var hbAge *float64
			if hf, isF := parseFloat(metaGet(db, "collector_heartbeat")); isF {
				a := nowF - hf
				hbAge = &a
			}
			line(hbAge != nil && *hbAge < 15,
				fmt.Sprintf("collector heartbeat %s", fmtAge(hbAge)))
			if last := metaGet(db, "collector_last_error"); last != "" {
				msg := last
				if parts := strings.SplitN(last, "|", 2); len(parts) == 2 {
					msg = parts[1]
				}
				line(false, fmt.Sprintf("collector last error: %s", msg))
			}
		}
	} else {
		line(false, fmt.Sprintf(
			"database %s does not exist yet (start the collector)", g.DB))
	}

	func() {
		defer func() { _ = recover() }()
		tag := latestReleaseTag("", 5)
		cur, _ := parseVersion(Version)
		if tag != "" {
			if nv, isV := parseVersion(tag); isV && versionLess(cur, nv) {
				line(false, fmt.Sprintf(
					"cpa-stats %s is behind %s — run `cpa-stats upgrade`",
					Version, tag))
			} else {
				line(true, fmt.Sprintf("cpa-stats %s (latest release)", Version))
			}
		} else {
			line(true, fmt.Sprintf(
				"cpa-stats %s (release check unreachable — offline?)", Version))
		}
	}()

	if !ok {
		os.Exit(1)
	}
}
