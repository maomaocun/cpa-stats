// Shared helpers: versions, durations, timestamps, formatting.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var versionRe = regexp.MustCompile(`(\d+)\.(\d+)(?:\.(\d+))?`)

// parseVersion "v2.3.0" / "2.3.0" -> (2,3,0); unparseable -> (0,0,0)+false.
func parseVersion(text string) ([3]int, bool) {
	m := versionRe.FindStringSubmatch(text)
	if m == nil {
		return [3]int{}, false
	}
	var v [3]int
	v[0], _ = strconv.Atoi(m[1])
	v[1], _ = strconv.Atoi(m[2])
	if m[3] != "" {
		v[2], _ = strconv.Atoi(m[3])
	}
	return v, true
}

func versionLess(a, b [3]int) bool {
	for i := 0; i < 3; i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return false
}

var durationRe = regexp.MustCompile(`^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$`)

// parseDuration "90s","15m","2h","7d","1w" -> seconds.
func parseDuration(text string) (float64, error) {
	m := durationRe.FindStringSubmatch(text)
	if m == nil {
		return 0, fmt.Errorf("invalid duration: %q (examples: 30m, 2h, 7d)", text)
	}
	f, _ := strconv.ParseFloat(m[1], 64)
	unit := map[string]float64{"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]
	return f * unit, nil
}

var tsLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02T15:04:05.999999999Z07:00",
	"2006-01-02 15:04:05.999999999-07:00",
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02T15:04:05Z07:00",
	"2006-01-02 15:04:05Z07:00",
	"2006-01-02T15:04:05",
	"2006-01-02 15:04:05",
}

// parseTS parses RFC3339 (any fractional-second width) -> epoch seconds.
func parseTS(value string) (float64, error) {
	for _, layout := range tsLayouts {
		if t, err := time.Parse(layout, value); err == nil {
			return float64(t.UnixNano()) / 1e9, nil
		}
	}
	return 0, fmt.Errorf("bad timestamp %q", value)
}

// parseISOTS -> epoch seconds; 0,false when unparseable.
func parseISOTS(value interface{}) (float64, bool) {
	if value == nil {
		return 0, false
	}
	s := strings.TrimSpace(fmt.Sprint(value))
	if s == "" {
		return 0, false
	}
	if t, err := parseTS(s); err == nil {
		return t, true
	}
	return 0, false
}

// parseFloat tolerant numeric parse ("81%", 81, "0.42", *int64).
func parseFloat(value interface{}) (float64, bool) {
	switch v := value.(type) {
	case nil:
		return 0, false
	case *int64:
		if v == nil {
			return 0, false
		}
		return float64(*v), true
	case *float64:
		if v == nil {
			return 0, false
		}
		return *v, true
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	case json.Number:
		f, err := v.Float64()
		return f, err == nil
	case string:
		s := strings.TrimSpace(strings.TrimSuffix(v, "%"))
		f, err := strconv.ParseFloat(s, 64)
		return f, err == nil
	}
	return 0, false
}

// parseEpoch: unix seconds/ms or RFC3339 -> epoch seconds.
func parseEpoch(value interface{}) (float64, bool) {
	if value == nil {
		return 0, false
	}
	if f, ok := parseFloat(value); ok && f > 0 {
		if f > 1e12 {
			f /= 1000
		}
		return f, true
	}
	return parseISOTS(value)
}

// parsePct "81%" / "81" / 81 -> 81.
var pctRe = regexp.MustCompile(`\s*(-?\d+)`)

func parsePct(value interface{}) *int64 {
	if value == nil {
		return nil
	}
	m := pctRe.FindStringSubmatch(fmt.Sprint(value))
	if m == nil {
		return nil
	}
	n, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return nil
	}
	return &n
}

func truthy(v interface{}) bool {
	s := strings.ToLower(strings.TrimSpace(fmt.Sprint(v)))
	return s == "true" || s == "1" || s == "yes"
}

func falsy(v interface{}) bool {
	s := strings.ToLower(strings.TrimSpace(fmt.Sprint(v)))
	return s == "false" || s == "0" || s == "no"
}

// fmt helpers mirroring cpa_stats/format.py ---------------------------------

func fmtMs(ms *float64) string {
	if ms == nil {
		return "—"
	}
	v := *ms
	if v >= 60000 {
		return fmt.Sprintf("%.1fm", v/60000)
	}
	if v >= 1000 {
		return fmt.Sprintf("%.2fs", v/1000)
	}
	return fmt.Sprintf("%.0fms", v)
}

func fmtTok(n interface{}) string {
	var f float64
	switch v := n.(type) {
	case nil:
		return "—"
	case int64:
		f = float64(v)
	case float64:
		f = v
	case int:
		f = float64(v)
	default:
		return "—"
	}
	switch {
	case f >= 1e9:
		return fmt.Sprintf("%.2fB", f/1e9)
	case f >= 1e6:
		return fmt.Sprintf("%.2fM", f/1e6)
	case f >= 10000:
		return fmt.Sprintf("%.0fK", f/1e3)
	case f >= 1000:
		return fmt.Sprintf("%.1fK", f/1e3)
	}
	return fmt.Sprintf("%.0f", f)
}

func fmtAge(seconds *float64) string {
	if seconds == nil {
		return "never"
	}
	s := *seconds
	switch {
	case s < 60:
		return fmt.Sprintf("%.0fs ago", s)
	case s < 3600:
		return fmt.Sprintf("%.0fm ago", s/60)
	case s < 86400:
		return fmt.Sprintf("%.1fh ago", s/3600)
	}
	return fmt.Sprintf("%.1fd ago", s/86400)
}

func fmtCountdown(seconds float64) string {
	if seconds <= 0 {
		return "due"
	}
	s := int(seconds)
	days := s / 86400
	s %= 86400
	hours := s / 3600
	s %= 3600
	mins := s / 60
	s %= 60
	if days > 0 {
		return fmt.Sprintf("%dd%dh", days, hours)
	}
	if hours > 0 {
		return fmt.Sprintf("%dh%dm", hours, mins)
	}
	if mins > 0 {
		return fmt.Sprintf("%dm", mins)
	}
	return fmt.Sprintf("%ds", s)
}

func windowLabel(seconds float64) string {
	for _, u := range []struct {
		tag  string
		size float64
	}{{"d", 86400}, {"h", 3600}, {"m", 60}} {
		if seconds >= u.size && math.Mod(seconds, u.size) == 0 {
			return fmt.Sprintf("%d%s", int(seconds/u.size), u.tag)
		}
	}
	return fmt.Sprintf("%ds", int(seconds))
}

// sparkline renders unicode blocks for a series.
func sparkline(buckets []float64) string {
	const blocks = "▁▂▃▄▅▆▇█"
	if len(buckets) == 0 {
		return ""
	}
	lo, hi := buckets[0], buckets[0]
	for _, v := range buckets {
		if v < lo {
			lo = v
		}
		if v > hi {
			hi = v
		}
	}
	var b strings.Builder
	runes := []rune(blocks)
	if hi == lo {
		ch := runes[0]
		if hi != 0 {
			ch = runes[3]
		}
		for range buckets {
			b.WriteRune(ch)
		}
		return b.String()
	}
	span := hi - lo
	for _, v := range buckets {
		idx := int((v - lo) / span * float64(len(runes)-1))
		if idx > len(runes)-1 {
			idx = len(runes) - 1
		}
		b.WriteRune(runes[idx])
	}
	return b.String()
}

func die(format string, a ...interface{}) {
	fmt.Fprintf(os.Stderr, "cpa-stats: "+format+"\n", a...)
	os.Exit(1)
}

func nowLog(format string, a ...interface{}) {
	fmt.Printf("%s %s\n", time.Now().Format("2006-01-02T15:04:05"), fmt.Sprintf(format, a...))
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func f64(v interface{}) *float64 {
	if v == nil {
		return nil
	}
	if f, ok := parseFloat(v); ok {
		return &f
	}
	return nil
}

func i64(v interface{}) *int64 {
	if v == nil {
		return nil
	}
	if f, ok := parseFloat(v); ok {
		n := int64(f)
		return &n
	}
	return nil
}

func s(v interface{}) *string {
	if v == nil {
		return nil
	}
	str, ok := v.(string)
	if !ok {
		str = fmt.Sprint(v)
	}
	return &str
}

func strv(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case *float64:
		if t == nil {
			return ""
		}
		return strconv.FormatFloat(*t, 'f', -1, 64)
	case *int64:
		if t == nil {
			return ""
		}
		return strconv.FormatInt(*t, 10)
	case *string:
		if t == nil {
			return ""
		}
		return *t
	case float64:
		// plain decimals like Python repr, not Go's %v exponent form
		return strconv.FormatFloat(t, 'f', -1, 64)
	case int64:
		return strconv.FormatInt(t, 10)
	case int:
		return strconv.Itoa(t)
	case bool:
		if t {
			return "true"
		}
		return "false"
	}
	return fmt.Sprint(v)
}

func derefS(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func derefI(p *int64) int64 {
	if p == nil {
		return 0
	}
	return *p
}

func derefF(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}

func b2i(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

// optBool mirrors _opt_bool: nil -> NULL, else Python truthiness (0/1).
func optBool(v interface{}) interface{} {
	if v == nil {
		return nil
	}
	switch t := v.(type) {
	case bool:
		return b2i(t)
	case string:
		if t == "" {
			return int64(0)
		}
		return int64(1) // python: bool("false") is True
	}
	if f, ok := parseFloat(v); ok {
		return b2i(f != 0)
	}
	return int64(1)
}
