"""Formatting helpers shared by all views."""

from __future__ import annotations


def fmt_ms(ms):
    if ms is None:
        return "—"
    if ms >= 60_000:
        return f"{ms / 60000:.1f}m"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def fmt_tok(n):
    if n is None:
        return "—"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}G"
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}K"
    if n >= 1000:
        return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def fmt_tps(v):
    return "—" if v is None else f"{v:.0f}"


def fmt_pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def fmt_cost(usd):
    if usd is None:
        return "—"
    if usd >= 100:
        return f"${usd:.0f}"
    if usd >= 1:
        return f"${usd:.2f}"
    if usd >= 0.01:
        return f"${usd:.3f}"
    return f"${usd:.4f}" if usd > 0 else "$0"


def fmt_age(seconds):
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def window_label(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def sparkline(buckets: list[float], blocks: str = "▁▂▃▄▅▆▇█") -> str:
    """Render a unicode sparkline for a list of bucket values."""
    if not buckets:
        return ""
    lo, hi = min(buckets), max(buckets)
    if hi == lo:
        return blocks[0] * len(buckets) if hi == 0 else blocks[3] * len(buckets)
    span = hi - lo
    return "".join(blocks[min(int((v - lo) / span * (len(blocks) - 1)), len(blocks) - 1)]
                   for v in buckets)
