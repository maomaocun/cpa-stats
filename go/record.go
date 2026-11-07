// Record -> requests-table row mapping (mirrors model.record_to_row).
package main

import (
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// Columns mirrors model.COLUMNS — INSERT order must match exactly.
var Columns = []string{
	"id", "ts", "model", "alias", "response_model", "provider", "effort", "service_tier",
	"endpoint", "source", "auth_index", "session_id", "request_id", "user_agent", "stream",
	"failed", "status_code", "error", "latency_ms", "ttft_ms", "input_tokens", "output_tokens",
	"reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "token_quality", "raw",
	// v3: routing, session topology, upstream detail
	"executor_type", "auth_type", "access_provider", "is_native_key",
	"trace_id", "parent_session_id", "node_kind", "is_fork", "is_compaction",
	"client_ip", "resolved_client_ip", "x_forwarded_for",
	"effective_service_tier", "response_service_tier", "upstream_service_tier",
	"speed", "inference_geo", "fail_code", "total_tokens", "generate",
}

// SENSITIVE_KEYS are never written to the database.
var sensitiveKeys = map[string]bool{
	"api_key": true, "response_headers": true, "access_token_sha256": true,
}

func submap(m map[string]interface{}, key string) map[string]interface{} {
	if v, ok := m[key].(map[string]interface{}); ok {
		return v
	}
	return map[string]interface{}{}
}

// marshalNoEscape mirrors json.dumps(ensure_ascii=False, separators=(",",":")).
func marshalNoEscape(v interface{}) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
	b := buf.Bytes()
	if len(b) > 0 && b[len(b)-1] == '\n' {
		b = b[:len(b)-1]
	}
	return string(b)
}

// strOrNil: absent -> NULL; string stays as-is (even ""); other -> fmt.Sprint.
func strOrNil(v interface{}) interface{} {
	if v == nil {
		return nil
	}
	if st, ok := v.(string); ok {
		return st
	}
	return fmt.Sprint(v)
}

// emptyToNil mirrors `x or None` for strings/numbers (0 and "" become NULL).
func emptyToNil(v interface{}) interface{} {
	switch t := v.(type) {
	case nil:
		return nil
	case string:
		if t == "" {
			return nil
		}
		return t
	case float64:
		if t == 0 {
			return nil
		}
		return t
	}
	return v
}

// intOrNil: absent/0 (after `or None`) -> NULL; numeric -> int64.
func intOrNil(v interface{}) interface{} {
	if v == nil {
		return nil
	}
	f, ok := parseFloat(v)
	if !ok || f == 0 {
		return nil
	}
	return int64(f)
}

func pyBool(v interface{}) int64 {
	if v == nil {
		return 0
	}
	if b, ok := v.(bool); ok {
		return b2i(b)
	}
	if s_, ok := v.(string); ok {
		return b2i(s_ != "")
	}
	if f, ok := parseFloat(v); ok {
		return b2i(f != 0)
	}
	return 1
}

// recordToRow converts one usage-queue record into a requests-table row.
func recordToRow(rec map[string]interface{}) []interface{} {
	tokens := submap(rec, "tokens")
	tb := submap(rec, "token_breakdown")
	fail := submap(rec, "fail")

	ident := strv(rec["execution_id"])
	// Python f-string str(): None->"None", True->"True" — keep digest identical.
	pyStr := func(v interface{}) string {
		switch t := v.(type) {
		case nil:
			return "None"
		case bool:
			if t {
				return "True"
			}
			return "False"
		}
		return strv(v)
	}
	digestSrc := fmt.Sprintf("%s|%s|%s|%s",
		pyStr(rec["timestamp"]), pyStr(rec["request_id"]), ident, pyStr(rec["model"]))
	rid := ident
	if rid == "" {
		sum := sha1.Sum([]byte(digestSrc))
		rid = hex.EncodeToString(sum[:])
	}

	clean := map[string]interface{}{}
	for k, v := range rec {
		if !sensitiveKeys[k] {
			clean[k] = v
		}
	}

	cacheWrite := tokens["cache_creation_tokens"]
	if cacheWrite == nil {
		if inp, ok := tb["input"].(map[string]interface{}); ok {
			cacheWrite = inp["cache_write_tokens"]
		}
	}
	cacheRead := tokens["cache_read_tokens"]
	if cacheRead == nil {
		cacheRead = tokens["cached_tokens"]
	}

	errStr := strv(fail["body"])
	if r := []rune(errStr); len(r) > 2000 {
		errStr = string(r[:2000])
	}

	tsF := float64(time.Now().UnixNano()) / 1e9
	if rec["timestamp"] != nil {
		if t, err := parseTS(fmt.Sprint(rec["timestamp"])); err == nil {
			tsF = t
		}
	}

	quality := strv(tb["quality"])

	values := map[string]interface{}{
		"id":                 rid,
		"ts":                 tsF,
		"model":              strOrNil(rec["model"]),
		"alias":              strOrNil(rec["alias"]),
		"response_model":     strOrNil(rec["response_model"]),
		"provider":           strOrNil(rec["provider"]),
		"effort":             strOrNil(rec["reasoning_effort"]),
		"service_tier":       strOrNil(rec["service_tier"]),
		"endpoint":           strOrNil(rec["endpoint"]),
		"source":             strOrNil(rec["source"]),
		"auth_index":         strOrNil(rec["auth_index"]),
		"session_id":         strOrNil(rec["session_id"]),
		"request_id":         strOrNil(rec["request_id"]),
		"user_agent":         strOrNil(rec["user_agent"]),
		"stream":             pyBool(rec["stream"]),
		"failed":             pyBool(rec["failed"]),
		"status_code":        i64(fail["status_code"]),
		"error":              emptyToNil(errStr),
		"latency_ms":         i64(rec["latency_ms"]),
		"ttft_ms":            intOrNil(rec["ttft_ms"]),
		"input_tokens":       i64(tokens["input_tokens"]),
		"output_tokens":      i64(tokens["output_tokens"]),
		"reasoning_tokens":   i64(tokens["reasoning_tokens"]),
		"cache_read_tokens":  i64(cacheRead),
		"cache_write_tokens": i64(cacheWrite),
		"token_quality":      emptyToNil(quality),
		"raw":                marshalNoEscape(clean),

		"executor_type":          strOrNil(rec["executor_type"]),
		"auth_type":              strOrNil(rec["auth_type"]),
		"access_provider":        strOrNil(rec["access_provider"]),
		"is_native_key":          optBool(rec["is_native_key"]),
		"trace_id":               strOrNil(rec["trace_id"]),
		"parent_session_id":      strOrNil(rec["parent_session_id"]),
		"node_kind":              strOrNil(rec["node_kind"]),
		"is_fork":                optBool(rec["is_fork"]),
		"is_compaction":          optBool(rec["is_compaction"]),
		"client_ip":              strOrNil(rec["client_ip"]),
		"resolved_client_ip":     strOrNil(rec["resolved_client_ip"]),
		"x_forwarded_for":        strOrNil(rec["x_forwarded_for"]),
		"effective_service_tier": strOrNil(rec["effective_service_tier"]),
		"response_service_tier":  strOrNil(rec["response_service_tier"]),
		"upstream_service_tier":  strOrNil(rec["upstream_service_tier"]),
		"speed":                  strOrNil(rec["speed"]),
		"inference_geo":          strOrNil(rec["inference_geo"]),
		"fail_code":              emptyToNil(strv(fail["code"])),
		"total_tokens": func() interface{} {
			// Python `or`: 0/"" also fall through to the breakdown field.
			if f, ok := parseFloat(tokens["total_tokens"]); ok && f != 0 {
				return int64(f)
			}
			return i64(tb["total_tokens"])
		}(),
		"generate": optBool(rec["generate"]),
	}

	row := make([]interface{}, len(Columns))
	for i, c := range Columns {
		row[i] = values[c]
	}
	return row
}
