"""Interactive TUI: `cpa-stats tui`.

Layout
    header   collector status · window · totals
    cards    all-time + per-window stat cards
    body     per-model table  |  recent requests (selectable)
    footer   keybindings

Keys
    1/2/3/4   window 15m / 1h / 6h / 24h
    up/down   select a row (mouse wheel scrolls; click selects)
    left/right switch right-hand tabs (recent / sessions / totals)
    tab       cycle focus between panels
    enter     request detail modal
    f         filter (model substring)
    p         pause / resume refresh
    r         force refresh
    q         quit
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .format import (fmt_age, fmt_cost, fmt_ms, fmt_pct, fmt_tok, fmt_tps,
                     sparkline, window_label)
from .model import (MIN_DECODE_MS, MIN_DECODE_TOKENS, aggregate, bucket_counts,
                    decode_tps, e2e_tps, group_by, short_model, token_parts)
from .storage import db_totals, load_rows, meta_get

WINDOWS = {  # key -> seconds
    "1": ("15m", 15 * 60),
    "2": ("1h", 60 * 60),
    "3": ("6h", 6 * 3600),
    "4": ("24h", 24 * 3600),
}


def build_app():
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, HorizontalGroup, Vertical
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (DataTable, Footer, Header, Input, Label,
                                 RichLog, Static, TabbedContent, TabPane)

    class DetailScreen(ModalScreen):
        """Modal showing every field of one request row."""

        BINDINGS = [Binding("escape,q,enter", "dismiss", "close")]

        def __init__(self, row):
            super().__init__()
            self.row = row

        def compose(self):
            r = self.row
            total, new, read, write = token_parts(r)
            tps = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
            e2e = e2e_tps(r["output_tokens"], r["latency_ms"])
            grid = [
                ("time", datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")),
                ("model", f"{short_model(r)}  (resp: {r['response_model'] or '—'})"),
                ("provider / effort / tier",
                 f"{r['provider'] or '—'} / {r['effort'] or '—'} / {r['service_tier'] or '—'}"),
                ("endpoint", r["endpoint"] or "—"),
                ("session", r["session_id"] or "—"),
                ("request / execution", f"{r['request_id'] or '—'} / {r['id']}"),
                ("account", r["source"] or r["auth_index"] or "—"),
                ("client", r["user_agent"] or "—"),
                ("status",
                 f"{'✗ FAILED ' + str(r['status_code'] or '') if r['failed'] else '✓ ok'}"
                 + (f"  stream={bool(r['stream'])}" if r["stream"] is not None else "")),
                ("", ""),
                ("latency / TTFT", f"{fmt_ms(r['latency_ms'])} / {fmt_ms(r['ttft_ms'])}"),
                ("TPS decode / e2e", f"{fmt_tps(tps)} / {fmt_tps(e2e)} tok/s"),
                ("input tokens", f"{fmt_tok(total)}  =  {fmt_tok(new)} new"
                 f" + {fmt_tok(read)} read + {fmt_tok(write)} write"
                 f"  (hit {fmt_pct(read / total if total else None)})"),
                ("output", f"{fmt_tok(r['output_tokens'])}"
                 + (f"  reasoning {fmt_tok(r['reasoning_tokens'])}" if r["reasoning_tokens"] else "")),
                ("token quality", r["token_quality"] or "—"),
            ]
            if r["error"]:
                grid.append(("error", r["error"][:400]))
            text = "\n".join(
                f"[dim]{k:>22}[/]  {v}" if k else "" for k, v in grid)
            yield Vertical(
                Static(f"[bold cyan]{short_model(r)}[/] · {r['id'][:12]}"),
                Static(text),
                Static("[dim]esc / enter to close[/]"),
                id="detail",
            )

    class StatsApp(App):
        CSS = """
        Screen { layout: vertical; }
        #cards { height: auto; }
        .statcard { width: 1fr; height: 5; border: round $surface-lighten-2;
                    padding: 0 1; content-align: center middle; }
        .statcard .t { color: $text-muted; text-style: bold; }
        .statcard .v { text-style: bold; }
        .statcard .s { color: $text-muted; }
        #activity { height: 2; padding: 0 1; color: $primary; }
        #body { height: 1fr; }
        #models { width: 46%; }
        #right { width: 54%; }
        DataTable { height: 100%; }
        #detail { width: 78; height: auto; max-height: 90%; padding: 1 2;
                  border: thick $primary; background: $surface; }
        #filter { dock: bottom; height: 3; display: none; }
        #statusbar { height: 1; padding: 0 1; }
        """

        BINDINGS = [
            Binding("q", "quit", "quit"),
            Binding("1", "win('1')", "15m"),
            Binding("2", "win('2')", "1h"),
            Binding("3", "win('3')", "6h"),
            Binding("4", "win('4')", "24h"),
            Binding("f", "toggle_filter", "filter"),
            Binding("p", "toggle_pause", "pause"),
            Binding("r", "refresh_data", "refresh"),
            Binding("enter", "detail", "detail"),
            Binding("left", "tab(-1)", "◀ tab"),
            Binding("right", "tab(+1)", "tab ▶"),
        ]

        TAB_IDS = ["recent", "sessions", "totals"]

        def action_tab(self, step: int):
            tc = self.query_one("#right", TabbedContent)
            try:
                idx = self.TAB_IDS.index(tc.active)
            except ValueError:
                idx = 0
            tc.active = self.TAB_IDS[(idx + step) % len(self.TAB_IDS)]

        window_key = reactive("2")
        paused = reactive(False)
        model_filter = reactive("")

        def __init__(self, db_path, pricing=None, refresh_s=2.0):
            super().__init__()
            self.db_path = Path(db_path)
            self.pricing = pricing
            self.refresh_s = refresh_s
            self.rows = []
            self.conn = None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Horizontal(id="cards")
            yield Static(id="activity")
            with Horizontal(id="body"):
                yield DataTable(id="models", cursor_type="row", zebra_stripes=True)
                with TabbedContent(id="right"):
                    with TabPane("recent", id="recent"):
                        yield DataTable(id="requests-table", cursor_type="row",
                                        zebra_stripes=True)
                    with TabPane("sessions", id="sessions"):
                        yield DataTable(id="sessions-table", cursor_type="row",
                                        zebra_stripes=True)
                    with TabPane("totals", id="totals"):
                        yield Static(id="totals-pane")
            yield Input(placeholder="filter model substring…  (esc to clear)",
                        id="filter")
            yield Static(id="statusbar")
            yield Footer()

        def on_mount(self):
            from .storage import open_db

            try:
                self.conn = open_db(self.db_path, readonly=True)
            except Exception as exc:
                self.notify(f"cannot open {self.db_path}: {exc}", severity="error")
            self._init_tables()
            self.refresh_data()
            self.query_one("#requests-table", DataTable).focus()
            self.set_interval(self.refresh_s, self._tick)

        # ------------------------------------------------------------ data

        def _window_s(self):
            return WINDOWS[self.window_key][1]

        def _tick(self):
            if not self.paused:
                self.refresh_data()

        def refresh_data(self):
            if not self.conn:
                return
            since = time.time() - self._window_s()
            self.rows = load_rows(self.conn, since, self.model_filter or None)
            self._render_all()

        # ------------------------------------------------------------ render

        def _init_tables(self):
            m = self.query_one("#models", DataTable)
            m.add_columns("model", "req", "fail", "TTFT p50", "TPS p50", "e2e p50",
                          "in", "new", "hit", "out")
            rq = self.query_one("#requests-table", DataTable)
            rq.add_columns("time", "model", "effort", "in", "hit", "out",
                           "TTFT", "lat", "TPS", "e2e", "session", "")
            s = self.query_one("#sessions-table", DataTable)
            s.add_columns("session", "req", "fail", "in", "new", "out",
                          "lat p50", "last")

        def _card(self, title, value, sub=""):
            return Static(
                f"[dim]{title}[/]\n[bold]{value}[/]\n[dim]{sub}[/]",
                classes="statcard")

        def _render_all(self):
            rows = self.rows
            agg = aggregate(rows, self._window_s(), self.pricing)
            totals = db_totals(self.conn, self.pricing)

            # --- statusbar
            hb = meta_get(self.conn, "collector_heartbeat")
            err = meta_get(self.conn, "collector_last_error") or ""
            age = time.time() - float(hb) if hb else None
            if age is not None and age < 15 and not err:
                coll = "[green]● collector live[/]"
            elif err:
                coll = f"[red]● collector error: {err[:60]}[/]"
            else:
                coll = f"[red]● collector stale ({fmt_age(age)})[/]"
            sb = self.query_one("#statusbar", Static)
            sb.update(
                f"{coll}   window [bold]{WINDOWS[self.window_key][0]}[/]"
                f"   {totals.n} stored"
                + (f"   filter [magenta]{self.model_filter}[/]" if self.model_filter else "")
                + ("   [yellow]PAUSED[/]" if self.paused else ""))

            # --- cards row
            cards = self.query_one("#cards", Horizontal)
            cards.remove_children()
            ok = agg.n - agg.failed
            succ = f"{ok / agg.n * 100:.1f}% ok" if agg.n else "no requests"
            cards.mount(
                self._card(f"req · {WINDOWS[self.window_key][0]}",
                           f"{agg.n}", f"{succ} · {agg.rpm:.1f}/min" if agg.rpm else succ),
                self._card("TTFT p50", fmt_ms(agg.ttft_p50),
                           f"p95 {fmt_ms(agg.ttft_p95)}"),
                self._card("TPS decode/e2e",
                           f"{fmt_tps(agg.tps_p50)} / {fmt_tps(agg.e2e_p50)}",
                           f"p10 {fmt_tps(agg.tps_p10)} · p90 {fmt_tps(agg.tps_p90)}"),
                self._card("latency p50", fmt_ms(agg.lat_p50),
                           f"p95 {fmt_ms(agg.lat_p95)}"),
                self._card("input", fmt_tok(agg.in_tok),
                           f"{fmt_tok(agg.uncached)} new · "
                           f"{fmt_tok(agg.cache_read + agg.cache_write)} cached"),
                self._card("cache", f"hit {fmt_pct(agg.cache_ratio)}",
                           f"read {fmt_tok(agg.cache_read)} · "
                           f"write {fmt_tok(agg.cache_write)}"),
                self._card("output", fmt_tok(agg.out_tok),
                           f"{agg.burst}× burst" if agg.burst else ""),
            )
            if agg.cost_usd is not None:
                cards.mount(self._card("est. cost", fmt_cost(agg.cost_usd),
                                       f"all-time {fmt_cost(totals.cost_usd)}"))

            # --- activity sparkline
            counts = bucket_counts(rows, time.time() - self._window_s(),
                                   self._window_s() / 40, 40)
            act = self.query_one("#activity", Static)
            act.update(
                f"activity {sparkline(counts)}  "
                f"[dim]{sum(counts)} req · peak {max(counts)}/bucket "
                f"· tokens clean {fmt_pct(agg.quality_ratio)}[/]")

            # --- per model
            mt = self.query_one("#models", DataTable)
            mt.clear()
            for name, grp in sorted(group_by(rows, short_model).items(),
                                    key=lambda kv: -len(kv[1])):
                a = aggregate(grp, self._window_s())
                mt.add_row(name, str(a.n), str(a.failed),
                           fmt_ms(a.ttft_p50), fmt_tps(a.tps_p50),
                           fmt_tps(a.e2e_p50), fmt_tok(a.in_tok),
                           fmt_tok(a.uncached), fmt_pct(a.cache_ratio),
                           fmt_tok(a.out_tok), key=name)

            # --- recent requests
            rt = self.query_one("#requests-table", DataTable)
            rt.clear()
            for r in rows[:200]:
                tps = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
                e2e = None if r["failed"] else e2e_tps(r["output_tokens"],
                                                     r["latency_ms"])
                total, _, read, _ = token_parts(r)
                ratio = read / total if total else None
                rt.add_row(
                    datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
                    short_model(r), r["effort"] or "",
                    fmt_tok(total), fmt_pct(ratio), fmt_tok(r["output_tokens"]),
                    fmt_ms(r["ttft_ms"]), fmt_ms(r["latency_ms"]),
                    "burst" if (not r["failed"] and tps is None
                                and (r["output_tokens"] or 0) >= MIN_DECODE_TOKENS
                                and r["latency_ms"] and r["ttft_ms"])
                    else fmt_tps(tps),
                    fmt_tps(e2e), (r["session_id"] or "")[-8:],
                    "✗" if r["failed"] else "✓",
                    key=r["id"])

            # --- top sessions
            st = self.query_one("#sessions-table", DataTable)
            st.clear()
            groups = group_by(rows, lambda r: r["session_id"] or "—")
            top = sorted(groups.items(),
                         key=lambda kv: -sum((r["output_tokens"] or 0) + token_parts(r)[0]
                                             for r in kv[1]))[:30]
            for sid, grp in top:
                a = aggregate(grp)
                st.add_row(sid[:16], str(a.n), str(a.failed),
                           fmt_tok(a.in_tok), fmt_tok(a.uncached),
                           fmt_tok(a.out_tok), fmt_ms(a.lat_p50),
                           datetime.fromtimestamp(max(r["ts"] for r in grp))
                           .strftime("%H:%M:%S"), key=sid)

            # --- totals pane
            tot = self.query_one("#totals-pane", Static)
            tot.update(
                f"[bold]all time[/]\n"
                f"  requests    {totals.n}  ({totals.n - totals.failed} ok, "
                f"{totals.failed} failed)\n"
                f"  since       {datetime.fromtimestamp(totals.first_ts) if totals.first_ts else '—'}\n"
                f"  input       {fmt_tok(totals.in_tok)}   "
                f"({fmt_tok(totals.uncached)} new · "
                f"{fmt_tok(totals.cache_read + totals.cache_write)} cached)\n"
                f"  cache       read {fmt_tok(totals.cache_read)} · "
                f"write {fmt_tok(totals.cache_write)} · "
                f"hit {fmt_pct(totals.cache_ratio)}\n"
                f"  output      {fmt_tok(totals.out_tok)}"
                + (f"  reasoning {fmt_tok(totals.reasoning_tok)}"
                   if totals.reasoning_tok else "")
                + (f"\n  est. cost   {fmt_cost(totals.cost_usd)}"
                   if totals.cost_usd is not None else "")
                + f"\n  token data  {fmt_pct(totals.quality_ratio)} clean "
                f"({totals.n_complete}/{totals.n})")

        # ------------------------------------------------------------ keys

        def action_win(self, key):
            self.window_key = key
            self.refresh_data()

        def action_toggle_pause(self):
            self.paused = not self.paused
            self._render_all()

        def action_toggle_filter(self):
            w = self.query_one("#filter", Input)
            w.display = not w.display
            if w.display:
                w.focus()
            else:
                w.value = ""
                self.model_filter = ""
                self.refresh_data()

        def on_input_submitted(self, event):
            self.model_filter = event.value.strip()
            self.query_one("#filter", Input).display = False
            self.refresh_data()

        def on_input_changed(self, event):
            self.model_filter = event.value.strip()
            self.refresh_data()

        def _selected_row(self):
            rt = self.query_one("#requests-table", DataTable)
            row_key = rt.cursor_row
            if row_key is None or row_key >= len(rt.rows):
                return None
            try:
                key = rt.coordinate_to_cell_key(
                    rt.cursor_coordinate).row_key.value
            except Exception:
                return None
            return next((r for r in self.rows if r["id"] == key), None)

        def action_detail(self):
            row = self._selected_row()
            if row:
                self.push_screen(DetailScreen(row))

        def on_data_table_row_selected(self, event):
            # Enter on a highlighted row also opens detail.
            if event.data_table.id == "requests":
                self.action_detail()

    return StatsApp


def cmd_tui(args) -> None:
    try:
        import textual  # noqa: F401
    except ImportError:
        from .collector import die

        die("the TUI needs 'textual': pip install --user textual")
    AppCls = build_app()
    app = AppCls(args.db, pricing=getattr(args, "pricing", None))
    app.run()
