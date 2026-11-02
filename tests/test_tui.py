"""TUI smoke tests: the app constructs, renders and refreshes headlessly."""

import pytest

textual = pytest.importorskip("textual")

from cpa_stats.storage import insert_records, open_db  # noqa: E402
from cpa_stats.tui import build_app  # noqa: E402


import time  # noqa: E402


def make_rec(i):
    ts = time.time() - i * 10  # spread over the last few minutes
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat()
    return {
        "timestamp": iso,
        "execution_id": f"e{i}",
        "model": "devin/swe-2",
        "alias": "swe-2",
        "latency_ms": 1200 + i,
        "ttft_ms": 400,
        "failed": False,
        "session_id": f"sess-{i % 3}",
        "tokens": {"input_tokens": 1000 + i, "output_tokens": 50,
                   "cache_read_tokens": 400},
        "token_breakdown": {"quality": "complete"},
    }


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "r.db")
    insert_records(conn, [make_rec(i) for i in range(20)])
    path = tmp_path / "r.db"
    conn.close()
    return path


@pytest.mark.asyncio
async def test_app_starts_and_renders(db):
    AppCls = build_app()
    app = AppCls(str(db))
    async with app.run_test() as pilot:
        await pilot.pause()
        # status bar populated
        sb = app.query_one("#statusbar")
        assert "collector" in str(sb.render())
        # tables filled
        models = app.query_one("#models")
        assert models.row_count >= 1
        requests = app.query_one("#requests-table")
        assert requests.row_count == 20
        # switch window
        await pilot.press("3")
        await pilot.pause()
        # open detail on first row
        await pilot.press("enter")
        await pilot.pause()
        # detail modal visible -> close
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_window_and_pause(db):
    AppCls = build_app()
    app = AppCls(str(db))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.window_key == "2"
        await pilot.press("4")
        await pilot.pause()
        assert app.window_key == "4"
        await pilot.press("p")
        await pilot.pause()
        assert app.paused
        await pilot.press("p")
        await pilot.pause()
        assert not app.paused
