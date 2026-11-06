"""Interactive TUI: `cpa-stats tui`.

Layout
    header   collector status · window · totals
    cards    all-time + per-window stat cards
    body     per-model table  |  recent requests (selectable)
    footer   keybindings

Keys
    1-7     window 15m / 1h / 6h / 24h / 7d / 30d / all
    up/down   select a row (mouse wheel scrolls; click selects)
    left/right switch right-hand tabs (recent / sessions / quota / totals)
    tab       cycle focus between panels
    enter     detail modal (request or credential)
    enter on models row    filter everything to that model (again to clear)
    f         filter (model substring)
    enter on sessions row  filter everything to that session (again to clear)
    p         pause / resume refresh
    r         force refresh
    q         quit
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .format import (fmt_age, fmt_cost, fmt_countdown, fmt_ms, fmt_pct, fmt_tok,
                     fmt_tps, sparkline, window_label)
from .model import (MIN_DECODE_MS, MIN_DECODE_TOKENS, aggregate, bucket_counts,
                    cred_sort_key, decode_tps, e2e_tps, field, group_by,
                    parse_credentials, parse_iso_ts, quota_forecast,
                    quota_pct_series, request_detail_pairs, short_endpoint,
                    short_model, token_parts)
from .storage import (db_totals, load_quota_history, load_quota_snapshot,
                      load_rows, meta_get)

WINDOWS = {  # key -> seconds
    "1": ("15m", 15 * 60),
    "2": ("1h", 60 * 60),
    "3": ("6h", 6 * 3600),
    "4": ("24h", 24 * 3600),
    "5": ("7d", 7 * 86400),
    "6": ("30d", 30 * 86400),
    "7": ("all", None),
}


def build_app():
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (DataTable, Footer, Header, Input, Static,
                                 TabbedContent, TabPane)
    from rich.markup import escape

    class DetailScreen(ModalScreen):
        """Modal showing every field of one request row."""

        BINDINGS = [Binding("escape,q,enter", "dismiss", "close")]

        def __init__(self, row):
            super().__init__()
            self.row = row

        def compose(self):
            r = self.row
            grid = request_detail_pairs(r)
            text = "\n".join(
                f"[dim]{k:>24}[/]  {escape(v)}" for k, v in grid)
            yield Vertical(
                Static(f"[bold cyan]{short_model(r)}[/] · {r['id'][:12]}"),
                Static(text),
                Static("[dim]esc / enter to close[/]"),
                id="detail",
            )

    class CredScreen(ModalScreen):
        """Modal showing quota signals / cooldowns for one credential."""

        BINDINGS = [Binding("escape,q,enter", "dismiss", "close")]

        def __init__(self, cred, history=None):
            super().__init__()
            self.cred = cred
            self.history = history or []

        def compose(self):
            c = self.cred
            lines = []
            state = c.status + (" · blocked" if c.blocked else "")
            if c.limited:
                state += " · limited"
            lines.append(f"[dim]{'status':>16}[/]  {escape(state)}")
            if c.status_message:
                lines.append(f"[dim]{'message':>16}[/]  [red]{escape(c.status_message)}[/]")
            lines.append(f"[dim]{'requests':>16}[/]  {c.ok} ok · {c.fail} failed")
            for key, tag in (("daily_pct", c.daily_label or "daily"),
                             ("weekly_pct", c.weekly_label or "weekly")):
                series = quota_pct_series(self.history, key)
                if len(series) >= 2:
                    fc = quota_forecast(self.history, key)
                    eta = (f" −{fc[1]:.0f}%/day → 0% in "
                           f"{fmt_countdown(fc[2] - time.time())}" if fc
                           else " · steady")
                    lines.append(
                        f"[dim]{tag:>16}[/]  [cyan]{sparkline(series[-40:])}[/]"
                        f"  {series[-1]}%{escape(eta)}")
            if c.retry_after_ts:
                lines.append(f"[dim]{'retry-after':>16}[/]  "
                             f"{escape(_fmt_reset(c.retry_after_ts))}")
            for lim in c.limits or []:
                bits = []
                for tag in ("short", "long"):
                    w = lim.get(tag)
                    if not w:
                        continue
                    s = (f"{tag} {w['pct']}%" if w.get("pct") is not None
                         else f"{tag} —")
                    if w.get("window"):
                        s += f"/{w['window']}"
                    if w.get("reset"):
                        s += f" resets {_fmt_reset(w['reset'])}"
                    bits.append(s)
                if lim.get("limited"):
                    bits.append("LIMITED")
                lines.append(f"[dim]{'limit':>16}[/]  "
                             f"{escape(str(lim['label']))}: {escape(' · '.join(bits))}")
            if c.credits:
                rendered = " ".join(f"{escape(str(k))}={escape(str(v))}"
                                    for k, v in sorted(c.credits.items()))
                lines.append(f"[dim]{'credits':>16}[/]  {rendered}")
            for k in sorted(c.signals or {}):
                lines.append(f"[dim]{'signal':>16}[/]  {escape(str(k))} = {escape(str(c.signals[k]))}")
            for cd in c.cooldowns or []:
                if not isinstance(cd, dict):
                    continue
                scope = cd.get("scope") or ""
                mk = cd.get("model_key") or ""
                bits = " ".join(
                    f"{escape(str(k))}={escape(str(v))}" for k, v in sorted(cd.items())
                    if v not in (None, ""))
                lines.append(f"[dim]{'cooldown':>16}[/]  {escape(scope)}"
                             + (f"/{escape(str(mk))}" if mk else "") + f"  {bits}")
            for model_name, q in sorted((c.model_quotas or {}).items()):
                sig = q.get("signals") if isinstance(q, dict) else {}
                rendered = " ".join(f"{escape(str(k))}={escape(str(v))}"
                                    for k, v in sorted((sig or {}).items())) or "—"
                lines.append(f"[dim]{'model quota':>16}[/]  {escape(str(model_name))}: {rendered}")
            if c.observed_at:
                lines.append(f"[dim]{'observed':>16}[/]  {escape(c.observed_at)}")
            yield Vertical(
                Static(f"[bold cyan]{escape(c.account)}[/] · {escape(c.provider)}"),
                Static("\n".join(lines)),
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
        /* TabbedContent/TabPane default to height:auto — without these the
           switcher collapses to the tab bar and tables render 0 rows tall. */
        #right { height: 100%; }
        #right ContentSwitcher { height: 1fr; }
        #right TabPane { height: 100%; }
        DataTable { height: 100%; }
        #detail { width: 92%; max-width: 110; height: auto; max-height: 90%;
                  padding: 1 2; border: thick $primary; background: $surface; }
        #filter { dock: bottom; height: 3; display: none; }
        #statusbar { height: 1; padding: 0 1; }
        """

        BINDINGS = [
            Binding("q", "quit", "quit"),
            Binding("1", "win('1')", "15m"),
            Binding("2", "win('2')", "1h"),
            Binding("3", "win('3')", "6h"),
            Binding("4", "win('4')", "24h"),
            Binding("5", "win('5')", "7d"),
            Binding("6", "win('6')", "30d"),
            Binding("7", "win('7')", "all"),
            Binding("f", "toggle_filter", "filter"),
            Binding("p", "toggle_pause", "pause"),
            Binding("r", "refresh_data", "refresh"),
            Binding("enter", "detail", "detail"),
            Binding("left", "tab(-1)", "◀ tab"),
            Binding("right", "tab(+1)", "tab ▶"),
        ]

        TAB_IDS = ["recent", "sessions", "quota", "totals"]

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
        session_filter = reactive("")

        def __init__(self, db_path, pricing=None, refresh_s=2.0):
            super().__init__()
            self.db_path = Path(db_path)
            self.pricing = pricing
            self.refresh_s = refresh_s
            self.rows = []
            self.creds = []
            self.creds_hist = {}
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
                    with TabPane("quota", id="quota"):
                        yield DataTable(id="quota-table", cursor_type="row",
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
            """Window in seconds; None means 'all stored rows'."""
            return WINDOWS[self.window_key][1]

        def _tick(self):
            if not self.paused:
                self.refresh_data()

        def refresh_data(self):
            if not self.conn:
                return
            win = self._window_s()
            since = time.time() - win if win else 0
            self.rows = load_rows(self.conn, since, self.model_filter or None,
                                  self.session_filter or None)
            self.creds = parse_credentials(load_quota_snapshot(self.conn))
            self.creds_hist = group_by(load_quota_history(self.conn),
                                       lambda r: r["name"])
            self._render_all()

        # ------------------------------------------------------------ render

        def _init_tables(self):
            m = self.query_one("#models", DataTable)
            m.add_columns("model", "req", "fail", "TTFT p50", "TPS p50", "e2e p50",
                          "in", "new", "hit", "out")
            rq = self.query_one("#requests-table", DataTable)
            rq.add_columns("time", "model", "endpoint", "effort", "in", "hit", "out",
                           "TTFT", "lat", "TPS", "e2e", "session", "")
            s = self.query_one("#sessions-table", DataTable)
            s.add_columns("session", "req", "fail", "in", "new", "out",
                          "lat p50", "last")
            qt = self.query_one("#quota-table", DataTable)
            qt.add_columns("account", "status", "ok/fail", "plan",
                           "daily", "weekly", "data", "resets d/w", "0% eta",
                           "signals")

        def _card(self, title, value, sub=""):
            return Static(
                f"[dim]{title}[/]\n[bold]{value}[/]\n[dim]{sub}[/]",
                classes="statcard")

        def _ensure_cards(self):
            """Mount stat cards once; update their text in place afterwards."""
            cards = self.query_one("#cards", Horizontal)
            if cards.children:
                return cards.children
            specs = ["req", "ttft", "tps", "lat", "input", "cache", "output"]
            if self.pricing and self.pricing.enabled:
                specs.append("cost")
            cards.mount(*(self._card(s, "") for s in specs))
            return cards.children

        def _set_card(self, cards, idx, title, value, sub=""):
            cards[idx].update(f"[dim]{title}[/]\n[bold]{value}[/]\n[dim]{sub}[/]")

        def _render_all(self):
            rows = self.rows
            win = self._window_s()
            # "all" spans the oldest stored row, not a fixed duration.
            span = win or (time.time() - min((r["ts"] for r in rows),
                                             default=time.time())) or 60
            agg = aggregate(rows, win, self.pricing)
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
                + _quota_chip(self.creds)
                + (f"   [magenta]sess {self.session_filter[-8:]}[/]"
                   if self.session_filter else "")
                + (f"   filter [magenta]{self.model_filter}[/]" if self.model_filter else "")
                + ("   [yellow]PAUSED[/]" if self.paused else ""))

            # --- cards row (mounted once in _ensure_cards; text updated here)
            ok = agg.n - agg.failed
            succ = f"{ok / agg.n * 100:.1f}% ok" if agg.n else "no requests"
            card_widgets = self._ensure_cards()
            labels = [
                (f"req · {WINDOWS[self.window_key][0]}", f"{agg.n}",
                 f"{succ} · {agg.rpm:.1f}/min" if agg.rpm else succ),
                ("TTFT p50", fmt_ms(agg.ttft_p50), f"p95 {fmt_ms(agg.ttft_p95)}"),
                ("TPS decode/e2e", f"{fmt_tps(agg.tps_p50)} / {fmt_tps(agg.e2e_p50)}",
                 f"p10 {fmt_tps(agg.tps_p10)} · p90 {fmt_tps(agg.tps_p90)}"),
                ("latency p50", fmt_ms(agg.lat_p50), f"p95 {fmt_ms(agg.lat_p95)}"),
                ("input", fmt_tok(agg.in_tok),
                 f"{fmt_tok(agg.uncached)} new · "
                 f"{fmt_tok(agg.cache_read + agg.cache_write)} cached"),
                ("cache", f"hit {fmt_pct(agg.cache_ratio)}",
                 f"read {fmt_tok(agg.cache_read)} · write {fmt_tok(agg.cache_write)}"),
                ("output", fmt_tok(agg.out_tok),
                 f"{agg.burst}× burst" if agg.burst else ""),
            ]
            if agg.cost_usd is not None and len(card_widgets) > 7:
                labels.append(("est. cost", fmt_cost(agg.cost_usd),
                               f"all-time {fmt_cost(totals.cost_usd)}"))
            for i, (t, v, s) in enumerate(labels):
                self._set_card(card_widgets, i, t, v, s)

            # --- activity sparkline
            counts = bucket_counts(rows, time.time() - span, span / 40, 40)
            tok_h = (sum(token_parts(r)[0] + (r["output_tokens"] or 0)
                         for r in rows) / (span / 3600))
            act = self.query_one("#activity", Static)
            act.update(
                f"activity {sparkline(counts)}  "
                f"[dim]{sum(counts)} req · peak {max(counts)}/bucket"
                f" · {fmt_tok(tok_h)} tok/h "
                f"· tokens clean {fmt_pct(agg.quality_ratio)}[/]")

            # --- per model
            mt = self.query_one("#models", DataTable)
            mt.clear()
            for name, grp in sorted(group_by(rows, short_model).items(),
                                    key=lambda kv: -len(kv[1])):
                a = aggregate(grp, win)
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
                    short_model(r), short_endpoint(field(r, "endpoint")),
                    r["effort"] or "",
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

            # --- quota / credentials
            qt = self.query_one("#quota-table", DataTable)
            qt.clear()
            for c in sorted(self.creds, key=cred_sort_key):
                state = c.status + (" · blocked" if c.blocked else "") \
                    + (" · limited" if c.limited else "")
                resets = f"{_fmt_reset(c.daily_reset)}/{_fmt_reset(c.weekly_reset)}"
                extra = " ".join(f"{k}={v}" for k, v in
                                 list((c.extra or {}).items())[:8])
                eta = _quota_eta_text(self.creds_hist.get(c.name))
                obs = parse_iso_ts(c.observed_at)
                qt.add_row(
                    c.account + (f" · {c.provider}" if c.provider else ""), state,
                    f"{c.ok}/{c.fail}", c.plan or "—",
                    (f"{c.daily_pct}% {c.daily_label}"
                     if c.daily_label not in ("daily", "") else f"{c.daily_pct}%")
                    if c.daily_pct is not None else "—",
                    (f"{c.weekly_pct}% {c.weekly_label}"
                     if c.weekly_label not in ("weekly", "") else f"{c.weekly_pct}%")
                    if c.weekly_pct is not None else "—",
                    (fmt_age(time.time() - obs).replace(" ago", "")
                     if obs else "—"),
                    resets if resets != "—/—" else "—", eta or "—",
                    extra or "—", key=c.name)

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

        def _selected_cred(self):
            qt = self.query_one("#quota-table", DataTable)
            if qt.cursor_row is None or qt.cursor_row >= len(qt.rows):
                return None
            try:
                key = qt.coordinate_to_cell_key(
                    qt.cursor_coordinate).row_key.value
            except Exception:
                return None
            return next((c for c in self.creds if c.name == key), None)

        def action_detail(self):
            if self.focused is not None and getattr(self.focused, "id", "") == "quota-table":
                cred = self._selected_cred()
                if cred:
                    self.push_screen(CredScreen(
                        cred, self.creds_hist.get(cred.name)))
                return
            row = self._selected_row()
            if row:
                self.push_screen(DetailScreen(row))

        def on_data_table_row_selected(self, event):
            # Enter on a highlighted row also opens detail.
            if event.data_table.id == "requests-table":
                self.action_detail()
            elif event.data_table.id == "quota-table":
                cred = self._selected_cred()
                if cred:
                    self.push_screen(CredScreen(
                        cred, self.creds_hist.get(cred.name)))
            elif event.data_table.id == "sessions-table":
                sid = self._selected_session()
                if sid:
                    self.session_filter = ("" if self.session_filter == sid
                                           else sid)
                    self.refresh_data()
            elif event.data_table.id == "models":
                mdl = self._selected_model()
                if mdl:
                    self.model_filter = ("" if self.model_filter == mdl
                                         else mdl)
                    self.refresh_data()

        def _selected_model(self):
            mt = self.query_one("#models", DataTable)
            if mt.cursor_row is None or mt.cursor_row >= len(mt.rows):
                return None
            try:
                return mt.coordinate_to_cell_key(
                    mt.cursor_coordinate).row_key.value
            except Exception:
                return None

        def _selected_session(self):
            st = self.query_one("#sessions-table", DataTable)
            if st.cursor_row is None or st.cursor_row >= len(st.rows):
                return None
            try:
                return st.coordinate_to_cell_key(
                    st.cursor_coordinate).row_key.value
            except Exception:
                return None

    return StatsApp


def _quota_eta_text(history_for_cred):
    if not history_for_cred:
        return ""
    for key, tag in (("weekly_pct", "w"), ("daily_pct", "d")):
        fc = quota_forecast(history_for_cred, key)
        if fc:
            return f"{tag} −{fc[1]:.0f}%/d → 0 in {fmt_countdown(fc[2] - time.time())}"
    return ""


def _fmt_reset(ts):
    if not ts:
        return "—"
    delta = ts - time.time()
    if delta > 0:
        return fmt_countdown(delta)
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _quota_chip(creds) -> str:
    """statusbar snippet: 'quota d100% w81%' from the lowest percentages."""
    vals = [c for c in creds if c.daily_pct is not None or c.weekly_pct is not None]
    if not vals:
        return ""
    daily = min((c.daily_pct for c in vals if c.daily_pct is not None), default=None)
    weekly = min((c.weekly_pct for c in vals if c.weekly_pct is not None), default=None)
    dl = next(({"daily": "d"}.get(c.daily_label, c.daily_label) for c in vals
               if c.daily_pct is not None and c.daily_pct == daily), "d")
    wl = next(({"weekly": "w"}.get(c.weekly_label, c.weekly_label) for c in vals
               if c.weekly_pct is not None and c.weekly_pct == weekly), "w")
    txt = "   quota"
    if daily is not None:
        txt += f" {dl}{daily}%"
    if weekly is not None:
        txt += f" {wl}{weekly}%"
    low = min(p for p in (daily, weekly) if p is not None)
    style = "green" if low >= 50 else ("yellow" if low >= 20 else "red")
    chip = f"[{style}]{txt}[/]"
    if any(c.blocked for c in creds):
        chip += " [red]blocked[/]"
    elif any(c.limited for c in creds):
        chip += " [red]limited[/]"
    return chip


def cmd_tui(args) -> None:
    try:
        import textual  # noqa: F401
    except ImportError:
        from .collector import die

        die("the TUI needs 'textual': pip install --user textual")
    AppCls = build_app()
    app = AppCls(args.db, pricing=getattr(args, "pricing", None))
    app.run()
