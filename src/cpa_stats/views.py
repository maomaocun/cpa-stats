"""Rich-rendered views: watch dashboard, tail, summary."""

from __future__ import annotations

import time
from datetime import datetime

from .format import (fmt_age, fmt_cost, fmt_countdown, fmt_ms, fmt_pct, fmt_tok,
                     fmt_tps, sparkline, window_label)
from .model import (MIN_DECODE_MS, MIN_DECODE_TOKENS, Agg, aggregate,
                    bucket_counts, cred_sort_key, decode_tps, e2e_tps, field,
                    group_by, parse_credentials, parse_iso_ts, quota_forecast,
                    quota_pace, quota_pct_series, request_detail_pairs,
                    short_endpoint, short_model, signal_pct_series,
                    token_parts, recovery_stats)
from .storage import (db_totals, load_quota_history, load_quota_snapshot,
                      load_rows, meta_get)

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False


def need_rich():
    if not HAVE_RICH:
        from .collector import die

        die("this view needs the 'rich' package: python3 -m pip install --user rich")


def ttft_style(ms):
    if ms is None:
        return "dim"
    if ms < 1500:
        return "green"
    if ms < 4000:
        return "yellow"
    return "red"


def tps_style(v):
    if v is None:
        return "dim"
    if v >= 80:
        return "green"
    if v >= 30:
        return "yellow"
    return "red"


def hit_style(ratio):
    if ratio is None:
        return "dim"
    if ratio >= 0.8:
        return "green"
    if ratio >= 0.4:
        return "yellow"
    return "red"


def quota_style(pct):
    if pct is None:
        return "dim"
    if pct >= 50:
        return "green"
    if pct >= 20:
        return "yellow"
    return "bold red"


def styled(text, style):
    return Text(text, style=style)


def _reset_str(ts):
    """Quota reset instant -> 'in 2d3h' (or the date when already past)."""
    if not ts:
        return "—"
    delta = ts - time.time()
    if delta > 0:
        return f"in {fmt_countdown(delta)}"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def collector_status(conn):
    hb = meta_get(conn, "collector_heartbeat")
    err = meta_get(conn, "collector_last_error") or ""
    age = time.time() - float(hb) if hb else None
    if age is not None and age < 15 and not err:
        return Text("● collector live", style="bold green"), age
    if err:
        _, _, msg = err.partition("|")
        return Text(f"● collector error: {msg[:70]}", style="bold red"), age
    if age is None:
        return Text("● collector never ran", style="bold red"), age
    return Text(f"● collector stale ({fmt_age(age)})", style="bold red"), age


def stat_card(title, value, sub="", style="bold"):
    body = Text(value, style=style, justify="center", no_wrap=True, overflow="ellipsis")
    if sub:
        body.append("\n")
        body.append_text(Text.from_markup(sub, style="dim", justify="center"))
    return Panel(body, title=f"[dim]{title}[/]", box=box.ROUNDED, padding=(0, 1))


def quality_sub(agg: Agg) -> str:
    q = agg.quality_ratio
    if q is None:
        return ""
    return f" · {fmt_pct(q)} clean" if q < 1 else ""


def build_cards(agg: Agg, window_label_s: str):
    ok = agg.n - agg.failed
    succ = f"{ok / agg.n * 100:.1f}% ok" if agg.n else "no requests"
    succ_style = "green" if agg.failed == 0 else ("yellow" if agg.n and agg.failed / agg.n < 0.05 else "red")
    rpm = f" · {agg.rpm:.1f}/min" if agg.rpm is not None else ""
    cards = [
        stat_card(f"requests · {window_label_s}", f"{agg.n}",
                  f"[{succ_style}]{succ}[/]{rpm}{quality_sub(agg)}"),
        stat_card("TTFT p50", fmt_ms(agg.ttft_p50), f"p95 {fmt_ms(agg.ttft_p95)}",
                  style=f"bold {ttft_style(agg.ttft_p50)}"),
        stat_card("TPS p50  decode / e2e", f"{fmt_tps(agg.tps_p50)} / {fmt_tps(agg.e2e_p50)} tok/s",
                  f"p10 {fmt_tps(agg.tps_p10)} · p90 {fmt_tps(agg.tps_p90)}"
                  + (f" · {agg.burst}× burst" if agg.burst else ""),
                  style=f"bold {tps_style(agg.tps_p50)}"),
        stat_card("latency p50", fmt_ms(agg.lat_p50), f"p95 {fmt_ms(agg.lat_p95)}"),
    ]
    if agg.cost_usd is not None:
        cards.append(stat_card("est. cost", fmt_cost(agg.cost_usd),
                               "tokens × price", style="bold yellow"))
    reasoning = (f"reasoning {fmt_tok(agg.reasoning_tok)}" if agg.reasoning_tok
                 else "reasoning not reported")
    token_cards = [
        stat_card("input tokens", fmt_tok(agg.in_tok),
                  f"[bold]{fmt_tok(agg.uncached)}[/] new · [bold]{fmt_tok(agg.cache_read + agg.cache_write)}[/] cached",
                  style="bold cyan"),
        stat_card("cache", f"hit {fmt_pct(agg.cache_ratio)}",
                  f"read {fmt_tok(agg.cache_read)} · write {fmt_tok(agg.cache_write)}",
                  style=f"bold {hit_style(agg.cache_ratio)}"),
        stat_card("output tokens", fmt_tok(agg.out_tok), reasoning, style="bold magenta"),
    ]
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in cards:
        grid.add_column(ratio=1)
    grid.add_row(*cards)
    grid2 = Table.grid(expand=True, padding=(0, 1))
    for _ in token_cards:
        grid2.add_column(ratio=1)
    grid2.add_row(*token_cards)
    return Group(grid, grid2)


def build_total_cards(tot: Agg):
    """All-time totals row, shown above the per-window stats."""
    since = (datetime.fromtimestamp(tot.first_ts).strftime("%m-%d %H:%M") if tot.first_ts else "—")
    ok = tot.n - tot.failed
    succ = f"{ok / tot.n * 100:.1f}% ok" if tot.n else "no requests"
    cards = [
        stat_card("all time · requests", f"{tot.n}",
                  f"[{'green' if not tot.failed else 'yellow'}]{succ}[/] · since {since}"),
        stat_card("all time · input", fmt_tok(tot.in_tok),
                  f"[bold]{fmt_tok(tot.uncached)}[/] new · [bold]{fmt_tok(tot.cache_read + tot.cache_write)}[/] cached",
                  style="bold cyan"),
        stat_card("all time · cache", f"hit {fmt_pct(tot.cache_ratio)}",
                  f"read {fmt_tok(tot.cache_read)} · write {fmt_tok(tot.cache_write)}",
                  style=f"bold {hit_style(tot.cache_ratio)}"),
        stat_card("all time · output", fmt_tok(tot.out_tok),
                  f"reasoning {fmt_tok(tot.reasoning_tok)}" if tot.reasoning_tok else "",
                  style="bold magenta"),
    ]
    if tot.cost_usd is not None:
        cards.append(stat_card("all time · est. cost", fmt_cost(tot.cost_usd),
                               "tokens × price", style="bold yellow"))
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in cards:
        grid.add_column(ratio=1)
    grid.add_row(*cards)
    return grid


def build_activity(rows, window_s: float, buckets: int = 24):
    """One-line request-rate sparkline over the window."""
    if window_s <= 0 or not rows:
        return None
    counts = bucket_counts(rows, time.time() - window_s, window_s / buckets, buckets)
    tok = sum(token_parts(r)[0] + (r["output_tokens"] or 0) for r in rows)
    line = Text()
    line.append("activity ", style="dim")
    line.append(sparkline(counts), style="cyan")
    line.append(f"  {sum(counts)} req · peak {max(counts)}/{window_s / buckets / 60:.0f}min bucket"
                f" · {fmt_tok(tok / (window_s / 3600))} tok/h", style="dim")
    return line


def _quota_eta(history_for_cred):
    """'w −3%/d → 0 in 2d1h' forecast text from history rows; '' when unknown."""
    if not history_for_cred:
        return ""
    for key, tag in (("weekly_pct", "w"), ("daily_pct", "d")):
        fc = quota_forecast(history_for_cred, key)
        if fc:
            _, per_day, eta = fc
            return f"{tag} −{per_day:.0f}%/d → 0 in {fmt_countdown(eta - time.time())}"
    return ""


def build_alerts(agg: Agg, creds) -> "Text | None":
    """Red warning line(s): blocked credentials, high failure rate."""
    warns = []
    for c in creds:
        if c.blocked or c.limited:
            tag = "blocked" if c.blocked else "upstream limit reached"
            msg = c.status_message.split(":")[-1].strip()[:80]
            weekly = (f"{c.weekly_label} {c.weekly_pct}%"
                      if c.weekly_pct is not None else "quota exhausted")
            warn = f"⚠ {c.account}: {tag} ({weekly}"
            if c.weekly_reset:
                warn += f", resets in {fmt_countdown(c.weekly_reset - time.time())}"
            warn += f") — {msg}" if msg else ")"
            warns.append(warn)
    if agg.n and agg.failed / agg.n > 0.05:
        warns.append(f"⚠ {agg.failed}/{agg.n} requests failed in window "
                     f"({agg.failed / agg.n * 100:.1f}%)")
    if not warns:
        return None
    return Text("\n".join(warns), style="bold red")


def build_quota_table(creds, fetched_at=None, history=None):
    """Per-credential quota / status table (accounts · quota)."""
    title = "accounts · quota"
    if fetched_at:
        title += f" · snapshot {fmt_age(time.time() - fetched_at)}"
    t = Table(box=box.SIMPLE_HEAD, expand=True, title=title,
              title_style="bold cyan", title_justify="left")
    t.add_column("account", style="bold", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("ok/fail", justify="right")
    t.add_column("plan", style="dim")
    t.add_column("daily", justify="right")
    t.add_column("weekly", justify="right")
    t.add_column("data", justify="right", style="dim")
    t.add_column("reset d·w", no_wrap=True)
    t.add_column("0% eta", no_wrap=True)
    t.add_column("signals", overflow="ellipsis", style="dim")
    from rich.markup import escape
    for c in sorted(creds, key=cred_sort_key):
        ok_state = c.status == "active" and not c.blocked and not c.limited
        status_style = "green" if ok_state else ("red" if c.blocked or c.limited
                                                 or c.status in ("error", "disabled") else "yellow")
        status_txt = c.status + (" · blocked" if c.blocked else "") \
            + (" · limited" if c.limited else "")
        resets = (f"{_reset_str(c.daily_reset)} / {_reset_str(c.weekly_reset)}")
        obs_age = parse_iso_ts(c.observed_at)
        obs_txt = (fmt_age(time.time() - obs_age).replace(" ago", "")
                   if obs_age else "—")
        extra = " ".join(f"{escape(str(k))}={escape(str(v))}"
                         for k, v in list((c.extra or {}).items())[:8]) or "—"
        fail_cell = styled(str(c.fail), "red" if c.fail else "dim")
        t.add_row(
            Text.assemble(escape(c.account),
                          (f" · {c.provider}", "dim") if c.provider else "",
                          (f" @{c.instance}", "dim") if c.instance else ""),
            styled(status_txt, status_style),
            Text.assemble(str(c.ok), "/", fail_cell),
            escape(c.plan) if c.plan else "—",
            styled(f"{c.daily_pct}%" + (f" {c.daily_label}"
                                        if c.daily_label not in ("daily", "")
                                        else ""), quota_style(c.daily_pct))
            if c.daily_pct is not None else "—",
            styled(f"{c.weekly_pct}%" + (f" {c.weekly_label}"
                                         if c.weekly_label not in ("weekly", "")
                                         else ""), quota_style(c.weekly_pct))
            if c.weekly_pct is not None else "—",
            styled(obs_txt, "yellow" if obs_age
                   and time.time() - obs_age > 300 else "dim"),
            resets if resets != "— / —" else "—",
            styled(_quota_eta((history or {}).get(c.name)) or "—",
                   "bold red" if c.blocked or c.limited else "dim"),
            extra,
        )
    if not creds:
        t.add_row(Text("no credentials in snapshot", style="dim"))
    return t


def show_reasoning(rows) -> bool:
    return any((r["reasoning_tokens"] or 0) > 0 for r in rows)


def build_model_table(rows, window_s):
    t = Table(box=box.SIMPLE_HEAD, expand=True, title="per model", title_style="bold cyan",
              title_justify="left")
    t.add_column("model", style="bold")
    t.add_column("req", justify="right")
    t.add_column("fail", justify="right")
    t.add_column("TTFT p50", justify="right")
    t.add_column("TTFT p95", justify="right")
    t.add_column("TPS p50", justify="right")
    t.add_column("TPS p10–p90", justify="right")
    t.add_column("e2e p50", justify="right")
    t.add_column("lat p50", justify="right")
    t.add_column("in", justify="right")
    t.add_column("new", justify="right")
    t.add_column("c.read", justify="right")
    t.add_column("c.write", justify="right")
    t.add_column("hit", justify="right")
    t.add_column("out", justify="right")
    with_reason = show_reasoning(rows)
    if with_reason:
        t.add_column("reason", justify="right")
    groups = group_by(rows, short_model)
    for name, grp in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        a = aggregate(grp, window_s)
        t.add_row(
            name,
            str(a.n),
            styled(str(a.failed), "red" if a.failed else "dim"),
            styled(fmt_ms(a.ttft_p50), ttft_style(a.ttft_p50)),
            styled(fmt_ms(a.ttft_p95), ttft_style(a.ttft_p95)),
            styled(fmt_tps(a.tps_p50), tps_style(a.tps_p50)),
            f"{fmt_tps(a.tps_p10)}–{fmt_tps(a.tps_p90)}",
            styled(fmt_tps(a.e2e_p50), tps_style(a.e2e_p50)),
            fmt_ms(a.lat_p50),
            fmt_tok(a.in_tok),
            fmt_tok(a.uncached),
            fmt_tok(a.cache_read),
            fmt_tok(a.cache_write),
            styled(fmt_pct(a.cache_ratio), hit_style(a.cache_ratio)),
            fmt_tok(a.out_tok),
            *([fmt_tok(a.reasoning_tok)] if with_reason else []),
        )
    if not groups:
        t.add_row(Text("no requests in window", style="dim"))
    return t


def build_session_table(rows, limit=5):
    """Top sessions by total tokens in the window."""
    t = Table(box=box.SIMPLE_HEAD, expand=True, title="top sessions · tokens",
              title_style="bold cyan", title_justify="left")
    t.add_column("session", style="bold", no_wrap=True)
    t.add_column("req", justify="right")
    t.add_column("fail", justify="right")
    t.add_column("in", justify="right")
    t.add_column("new", justify="right")
    t.add_column("out", justify="right")
    t.add_column("lat p50", justify="right")
    t.add_column("last", justify="right", style="dim")
    groups = group_by(rows, lambda r: r["session_id"] or "—")
    top = sorted(groups.items(), key=lambda kv: -sum((r["output_tokens"] or 0) + token_parts(r)[0]
                                                     for r in kv[1]))[:limit]
    for sid, grp in top:
        if sid == "—":
            continue
        a = aggregate(grp)
        last = datetime.fromtimestamp(max(r["ts"] for r in grp)).strftime("%H:%M:%S")
        t.add_row(sid[:12], str(a.n), styled(str(a.failed), "red" if a.failed else "dim"),
                  fmt_tok(a.in_tok), fmt_tok(a.uncached), fmt_tok(a.out_tok),
                  fmt_ms(a.lat_p50), last)
    if not top:
        t.add_row(Text("no requests in window", style="dim"))
    return t


def build_recent_table(rows, limit):
    t = Table(box=box.SIMPLE_HEAD, expand=True, title="recent requests", title_style="bold cyan",
              title_justify="left")
    t.add_column("time", style="dim", no_wrap=True)
    t.add_column("model", no_wrap=True)
    t.add_column("endpoint", style="dim", no_wrap=True)
    t.add_column("effort", style="dim")
    t.add_column("in", justify="right")
    t.add_column("new", justify="right")
    t.add_column("c.read", justify="right")
    t.add_column("c.write", justify="right")
    t.add_column("hit", justify="right")
    t.add_column("out", justify="right")
    with_reason = show_reasoning(rows[:limit])
    if with_reason:
        t.add_column("reason", justify="right")
    t.add_column("TTFT", justify="right")
    t.add_column("latency", justify="right")
    t.add_column("TPS", justify="right")
    t.add_column("e2e", justify="right")
    t.add_column("session", style="dim", no_wrap=True)
    t.add_column("status", no_wrap=True)
    for r in rows[:limit]:
        tps = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
        e2e = None if r["failed"] else e2e_tps(r["output_tokens"], r["latency_ms"])
        burst = (not r["failed"] and tps is None and (r["output_tokens"] or 0) >= MIN_DECODE_TOKENS
                 and r["latency_ms"] and r["ttft_ms"])
        total, uncached, read, write = token_parts(r)
        ratio = read / total if total else None
        status = (Text("✗ " + str(r["status_code"] or "err"), style="bold red")
                  if r["failed"] else Text("✓", style="green"))
        t.add_row(
            datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
            short_model(r),
            short_endpoint(field(r, "endpoint")),
            r["effort"] or "",
            fmt_tok(total),
            fmt_tok(uncached),
            fmt_tok(read),
            styled(fmt_tok(write), "dim" if not write else ""),
            styled(fmt_pct(ratio), hit_style(ratio)),
            fmt_tok(r["output_tokens"]),
            *([fmt_tok(r["reasoning_tokens"])] if with_reason else []),
            styled(fmt_ms(r["ttft_ms"]), ttft_style(r["ttft_ms"])),
            fmt_ms(r["latency_ms"]),
            styled("burst", "dim italic") if burst else styled(fmt_tps(tps), tps_style(tps)),
            styled(fmt_tps(e2e), tps_style(e2e)),
            (r["session_id"] or "")[-8:],
            status,
        )
    if not rows:
        t.add_row(Text("waiting for requests…", style="dim"))
    return t


def render_dashboard(conn, args, console):
    window_s = args.window
    rows = load_rows(conn, time.time() - window_s, args.model, args.session,
                     account=getattr(args, "account", None))
    pricing = getattr(args, "pricing", None)
    agg = aggregate(rows, window_s, pricing)
    status, _ = collector_status(conn)
    totals = db_totals(conn, pricing)
    total = totals.n
    snap = load_quota_snapshot(conn)
    creds = parse_credentials(snap)
    quota_hist = group_by(load_quota_history(conn), lambda r: r["name"])
    header = Table.grid(expand=True)
    header.add_column()
    header.add_column(justify="right")
    title = Text()
    title.append(" cpa-stats ", style="bold black on cyan")
    title.append(f"  window {window_label(window_s)}", style="bold")
    if args.model:
        title.append(f" · model~{args.model}", style="magenta")
    if args.session:
        title.append(f" · session {args.session}", style="magenta")
    if getattr(args, "account", None):
        title.append(f" · acct~{args.account}", style="magenta")
    right = Text()
    right.append_text(status)
    right.append(f"   {total} stored · {datetime.now().strftime('%H:%M:%S')}", style="dim")
    header.add_row(title, right)

    height = console.size.height
    # Fixed overhead: header + totals row + window rows + tokens row + activity
    # line + session table + footer ≈ 38 lines; per-model table adds up to 6.
    quota_lines = len(creds) + 2 if creds else 1
    recent_limit = args.limit or max(5, height - 38 - quota_lines
                                   - min(len(group_by(rows, short_model)), 6))
    footer = Text(
        "TTFT = time to first token · TPS = out / (latency − TTFT) · e2e = out / latency · "
        f"burst = upstream sent everything within {MIN_DECODE_MS}ms (Devin buffers tool calls)\n"
        "in = new + c.read + c.write · hit = c.read / in · "
        + ("cost = est. USD from --price · " if getattr(args, "pricing", None)
           and args.pricing.enabled else "")
        + ("" if show_reasoning(rows) else "reasoning tokens: not reported by upstream "
           "(Devin counts them inside out) · ")
        + "Ctrl-C to quit",
        style="dim",
    )
    parts = [header, build_total_cards(totals), build_cards(agg, window_label(window_s))]
    act = build_activity(rows, window_s)
    if act is not None:
        parts.append(act)
    if creds:
        alerts = build_alerts(agg, creds)
        if alerts:
            parts.append(alerts)
        parts.append(build_quota_table(creds, (snap or {}).get("fetched_at"),
                                       quota_hist))
    else:
        parts.append(Text("quota: no credentials snapshot yet — restart "
                          "`cpa-stats collect` to populate (every --quota-interval s)",
                          style="dim"))
    parts += [build_model_table(rows, window_s), build_session_table(rows),
              build_recent_table(rows, recent_limit), footer]
    return Group(*parts)


def cmd_watch(args) -> None:
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console()
    if not console.is_terminal or args.once:
        console.print(render_dashboard(conn, args, console))
        return
    with Live(render_dashboard(conn, args, console), console=console, screen=True,
              refresh_per_second=4, transient=False) as live:
        try:
            while True:
                time.sleep(args.refresh)
                live.update(render_dashboard(conn, args, console))
        except KeyboardInterrupt:
            pass


def cmd_tail(args) -> None:
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console(highlight=False)
    since = time.time() - args.since if args.since else time.time()
    last_ts, seen = since, set()
    console.print(Text("tailing requests (Ctrl-C to quit)", style="dim"))
    try:
        while True:
            rows = list(reversed(load_rows(conn, last_ts - 1, args.model,
                                         args.session,
                                         account=getattr(args, "account", None))))
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                last_ts = max(last_ts, r["ts"])
                tps = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
                total, uncached, read, write = token_parts(r)
                ratio = read / total if total else None
                line = Text()
                line.append(datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S "), style="dim")
                if r["failed"]:
                    line.append("✗ ", style="bold red")
                else:
                    line.append("✓ ", style="green")
                line.append(f"{short_model(r):<18}", style="bold")
                ep = short_endpoint(field(r, "endpoint"))
                if ep:
                    line.append(f"{ep:<12}", style="dim")
                resp = field(r, "response_model")
                if resp and resp != r["model"] and resp != r["alias"]:
                    line.append(f"→{resp} ", style="dim")
                line.append(f"{(r['effort'] or ''):<7}", style="dim")
                line.append(f" in {fmt_tok(total):>6}")
                line.append(
                    f" (new {fmt_tok(uncached):>5} · read {fmt_tok(read):>5} · "
                    f"write {fmt_tok(write):>5} · hit {fmt_pct(ratio):>4})", style="dim")
                line.append(f"  out {fmt_tok(r['output_tokens']):>6}")
                if r["reasoning_tokens"]:
                    line.append(f" (reason {fmt_tok(r['reasoning_tokens'])})", style="dim")
                line.append("  TTFT ")
                line.append(f"{fmt_ms(r['ttft_ms']):>7}", style=ttft_style(r["ttft_ms"]))
                line.append(f"  lat {fmt_ms(r['latency_ms']):>7}")
                line.append("  TPS ")
                line.append(f"{fmt_tps(tps):>4}", style=tps_style(tps))
                e2e = None if r["failed"] else e2e_tps(r["output_tokens"], r["latency_ms"])
                line.append("  e2e ")
                line.append(f"{fmt_tps(e2e):>4}", style=tps_style(e2e))
                if r["session_id"]:
                    line.append(f"  sess {r['session_id'][-8:]}", style="dim")
                if r["failed"] and r["error"]:
                    code = f" ({field(r, 'fail_code')})" if field(r, "fail_code") else ""
                    line.append(f"  {r['status_code']}{code}: {r['error'][:120]}",
                                style="red")
                console.print(line, soft_wrap=True)
                if args.verbose:
                    det = Text("      ", style="dim")
                    det.append(f"acct {r['source'] or '—'} "
                               f"· auth {field(r, 'auth_type') or '—'}"
                               f" via {field(r, 'access_provider') or '—'}"
                               f" · endpoint {r['endpoint'] or '—'}")
                    node_bits = [field(r, "node_kind"),
                                 "fork" if field(r, "is_fork") else None,
                                 "compaction" if field(r, "is_compaction") else None]
                    node = "/".join(b for b in node_bits if b)
                    if node:
                        det.append(f" · node {node}")
                    if field(r, "parent_session_id"):
                        det.append(f" · parent {field(r, 'parent_session_id')}")
                    tiers = [r["service_tier"],
                             field(r, "effective_service_tier"),
                             field(r, "response_service_tier"),
                             field(r, "upstream_service_tier")]
                    if any(tiers):
                        det.append(f" · tier {'→'.join(t or '—' for t in tiers)}")
                    det.append(f" · exec {r['id'][:12]}"
                               f" · trace {field(r, 'trace_id') or '—'}")
                    if field(r, "client_ip"):
                        det.append(f" · ip {field(r, 'client_ip')}")
                    if field(r, "speed") or field(r, "inference_geo"):
                        det.append(f" · {field(r, 'speed') or '—'}"
                                   f"/{field(r, 'inference_geo') or '—'}")
                    console.print(det, soft_wrap=True)
            if len(seen) > 5000 and rows:
                seen = {r["id"] for r in rows}
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass


def cmd_summary(args) -> None:
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console()
    now = time.time()
    rows = load_rows(conn, now - args.since, args.model, args.session,
                     account=getattr(args, "account", None))
    agg = aggregate(rows, args.since, getattr(args, "pricing", None))
    console.print(Text(f"cpa-stats summary · last {window_label(args.since)}", style="bold cyan"))
    console.print(build_cards(agg, window_label(args.since)))
    prev_rows = [r for r in load_rows(conn, now - 2 * args.since,
                                    args.model, args.session,
                                    account=getattr(args, "account", None))
                 if r["ts"] < now - args.since]
    if prev_rows:
        prev = aggregate(prev_rows, args.since)

        def d(cur, old, unit="%"):
            if not old:
                return "n/a"
            diff = (cur - old) / old * 100
            sign = "+" if diff >= 0 else "−"
            return f"{sign}{abs(diff):.0f}{unit}"

        fail_now = agg.failed / agg.n * 100 if agg.n else 0
        fail_prev = prev.failed / prev.n * 100 if prev.n else 0
        fd = fail_now - fail_prev
        console.print(Text(
            f"vs previous {window_label(args.since)}:  "
            f"req {agg.n} ({d(agg.n, prev.n)})  "
            f"fail {fail_now:.1f}% ({'+' if fd >= 0 else '−'}"
            f"{abs(fd):.1f}pt)  "
            f"in {fmt_tok(agg.in_tok)} ({d(agg.in_tok, prev.in_tok)})  "
            f"out {fmt_tok(agg.out_tok)} ({d(agg.out_tok, prev.out_tok)})  "
            f"ttft p50 {fmt_ms(agg.ttft_p50)} "
            f"({d(agg.ttft_p50 or 0, prev.ttft_p50 or 0)})  "
            f"lat p50 {fmt_ms(agg.lat_p50)} "
            f"({d(agg.lat_p50 or 0, prev.lat_p50 or 0)})",
            style="dim"))
    keyfn = {
        "model": short_model,
        "session": lambda r: (r["session_id"] or "—"),
        "account": lambda r: (r["source"] or r["auth_index"] or "—"),
        "effort": lambda r: f"{short_model(r)} · {r['effort'] or '—'}",
        "endpoint": lambda r: (r["endpoint"] or "—"),
        "provider": lambda r: (r["provider"] or "—"),
        "upstream": lambda r: (r["response_model"] or "—"),
        "node_kind": lambda r: (field(r, "node_kind") or "—"),
        "hour": lambda r: datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:00"),
        "day": lambda r: datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d"),
    }[args.by]
    t = Table(box=box.SIMPLE_HEAD, title=f"by {args.by}", title_style="bold cyan",
              title_justify="left")
    for col, just in (
        (args.by, "left"), ("req", "right"), ("fail", "right"), ("TTFT p50", "right"),
        ("TTFT p95", "right"), ("TPS p50", "right"), ("TPS p10–p90", "right"),
        ("e2e p50", "right"), ("lat p50", "right"), ("lat p95", "right"),
        ("in", "right"), ("new", "right"), ("c.read", "right"), ("c.write", "right"),
        ("hit", "right"), ("out", "right"),
    ):
        t.add_column(col, justify=just)
    with_reason = show_reasoning(rows)
    if with_reason:
        t.add_column("reason", justify="right")
    groups = group_by(rows, keyfn)
    order = sorted(groups.items(), key=(lambda kv: kv[0]) if args.by in ("hour", "day")
                   else (lambda kv: -len(kv[1])))
    for name, grp in order:
        a = aggregate(grp)
        t.add_row(
            name, str(a.n), styled(str(a.failed), "red" if a.failed else "dim"),
            styled(fmt_ms(a.ttft_p50), ttft_style(a.ttft_p50)),
            styled(fmt_ms(a.ttft_p95), ttft_style(a.ttft_p95)),
            styled(fmt_tps(a.tps_p50), tps_style(a.tps_p50)),
            f"{fmt_tps(a.tps_p10)}–{fmt_tps(a.tps_p90)}",
            styled(fmt_tps(a.e2e_p50), tps_style(a.e2e_p50)),
            fmt_ms(a.lat_p50), fmt_ms(a.lat_p95),
            fmt_tok(a.in_tok), fmt_tok(a.uncached), fmt_tok(a.cache_read), fmt_tok(a.cache_write),
            styled(fmt_pct(a.cache_ratio), hit_style(a.cache_ratio)), fmt_tok(a.out_tok),
            *([fmt_tok(a.reasoning_tok)] if with_reason else []),
        )
    if not groups:
        t.add_row(Text("no requests in window", style="dim"))
    console.print(t)
    if rows and not with_reason:
        console.print(Text("reasoning tokens: not reported by upstream "
                           "(Devin counts them inside out)", style="dim"))
    fails = [r for r in rows if r["failed"]]
    if fails:
        ft = Table(box=box.SIMPLE_HEAD, title="recent failures", title_style="bold red",
                   title_justify="left")
        ft.add_column("time", style="dim")
        ft.add_column("model")
        ft.add_column("status", justify="right")
        ft.add_column("error", overflow="fold")
        for r in fails[:10]:
            ft.add_row(datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S"), short_model(r),
                       str(r["status_code"] or ""), (r["error"] or "")[:300])
        console.print(ft)
    snap = load_quota_snapshot(conn)
    creds = parse_credentials(snap)
    if creds:
        quota_hist = group_by(load_quota_history(conn), lambda r: r["name"])
        console.print(build_quota_table(creds, snap.get("fetched_at"),
                                        quota_hist))


def cmd_quota(args) -> None:
    """`cpa-stats quota` — full credential snapshot: quota, cooldowns, signals."""
    need_rich()
    from .storage import open_db, open_db_ro

    conn = open_db(args.db) if getattr(args, "refresh", False) else open_db_ro(args.db)
    snap = None
    if getattr(args, "refresh", False):
        from .collector import (fetch_credentials, instance_tag,
                                load_cpa_configs, refresh_credential,
                                stale_quota_credentials)
        from .storage import save_quota_history, save_quota_snapshot

        cfgs = load_cpa_configs(args)
        all_files: list = []
        errs_all: list = []
        for cfg in cfgs:
            snap = fetch_credentials(cfg)
            names = stale_quota_credentials(snap, 0.0, time.time())
            for n in names:
                err = refresh_credential(cfg, n)
                if err is not None:
                    errs_all.append(err)
            if names:
                snap = fetch_credentials(cfg)
            for f in snap.get("files") or []:
                if isinstance(f, dict):
                    all_files.append(dict(f, _instance=instance_tag(cfg.url))
                                     if len(cfgs) > 1 else f)
        if errs_all:
            console_err = Console(stderr=True)
            console_err.print(
                Text(f"quota refresh failed for {len(errs_all)} credential(s): "
                     f"{errs_all[0]}", style="yellow"))
        payload = {"fetched_at": time.time(),
                   "files": all_files}
        with conn:
            save_quota_snapshot(conn, payload)
            save_quota_history(conn, parse_credentials(payload), time.time())
        snap = payload
    snap = snap or load_quota_snapshot(conn)
    console = Console()
    if not snap or not isinstance(snap.get("files"), list):
        console.print(Text(
            "no credential snapshot yet — restart `cpa-stats collect` "
            "(it stores /management/credentials every --quota-interval seconds)",
            style="dim"))
        return
    creds = parse_credentials(snap)
    fetched = snap.get("fetched_at")
    console.print(Text(
        f"cpa-stats quota · {len(creds)} credential(s)"
        + (f" · snapshot {fmt_age(time.time() - fetched)}" if fetched else ""),
        style="bold cyan"))
    quota_hist = group_by(load_quota_history(conn), lambda r: r["name"])
    console.print(build_quota_table(creds, fetched, quota_hist))
    from rich.markup import escape
    for c in sorted(creds, key=cred_sort_key):
        lines = []
        if c.limited:
            lines.append("[red]limited[/]    upstream reports limit reached / rejected"
                         + (f" · retry {_reset_str(c.retry_after_ts)}"
                            if c.retry_after_ts else ""))
        elif c.retry_after_ts:
            lines.append(f"[yellow]retry[/]      upstream Retry-After "
                         f"{_reset_str(c.retry_after_ts)}")
        if c.status_message:
            lines.append(f"[red]status[/]      {escape(c.status_message)}")
        hist = quota_hist.get(c.name) or []
        for key, tag in (("daily_pct", c.daily_label or "daily"),
                         ("weekly_pct", c.weekly_label or "weekly")):
            series = quota_pct_series(hist, key)
            if len(series) >= 2:
                trend = sparkline(series[-40:])
                fc = quota_forecast(hist, key)
                eta = (f"· −{fc[1]:.0f}%/day → 0% in "
                       f"{fmt_countdown(fc[2] - time.time())}" if fc
                       else "· holding steady")
                lines.append(f"[dim]{tag:>8}[/]     [cyan]{trend}[/] "
                             f"{escape(tag)} quota {series[-1]}% {escape(eta)}")
            now = time.time()
            pct_now = getattr(c, key)
            reset_ts = c.daily_reset if key == "daily_pct" else c.weekly_reset
            p = quota_pace(pct_now, reset_ts,
                           (quota_forecast(hist, key) or (None, None))[1], now)
            if p:
                budget, burn, over = p
                pace_txt = f"[dim]{tag:>8}[/]     pace ≤{budget:.0f}%/day"
                if burn is not None:
                    pace_txt += (f" · burning {burn:.0f}%/day → "
                                 + ("[red]over pace[/]" if over
                                    else "on pace"))
                lines.append(pace_txt)
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
                    s += f" resets {_reset_str(w['reset'])}"
                if w.get("over_pct"):
                    s += f" (+{w['over_pct']:.0f}% over)"
                bits.append(s)
            if lim.get("limited"):
                bits.append("LIMITED")
            lines.append(f"[dim]limit[/]      {escape(str(lim['label']))}: "
                         + escape(" · ".join(bits)))
            # trend from the raw signal key stored in quota_history
            sig_key = lim.get("sig_prefix")
            if sig_key:
                series = signal_pct_series(hist, f"{sig_key}-primary-used-percent")
                if len(series) >= 2:
                    lines.append(f"[dim]{'':>8}[/]     [cyan]"
                                 f"{sparkline(series[-40:])}[/] "
                                 f"{series[-1]}% remaining")
        if c.credits:
            rendered = " ".join(f"{escape(str(k))}={escape(str(v))}"
                                for k, v in sorted(c.credits.items()))
            lines.append(f"[dim]credits[/]    {rendered}")
        for k in sorted(c.signals or {}):
            lines.append(f"[dim]signal[/]      {escape(str(k))} = {escape(str(c.signals[k]))}")
        for cd in c.cooldowns or []:
            if not isinstance(cd, dict):
                continue
            scope = cd.get("scope") or ""
            mk = cd.get("model_key") or ""
            retry = parse_iso_ts(cd.get("retry_at"))
            rem = cd.get("remaining_seconds")
            lines.append(
                f"[yellow]cooldown[/]   {escape(scope)}"
                + (f"/{escape(str(mk))}" if mk else "")
                + f" · {escape(str(cd.get('reason') or ''))}"
                + (f" · retry {datetime.fromtimestamp(retry).strftime('%m-%d %H:%M')}"
                   f" (in {fmt_countdown(retry - time.time())})" if retry else "")
                + (f" · remaining {fmt_countdown(rem)}" if rem else ""))
        for model_name, q in sorted((c.model_quotas or {}).items()):
            sig = q.get("signals") if isinstance(q, dict) else {}
            rendered = " ".join(f"{escape(str(k))}={escape(str(v))}"
                                for k, v in sorted((sig or {}).items())) or "—"
            lines.append(f"[dim]model[/]       {escape(str(model_name))}: {rendered}")
        if c.observed_at:
            lines.append(f"[dim]observed[/]    {escape(c.observed_at)}")
        if lines:
            body = "\n".join(lines)
            console.print(Panel(body, title=f"[bold]{escape(c.account)}[/]",
                                box=box.ROUNDED, padding=(0, 1)))


def cmd_show(args) -> None:
    """`cpa-stats show <id>` — every captured field of one request."""
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console()
    rid = args.request_id
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    where = "id LIKE ? OR request_id LIKE ?"
    params = [f"{rid}%", f"{rid}%"]
    if "trace_id" in cols:
        where += " OR trace_id LIKE ?"
        params.append(f"{rid}%")
    row = conn.execute(
        f"SELECT * FROM requests WHERE {where} ORDER BY ts DESC LIMIT 1",
        params).fetchone()
    if not row:
        console.print(Text(f"no request matching '{rid}'", style="red"))
        raise SystemExit(1)
    from rich.markup import escape

    t = Table(box=box.SIMPLE_HEAD, show_header=False, title="request detail",
              title_style="bold cyan", title_justify="left")
    t.add_column("k", style="dim", justify="right", no_wrap=True)
    t.add_column("v", overflow="fold")
    for k, v in request_detail_pairs(row):
        t.add_row(k, escape(str(v)))
    console.print(t)
    if args.raw:
        raw = field(row, "raw")
        if raw:
            try:
                console.print_json(raw)
            except ValueError:
                console.print(raw)


def cmd_errors(args) -> None:
    """`cpa-stats errors` — failures grouped by model / status / fail code."""
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console()
    rows = load_rows(conn, time.time() - args.since, args.model, args.session,
                     account=getattr(args, "account", None))
    fails = [r for r in rows if r["failed"]]
    console.print(Text(
        f"cpa-stats errors · last {window_label(args.since)} · "
        f"{len(fails)}/{len(rows)} failed"
        + (f" ({len(fails) / len(rows) * 100:.1f}%)" if rows else ""),
        style="bold cyan"))
    if not fails:
        console.print(Text("no failures in window", style="dim"))
        return
    rec = recovery_stats(rows)
    if rec:
        med, share = rec
        console.print(Text(
            f"recovery: median {fmt_ms(int(med * 1000))} until the same model "
            f"succeeds again · {share * 100:.0f}% recover within 5m",
            style="dim"))
    groups = group_by(fails, lambda r: (
        short_model(r), r["status_code"] or 0, field(r, "fail_code") or "",
        r["endpoint"] or ""))
    t = Table(box=box.SIMPLE_HEAD, expand=True, title="by model · status · code · endpoint",
              title_style="bold red", title_justify="left")
    for col, just in (("model", "left"), ("status", "right"), ("code", "left"),
                      ("endpoint", "left"), ("count", "right"), ("last", "right"),
                      ("sample error", "left")):
        t.add_column(col, justify=just)
    from rich.markup import escape
    for (model, status, code, ep), grp in sorted(groups.items(),
                                                key=lambda kv: -len(kv[1])):
        last = max(r["ts"] for r in grp)
        sample = next((r["error"] for r in grp if r["error"]) or "")
        t.add_row(
            model, str(status), escape(code or "—"), escape(short_endpoint(ep) or ep),
            str(len(grp)), datetime.fromtimestamp(last).strftime("%m-%d %H:%M:%S"),
            Text(escape(sample[:100]), style="red", overflow="ellipsis"))
    console.print(t)


def cmd_top(args) -> None:
    """`cpa-stats top` — heaviest requests by tokens / output / cost / latency."""
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    console = Console()
    rows = load_rows(conn, time.time() - args.since, args.model, args.session,
                     account=getattr(args, "account", None))
    pricing = getattr(args, "pricing", None)
    scored = []
    for r in rows:
        total, uncached, read, _ = token_parts(r)
        out_tok = r["output_tokens"] or 0
        cost = (pricing.cost(uncached, read, out_tok)
                if pricing and pricing.enabled else None)
        scored.append((r, total + out_tok, out_tok, cost, r["latency_ms"] or 0))
    idx = {"tokens": 1, "output": 2, "cost": 3, "latency": 4}[args.by]
    scored.sort(key=lambda t: (t[idx] is None, -(t[idx] or 0)))
    top = scored[:args.limit]
    if args.by == "cost" and not (pricing and pricing.enabled):
        console.print(Text(
            "hint: pass --price in,cached,out to sort by estimated cost",
            style="yellow"))
    if not top:
        console.print(Text(
            f"cpa-stats top · last {window_label(args.since)} · "
            "no requests in window", style="dim"))
        return
    console.print(Text(
        f"cpa-stats top · last {window_label(args.since)} · by {args.by}",
        style="bold cyan"))
    t = Table(box=box.SIMPLE_HEAD, expand=True, title_justify="left")
    for col, just in (("time", "left"), ("model", "left"), ("endpoint", "left"),
                      ("account", "left"), ("session", "left"),
                      ("in", "right"), ("hit", "right"), ("out", "right"),
                      ("TTFT", "right"), ("lat", "right"), ("TPS", "right")):
        t.add_column(col, justify=just)
    if pricing and pricing.enabled:
        t.add_column("cost", justify="right")
    from rich.markup import escape
    for r, _, _, cost, _lat in top:
        tps = decode_tps(r["output_tokens"], r["latency_ms"], r["ttft_ms"])
        total, _u, read, _w = token_parts(r)
        ratio = read / total if total else None
        t.add_row(
            datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S"),
            escape(short_model(r)),
            escape(short_endpoint(field(r, "endpoint")) or "—"),
            escape(r["source"] or "—"),
            escape((r["session_id"] or "")[-8:] or "—"),
            fmt_tok(total), fmt_pct(ratio), fmt_tok(r["output_tokens"]),
            fmt_ms(r["ttft_ms"]), fmt_ms(r["latency_ms"]), fmt_tps(tps),
            *([fmt_cost(cost)] if pricing and pricing.enabled else []),
        )
    console.print(t)


def cmd_alert(args) -> None:
    """`cpa-stats alert` — show alert state, or POST a test webhook alert."""
    import json

    from .collector import send_webhook

    if getattr(args, "webhook", None):
        send_webhook(args.webhook, "cpa-stats: test alert ✅")
        print("sent test alert")
        return
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    raw = meta_get(conn, "alert_state")
    if not raw:
        print("no alert state yet (collector writes it once "
              "--alert-webhook is set and a threshold is crossed)")
        return
    try:
        state = json.loads(raw)
    except ValueError:
        print(raw)
        return
    for name, st in state.items():
        active = [k for k, v in st.items() if v]
        print(f"{name}: {', '.join(active) if active else 'clear'}")


def make_serve_server(args):
    """`cpa-stats serve` — read-only web dashboard with meta-refresh."""
    need_rich()
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from .model import COLUMNS
    from .storage import open_db_ro

    class H(BaseHTTPRequestHandler):
        def _send(self, body, ctype="text/html; charset=utf-8", code=200):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/healthz":
                    return self._send("ok", "text/plain")
                if path in ("/", "/index.html"):
                    conn = open_db_ro(args.db)
                    try:
                        con = Console(record=True, width=args.width,
                                      height=120)
                        con.print(render_dashboard(conn, args, con))
                        html = con.export_html(inline_styles=True)
                    finally:
                        conn.close()
                    page = ('<!doctype html><meta charset="utf-8">'
                            f'<meta http-equiv="refresh" '
                            f'content="{args.refresh}">' + html)
                    return self._send(page)
                if path == "/api/rows":
                    conn = open_db_ro(args.db)
                    try:
                        rows = load_rows(
                            conn, time.time() - args.window,
                            args.model, args.session, limit=500,
                            account=getattr(args, "account", None))
                        out = [{k: r[k] for k in COLUMNS
                                if k != "raw" and k in r.keys()}
                               for r in rows]
                    finally:
                        conn.close()
                    return self._send(_json.dumps(out, default=str),
                                      "application/json")
                self._send("not found", "text/plain", 404)
            except Exception as exc:  # noqa: BLE001
                self._send(f"error: {exc}", "text/plain", 500)

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((args.bind, args.port), H)


def cmd_serve(args) -> None:
    srv = make_serve_server(args)
    print(f"cpa-stats serve: http://{args.bind}:{args.port} "
          f"(refresh {args.refresh}s · Ctrl-C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
