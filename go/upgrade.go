// `cpa-stats upgrade` — check GitHub for a newer release, optionally apply.
package main

import (
	"fmt"
	"os"
)

func cmdUpgrade(g *GlobalFlags, argv []string) {
	var apply bool
	var tag string
	fs := newFlagSet("upgrade", g)
	fs.BoolVar(&apply, "apply", false,
		"run the upgrade (download the release binary)")
	fs.StringVar(&tag, "tag", "",
		"target tag instead of querying GitHub (e.g. v2.3.0)")
	mustParse(fs, argv)

	exe, err := os.Executable()
	if err != nil {
		die("cannot resolve own path: %v", err)
	}
	fmt.Printf("cpa-stats %s (binary)\n", Version)
	if tag == "" {
		tag = latestReleaseTag("", 5)
	}
	if tag == "" {
		die("cannot determine latest release (github.com/%s unreachable or "+
			"no releases yet)", defaultRepo())
	}
	fmt.Printf("latest release: %s\n", tag)
	cur, _ := parseVersion(Version)
	if nv, ok := parseVersion(tag); ok && !versionLess(cur, nv) {
		fmt.Println("already up to date")
		return
	}
	url := binaryReleaseURL(tag, "")
	hint := fmt.Sprintf("curl -fsSL %s -o %s && chmod +x %s", url, exe, exe)
	if !apply {
		fmt.Printf("upgrade with: %s\n", hint)
		return
	}
	fmt.Printf("+ download %s -> %s\n", url, exe)
	if err := downloadTo(url, exe); err != nil {
		die("download failed: %v", err)
	}
	fmt.Println("done — restart `cpa-stats collect` (and any `tui`) to pick it up")
}
