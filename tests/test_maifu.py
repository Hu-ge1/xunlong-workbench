from __future__ import annotations

from datetime import date

from app.maifu import build_news_events, build_overview, upcoming_events


def test_upcoming_events_sorted_and_windowed():
    events = upcoming_events(date(2026, 9, 6))
    assert events, "9 月应至少命中国庆/三季报等近期事件"
    days = [item["days_to_start"] for item in events]
    assert days == sorted(days)
    assert any(item["name"].startswith("国庆") for item in events)


def test_build_overview_matches_industries():
    snaps = [
        {"code": "600054", "name": "黄山旅游", "industry": "旅游及酒店", "amount": 500_000_000.0},
        {"code": "300999", "name": "金龙鱼", "industry": "食品加工制造", "amount": 900_000_000.0},
        {"code": "600036", "name": "招商银行", "industry": "银行", "amount": 2_000_000_000.0},
    ]
    pool = {
        "by_code": {
            "600598": {"ts_code": "600598.SH", "pool_type": "limit_up", "lianban_count": 2, "open_count": 0, "first_limit_time": "10:00:00", "limit_reason": "一号文件预期", "related_concepts": "农业"},
        },
        "stats": {"limit_up_count": 43, "broken_count": 47, "board_ladder": {"首板": 36, "2板": 6}},
    }
    overview = build_overview(
        snaps,
        limit_pool=pool,
        trade_date="20260904",
        news=[{"title": "测试快讯", "published": "2026-09-06T10:00:00", "source": "FFD"}],
        today=date(2026, 9, 6),
    )
    guoqing = next(item for item in overview["events"] if item["name"].startswith("国庆"))
    assert guoqing["in_window"]
    assert guoqing["matched"]["count"] == 1
    assert guoqing["matched"]["examples"][0]["code"] == "600054"
    assert overview["hot_stocks"][0]["lianban_count"] == 2
    assert overview["news"][0]["title"] == "测试快讯"
    assert overview["board_ladder"]["首板"] == 36


def test_build_news_events_tracks_follow_up_outcomes():
    events = build_news_events(
        [
            {"title": "某项目计划落地", "summary": "市场预期", "source": "A", "published": "2026-09-06T10:00:00+08:00"},
            {"title": "某项目正式落地", "summary": "项目已完成签约", "source": "B", "published": "2026-09-07T10:00:00+08:00"},
            {"title": "某政策传闻被否认", "summary": "相关部门称消息不实", "source": "C", "published": "2026-09-07T09:00:00+08:00"},
            {"title": "某公司拟扩产", "summary": "尚待后续公告", "source": "D", "published": "2026-09-07T08:00:00+08:00"},
        ]
    )
    statuses = {item["status"] for item in events}
    assert {"realized", "unrealized", "pending"} <= statuses
    realized = next(item for item in events if item["status"] == "realized")
    assert realized["news_count"] == 2
    assert realized["source_count"] == 2
    assert realized["event_title"] == "某项目正式落地"
    assert "已兑现" in realized["summary"]
    assert realized["result_signal"] == "正式落地"


def test_build_news_events_summarizes_bracketed_fast_news_title():
    events = build_news_events(
        [
            {
                "title": "【某航司将开通新航线】据机场消息，将于12月14日起开通直飞航线。",
                "summary": "将于12月14日起开通直飞航线，每周4班。",
                "source": "新浪7x24",
                "published": "2026-09-12T10:00:00+08:00",
            }
        ]
    )
    assert events[0]["event_title"] == "某航司将开通新航线"
    assert "将于12月14日起开通直飞航线" in events[0]["summary"]
    assert "兑现或证伪" in events[0]["summary"]


def test_pending_news_with尚未_is_not_marked_unrealized():
    events = build_news_events(
        [{
            "title": "某政策正在征求意见",
            "summary": "目前尚未正式发布，需等待后续公告。",
            "source": "A",
            "published": "2026-09-12T10:00:00+08:00",
        }]
    )
    assert events[0]["status"] == "pending"
