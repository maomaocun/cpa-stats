"""cpa-stats: per-request latency / TTFT / TPS / cost monitoring for CLIProxyAPI.

CLIProxyAPI exposes per-request usage records on a destructive, short-lived
queue (GET /v8/management/observability/usage/queue, or /v0/management/usage-queue
on older builds, pops records). ``cpa-stats collect``
drains that queue into a local SQLite database; every other subcommand only
reads the database, so any number of viewers can run at the same time.

Metrics
  TTFT        time from request start to first upstream token (ttft_ms)
  latency     total request time (latency_ms)
  TPS         decode speed = output_tokens / (latency - TTFT). Upper bound:
              output_tokens includes hidden reasoning that may be generated
              before the first byte.
  burst       whole response arrived within 300ms of the first byte. Devin
              streams plain text token by token but delivers tool calls (most
              Codex turns) in one piece at the end, so those requests have
              TTFT ~= latency.
  E2E TPS     output_tokens / latency
  in          prompt tokens = new (uncached) + cache read + cache write
  hit         cache read / in
  cost        estimated USD = (uncached*in_price + cached*hit_price + out*out_price)/1e6.
              Prices default to Devin's published numbers and can be overridden.
  quality     share of rows whose upstream token_breakdown is marked complete
              (vs. inconsistent/partial) — a data-trust indicator.
  reasoning   shown only when the upstream reports it; Devin does not (it is
              counted inside output tokens)
  quota       per-credential plan/quota signals from /management/credentials
              normalized across providers: Devin daily/weekly remaining %,
              Codex x-codex-primary/secondary used-percent (+ named extra
              limits and credits), Claude anthropic-ratelimit-unified-5h/7d
              utilization. Snapshotted by the collector into the db so viewers
              never hit the management API themselves.
"""

__version__ = "2.3.3"
