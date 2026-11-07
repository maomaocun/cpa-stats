// CPA instance discovery and management-key resolution (mirrors collector.py).
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type CPAConfig struct {
	URL     string
	MgmtKey string
}

// Global flag values shared by all subcommands.
type GlobalFlags struct {
	DB          string
	CPADir      string
	URL         string
	MgmtKeyFile string
}

func defaultDBPath() string {
	base := os.Getenv("XDG_DATA_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".local", "share")
	}
	return filepath.Join(base, "cpa-stats", "requests.db")
}

// findCPADir locates the CLIProxyAPI install dir: --cpa-dir / CPA_DIR, then the
// usual suspects under $HOME.
func findCPADir(g *GlobalFlags) string {
	if g.CPADir != "" {
		d := expandHome(g.CPADir)
		if fileExists(filepath.Join(d, "config.yaml")) {
			return d
		}
		return ""
	}
	home, _ := os.UserHomeDir()
	for _, cand := range []string{
		filepath.Join(home, ".config", "cliproxy"),
		filepath.Join(home, ".cliproxy"),
		filepath.Join(home, "cliproxy"),
	} {
		if fileExists(filepath.Join(cand, "config.yaml")) {
			return cand
		}
	}
	// Legacy per-user installs: ~/cliproxy-<anything>/config.yaml
	matches, _ := filepath.Glob(filepath.Join(home, "cliproxy-*", "config.yaml"))
	if len(matches) > 0 {
		sort.Strings(matches)
		return filepath.Dir(matches[0])
	}
	return ""
}

func expandHome(p string) string {
	if strings.HasPrefix(p, "~/") || p == "~" {
		home, _ := os.UserHomeDir()
		return filepath.Join(home, strings.TrimPrefix(p, "~"))
	}
	return p
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

var (
	serverPortRe = regexp.MustCompile(`(?m)^server:\s*\n(?:[ \t]+.*\n)*?[ \t]+port:\s*(\d+)`)
	barePortRe   = regexp.MustCompile(`(?m)^port:\s*(\d+)`)
)

// loadCPAConfigs resolves one config per CPA instance. --url/CPA_URL and
// --mgmt-key-file accept comma-separated lists; a single key applies to all.
func loadCPAConfigs(g *GlobalFlags) []CPAConfig {
	cfgs, err := loadCPAConfigsE(g)
	if err != nil {
		die("%s", err)
	}
	return cfgs
}

// loadCPAConfigsE is the error-returning variant (doctor needs soft failure).
func loadCPAConfigsE(g *GlobalFlags) ([]CPAConfig, error) {
	cpaDir := findCPADir(g)
	urlArg := g.URL
	if urlArg == "" {
		urlArg = os.Getenv("CPA_URL")
	}
	var urls []string
	for _, u := range strings.Split(urlArg, ",") {
		if u = strings.TrimRight(strings.TrimSpace(u), "/"); u != "" {
			urls = append(urls, u)
		}
	}
	if len(urls) == 0 {
		if cpaDir == "" {
			return nil, fmt.Errorf(
				"cannot find a CLIProxyAPI install dir; pass --url or --cpa-dir")
		}
		cfgPath := filepath.Join(cpaDir, "config.yaml")
		data, err := os.ReadFile(cfgPath)
		if err != nil {
			return nil, fmt.Errorf("cannot find %s; pass --url or --cpa-dir", cfgPath)
		}
		m := serverPortRe.FindSubmatch(data)
		if m == nil {
			m = barePortRe.FindSubmatch(data)
		}
		if m == nil {
			return nil, fmt.Errorf("cannot find server port in %s; pass --url", cfgPath)
		}
		urls = []string{fmt.Sprintf("http://127.0.0.1:%s", m[1])}
	}
	keys := make([]string, 0, len(urls))
	if key := os.Getenv("CPA_MGMT_KEY"); key != "" {
		for range urls {
			keys = append(keys, key)
		}
	} else {
		kfArg := g.MgmtKeyFile
		if kfArg == "" && cpaDir != "" {
			kfArg = filepath.Join(cpaDir, "management.key")
		}
		var keyFiles []string
		for _, k := range strings.Split(kfArg, ",") {
			if k = strings.TrimSpace(k); k != "" {
				keyFiles = append(keyFiles, k)
			}
		}
		if len(keyFiles) == 1 && len(urls) > 1 {
			kf := keyFiles[0]
			keyFiles = make([]string, len(urls))
			for i := range keyFiles {
				keyFiles[i] = kf
			}
		}
		if len(keyFiles) != len(urls) {
			return nil, fmt.Errorf("--mgmt-key-file count (%d) must be 1 or "+
				"match --url count (%d)", len(keyFiles), len(urls))
		}
		for _, kf := range keyFiles {
			kf = expandHome(kf)
			data, err := os.ReadFile(kf)
			if err != nil {
				return nil, fmt.Errorf("cannot locate management key file %s; "+
					"set CPA_MGMT_KEY, --mgmt-key-file or --cpa-dir", kf)
			}
			keys = append(keys, strings.TrimSpace(string(data)))
		}
	}
	out := make([]CPAConfig, len(urls))
	for i := range urls {
		out[i] = CPAConfig{URL: urls[i], MgmtKey: keys[i]}
	}
	return out, nil
}

// instanceTag shortens a URL to host:port for multi-instance labels.
func instanceTag(url string) string {
	s := url
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
	}
	if i := strings.Index(s, "/"); i >= 0 {
		s = s[:i]
	}
	return s
}
