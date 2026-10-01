from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.news_feed import _parse_time, merge_news


def test_naive_news_time_stays_in_local_timezone():
    local = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 12, 16, 20, tzinfo=local)
    parsed = _parse_time("2026-09-12 16:16:53", now)
    assert parsed.hour == 16
    assert parsed.minute == 16
    assert parsed.utcoffset() == timedelta(hours=8)
    assert _parse_time("1789200715", now).year == 2026


def test_merge_news_deduplicates_by_source_and_discards_old_items():
    now = datetime.now().astimezone()
    fresh = now.isoformat(timespec="seconds")
    old = (now - timedelta(hours=13)).isoformat(timespec="seconds")
    rows = merge_news(
        [[
            {"source": "sina7x24", "title": "同一条消息", "published": fresh},
            {"source": "sina7x24", "title": "同一条消息", "published": fresh},
            {"source": "sina7x24", "title": "过期消息", "published": old},
        ]],
        limit=10,
    )
    assert [item["title"] for item in rows] == ["同一条消息"]
