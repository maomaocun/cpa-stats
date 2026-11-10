// Shared TUI helpers on top of tcell: styles, box writer, table writer.
package main

import (
	"strings"

	"github.com/gdamore/tcell/v2"
	"github.com/mattn/go-runewidth"
)

var (
	stDim    = tcell.StyleDefault.Foreground(tcell.ColorGray)
	stBold   = tcell.StyleDefault.Bold(true)
	stCyan   = tcell.StyleDefault.Foreground(tcell.ColorTeal)
	stCyanB  = stCyan.Bold(true)
	stGreen  = tcell.StyleDefault.Foreground(tcell.ColorGreen)
	stRed    = tcell.StyleDefault.Foreground(tcell.ColorRed)
	stYellow = tcell.StyleDefault.Foreground(tcell.ColorYellow)
	stSel    = tcell.StyleDefault.Background(tcell.ColorDarkSlateGray).
			Foreground(tcell.ColorWhite)
	stHdr = stDim
)

func cellWidth(s string) int {
	return runewidth.StringWidth(stripANSI(s))
}

// put writes text at (x,y); returns the x after the text.
func put(s tcell.Screen, x, y int, st tcell.Style, text string) int {
	cx := x
	for _, r := range text {
		w := runewidth.RuneWidth(r)
		s.SetContent(cx, y, r, nil, st)
		cx += w
	}
	return cx
}

// putClipped writes text but never past x1.
func putClipped(s tcell.Screen, x, y, x1 int, st tcell.Style, text string) int {
	cx := x
	for _, r := range text {
		w := runewidth.RuneWidth(r)
		if cx+w > x1 {
			break
		}
		s.SetContent(cx, y, r, nil, st)
		cx += w
	}
	return cx
}

// clip s to maxW display cells.
func clip(s string, maxW int) string {
	var b []rune
	w := 0
	for _, r := range s {
		rw := runewidth.RuneWidth(r)
		if w+rw > maxW {
			break
		}
		b = append(b, r)
		w += rw
	}
	if len([]rune(s)) > len(b) && maxW >= 1 {
		b[len(b)-1] = '…'
	}
	return string(b)
}

// tuiRow is one display row: cell text + per-cell alignment.
type tuiRow struct {
	cells []string
	right []bool
}

// drawTable renders headers + rows into (x,y..y+maxH). Returns lines used.
func drawTable(s tcell.Screen, x, y, w, maxH int,
	headers []string, rows []tuiRow, sel int) int {
	if len(headers) == 0 || maxH < 2 {
		return 0
	}
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = cellWidth(h)
	}
	for _, r := range rows {
		for i, c := range r.cells {
			if i < len(widths) && cellWidth(c) > widths[i] {
				widths[i] = cellWidth(c)
			}
		}
	}
	// shrink to w: first drop trailing columns that cannot fit at all,
	// then trim the widest remaining columns down to a floor of 4 cells.
	total := 0
	for _, wd := range widths {
		total += wd + 2
	}
	for total > w && len(headers) > 2 {
		total -= widths[len(widths)-1] + 2
		headers = headers[:len(headers)-1]
		widths = widths[:len(widths)-1]
	}
	const floor = 4
	for total > w {
		mi := 0
		for i, wd := range widths {
			if wd > widths[mi] {
				mi = i
			}
		}
		if widths[mi] <= floor {
			break
		}
		widths[mi]--
		total--
	}
	cy := y
	// header
	cx := x
	for i, h := range headers {
		put(s, cx, cy, stHdr, padCell(h, widths[i], rAlign(headers[i], i, rows, true)))
		cx += widths[i] + 2
	}
	cy++
	// rows
	for ri, r := range rows {
		if cy >= y+maxH {
			break
		}
		st := tcell.StyleDefault
		if ri == sel {
			st = stSel
		}
		cx = x
		for i, c := range r.cells {
			if i >= len(widths) {
				break
			}
			alignRight := false
			if i < len(r.right) {
				alignRight = r.right[i]
			}
			put(s, cx, cy, st, padCell(c, widths[i], alignRight))
			cx += widths[i] + 2
		}
		cy++
	}
	return cy - y
}

func rAlign(h string, i int, rows []tuiRow, isHeader bool) bool {
	for _, r := range rows {
		if i < len(r.right) {
			return r.right[i]
		}
	}
	return false
}

func padCell(s string, w int, right bool) string {
	d := w - cellWidth(s)
	if d <= 0 {
		return clip(s, w)
	}
	if right {
		return spaces(d) + s
	}
	return s + spaces(d)
}

func spaces(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.Repeat(" ", n)
}

func repeats(r rune, n int) string {
	if n <= 0 {
		return ""
	}
	return strings.Repeat(string(r), n)
}

// modal draws a bordered box centered on screen; returns inner rect.
func modalBox(s tcell.Screen, title string, maxWFrac, maxHFrac float64) (x, y, w, h int) {
	sw, sh := s.Size()
	w = int(float64(sw) * maxWFrac)
	h = int(float64(sh) * maxHFrac)
	if w > 110 {
		w = 110
	}
	if h > sh-4 {
		h = sh - 4
	}
	if w < 40 {
		w = 40
	}
	if h < 3 {
		h = 3
	}
	// never exceed the terminal — narrow screens just fill
	if w > sw {
		w = sw
	}
	if h > sh {
		h = sh
	}
	x = (sw - w) / 2
	y = (sh - h) / 2
	if x < 0 {
		x = 0
	}
	if y < 0 {
		y = 0
	}
	st := stBold.Foreground(tcell.ColorTeal)
	// border
	put(s, x, y, st, "┌"+titleBar(title, w-2)+"┐")
	for i := 1; i < h-1; i++ {
		put(s, x, y+i, st, "│")
		put(s, x+w-1, y+i, st, "│")
	}
	put(s, x, y+h-1, st, "└"+repeats('─', w-2)+"┘")
	return x + 2, y + 1, w - 4, h - 2
}

func titleBar(title string, w int) string {
	t := " " + title + " "
	d := w - cellWidth(t)
	if d <= 0 {
		return clip(t, w)
	}
	return t + repeats('─', d)
}
