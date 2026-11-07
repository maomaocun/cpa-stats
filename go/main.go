// cpa-stats (Go) — per-request latency / TTFT / TPS / cost monitoring for
// CLIProxyAPI. Single static binary; shares the SQLite db with the Python
// implementation (same schema, interchangeable collectors).
package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
)

// Version is stamped by goreleaser/go build -ldflags "-X main.Version=...".
var Version = "2.5.0"

// flagSetLike wraps flag.FlagSet with post-parse hooks (duration/price parsing).
type flagSetLike struct {
	*flag.FlagSet
	postParse []func() error
}

// newFlagSet creates a subcommand flagset carrying the global flags too, so
// `cpa-stats collect --db x` works the same as `cpa-stats --db x collect`.
func newFlagSet(name string, g *GlobalFlags) *flagSetLike {
	fs := &flagSetLike{FlagSet: flag.NewFlagSet(name, flag.ExitOnError)}
	// defaults carry values already set by the leading global-flag scan —
	// flag.Var assigns the default at registration, so "" would wipe them.
	fs.StringVar(&g.DB, "db", g.DB, "SQLite path")
	fs.StringVar(&g.CPADir, "cpa-dir", g.CPADir,
		"CLIProxyAPI install dir (reads config.yaml and management.key)")
	fs.StringVar(&g.URL, "url", g.URL,
		"CPA base URL(s), comma-separated for multi-instance collect "+
			"(default: from config.yaml)")
	fs.StringVar(&g.MgmtKeyFile, "mgmt-key-file", g.MgmtKeyFile,
		"management key file(s), comma-separated to match --url "+
			"(default: <cpa-dir>/management.key)")
	return fs
}

func mustParse(fs *flagSetLike, argv []string) {
	if err := fs.Parse(argv); err != nil {
		os.Exit(2)
	}
	for _, fn := range fs.postParse {
		if err := fn(); err != nil {
			fmt.Fprintf(os.Stderr, "cpa-stats: %v\n", err)
			os.Exit(2)
		}
	}
}

const usage = `cpa-stats: per-request latency / TTFT / TPS / cost monitoring for CLIProxyAPI.

usage: cpa-stats [--db DB] [--cpa-dir DIR] [--url URL] [--mgmt-key-file F] <cmd> [flags]

resident / ops:
  collect          drain CPA usage queue into the database (daemon)
  serve            read-only web dashboard + /metrics
  install-service  systemd --user (linux) / launchd agent (macOS) for collect
  prune            delete old records (--older-than 30d [--vacuum])
  doctor           check CPA, collector and database
  alert            show alert state / send a test webhook alert
  upgrade          check for a newer release and print/run the upgrade

viewers:
  summary          aggregate report (--by model|effort|session|account|...)
  quota            credential quota/status snapshot
  errors           failure breakdown by model / status / code / endpoint
  top              heaviest requests by tokens / output / cost / latency
  show             full detail for one request (id / request_id / trace_id)
  trend            metric trends over time buckets (sparklines)
  tail             stream one line per request
  sql              run a read-only SQL query on the requests db
  export           export rows as csv / jsonl
  report           compact digest; --webhook posts it (Lark/generic)

not in the Go build (use the Python package for these):
  watch, tui       interactive dashboards

env: CPA_STATS_DB CPA_DIR CPA_URL CPA_MGMT_KEY CPA_STATS_ALERT_WEBHOOK
     CPA_STATS_PRICE CPA_STATS_REPO
`

func main() {
	g := &GlobalFlags{DB: defaultDBPath(), CPADir: os.Getenv("CPA_DIR")}
	if v := os.Getenv("CPA_STATS_DB"); v != "" {
		g.DB = v
	}

	argv := os.Args[1:]
	// global flags may precede the subcommand
	var rest []string
	i := 0
	for i < len(argv) {
		a := argv[i]
		if a == "--version" {
			fmt.Printf("cpa-stats %s\n", Version)
			return
		}
		if a == "-h" || a == "--help" {
			fmt.Print(usage)
			return
		}
		switch {
		case a == "--db" || a == "--cpa-dir" || a == "--url" ||
			a == "--mgmt-key-file":
			if i+1 >= len(argv) {
				die("flag %s needs a value", a)
			}
			setGlobal(g, a, argv[i+1])
			i += 2
		case strings.HasPrefix(a, "--db=") || strings.HasPrefix(a, "--cpa-dir=") ||
			strings.HasPrefix(a, "--url=") || strings.HasPrefix(a, "--mgmt-key-file="):
			kv := strings.SplitN(a, "=", 2)
			setGlobal(g, kv[0], kv[1])
			i++
		case strings.HasPrefix(a, "-"):
			rest = append(rest, a)
			i++
		default:
			rest = argv[i:]
			i = len(argv)
		}
	}

	if len(rest) == 0 {
		// bare `cpa-stats` -> compact status (the Python default is `watch`)
		cmdSummary(g, []string{"--since", "1h"})
		return
	}
	cmd, args := rest[0], rest[1:]
	switch cmd {
	case "collect":
		cmdCollect(g, args)
	case "prune":
		cmdPrune(g, args)
	case "doctor":
		cmdDoctor(g, args)
	case "alert":
		cmdAlert(g, args)
	case "serve":
		cmdServe(g, args)
	case "install-service":
		cmdInstallService(g, args)
	case "upgrade":
		cmdUpgrade(g, args)
	case "sql":
		cmdSQL(g, args)
	case "export":
		cmdExport(g, args)
	case "show":
		cmdShow(g, args)
	case "quota":
		cmdQuota(g, args)
	case "summary":
		cmdSummary(g, args)
	case "errors":
		cmdErrors(g, args)
	case "top":
		cmdTop(g, args)
	case "tail":
		cmdTail(g, args)
	case "report":
		cmdReport(g, args)
	case "trend":
		cmdTrend(g, args)
	case "watch":
		cmdWatch(g, args)
	case "tui":
		cmdTUI(g, args)
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "cpa-stats: unknown command %q\n\n%s", cmd, usage)
		os.Exit(2)
	}
}

func setGlobal(g *GlobalFlags, name, val string) {
	switch name {
	case "--db":
		g.DB = val
	case "--cpa-dir":
		g.CPADir = val
	case "--url":
		g.URL = val
	case "--mgmt-key-file":
		g.MgmtKeyFile = val
	}
}
