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


def test_parsed_timezone_follows_reference_not_machine_timezone():
    """输出时区必须跟随 ``now``，而不是进程的系统时区。

    回归测试。原先 ``_parse_time`` 用的是无参 ``astimezone()``，会把结果换算到**机器**
    本地时区。开发机恰好在 UTC+8 时结果碰巧正确，所以这个缺陷只会在 CI（UTC runner）
    上暴露成 ``assert 8 == 16``。

    这里主动用几个与「机器时区」不同的参照来断言，**在任何时区的机器上都能测出来**——
    包括原来掩盖了这个 bug 的 UTC+8 开发机。
    """

    for offset_hours in (8, -5, 0, 5.5):
        reference = timezone(timedelta(hours=offset_hours))
        now = datetime(2026, 9, 12, 16, 20, tzinfo=reference)

        # naive 输入：按参照时区解释
        parsed = _parse_time("2026-09-12 16:16:53", now)
        assert parsed.utcoffset() == timedelta(hours=offset_hours)
        assert (parsed.hour, parsed.minute) == (16, 16)

        # 时间戳输入：换算到参照时区
        assert _parse_time("1789200715", now).utcoffset() == timedelta(hours=offset_hours)

        # 自带偏移的输入：先按自带偏移解析，再换算到参照时区
        aware = _parse_time("2026-09-12T16:16:53+08:00", now)
        assert aware.utcoffset() == timedelta(hours=offset_hours)


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
