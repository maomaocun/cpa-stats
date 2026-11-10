// HTTP layer: management API (proxy bypassed, like Python's ProxyHandler({}))
// and external calls (release check / webhooks honor http(s)_proxy).
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

// USAGE_QUEUE_PATHS across CPA API versions; newest first, first non-404 wins.
var usageQueuePaths = []string{
	"/v8/management/observability/usage/queue",
	"/v0/management/usage-queue",
}

var credentialsPaths = []string{
	"/v8/management/credentials",
	"/v0/management/auth-files",
}

var credentialsRefreshPaths = []string{
	"/v8/management/credentials/refresh",
	"/v0/management/auth-files/refresh",
}

var configPaths = []string{
	"/v8/management/config",
	"/v0/management/config",
}

// QUOTA_PROVIDERS mirrors ProviderSupportsQuotaObservation upstream.
var quotaProviders = map[string]bool{"claude": true, "codex": true, "devin": true}

func defaultRepo() string { return getenv("CPA_STATS_REPO", "maomaocun/cpa-stats") }

// mgmtClient bypasses env proxies (management is local); extClient honors them.
var (
	mgmtClient = &http.Client{Transport: &http.Transport{Proxy: nil}}
	extClient  = &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment}}
)

// HTTPError carries a non-2xx status like urllib.error.HTTPError.
type HTTPError struct {
	Code int
	URL  string
}

func (e *HTTPError) Error() string { return fmt.Sprintf("HTTP %d for %s", e.Code, e.URL) }

// mgmtGet GETs a management endpoint with the bearer key; returns parsed JSON.
func mgmtGet(cfg CPAConfig, path string, timeout float64) (interface{}, error) {
	req, err := http.NewRequest("GET", cfg.URL+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.MgmtKey)
	cli := &http.Client{Transport: mgmtClient.Transport,
		Timeout: time.Duration(timeout * float64(time.Second))}
	resp, err := cli.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return nil, &HTTPError{Code: resp.StatusCode, URL: cfg.URL + path}
	}
	var data interface{}
	if len(bytes.TrimSpace(body)) > 0 {
		dec := json.NewDecoder(bytes.NewReader(body))
		dec.UseNumber()
		if err := dec.Decode(&data); err != nil {
			return nil, err
		}
	}
	return data, nil
}

// Per-instance endpoint caches — instances may run different CPA builds.
var pathCache = struct {
	sync.Mutex
	m map[string]map[string]string // kind -> url -> path
}{m: map[string]map[string]string{}}

func cachedPath(kind, url string) (string, bool) {
	pathCache.Lock()
	defer pathCache.Unlock()
	if byURL, ok := pathCache.m[kind]; ok {
		p, ok := byURL[url]
		return p, ok
	}
	return "", false
}

func setCachedPath(kind, url, path string) {
	pathCache.Lock()
	defer pathCache.Unlock()
	if pathCache.m[kind] == nil {
		pathCache.m[kind] = map[string]string{}
	}
	pathCache.m[kind][url] = path
}

// probePaths tries candidates (cached first); on 404 tries the next version.
func probePaths(kind string, cfg CPAConfig, paths []string, timeout float64,
	fetch func(path string) (interface{}, error)) (interface{}, string, error) {
	var candidates []string
	if cached, ok := cachedPath(kind, cfg.URL); ok {
		candidates = []string{cached}
	} else {
		candidates = paths
	}
	var lastErr error
	for _, path := range candidates {
		data, err := fetch(path)
		if err == nil {
			setCachedPath(kind, cfg.URL, path)
			return data, path, nil
		}
		lastErr = err
		if he, ok := err.(*HTTPError); ok && he.Code == 404 {
			continue
		}
		return nil, "", err
	}
	return nil, "", lastErr
}

// fetchQueue drains the usage queue; tolerates {"items":[]} / {"data":[]} wraps.
func fetchQueue(cfg CPAConfig, batch int, timeout float64) ([]map[string]interface{}, error) {
	data, _, err := probePaths("queue", cfg, usageQueuePaths, timeout,
		func(path string) (interface{}, error) {
			return mgmtGet(cfg, fmt.Sprintf("%s?count=%d", path, batch), timeout)
		})
	if err != nil {
		return nil, err
	}
	var items []interface{}
	switch d := data.(type) {
	case map[string]interface{}:
		if v, ok := d["items"].([]interface{}); ok {
			items = v
		} else if v, ok := d["data"].([]interface{}); ok {
			items = v
		}
	case []interface{}:
		items = d
	}
	var out []map[string]interface{}
	for _, it := range items {
		if m, ok := it.(map[string]interface{}); ok {
			out = append(out, m)
		}
	}
	return out, nil
}

// fetchCredentials returns the {files:[...]} snapshot (bare lists tolerated).
func fetchCredentials(cfg CPAConfig, timeout float64) (map[string]interface{}, error) {
	data, _, err := probePaths("credentials", cfg, credentialsPaths, timeout,
		func(path string) (interface{}, error) {
			return mgmtGet(cfg, path, timeout)
		})
	if err != nil {
		return nil, err
	}
	switch d := data.(type) {
	case map[string]interface{}:
		return d, nil
	case []interface{}:
		return map[string]interface{}{"files": d}, nil
	}
	return map[string]interface{}{"files": []interface{}{}}, nil
}

// refreshCredential POSTs /credentials/refresh?name — CPA re-observes upstream.
func refreshCredential(cfg CPAConfig, name string, timeout float64) error {
	body, _ := json.Marshal(map[string]string{"name": name})
	_, _, err := probePaths("credentials_refresh", cfg, credentialsRefreshPaths,
		timeout, func(path string) (interface{}, error) {
			req, err := http.NewRequest("POST", cfg.URL+path, bytes.NewReader(body))
			if err != nil {
				return nil, err
			}
			req.Header.Set("Authorization", "Bearer "+cfg.MgmtKey)
			req.Header.Set("Content-Type", "application/json")
			cli := &http.Client{Transport: mgmtClient.Transport,
				Timeout: time.Duration(timeout * float64(time.Second))}
			resp, err := cli.Do(req)
			if err != nil {
				return nil, err
			}
			defer resp.Body.Close()
			io.Copy(io.Discard, resp.Body)
			if resp.StatusCode/100 != 2 {
				return nil, &HTTPError{Code: resp.StatusCode, URL: cfg.URL + path}
			}
			return nil, nil
		})
	return err
}

// staleQuotaCredentials: names of quota-observable creds with stale signals.
func staleQuotaCredentials(snap map[string]interface{}, maxAgeS, now float64) []string {
	var stale []string
	files, _ := snap["files"].([]interface{})
	for _, f := range files {
		fm, ok := f.(map[string]interface{})
		if !ok {
			continue
		}
		provider := strings.ToLower(firstNonEmpty(
			strv(fm["provider"]), strv(fm["type"])))
		if !quotaProviders[provider] {
			continue
		}
		quota, _ := fm["quota"].(map[string]interface{})
		observed, ok := parseISOTS(quota["observed_at"])
		if !ok || now-observed > maxAgeS {
			if name := firstNonEmpty(strv(fm["name"]), strv(fm["id"])); name != "" {
				stale = append(stale, name)
			}
		}
	}
	return stale
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// sendWebhook posts a text alert (generic `text` + Lark bot shape). Honors proxies.
func sendWebhook(url, text string, timeout float64) error {
	body, _ := json.Marshal(map[string]interface{}{
		"msg_type": "text",
		"content":  map[string]string{"text": text},
		"text":     text,
	})
	req, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	cli := &http.Client{Transport: extClient.Transport,
		Timeout: time.Duration(timeout * float64(time.Second))}
	resp, err := cli.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode/100 != 2 {
		return &HTTPError{Code: resp.StatusCode, URL: url}
	}
	return nil
}

// githubToken resolves a token for api.github.com: GH_TOKEN / GITHUB_TOKEN
// env first, then the `gh` CLI login (`gh auth token`, hosts.yml fallback).
// Anonymous calls share a tiny rate limit per IP, which breaks the release
// check behind shared proxies — the gh login raises it to a per-user quota.
func githubToken() string {
	for _, k := range []string{"GH_TOKEN", "GITHUB_TOKEN"} {
		if v := strings.TrimSpace(os.Getenv(k)); v != "" {
			return v
		}
	}
	if out, err := exec.Command("gh", "auth", "token").Output(); err == nil {
		if t := strings.TrimSpace(string(out)); t != "" {
			return t
		}
	}
	if home, err := os.UserHomeDir(); err == nil {
		data, err := os.ReadFile(filepath.Join(home, ".config", "gh", "hosts.yml"))
		if err == nil {
			if m := ghTokenRe.FindSubmatch(data); len(m) == 2 {
				return string(m[1])
			}
		}
	}
	return ""
}

var ghTokenRe = regexp.MustCompile(`oauth_token:\s*(\S+)`)

// latestReleaseTag returns the newest GitHub release tag (falls back to tags).
func latestReleaseTag(repo string, timeout float64) string {
	if repo == "" {
		repo = defaultRepo()
	}
	for _, path := range []string{
		"/repos/" + repo + "/releases/latest",
		"/repos/" + repo + "/tags",
	} {
		req, err := http.NewRequest("GET", "https://api.github.com"+path, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("User-Agent", "cpa-stats/"+Version)
		if tok := githubToken(); tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		cli := &http.Client{Transport: extClient.Transport,
			Timeout: time.Duration(timeout * float64(time.Second))}
		resp, err := cli.Do(req)
		if err != nil {
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 {
			continue
		}
		var data interface{}
		if json.Unmarshal(body, &data) != nil {
			continue
		}
		if m, ok := data.(map[string]interface{}); ok {
			if tag := strv(m["tag_name"]); tag != "" {
				return tag
			}
		}
		if list, ok := data.([]interface{}); ok && len(list) > 0 {
			if m, ok := list[0].(map[string]interface{}); ok {
				if tag := strv(m["name"]); tag != "" {
					return tag
				}
			}
		}
	}
	return ""
}

// binaryAssetName: release asset name for this platform: cpa-stats-<os>-<arch>.
func binaryAssetName() string {
	osName := map[string]string{"linux": "linux", "darwin": "darwin"}[runtime.GOOS]
	if osName == "" {
		osName = runtime.GOOS
	}
	arch := map[string]string{"x86_64": "amd64", "amd64": "amd64",
		"aarch64": "arm64", "arm64": "arm64"}[runtime.GOARCH]
	if arch == "" {
		arch = runtime.GOARCH
	}
	return fmt.Sprintf("cpa-stats-%s-%s", osName, arch)
}

func binaryReleaseURL(tag, repo string) string {
	if repo == "" {
		repo = defaultRepo()
	}
	return fmt.Sprintf("https://github.com/%s/releases/download/%s/%s",
		repo, tag, binaryAssetName())
}

// downloadTo fetches url into dest via temp file + atomic rename (mode 0755).
func downloadTo(url, dest string) error {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "cpa-stats")
	cli := &http.Client{Timeout: 60 * time.Second} // proxy honored
	resp, err := cli.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return &HTTPError{Code: resp.StatusCode, URL: url}
	}
	dir := filepath.Dir(dest)
	tmp, err := os.CreateTemp(dir, filepath.Base(dest)+".*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath) // no-op once renamed
	if _, err := io.Copy(tmp, resp.Body); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpPath, 0o755); err != nil {
		return err
	}
	return os.Rename(tmpPath, dest)
}
