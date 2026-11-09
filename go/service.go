// `cpa-stats install-service` — systemd --user unit (linux) or launchd agent
// (macOS) for the collector.
package main

import (
	"fmt"
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

func cmdInstallService(g *GlobalFlags, argv []string) {
	var retentionDays float64 = 90
	var noEnable bool
	fs := newFlagSet("install-service", g)
	fs.Float64Var(&retentionDays, "retention-days", 90, "")
	fs.BoolVar(&noEnable, "no-enable", false,
		"write the unit but don't enable it")
	mustParse(fs, argv)

	exe, err := os.Executable()
	if err != nil || exe == "" {
		die("cannot resolve own executable path")
	}
	if abs, err2 := filepath.Abs(exe); err2 == nil {
		exe = abs
	}
	if r, err2 := filepath.EvalSymlinks(exe); err2 == nil {
		exe = r
	}

	switch runtime.GOOS {
	case "darwin":
		installLaunchd(g, exe, retentionDays, noEnable)
	default:
		installSystemd(g, exe, retentionDays, noEnable)
	}
}

func installSystemd(g *GlobalFlags, exe string, retentionDays float64, noEnable bool) {
	home, _ := os.UserHomeDir()
	unitDir := filepath.Join(home, ".config", "systemd", "user")
	if err := os.MkdirAll(unitDir, 0o755); err != nil {
		die("mkdir %s: %v", unitDir, err)
	}
	unit := fmt.Sprintf(`[Unit]
Description=cpa-stats collector (CLIProxyAPI usage queue -> SQLite)
After=cliproxy-api.service
Wants=cliproxy-api.service

[Service]
Type=simple
ExecStart="%s" --db "%s" collect --retention-days %v
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
`, exe, g.DB, retentionDays)
	path := filepath.Join(unitDir, "cpa-stats.service")
	if err := os.WriteFile(path, []byte(unit), 0o644); err != nil {
		die("write %s: %v", path, err)
	}
	fmt.Printf("wrote %s\n", path)
	if noEnable {
		return
	}
	for _, args := range [][]string{
		{"systemctl", "--user", "daemon-reload"},
		{"systemctl", "--user", "enable", "--now", "cpa-stats"},
	} {
		cmd := exec.Command(args[0], args[1:]...)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr,
				"systemctl failed (%v); enable manually: "+
					"systemctl --user enable --now cpa-stats\n", err)
			break
		}
		fmt.Println("+ " + strings.Join(args, " "))
	}
}

func installLaunchd(g *GlobalFlags, exe string, retentionDays float64, noEnable bool) {
	home, _ := os.UserHomeDir()
	dir := filepath.Join(home, "Library", "LaunchAgents")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		die("mkdir %s: %v", dir, err)
	}
	logDir := filepath.Join(filepath.Dir(g.DB))
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.cpa-stats.collector</string>
	<key>ProgramArguments</key>
	<array>
		<string>%s</string>
		<string>--db</string>
		<string>%s</string>
		<string>collect</string>
		<string>--retention-days</string>
		<string>%v</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>3</integer>
	<key>StandardOutPath</key>
	<string>%s/collector.log</string>
	<key>StandardErrorPath</key>
	<string>%s/collector.log</string>
</dict>
</plist>
`, html.EscapeString(exe), html.EscapeString(g.DB), retentionDays,
		html.EscapeString(logDir), html.EscapeString(logDir))
	path := filepath.Join(dir, "com.cpa-stats.collector.plist")
	if err := os.WriteFile(path, []byte(plist), 0o644); err != nil {
		die("write %s: %v", path, err)
	}
	fmt.Printf("wrote %s\n", path)
	if noEnable {
		return
	}
	uid := os.Getuid()
	domain := fmt.Sprintf("gui/%d", uid)
	// reload a previously-installed agent first (ignore errors)
	_ = exec.Command("launchctl", "bootout", domain, path).Run()
	for _, args := range [][]string{
		{"launchctl", "bootstrap", domain, path},
		{"launchctl", "kickstart", domain + "/com.cpa-stats.collector"},
	} {
		cmd := exec.Command(args[0], args[1:]...)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr, "%s failed (%v); enable manually: "+
				"launchctl bootstrap %s %s\n", args[0], err, domain, path)
			break
		}
		fmt.Println("+ " + strings.Join(args, " "))
	}
}
