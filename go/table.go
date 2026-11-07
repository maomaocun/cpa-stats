// Minimal unicode-aware table writer for the plain-text viewers.
package main

import (
	"fmt"
	"os"
	"strings"
	"unicode/utf8"
)

func visLen(s string) int { return utf8.RuneCountInString(stripANSI(s)) }

func stripANSI(s string) string {
	// strip \x1b[...m sequences
	var b strings.Builder
	i := 0
	for i < len(s) {
		if s[i] == 0x1b && i+1 < len(s) && s[i+1] == '[' {
			j := i + 2
			for j < len(s) && s[j] != 'm' {
				j++
			}
			i = j + 1
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func pad(s string, w int, right bool) string {
	d := w - visLen(s)
	if d <= 0 {
		return s
	}
	if right {
		return strings.Repeat(" ", d) + s
	}
	return s + strings.Repeat(" ", d)
}

// printTable renders an ASCII table; rightCols marks right-aligned columns.
func printTable(w *os.File, headers []string, rows [][]string, rightCols map[int]bool) {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = visLen(h)
	}
	for _, r := range rows {
		for i, c := range r {
			if i < len(widths) && visLen(c) > widths[i] {
				widths[i] = visLen(c)
			}
		}
	}
	var hdr, sep []string
	for i, h := range headers {
		hdr = append(hdr, pad(h, widths[i], rightCols[i]))
		sep = append(sep, strings.Repeat("-", widths[i]))
	}
	fmt.Fprintln(w, strings.Join(hdr, "  "))
	fmt.Fprintln(w, strings.Join(sep, "  "))
	for _, r := range rows {
		var cells []string
		for i := range headers {
			c := ""
			if i < len(r) {
				c = r[i]
			}
			cells = append(cells, pad(c, widths[i], rightCols[i]))
		}
		fmt.Fprintln(w, strings.Join(cells, "  "))
	}
}
