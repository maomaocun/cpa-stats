"""Rich-rendered views: watch dashboard, tail, summary."""

from __future__ import annotations

import time
from datetime import datetime

from .format import (fmt_age, fmt_cost, fmt_countdown, fmt_ms, fmt_pct, fmt_tok,
                     fmt_tps, sparkline, window_label)
from .model import (MIN_DECODE_MS, MIN_DECODE_TOKENS, Agg, aggregate,
                    bucket_counts, decode_tps, e2e_tps, field, group_by,
                    parse_credentials, parse_iso_ts, request_detail_pairs,
                    short_endpoint, short_model, token_parts)
from .storage import db_totals, load_quota_snapshot, load_rows, meta_get

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
    line = Text()
    line.append("activity ", style="dim")
    line.append(sparkline(counts), style="cyan")
    line.append(f"  {sum(counts)} req · peak {max(counts)}/{window_s / buckets / 60:.0f}min bucket",
                style="dim")
    return line


def build_quota_table(creds, fetched_at=None):
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
    t.add_column("reset d·w", no_wrap=True)
    t.add_column("signals", overflow="ellipsis", style="dim")
    from rich.markup import escape
    for c in creds:
        ok_state = c.status == "active" and not c.blocked
        status_style = "green" if ok_state else ("red" if c.blocked
                                                 or c.status in ("error", "disabled") else "yellow")
        status_txt = c.status + (" · blocked" if c.blocked else "")
        resets = f"{_reset_str(c.daily_reset)} / {_reset_str(c.weekly_reset)}"
        extra = " ".join(f"{escape(str(k))}={escape(str(v))}"
                         for k, v in list((c.extra or {}).items())[:8]) or "—"
        fail_cell = styled(str(c.fail), "red" if c.fail else "dim")
        t.add_row(
            Text.assemble(escape(c.account),
                          (f" · {c.provider}", "dim") if c.provider else ""),
            styled(status_txt, status_style),
            Text.assemble(str(c.ok), "/", fail_cell),
            escape(c.plan) if c.plan else "—",
            styled(f"{c.daily_pct}%", quota_style(c.daily_pct))
            if c.daily_pct is not None else "—",
            styled(f"{c.weekly_pct}%", quota_style(c.weekly_pct))
            if c.weekly_pct is not None else "—",
            resets if resets != "— / —" else "—",
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
    rows = load_rows(conn, time.time() - window_s, args.model, args.session)
    pricing = getattr(args, "pricing", None)
    agg = aggregate(rows, window_s, pricing)
    status, _ = collector_status(conn)
    totals = db_totals(conn, pricing)
    total = totals.n
    snap = load_quota_snapshot(conn)
    creds = parse_credentials(snap)
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
        parts.append(build_quota_table(creds, (snap or {}).get("fetched_at")))
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
            rows = list(reversed(load_rows(conn, last_ts - 1, args.model, args.session)))
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
    rows = load_rows(conn, time.time() - args.since, args.model, args.session)
    agg = aggregate(rows, args.since, getattr(args, "pricing", None))
    console.print(Text(f"cpa-stats summary · last {window_label(args.since)}", style="bold cyan"))
    console.print(build_cards(agg, window_label(args.since)))
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
        console.print(build_quota_table(creds, snap.get("fetched_at")))


def cmd_quota(args) -> None:
    """`cpa-stats quota` — full credential snapshot: quota, cooldowns, signals."""
    need_rich()
    from .storage import open_db_ro

    conn = open_db_ro(args.db)
    snap = load_quota_snapshot(conn)
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
    console.print(build_quota_table(creds, fetched))
    from rich.markup import escape
    for c in creds:
        lines = []
        if c.status_message:
            lines.append(f"[red]status[/]      {escape(c.status_message)}")
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
