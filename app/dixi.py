"""素衣不染尘低吸模式（趋势+人气+回调反包）盘后筛选。

规则来源：桌面《素衣不染尘低吸模式-视频总结》与淘股吧公开拆解帖
（tgb.cn/a/2tJUG0caQKS-1）：

- 上升趋势通道中期，图形和趋势不破
- 成交额排名市场前 100 名（大成交、高人气）
- 回调不超过一周，在均线附近出现反包阳线
- 最多做第三次回调，超过三次成功率骤降
- 成交量持续放大、股性活跃（历史涨停频繁）
- 风控：两市大幅缩量时控制仓位

所有阈值均为经验参数（待回测），输出仅供研究，不构成投资建议。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import scoring


STRATEGY_VERSION = "dixi-trend-pullback-v1-2026.09.06"

DEFAULT_PARAMS: dict[str, float] = {
    "min_amount": 300_000_000.0,        # 成交额硬门槛（3亿）
    "amount_rank_top": 100.0,           # 成交额排名前 100（素衣口径）
    "max_pullback_days": 5.0,           # 回调不超过一周
    "max_pullback_rounds": 3.0,         # 最多做第三次回调
    "ma_touch_band": 2.0,               # 均线附近（±2%）
    "min_activity_limit_ups": 2.0,      # 股性活跃：近60日涨停次数下限
    "market_shrink_ratio": 0.85,        # 两市缩量风控线
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _pct(current: float, base: float) -> float:
    return (current / base - 1.0) * 100.0 if base else 0.0


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def is_main_board_stock(code: Any, name: Any = "") -> bool:
    label = str(name or "").upper().replace(" ", "")
    return (
        scoring.is_main_board_security(code, name)
        and "ST" not in label
        and "退" not in label
    )


def _ma(values: list[float], window: int) -> float:
    if len(values) < window:
        return 0.0
    return sum(values[-window:]) / window


def _ma_slope(values: list[float], window: int) -> float:
    """均线斜率：MA(n) 今日与 3 日前之差占均值的百分比。"""

    if len(values) < window + 3:
        return 0.0
    current = sum(values[-window:]) / window
    previous = sum(values[-window - 3:-3]) / window
    return _pct(current, previous) if previous else 0.0


def count_recent_limit_ups(closes: list[float], *, days: int = 60, limit_up_pct: float = 9.5) -> int:
    start = max(1, len(closes) - days)
    return sum(
        1
        for index in range(start, len(closes))
        if _pct(closes[index], closes[index - 1]) >= limit_up_pct
    )


def _pullback_rounds(
    closes: list[float], highs: list[float], lows: list[float], *, lookback: int = 60, swing_pct: float = 5.0
) -> int:
    """自趋势起点（lookback 日最低点）起计的回调轮次。

    只统计"有效回调"：从上涨段的摆动高点回撤 ≥swing_pct% 才记一轮，
    避免把日常震荡误判为多次回调（素衣规则：最多做第三次回调）。
    当前若正处在回撤中，该轮回撤完成即计入。
    """

    if len(closes) < 15:
        return 0
    start = max(1, len(closes) - lookback)
    low_index = min(range(start, len(closes)), key=lambda i: lows[i])
    rounds = 0
    peak = highs[low_index]
    for index in range(low_index + 1, len(closes)):
        peak = max(peak, highs[index])
        if peak > 0 and (peak - closes[index]) / peak * 100.0 >= swing_pct:
            rounds += 1
            peak = highs[index]  # 回调确认后重新起算上涨段
    return rounds


def evaluate_candidate(
    snapshot: Mapping[str, Any],
    *,
    params: Mapping[str, Any] | None = None,
    amount_rank: int = 0,
    hot_rank: int | None = None,
    market_shrink: bool = False,
) -> dict[str, Any] | None:
    """对一只主板股票做趋势低吸评估；趋势破坏或流动性不足返回 None。"""

    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update({k: v for k, v in params.items() if v is not None})
    code = str(snapshot.get("code") or "")
    name = str(snapshot.get("name") or "")
    if not is_main_board_stock(code, name):
        return None
    bars = [dict(row) for row in (snapshot.get("postclose_kline") or []) if isinstance(row, Mapping)]
    if len(bars) < 40:
        return None
    closes = [_number(row.get("close")) for row in bars]
    highs = [_number(row.get("high"), close) for row, close in zip(bars, closes)]
    lows = [_number(row.get("low"), close) for row, close in zip(bars, closes)]
    opens = [_number(row.get("open"), close) for row, close in zip(bars, closes)]
    volumes = [_number(row.get("volume") or row.get("vol")) for row in bars]
    amounts = [_number(row.get("amount")) for row in bars]
    if min(closes[-2:]) <= 0:
        return None
    amount = _number(snapshot.get("amount"), amounts[-1] if amounts else 0.0)
    if amount < float(cfg["min_amount"]):
        return None

    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    # 趋势硬门槛：收盘不有效跌破 MA20，且 MA20 未明显下行
    if closes[-1] < ma20 * 0.95:
        return None
    ma20_slope = _ma_slope(closes, 20)
    trend_ok = closes[-1] >= ma10 * 0.98 or closes[-1] >= ma20
    if not trend_ok:
        return None

    low_60 = min(lows[-60:])
    channel_gain = _pct(closes[-1], low_60)  # 上升趋势"中期"代理：距 60 日低点涨幅适中
    mid_channel = 15.0 <= channel_gain <= 120.0

    activity = count_recent_limit_ups(closes)
    activity_ok = activity >= int(float(cfg["min_activity_limit_ups"]))
    if not activity_ok:
        return None  # 股性活跃是硬门槛：近期无涨停的票不符合"人气趋势强票"

    # ---------- 回调反包结构 ----------
    pullback_days = 0
    pullback_low = closes[-1]
    index = len(closes) - 2
    max_back = int(float(cfg["max_pullback_days"]))
    while index >= 1 and pullback_days < max_back and closes[index] < closes[index - 1]:
        pullback_days += 1
        pullback_low = min(pullback_low, closes[index], closes[index - 1])
        index -= 1
    pullback_high = max(highs[max(0, len(closes) - 1 - pullback_days - 8):len(closes) - 1 - pullback_days] or [highs[-2]])
    pullback_rounds = _pullback_rounds(closes, highs, lows)
    too_many_rounds = pullback_rounds > int(float(cfg["max_pullback_rounds"]))

    ma_band = float(cfg["ma_touch_band"]) / 100.0
    near_ma10 = abs(closes[-1] - ma10) <= ma10 * ma_band if ma10 else False
    near_ma5 = abs(closes[-1] - ma5) <= ma5 * ma_band if ma5 else False
    bullish_wrap = closes[-1] > opens[-1] and closes[-1] > closes[-2] and closes[-1] >= highs[-2] * 0.995
    reclaim = closes[-1] >= ma10 and closes[-2] < ma10 * 0.995 if ma10 else False
    triggered = bool((bullish_wrap or reclaim) and pullback_days >= 2 and (near_ma10 or near_ma5))
    in_pullback = pullback_days >= 2 and closes[-1] <= pullback_high * 0.97

    volume_ratio = volumes[-1] / (sum(v for v in volumes[-6:-1] if v > 0) / max(1, len([v for v in volumes[-6:-1] if v > 0]))) if any(v > 0 for v in volumes[-6:-1]) else 0.0
    pullback_shrink = (
        sum(volumes[len(volumes) - 1 - pullback_days:]) / max(1, pullback_days)
    ) / (sum(v for v in volumes[-20 - pullback_days:-pullback_days] if v > 0) / max(1, len([v for v in volumes[-20 - pullback_days:-pullback_days] if v > 0]))) if pullback_days else 1.0

    change_pct = _pct(closes[-1], closes[-2])
    dip_rebound = (opens[-1] - lows[-1]) / opens[-1] * 100.0 >= 3.0 and closes[-1] >= opens[-1]
    dip_watch = change_pct <= -3.0 and closes[-1] >= ma20
    if triggered:
        buy_point = "回调反包"
    elif in_pullback and (near_ma10 or near_ma5):
        buy_point = "均线低吸"
    elif dip_rebound:
        buy_point = "急跌拉回"
    elif dip_watch:
        buy_point = "急跌观察"
    else:
        return None  # 无买点结构的不进池

    if too_many_rounds:
        return None  # 最多做第三次回调

    # ---------- 评分 ----------
    volume_score = _clamp((volume_ratio - 0.8) / (1.8 - 0.8) * 100.0)
    rank = amount_rank if amount_rank > 0 else 999
    if rank <= int(float(cfg["amount_rank_top"])):
        hot_score = 100.0 if hot_rank is None else _clamp(100.0 - (hot_rank or 100) * 0.4)
    elif rank <= 200:
        hot_score = 65.0
    else:
        hot_score = 35.0
    activity_score = {0: 20.0, 1: 50.0, 2: 75.0}.get(activity, 100.0) if activity < 3 else 100.0
    wrap_quality = (closes[-1] - lows[-1]) / max(1e-9, highs[-1] - lows[-1]) * 100.0
    trend_score = _clamp(
        (40.0 if trend_ok else 0.0)
        + (25.0 if ma20_slope >= 0 else 0.0)
        + (20.0 if mid_channel else 8.0)
        + (15.0 if pullback_shrink <= 0.9 else 5.0)
    )
    structure_score = _clamp(
        (60.0 if triggered else 30.0 if in_pullback else 20.0)
        + (25.0 if pullback_shrink <= 0.85 else 10.0 if pullback_shrink <= 1.0 else 0.0)
        + (15.0 if wrap_quality >= 60 else 8.0 if wrap_quality >= 40 else 0.0)
    )
    total = volume_score * 0.20 + hot_score * 0.20 + activity_score * 0.15 + trend_score * 0.20 + structure_score * 0.25

    risk_flags: list[str] = []
    if market_shrink:
        risk_flags.append("两市明显缩量，按模式纪律控制仓位")
    if channel_gain > 80.0:
        risk_flags.append(f"距60日低点已涨 {channel_gain:.0f}%，趋势位置偏高")
    if ma20_slope < -0.5:
        risk_flags.append("MA20 走弱，注意趋势变坏")
    if hot_rank is not None and hot_rank > 100:
        risk_flags.append(f"同花顺人气排名 {hot_rank}，热度一般")

    decision = "candidate" if triggered and total >= 60 else "watch"
    return {
        "code": code,
        "name": name,
        "industry": str(snapshot.get("industry") or "其他"),
        "trade_date": str(bars[-1].get("date") or "")[:10],
        "price": round(closes[-1], 2),
        "change_pct": round(change_pct, 2),
        "amount": round(amount, 2),
        "amount_rank": amount_rank,
        "hot_rank": hot_rank,
        "volume_ratio": round(volume_ratio, 2),
        "activity_60d_limit_ups": activity,
        "pullback_days": pullback_days,
        "pullback_rounds": pullback_rounds,
        "buy_point": buy_point,
        "triggered": triggered,
        "near_ma10": near_ma10,
        "channel_gain_pct": round(channel_gain, 1),
        "pullback_shrink_ratio": round(pullback_shrink, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "ma20_slope": round(ma20_slope, 2),
        "score": round(total, 1),
        "breakdown": {
            "volume": {"weight": "20%", "score": round(volume_score, 1), "label": "量能放大"},
            "hot": {"weight": "20%", "score": round(hot_score, 1), "label": "成交额热度"},
            "activity": {"weight": "15%", "score": round(activity_score, 1), "label": "股性活跃（60日涨停数）"},
            "trend": {"weight": "20%", "score": round(trend_score, 1), "label": "趋势与位置"},
            "structure": {"weight": "25%", "score": round(structure_score, 1), "label": "回调反包结构"},
        },
        "buy_reference": f"分时/日线回踩 MA10 {ma10:.2f} 附近低吸，不追高",
        "stop_reference": f"有效跌破 MA20 {ma20 * 0.97:.2f} 或买点低点 {pullback_low:.2f}",
        "decision": decision,
        "decision_reason": (
            "回调≤5日且均线附近反包（模式触发）" if triggered
            else "处于回调观察期，等待反包确认" if in_pullback
            else "急跌/超预期低吸观察"
        ),
        "risk_flags": risk_flags,
        "strategy_version": STRATEGY_VERSION,
    }


def auction_verify(
    rows: Iterable[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    enforce: bool = True,
) -> list[dict[str, Any]]:
    """次日竞价验证：昨晚计划池在今早竞价的表现是否符合预期。

    素衣模式纪律："思考市场是不是符合昨天的思路，按照昨天的计划交易"。
    判定：竞价高开 0~7% 为符合计划（按位执行）；低开超 3% 放弃；
    高开超 7% 追高风险；无竞价数据标记缺失。
    """

    quote_map = {
        str(k): dict(v)
        for k, v in (quotes or {}).items()
        if isinstance(v, Mapping)
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "")
        quote = quote_map.get(code) or {}
        auction_price = _number(quote.get("auction_price"))
        last_close = _number(quote.get("last_close")) or _number(row.get("price"))
        if auction_price > 0 and last_close > 0:
            change = (auction_price / last_close - 1.0) * 100.0
            available = True
        else:
            change = _number(quote.get("change_pct"))
            available = bool(quote.get("available")) and change is not None and change != 0.0
        if not available:
            verdict, cls = ("竞价数据缺失", "neutral") if enforce else (f"参考 · 竞价偏离 {change:+.1f}%", "neutral")
        elif not enforce:
            verdict, cls = f"参考 · 竞价偏离 {change:+.1f}%（非竞价时段）", "neutral"
        elif change < -3.0:
            verdict, cls = "放弃 · 低开过大（不符合昨晚思路）", "down"
        elif change > 7.0:
            verdict, cls = "谨慎 · 高开过大，防兑现", "warning"
        elif -1.0 <= change <= 5.0:
            verdict, cls = "符合计划 · 可按位执行", "bullish"
        else:
            verdict, cls = "观察 · 偏离理想区间", "warning"
        out.append(
            {
                "code": code,
                "name": str(row.get("name") or ""),
                "buy_point": str(row.get("buy_point") or ""),
                "score": _number(row.get("score")),
                "auction_price": round(auction_price, 2) if auction_price else None,
                "auction_change_pct": round(change, 2) if available else None,
                "verdict": verdict,
                "verdict_class": cls,
                "available": available,
            }
        )
    out.sort(key=lambda item: (item["available"] is False, -(item["available"] and 1 or 0) * 0 - _number(item["score"])))
    return out


def screen_universe(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    hot_stocks: Iterable[Mapping[str, Any]] = (),
    limit: int = 50,
    params: Mapping[str, Any] | None = None,
    market_total_amount: float = 0.0,
    market_amount_history: Iterable[float] = (),
) -> dict[str, Any]:
    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update({k: v for k, v in params.items() if v is not None})
    rows = [dict(item) for item in snapshots if isinstance(item, Mapping)]
    main_board = [item for item in rows if is_main_board_stock(item.get("code"), item.get("name"))]
    ranked = sorted(
        main_board,
        key=lambda item: _number(item.get("amount")),
        reverse=True,
    )
    rank_by_code = {str(item.get("code") or ""): pos for pos, item in enumerate(ranked, 1)}
    hot_by_code = {
        str(item.get("code") or ""): item
        for item in hot_stocks
        if isinstance(item, Mapping)
    }
    history = [ _number(v) for v in market_amount_history ]
    market_shrink = bool(
        market_total_amount > 0
        and history
        and market_total_amount < sum(history[-5:]) / max(1, len(history[-5:])) * float(cfg["market_shrink_ratio"])
    )

    dates = [
        str((item.get("postclose_kline") or [{}])[-1].get("date") or "")[:10]
        for item in main_board
        if isinstance(item.get("postclose_kline"), list) and item.get("postclose_kline")
    ]
    candidates: list[dict[str, Any]] = []
    for item in main_board:
        code = str(item.get("code") or "")
        hot = hot_by_code.get(code)
        hot_rank = int(_number(hot.get("rank"))) if hot and hot.get("rank") is not None else None
        candidate = evaluate_candidate(
            item,
            params=cfg,
            amount_rank=rank_by_code.get(code, 0),
            hot_rank=hot_rank,
            market_shrink=market_shrink,
        )
        if candidate:
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (item.get("decision") == "candidate", -_number(item.get("score")), -_number(item.get("amount"))),
        reverse=False,
    )
    candidates.sort(key=lambda item: (-(1 if item.get("decision") == "candidate" else 0), -_number(item.get("score"))))
    limit = max(1, min(int(limit), 200))
    selected = candidates[:limit]
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank
    return {
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": max(dates, default=""),
        "source": "local_tdx_postclose",
        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605），排除 ST 与退市标的",
        "thresholds": {k: v for k, v in cfg.items()},
        "market_state": {
            "market_shrink": market_shrink,
            "amount_top_codes": len(rank_by_code),
        },
        "funnel": {
            "input": len(rows),
            "main_board": len(main_board),
            "amount_qualified": sum(1 for item in main_board if _number(item.get("amount")) >= float(cfg["min_amount"])),
            "matched": len(candidates),
            "returned": len(selected),
            "triggered": sum(1 for item in candidates if item.get("triggered")),
        },
        "rows": selected,
        "methodology": [
            "标的池：沪深主板非 ST，成交额 ≥3 亿且按成交额排名（素衣口径：只做市场前 100 的大成交票）。",
            "趋势硬门槛：收盘不有效跌破 MA20、MA20 未明显下行，距 60 日低点涨幅处于上升通道中期（15%~120%）。",
            "股性硬门槛：近 60 日涨停 ≥2 次（人气趋势强票）。",
            "买点结构：回调 ≤5 日（不超过一周）、最多第三轮回调，在 MA5/MA10 附近出现反包阳线或收复 MA10。",
            "买点分类：回调反包（触发）、均线低吸（观察）、急跌拉回/急跌观察（超预期低吸）。",
            "风控：两市较 5 日均量缩量超 15% 时提示控仓；拒绝尾盘追高，买点参考回踩 MA10。",
        ],
        "threshold_status": "全部阈值来自战法公开拆解的经验参数，待回测。",
        "disclaimer": "仅供研究，不构成投资建议。低吸不等于必反弹，跌破止损位必须执行。",
    }
