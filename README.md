# cpa-stats

Per-request latency / TTFT / TPS / cost monitoring for **CLIProxyAPI** (CPA).

CPA exposes per-request usage records on a destructive, short-lived queue
(`GET /v8/management/observability/usage/queue` — or `/v0/management/usage-queue`
on older builds — pops records). `cpa-stats collect` drains that
queue into a local SQLite database; every other subcommand only reads the
database, so any number of viewers can run at the same time.

```
CPA (127.0.0.1:PORT)
   │  every 2 s, ≤ 500 records per poll; credentials/quota snapshot every 60 s
   ▼
cpa-stats collect  →  SQLite (WAL)  →  watch / tui / tail / summary / quota / export
```

## Install

```bash
pipx install .          # or: pip install --user .
cpa-stats doctor        # sanity-check CPA endpoint, key, collector, db
```

Requires Python ≥ 3.10 and `rich`.

## Quick start

```bash
cpa-stats collect &                       # start the collector
cpa-stats                                 # live dashboard (default 1h window)
cpa-stats tui                             # interactive TUI (keys/mouse)
cpa-stats -w 15m -m swe-2                 # filter window + model
cpa-stats tail                            # one line per request, live (-v: full routing detail)
cpa-stats show <id>                       # every captured field of one request (--raw for JSON)
cpa-stats quota                           # credential quota / cooldowns + burn forecast
cpa-stats errors                          # failure breakdown by model / status / code / endpoint
cpa-stats summary --since 7d --by model   # aggregate report
cpa-stats export --since 7d -o out.csv    # CSV / JSONL export
cpa-stats prune --older-than 30d          # drop old records
```

### `cpa-stats tui`

Interactive dashboard (textual). Same data as `watch`, but navigable:

| key / mouse | action |
|-|-|
| `1` `2` `3` `4` | switch window: 15 m / 1 h / 6 h / 24 h |
| `↑` `↓` / wheel / click | select a row in the focused table |
| `←` `→` | cycle right-hand tabs (recent · sessions · quota · totals) |
| `tab` | cycle focus between panels |
| `enter` | request/credential detail modal; on a session row: filter to it (again to clear) |
| `f` | toggle the model filter input |
| `p` | pause / resume auto-refresh |
| `r` | force refresh |
| `q` / `esc` | quit / close modal |

Four tabs on the right: **recent** (every request, selectable → detail),
**sessions** (top sessions by tokens), **quota** (per-credential quota signals,
selectable → full cooldown/signal detail), **totals** (all-time rollup + data
quality). The statusbar shows the lowest remaining quota across credentials.

## Dashboard

```
cpa-stats   window 1h                                    ● collector live  2000 stored · 08:08
╭─ all time · requests ─╮ ╭─ all time · input ──╮ ╭─ all time · cache ──╮ ╭─ all time · est. cost
│        2000           │ │       66.74M        │ │       hit 96%       │ │        $64.77
╭─ requests · 1h ───────╮ ╭─ TTFT p50 ──────────╮ ╭─ TPS p50 decode/e2e ╮ ╭─ latency p50 ───────
│        1219           │ │        784ms        │ │   103 / 64 tok/s    │ │        1.86s
activity ▁▁▁▄▃▂▂▁▃▁▁▁▅█▁▁▁▁▁▁▁▁▁▁  1219 req · peak 308/2min bucket
accounts · quota                                        ← per-credential daily/weekly %, resets, cooldowns
per model · per session · recent requests
```

- **all-time row** — totals over the whole database (requests, tokens, cache,
  output, est. cost). Computed by SQL, not by loading rows.
- **per-window cards** — success rate, TTFT / TPS / latency percentiles, token
  split (new / cache-read / cache-write), cache hit ratio.
- **activity** — request-rate sparkline over the window.
- **top sessions** — the sessions consuming the most tokens right now.
- **est. cost** — shown when `--price 'IN,CACHED,OUT'` is passed (USD per 1M
  tokens, e.g. `--price '6,0.6,18'`).

## Metrics

| metric | definition | caveat |
|-|-|-|
| TTFT | time to first upstream byte | tool-call requests have TTFT ≈ latency (see burst) |
| latency | total request time | |
| TPS | out ÷ (latency − TTFT) | upper bound: out includes hidden reasoning generated before the first byte |
| e2e | out ÷ latency | most reliable speed metric |
| burst | out ≥ 16 and (latency − TTFT) < 300 ms | Devin sends tool calls in one piece |
| in / new / c.read / c.write | in = new + c.read + c.write | a few records (Claude cache-creation) put c.write outside `in`; `token_parts()` handles both |
| hit | c.read ÷ in | ~90 % is typical for Devin swe-2 |
| clean | share of rows whose upstream `token_breakdown.quality` is `complete` | low values mean upstream accounting was inconsistent |
| cost | (new·P_in + c.read·P_cached + out·P_out) / 1e6 | only shown with `--price` |
| reasoning | from upstream when reported | Devin does not report it (counted inside `out`) |

## Quota

The collector also snapshots `GET /v8/management/credentials` (fallback
`/v0/management/auth-files`) every `--quota-interval` seconds (default 60; `0`
disables). The latest payload lives in `meta.credentials_snapshot` and each
poll also appends to the `quota_history` table — every viewer reads the db
locally, no extra management calls. `cpa-stats quota`, the `watch` dashboard,
and the TUI **quota** tab show per-credential: plan, daily/weekly quota
remaining %, reset countdowns, ok/fail request counts, status
(`active` / `error` / `blocked`), cooldowns, per-model quotas, and any other
provider signals (e.g. `x-codex-*`, `anthropic-ratelimit-*` for Codex/Claude
credentials). From the history, `quota` computes a burn forecast
(`−N%/day → 0 in Xd`) and a trend sparkline — so an account heading for
exhaustion is visible before it is blocked. `watch` also shows a red ⚠ line
when a credential is blocked or the window failure rate exceeds 5%.

## Collector

The usage queue is destructive — exactly one reader is allowed, so `collect`
must be a singleton.

```bash
cpa-stats collect                       # foreground
cpa-stats collect --retention-days 30   # auto-prune, checked hourly
cpa-stats collect --quota-interval 30   # credentials/quota snapshot every 30 s
cpa-stats install-service               # write + enable a systemd --user unit
```

If the database write fails after records were popped, they are appended to
`spool.jsonl` next to the db and replayed on the next start — popped data is
never lost.

Configuration is auto-discovered from `~/.config/cliproxy/config.yaml` (port)
and `management.key` next to it. Override with `CPA_URL`, `CPA_MGMT_KEY`,
`CPA_DIR`, `CPA_STATS_DB`, or the matching flags.

## Database

`~/.local/share/cpa-stats/requests.db`, mode 600, WAL. Schema is versioned
(`meta.schema_version`); `open_db` runs pending migrations automatically. The
latest credentials/quota snapshot lives in `meta.credentials_snapshot`.
Sensitive record fields (`api_key`, `response_headers`, `access_token_sha256`)
are dropped before storage.

## Development

```bash
git clone <this repo> && cd cpa-stats
pip install --user -e '.[test]'
pytest                    # 47 tests
python -m cpa_stats ...   # run without installing
```

Layout:

```
src/cpa_stats/
  model.py      pure metric math + record parsing (no I/O)
  storage.py    schema, migrations, queries
  collector.py  usage-queue daemon
  views.py      rich rendering
  cli.py        argparse + entrypoint
  format.py     number / time / sparkline formatting
tests/          unit + end-to-end (fake usage-queue HTTP server)
```
