"""埋伏挖掘（事件日历 + 大热股映射）——最左侧的事件驱动打法。

规则来源：桌面《埋伏挖掘模式-视频总结》（上山打老股）与公开"A股日历效应"
资料（知乎/雪球/财联社等）：

- 固定会议、节日、事件提前埋伏（一号文件、两会、中报、生肖、国庆等）
- 近期热议话题（热搜/身边话题）发现即挖票
- 大热股映射：最热的大热股 → A 股同题材补涨标的
- 买在发酵前的低位；两市缩量、业绩披露大考前注意退潮

日历为内置静态知识（每年固定窗口），事件相关的本地行业匹配不消耗 FFD 额度。
输出仅供研究，不构成投资建议。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any, Iterable, Mapping

from . import scoring


STRATEGY_VERSION = "maifu-calendar-v1-2026.09.06"

EVENT_REALIZED_TERMS = (
    "正式成立", "正式发布", "正式落地", "签约", "获批", "获准", "开工", "投产",
    "上线", "完成", "实现", "达到", "突破", "公布", "落地", "已启动", "已交付",
)
EVENT_UNREALIZED_TERMS = (
    "否认", "不实", "取消", "终止", "失败", "未能", "未兑现", "落空", "不及预期",
    "下调", "暂停", "推迟", "延期", "被叫停", "尚未",
)
EVENT_HARD_UNREALIZED_TERMS = tuple(term for term in EVENT_UNREALIZED_TERMS if term != "尚未")
EVENT_TOPIC_TERMS = {
    "政策": ("央行", "国务院", "证监会", "财政部", "降准", "降息", "LPR", "监管"),
    "科技": ("芯片", "人工智能", "AI", "算力", "机器人", "卫星", "太空"),
    "能源": ("原油", "天然气", "石油", "黄金", "白银", "锂", "光伏"),
    "地缘": ("乌克兰", "俄罗斯", "美国", "伊朗", "以色列", "关税", "制裁"),
    "产业": ("汽车", "电池", "半导体", "医药", "房地产", "旅游", "消费"),
    "市场": ("A股", "港股", "美股", "指数", "股市", "市场", "涨停", "跌停"),
}


# 全年固定事件日历（month/day 为典型炒作启动日；window_days 为建议提前埋伏窗口）
EVENT_CALENDAR: list[dict[str, Any]] = [
    {"month": 1, "day": 5, "window_days": 30, "name": "中央一号文件预期", "match": ("农林牧渔", "种植业", "种业", "养殖", "化肥"), "concepts": "农业、种业、养殖、化肥", "note": "一号文件历年聚焦三农，1 月是农业板块传统炒作期，2 月初文件落地"},
    {"month": 1, "day": 20, "window_days": 25, "name": "年报预披露行情", "match": ("",), "concepts": "首批年报、业绩预增", "note": "首份年报必炒；12 月底开始按预约披露时间排序埋伏"},
    {"month": 2, "day": 10, "window_days": 30, "name": "春节消费 + 生肖题材高潮", "match": ("食品饮料", "白酒", "饮料制造", "影视", "传媒"), "concepts": "白酒食品、春节档影视、生肖名称股", "note": "腊月提前埋伏，节后冲高兑现；生肖股高潮在元旦至春节，节后退潮"},
    {"month": 3, "day": 3, "window_days": 30, "name": "全国两会政策预期", "match": ("军工", "新能源", "半导体", "计算机", "通信"), "concepts": "当年政策主线（军工/新质生产力等）", "note": "1 月底~2 月预埋，开会期间兑现"},
    {"month": 4, "day": 10, "window_days": 20, "name": "年报一季报 + 高送转", "match": ("",), "concepts": "高送转、绩优白马", "note": "3 月抢跑、4 月底业绩大考前撤离；4 月底 8 月底是题材股退潮节点"},
    {"month": 5, "day": 20, "window_days": 25, "name": "中报预增预期启动", "match": ("",), "concepts": "中报预增、景气行业", "note": "5 月下旬关注业绩预告，7 月 15 日前强制预告"},
    {"month": 7, "day": 5, "window_days": 20, "name": "中报行情", "match": ("",), "concepts": "中报预增股", "note": "6 月底~7 月上旬埋伏预增股，7 月兑现"},
    {"month": 9, "day": 10, "window_days": 20, "name": "国庆黄金周", "match": ("旅游及酒店", "旅游", "酒店", "机场航运", "航空", "餐饮"), "concepts": "旅游、酒店、航空、免税消费", "note": "9 月埋伏，节后逢高减仓（节前炒节后卖）"},
    {"month": 10, "day": 15, "window_days": 20, "name": "三季报预告", "match": ("",), "concepts": "三季报预增", "note": "10 月中旬三季报预告窗口"},
    {"month": 11, "day": 10, "window_days": 30, "name": "业绩真空期题材黄金期", "match": ("",), "concepts": "纯题材轮动、高送转预期", "note": "11 月~次年 4 月底业绩真空，题材炒作黄金期"},
    {"month": 12, "day": 10, "window_days": 25, "name": "中央经济工作会议 + 跨年行情", "match": ("",), "concepts": "政策主线、跨年妖股", "note": "12 月中旬经济工作会议定调；次年生肖题材此时启动"},
    {"month": 12, "day": 20, "window_days": 40, "name": "生肖题材（次年生肖）", "match": ("",), "concepts": "名字含次年生肖字的股票", "note": "提前按次年生肖找名字带字股，加自选关注，高潮在元旦至春节"},
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _is_main_board(snapshot: Mapping[str, Any]) -> bool:
    return scoring.is_main_board_security(snapshot.get("code"), snapshot.get("name"))


def _next_occurrence(month: int, day: int, today: date) -> date:
    candidate = date(today.year, month, day)
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return candidate


def upcoming_events(today: date | None = None, *, within_days: int = 75) -> list[dict[str, Any]]:
    """按"距离下一次启动日的天数"排序的近期事件。"""

    today = today or date.today()
    rows: list[dict[str, Any]] = []
    for event in EVENT_CALENDAR:
        start = _next_occurrence(int(event["month"]), int(event["day"]), today)
        days_to_start = (start - today).days
        if days_to_start > within_days + max(0, int(event["window_days"])):
            continue
        rows.append(
            {
                **event,
                "start_date": start.isoformat(),
                "days_to_start": days_to_start,
                "advance_days": int(event["window_days"]),
                "in_window": days_to_start <= int(event["window_days"]),
                "status": (
                    "埋伏窗口内" if days_to_start <= int(event["window_days"]) else "待观察"
                ),
            }
        )
    rows.sort(key=lambda item: item["days_to_start"])
    return rows


def _match_stocks_for_event(
    event: Mapping[str, Any],
    snapshots: Iterable[Mapping[str, Any]],
    *,
    top: int = 6,
) -> dict[str, Any]:
    """用本地行业名匹配事件受益方向（零 FFD 消耗）。"""

    keywords = [k for k in (event.get("match") or ()) if k]
    matched: list[dict[str, Any]] = []
    if keywords:
        for item in snapshots:
            if not _is_main_board(item):
                continue
            industry = str(item.get("industry") or "")
            if not any(keyword in industry for keyword in keywords):
                continue
            matched.append(item)
    matched.sort(key=lambda item: -_number(item.get("amount")))
    return {
        "count": len(matched),
        "examples": [
            {
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or ""),
                "industry": str(item.get("industry") or ""),
                "amount_yi": round(_number(item.get("amount")) / 1e8, 1),
            }
            for item in matched[:top]
        ],
    }


def _news_timestamp(item: Mapping[str, Any]) -> float:
    value = item.get("published") or item.get("time") or ""
    try:
        if isinstance(value, (int, float)):
            return float(value) / (1000 if float(value) > 10_000_000_000 else 1)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _clean_news_title(title: str) -> str:
    """把快讯标题压缩成适合事件卡片展示的事件主标题。"""

    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""
    bracket = re.match(r"^[\[【]([^\]】]{2,160})[\]】]", text)
    if bracket:
        # 7x24 常见“【事件标题】据……”格式，括号内就是最稳定的事件名。
        text = bracket.group(1).strip()
    text = re.sub(r"^(?:市场消息|快讯|突发消息|最新消息)\s*[:：]\s*", "", text)
    text = re.sub(r"\s*[（(](?:财联社|同花顺|华尔街见闻|新浪|新华社|央视新闻|证券时报)[）)]\s*$", "", text)
    return text.strip(" ：:，,。．")


def _news_summary(item: Mapping[str, Any]) -> str:
    """优先使用源摘要，避免用整段原始标题冒充事件结论。"""

    summary = str(item.get("summary") or item.get("content") or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    if not summary:
        return ""
    title = _clean_news_title(str(item.get("title") or ""))
    for prefix in (title, f"【{title}】", f"[{title}]"):
        if prefix and summary.startswith(prefix):
            summary = summary[len(prefix):].lstrip(" ：:，,。．")
            break
    return summary[:180].rstrip(" ，,；;。.")


def _find_event_signal(text: str, terms: Iterable[str]) -> str:
    """Find a result term while ignoring terms inside a negative context."""

    for term in terms:
        for match in re.finditer(re.escape(term), text):
            prefix = text[max(0, match.start() - 5):match.start()]
            if any(marker in prefix for marker in ("尚未", "未", "没有", "暂无", "尚无")):
                continue
            return term
    return ""


def _event_key(title: str) -> str:
    """Remove source prefixes and create a stable key for follow-up headlines."""
    text = _clean_news_title(title)
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()
    for term in (*EVENT_REALIZED_TERMS, *EVENT_UNREALIZED_TERMS, "计划", "拟", "预期", "有望", "传闻", "消息", "将"):
        text = text.replace(term.lower(), "")
    return (text if len(text) >= 3 else re.sub(r"[^\w\u4e00-\u9fff]", "", title).lower())[:36]


def _build_event_summary(
    title: str,
    latest: Mapping[str, Any],
    *,
    status: str,
    signal: str,
) -> str:
    """从新闻摘要和结果信号生成可读、可审计的事件总结。"""

    source_summary = _news_summary(latest)
    prefix = {
        "realized": "新闻已确认",
        "unrealized": "新闻已出现反向结果",
        "pending": "新闻线索显示",
    }.get(status, "新闻线索显示")
    result = f"{prefix}：{title}。"
    if source_summary and source_summary not in title:
        result += f" {source_summary}。"
    if status == "realized":
        result += f"检测到“{signal}”等明确结果信号，判定为已兑现。"
    elif status == "unrealized":
        result += f"检测到“{signal}”等否定或落空信号，判定为未兑现。"
    else:
        result += "目前仍是预期、进展或讨论，尚未发现明确的兑现或证伪结果。"
    return result


def _event_topic(text: str) -> str:
    for topic, terms in EVENT_TOPIC_TERMS.items():
        if any(term.lower() in text.lower() for term in terms):
            return topic
    return "综合"


def build_news_events(news: Iterable[Mapping[str, Any]], *, limit: int = 500) -> list[dict[str, Any]]:
    """Convert the retained global news stream into auditable event threads.

    This is deliberately conservative: a headline is marked realized only
    when a follow-up contains an explicit completion/implementation signal;
    explicit denial/cancellation is marked unrealized; everything else stays
    pending instead of being presented as a false conclusion.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in news:
        if not isinstance(raw, Mapping) or not str(raw.get("title") or "").strip():
            continue
        item = dict(raw)
        key = _event_key(str(item.get("title") or ""))
        if key:
            groups.setdefault(key, []).append(item)

    events: list[dict[str, Any]] = []
    for key, items in groups.items():
        items.sort(key=_news_timestamp, reverse=True)
        texts = [f"{item.get('title', '')} {item.get('summary', '')}" for item in items]
        negative = next((text for text in texts if _find_event_signal(text, EVENT_HARD_UNREALIZED_TERMS)), "")
        realized = next((text for text in texts if _find_event_signal(text, EVENT_REALIZED_TERMS)), "")
        if negative:
            status, status_label, status_detail = "unrealized", "未兑现", "出现否认、取消、延期或不及预期等跟进信号"
            signal = _find_event_signal(negative, EVENT_HARD_UNREALIZED_TERMS)
        elif realized:
            status, status_label, status_detail = "realized", "已兑现", "出现正式发布、落地、完成或交付等跟进信号"
            signal = _find_event_signal(realized, EVENT_REALIZED_TERMS)
        else:
            status, status_label, status_detail = "pending", "待验证", "目前只有预期或线索，尚未发现明确兑现结果"
            signal = ""
        latest = items[0]
        oldest = items[-1]
        event_title = _clean_news_title(str(latest.get("title") or "")) or str(latest.get("title") or "")
        source_names = list(dict.fromkeys(str(item.get("source_name") or item.get("publisher") or item.get("source") or "") for item in items if item.get("source_name") or item.get("publisher") or item.get("source")))
        related_codes = sorted({code for text in texts for code in re.findall(r"(?<!\d)\d{6}(?!\d)", text)})[:12]
        events.append(
            {
                "event_id": key,
                "title": str(latest.get("title") or ""),
                "event_title": event_title,
                "topic": _event_topic(" ".join(texts)),
                "status": status,
                "status_label": status_label,
                "status_detail": status_detail,
                "summary": _build_event_summary(event_title, latest, status=status, signal=signal),
                "result_signal": signal,
                "news_count": len(items),
                "source_count": len(source_names),
                "sources": source_names[:8],
                "first_seen": str(oldest.get("published") or oldest.get("time") or ""),
                "last_seen": str(latest.get("published") or latest.get("time") or ""),
                "related_codes": related_codes,
                "evidence": [
                    {
                        "title": str(item.get("title") or ""),
                        "source": str(item.get("source_name") or item.get("publisher") or item.get("source") or ""),
                        "published": str(item.get("published") or item.get("time") or ""),
                    }
                    for item in items[:4]
                ],
            }
        )
    priority = {"pending": 0, "realized": 1, "unrealized": 2}
    events.sort(key=lambda item: (priority.get(item["status"], 0), -_news_timestamp({"published": item["last_seen"]})))
    return events[: max(1, int(limit))]


def build_overview(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    limit_pool: Mapping[str, Any] | None = None,
    trade_date: str = "",
    news: Iterable[Mapping[str, Any]] = (),
    today: date | None = None,
) -> dict[str, Any]:
    """组装埋伏挖掘总览：日历事件 + 大热股映射 + 热议快讯。"""

    rows = [dict(item) for item in snapshots if isinstance(item, Mapping)]
    events = []
    for event in upcoming_events(today):
        match = _match_stocks_for_event(event, rows)
        events.append({**event, "matched": match})
    pool = dict(limit_pool or {})
    pool_stats = dict(pool.get("stats") or {})
    by_code = pool.get("by_code") or {}
    hot_stocks = []
    for code, row in sorted(
        by_code.items(),
        key=lambda pair: -_number((pair[1] or {}).get("lianban_count")),
    ):
        lianban = int(_number((row or {}).get("lianban_count")))
        if lianban < 2:
            continue
        hot_stocks.append(
            {
                "code": code,
                "name": str((row or {}).get("name") or ""),
                "lianban_count": lianban,
                "open_count": int(_number((row or {}).get("open_count"))),
                "first_limit_time": str((row or {}).get("first_limit_time") or ""),
                "limit_reason": str((row or {}).get("limit_reason") or ""),
                "related_concepts": str((row or {}).get("related_concepts") or ""),
            }
        )
    return {
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime_iso(),
        "trade_date": trade_date,
        "events": events,
        "events_in_window": sum(1 for item in events if item.get("in_window")),
        "hot_stocks": hot_stocks[:12],
        "hot_stocks_count": len(hot_stocks),
        "board_ladder": pool_stats.get("board_ladder") or {},
        "limit_pool_stats": pool_stats,
        "news": [
            {
                "title": str(item.get("title") or ""),
                "published": str(item.get("published") or item.get("time") or ""),
                "source": str(item.get("source_name") or item.get("publisher") or item.get("source") or ""),
                "summary": str(item.get("summary") or item.get("content") or ""),
                "tags": [
                    str(tag)
                    for tag in [*(item.get("tags") or []), *(item.get("sector_tags") or []), *(item.get("market_tags") or [])]
                    if str(tag).strip()
                ][:8],
                "important": bool(item.get("important")),
                "stale": bool(item.get("stale") or item.get("cache_only")),
            }
            for item in list(news or [])
        ],
        "news_events": build_news_events(news),
        "methodology": [
            "埋伏逻辑：在事件/热点发酵前低位拿筹码，买在无人问津，卖在消息明朗。",
            "日历型：内置全年固定事件窗口（一号文件/两会/中报/国庆/生肖等），提前 20~40 天关注受益行业。",
            "映射型：看连板梯队里最热的大热股，去挖 A 股同题材/供应链的补涨标的。",
            "热议型：每天早上看快讯热搜与身边话题（同事指数），发现即挖票当天上车。",
            "监管生态：异常波动条款压缩游资连板空间，持续性好的行情靠合力，低位埋伏顺势。",
            "风控：4 月底/8 月底业绩大考是题材退潮节点；两市大幅缩量时降低埋伏仓位。",
        ],
        "threshold_status": "事件窗口为历史规律总结，题材匹配基于本地行业名，待人工校准。",
        "disclaimer": "仅供研究，不构成投资建议。埋伏逻辑失效时（事件证伪/市场退潮）必须止损离场。",
    }


def datetime_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
