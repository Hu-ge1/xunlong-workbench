"""Explainable main-board oversold-rebound screening.

Version 2 follows the user's five-video playbook: market style first, then a
known low (P0), the high two bars to its left (R0), volume-confirmed breakout,
and an optional 3-9 session contracting-volume second wave.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import scoring


STRATEGY_VERSION = "mainboard-oversold-rebound-v3-capital-ths-2026.08.09"

from collections import Counter as _Counter

VETO_STATS: _Counter = _Counter()


def _veto(reason: str):
    """Record why a candidate was rejected; returns None so call sites stay terse."""

    VETO_STATS[reason] += 1
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _pct(current: float, base: float) -> float:
    return (current / base - 1.0) * 100.0 if base else 0.0


def _date(row: Mapping[str, Any]) -> str:
    return str(row.get("date") or row.get("datetime") or "")[:10]


def is_main_board_stock(code: Any, name: Any = "") -> bool:
    label = str(name or "").upper().replace(" ", "")
    return (
        scoring.is_main_board_security(code, name)
        and "ST" not in label
        and "退" not in label
    )


def _volume_ratio(volumes: list[float], index: int, window: int = 5) -> float:
    start = max(0, index - window)
    baseline = [value for value in volumes[start:index] if value > 0]
    average = sum(baseline) / len(baseline) if baseline else 0.0
    return volumes[index] / average if average else 0.0


def _amount_ratio(amounts: list[float], index: int, window: int = 5) -> float:
    start = max(0, index - window)
    baseline = [value for value in amounts[start:index] if value > 0]
    average = sum(baseline) / len(baseline) if baseline else 0.0
    return amounts[index] / average if average else 0.0


def _locate_structure(
    lows: list[float], highs: list[float], *, lookback: int = 60
) -> tuple[int, float, float] | None:
    """Locate the latest known P0 and its video-defined R0.

    The current bar is excluded from P0 discovery so the scanner never calls an
    intraday/new low a confirmed structural low.
    """

    if len(lows) < 35:
        return None
    start = max(2, len(lows) - lookback)
    stop = len(lows) - 1
    if stop <= start:
        return None
    p0_index = min(range(start, stop), key=lambda index: lows[index])
    if p0_index < 2:
        return None
    return p0_index, lows[p0_index], highs[p0_index - 2]


def _recent_limit_up(closes: list[float], index: int, sessions: int = 10) -> bool:
    start = max(1, index - sessions)
    return any(_pct(closes[pos], closes[pos - 1]) >= 9.5 for pos in range(start, index))


def _market_style(
    rows: list[Mapping[str, Any]], *, drawdown_min: float
) -> dict[str, Any]:
    """Estimate whether current strong stocks are led by oversold repair."""

    strong = 0
    oversold_strong = 0
    repair_breadth = 0
    usable = 0
    for item in rows:
        bars = item.get("postclose_kline") or []
        if not isinstance(bars, list) or len(bars) < 30:
            continue
        closes = [_number(row.get("close")) for row in bars]
        highs = [_number(row.get("high"), closes[index]) for index, row in enumerate(bars)]
        if min(closes[-2:]) <= 0:
            continue
        usable += 1
        daily_return = _pct(closes[-1], closes[-2])
        prior_high = max(highs[-60:])
        drawdown = max(0.0, -_pct(closes[-2], prior_high))
        indicators = scoring.standard_indicators(bars)
        ma20_series = list((indicators.get("series") or {}).get("ma20") or [])
        prior_ma20 = _number(ma20_series[-2]) if len(ma20_series) >= 2 else 0.0
        oversold_background = drawdown >= drawdown_min or bool(prior_ma20 and closes[-2] < prior_ma20)
        if daily_return >= 5.0:
            strong += 1
            if oversold_background:
                oversold_strong += 1
        if daily_return >= 3.0 and oversold_background:
            repair_breadth += 1
    share = oversold_strong / strong if strong else 0.0
    score = round(min(20.0, share * 12.0 + min(8.0, repair_breadth * 0.8)), 1)
    supportive = bool(strong >= 1 and share >= 0.45 and repair_breadth >= 2)
    if supportive and score >= 14:
        label = "超跌修复占优"
    elif score >= 8:
        label = "混沌轮动观察"
    else:
        label = "趋势/其他风格"
    return {
        "label": label,
        "supportive": supportive,
        "score": score,
        "strong_count": strong,
        "oversold_strong_count": oversold_strong,
        "oversold_share": round(share * 100.0, 1),
        "repair_breadth": repair_breadth,
        "usable_count": usable,
        "definition": "强势样本中超跌背景占比与板块修复广度的盘后代理指标",
    }


def evaluate_candidate(
    snapshot: Mapping[str, Any],
    bars: Iterable[Mapping[str, Any]],
    *,
    market: Mapping[str, Any] | None = None,
    popularity: Mapping[str, Any] | None = None,
    industry_hot_count: int = 0,
    drawdown_min: float = 12.0,
    volume_multiple: float = 1.5,
    min_amount: float = 100_000_000.0,
    amount_multiple: float = 1.2,
    profile: str = "steady",
    wave: str = "all",
    triggered_only: bool = True,
    market_filter: bool = True,
    min_reward_risk: float = 1.0,
    require_ths_hot: bool = True,
    require_positive_dde: bool = True,
) -> dict[str, Any] | None:
    """Evaluate one stock using the P0/R0 first-wave and second-wave rules."""

    code = str(snapshot.get("code") or "")
    name = str(snapshot.get("name") or "")
    if not is_main_board_stock(code, name):
        return None
    clean = [dict(row) for row in bars if isinstance(row, Mapping)]
    if len(clean) < 35:
        return None
    closes = [_number(row.get("close")) for row in clean]
    highs = [_number(row.get("high"), closes[index]) for index, row in enumerate(clean)]
    lows = [_number(row.get("low"), closes[index]) for index, row in enumerate(clean)]
    opens = [_number(row.get("open"), closes[index]) for index, row in enumerate(clean)]
    volumes = [_number(row.get("volume") or row.get("vol")) for row in clean]
    amounts = [_number(row.get("amount")) for row in clean]
    if min(closes[-2:]) <= 0:
        return None
    latest_amount = _number(snapshot.get("amount"), amounts[-1] if amounts else 0.0)
    if latest_amount < min_amount:
        return None
    current = len(clean) - 1
    current_amount_ratio = _amount_ratio(amounts, current)
    price_change = _pct(closes[current], closes[current - 1])
    quick_ma5 = sum(closes[-5:]) / 5.0

    structure = _locate_structure(lows, highs)
    if not structure:
        return None
    p0_index, p0, r0 = structure
    if p0 <= 0 or r0 <= p0:
        return None
    decline_high = max(highs[max(0, p0_index - 40):p0_index], default=r0)
    drawdown = max(0.0, -_pct(p0, decline_high))
    if drawdown < drawdown_min:
        return _veto(f"前期跌幅{drawdown:.0f}%<{drawdown_min:.0f}%")

    # 突破后回踩候选（缩量回踩守住 R0）：豁免"放量阳线/大单净量"前置闸门，
    # 否则回踩买点会在进入结构判定前被误杀。豁免仅限"突破发生在 1~5 日前
    # 且当前不是突破当日"的回踩状态。
    bullish_bar = price_change > 0 and closes[current] > opens[current]
    amount_ok = current_amount_ratio >= amount_multiple
    earlier_breakout = any(
        closes[i - 1] <= r0 < closes[i] for i in range(max(1, current - 5), current)
    )
    today_breakout = closes[current - 1] <= r0 < closes[current]
    pullback_candidate = bool(
        earlier_breakout
        and not today_breakout
        and r0 * 0.98 <= closes[current] <= r0 * 1.06
        and closes[current] > quick_ma5
    )

    ths = dict(popularity or {})
    if require_ths_hot and not ths:
        return _veto("不在同花顺人气池")
    dde = ths.get("large_order_net_ratio")
    dde_value = _number(dde) if dde is not None else None
    if require_positive_dde and not pullback_candidate and (dde_value is None or dde_value <= 0):
        return _veto("大单净量非正")
    if not (amount_ok and bullish_bar) and not pullback_candidate:
        return _veto("非放量阳线且非回踩确认")

    indicators = scoring.standard_indicators(clean)
    series = indicators.get("series") or {}
    latest = indicators.get("latest") or {}
    ma5 = _number(latest.get("ma5"))
    ma20 = _number(latest.get("ma20"))
    ma5_series = list(series.get("ma5") or [])
    ma20_series = list(series.get("ma20") or [])
    current_ratio = _volume_ratio(volumes, current)
    above_ma5 = bool(ma5 and closes[current] > ma5)
    above_ma20 = bool(ma20 and closes[current] > ma20)
    average_profile = profile == "basic"
    moving_average_pass = above_ma5 and (average_profile or above_ma20)
    body_confirmed = closes[current] > r0 and closes[current] > opens[current]
    first_breakout = (
        closes[current - 1] <= r0
        and body_confirmed
        and current_ratio >= volume_multiple
        and moving_average_pass
    )

    historical_breakouts: list[int] = []
    for index in range(max(p0_index + 1, 20), current - 2):
        ma5_i = _number(ma5_series[index]) if index < len(ma5_series) else 0.0
        ma20_i = _number(ma20_series[index]) if index < len(ma20_series) else 0.0
        ma_pass = bool(ma5_i and closes[index] > ma5_i) and (
            average_profile or bool(ma20_i and closes[index] > ma20_i)
        )
        if (
            closes[index - 1] <= r0 < closes[index]
            and closes[index] > opens[index]
            and _volume_ratio(volumes, index) >= volume_multiple
            and ma_pass
        ):
            historical_breakouts.append(index)

    second_setup: dict[str, Any] | None = None
    if historical_breakouts:
        wave1_start = historical_breakouts[-1]
        candidate_peaks = range(max(wave1_start, current - 9), current - 2)
        if candidate_peaks:
            peak_index = max(candidate_peaks, key=lambda index: highs[index])
            adjustment_days = current - peak_index
            adjustment = list(range(peak_index + 1, current))
            if 3 <= adjustment_days <= 9 and adjustment:
                launch_volume = max(volumes[wave1_start], volumes[peak_index])
                adjustment_average = sum(volumes[index] for index in adjustment) / len(adjustment)
                shrink = bool(
                    launch_volume > 0
                    and adjustment_average <= launch_volume * 0.8
                    and volumes[adjustment[-1]] <= volumes[adjustment[0]] * 1.1
                )
                support_held = min(lows[index] for index in adjustment) >= max(p0, r0 * 0.98)
                wave1_high = highs[peak_index]
                second_trigger = bool(
                    shrink
                    and support_held
                    and closes[current - 1] <= wave1_high < closes[current]
                    and closes[current] > opens[current]
                    and current_ratio >= volume_multiple
                    and moving_average_pass
                )
                second_setup = {
                    "triggered": second_trigger,
                    "wave1_start": wave1_start,
                    "wave1_high": wave1_high,
                    "peak_index": peak_index,
                    "adjustment_days": adjustment_days,
                    "adjustment_volume_ratio": adjustment_average / launch_volume if launch_volume else 0.0,
                    "shrink": shrink,
                    "support_held": support_held,
                    "adjustment_low": min(lows[index] for index in adjustment),
                }

    wave_type = "二波" if second_setup and second_setup["triggered"] else "一波" if first_breakout else ""
    observation_type = ""
    if (
        not wave_type
        and second_setup
        and second_setup["shrink"]
        and second_setup["support_held"]
        and moving_average_pass
        and second_setup["wave1_high"] * 0.95 <= closes[current] <= second_setup["wave1_high"]
    ):
        observation_type = "二波观察"
    elif not wave_type and above_ma5 and closes[current] <= r0 and closes[current] >= r0 * 0.95:
        observation_type = "一波观察"

    # 突破后回踩确认：近 5 日内突破 R0，当前缩量回踩仍守住 R0（压力变支撑）。
    recent_breakout_index = None
    for bar_index in range(max(1, current - 5), current + 1):
        if closes[bar_index - 1] <= r0 < closes[bar_index]:
            recent_breakout_index = bar_index
            break
    pullback_confirm = bool(
        not wave_type
        and recent_breakout_index
        and r0 * 0.98 <= closes[current] <= r0 * 1.06
        and above_ma5
        and volumes[current] <= volumes[recent_breakout_index] * 1.2
    )
    if pullback_confirm:
        wave_type = "一波"
    if wave == "first" and wave_type != "一波" and observation_type != "一波观察":
        return None
    if wave == "second" and wave_type != "二波" and observation_type != "二波观察":
        return None
    triggered = bool(wave_type)
    if triggered_only and not triggered:
        return _veto("仅结构观察未触发")

    market_state = dict(market or {})
    market_ok = bool(market_state.get("supportive"))
    # 只有市场风格明确敌对（资金明显不在超跌风格、风格分过低）才一票否决；
    # "混沌轮动观察"阶段让个股结构自己决定，避免长期空转。
    market_hostile = (
        market_state.get("supportive") is False
        and _number(market_state.get("score")) < 8.0
    )
    if market_filter and market_hostile:
        return _veto("市场风格敌对")

    stop = r0 * 0.98 if pullback_confirm and not first_breakout else p0 if wave_type != "二波" or not second_setup else second_setup["adjustment_low"]
    pressure_pool = [value for value in highs[max(0, p0_index - 40):current] if value > closes[current]]
    if pullback_confirm and recent_breakout_index and not pressure_pool:
        pressure_pool = [max(highs[max(0, recent_breakout_index - 2):current + 1])]
    target = min(pressure_pool) if pressure_pool else max(highs[-60:])
    risk = max(0.01, closes[current] - stop)
    reward = max(0.0, target - closes[current])
    reward_risk = reward / risk
    if pullback_confirm and not pressure_pool:
        # 60 日内无可见压力时按 1:1 保守估算，不虚构目标位
        reward_risk = max(reward_risk, 1.0)
    if reward_risk < min_reward_risk:
        return None

    recent_limit = _recent_limit_up(closes, current)
    structure_score = 15.0
    breakout_score = 20.0 if triggered else 8.0 if observation_type else 0.0
    volume_score = min(15.0, current_ratio / max(volume_multiple, 0.1) * 15.0) if triggered else 0.0
    moving_average_score = 10.0 if above_ma20 else 6.0 if above_ma5 else 0.0
    funding_score = 5.0 if ths and dde_value is not None and dde_value > 0 else 0.0
    second_score = 10.0 if wave_type == "二波" else 5.0 if second_setup and second_setup["shrink"] else 0.0
    reward_score = 5.0 if reward_risk >= 2.0 else 3.0 if reward_risk >= 1.5 else 0.0
    score = round(min(100.0, _number(market_state.get("score")) + structure_score + breakout_score + volume_score + moving_average_score + funding_score + second_score + reward_score), 1)

    vetoes: list[str] = []
    if not market_ok:
        vetoes.append("市场风格未确认超跌修复占优")
    if not moving_average_pass:
        vetoes.append("未站上要求的关键均线")
    if current_ratio < volume_multiple:
        vetoes.append("量能未达到预设倍量")
    if second_setup and not second_setup["shrink"]:
        vetoes.append("二波调整期未缩量")
    stage = "突破后回踩" if pullback_confirm and not first_breakout else f"{wave_type}触发" if wave_type else observation_type or "结构观察"
    trigger_price = second_setup["wave1_high"] if wave_type == "二波" and second_setup else r0
    reason_extra = "；缩量回踩突破位确认，压力转支撑" if pullback_confirm and not first_breakout else ""
    return {
        "code": code,
        "name": name,
        "industry": str(snapshot.get("industry") or "其他"),
        "trade_date": _date(clean[-1]) or str(snapshot.get("as_of") or ""),
        "price": round(closes[current], 2),
        "change_pct": round(_pct(closes[current], closes[current - 1]), 2),
        "drawdown": round(drawdown, 2),
        "p0": round(p0, 2),
        "p0_date": _date(clean[p0_index]),
        "r0": round(r0, 2),
        "r0_date": _date(clean[p0_index - 2]),
        "trigger_price": round(trigger_price, 2),
        "volume_multiple": round(current_ratio, 2),
        "amount_multiple": round(current_amount_ratio, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "above_ma5": above_ma5,
        "above_ma20": above_ma20,
        "recent_limit_up": recent_limit,
        "wave": wave_type or observation_type.replace("观察", ""),
        "stage": stage,
        "triggered": triggered,
        "adjustment_days": second_setup.get("adjustment_days") if second_setup else None,
        "adjustment_shrink_ratio": round(second_setup.get("adjustment_volume_ratio", 0.0), 2) if second_setup else None,
        "support": round(stop, 2),
        "resistance": round(target, 2),
        "invalidation": round(stop * 0.995, 2),
        "reward_risk": round(reward_risk, 2),
        "amount": round(latest_amount, 2),
        "ths_hot": bool(ths),
        "ths_hot_rank": ths.get("rank"),
        "ths_hot_reason": str(ths.get("reason") or ""),
        "ths_turnover": round(_number(ths.get("turnover")), 2),
        "ths_large_order_net_ratio": round(dde_value, 2) if dde_value is not None else None,
        "industry_hot_count": int(industry_hot_count),
        "score": score,
        "market_style": market_state.get("label", "未评估"),
        "vetoes": vetoes,
        "reason": (
            f"P0 {p0:.2f}（{_date(clean[p0_index])}），R0 {r0:.2f}；"
            f"当前量能 {current_ratio:.2f} 倍，{stage}。{reason_extra}"
        ),
        "trigger_condition": f"放量至少 {volume_multiple:.1f} 倍并有效站上 {trigger_price:.2f}。",
        "invalidation_condition": f"跌破结构低点 {stop:.2f}，或突破后收盘重新跌回关键线。",
        "strategy_version": STRATEGY_VERSION,
    }


def screen_universe(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    hot_stocks: Iterable[Mapping[str, Any]] = (),
    limit: int = 80,
    drawdown_min: float = 12.0,
    volume_multiple: float = 1.5,
    min_amount: float = 100_000_000.0,
    amount_multiple: float = 1.2,
    profile: str = "steady",
    wave: str = "all",
    triggered_only: bool = True,
    market_filter: bool = True,
    min_reward_risk: float = 1.0,
    require_ths_hot: bool = True,
    require_positive_dde: bool = True,
) -> dict[str, Any]:
    VETO_STATS.clear()
    rows = [dict(item) for item in snapshots if isinstance(item, Mapping)]
    main_board = [item for item in rows if is_main_board_stock(item.get("code"), item.get("name"))]
    hot_rows = [dict(item) for item in hot_stocks if isinstance(item, Mapping)]
    hot_by_code = {str(item.get("code") or ""): item for item in hot_rows}
    main_board_codes = {str(item.get("code") or "") for item in main_board}
    industry_hot_counts: dict[str, int] = {}
    for item in main_board:
        if str(item.get("code") or "") not in hot_by_code:
            continue
        industry = str(item.get("industry") or "其他")
        industry_hot_counts[industry] = industry_hot_counts.get(industry, 0) + 1
    market = _market_style(main_board, drawdown_min=drawdown_min)
    with_history = 0
    structural = 0
    capital_ready = 0
    candidates: list[dict[str, Any]] = []
    for item in main_board:
        bars = item.get("postclose_kline") or []
        if isinstance(bars, list) and len(bars) >= 35:
            with_history += 1
            lows = [_number(row.get("low"), _number(row.get("close"))) for row in bars]
            highs = [_number(row.get("high"), _number(row.get("close"))) for row in bars]
            if _locate_structure(lows, highs):
                structural += 1
            amounts = [_number(row.get("amount")) for row in bars]
            closes = [_number(row.get("close")) for row in bars]
            opens = [_number(row.get("open"), closes[index]) for index, row in enumerate(bars)]
            hot = hot_by_code.get(str(item.get("code") or ""))
            dde = hot.get("large_order_net_ratio") if hot else None
            if (
                hot
                and _number(item.get("amount"), amounts[-1] if amounts else 0.0) >= min_amount
                and _amount_ratio(amounts, len(amounts) - 1) >= amount_multiple
                and len(closes) >= 2
                and closes[-1] > closes[-2]
                and closes[-1] > opens[-1]
                and (not require_positive_dde or (dde is not None and _number(dde) > 0))
            ):
                capital_ready += 1
        candidate = evaluate_candidate(
            item,
            bars if isinstance(bars, list) else [],
            market=market,
            popularity=hot_by_code.get(str(item.get("code") or "")),
            industry_hot_count=industry_hot_counts.get(str(item.get("industry") or "其他"), 0),
            drawdown_min=drawdown_min,
            volume_multiple=volume_multiple,
            min_amount=min_amount,
            amount_multiple=amount_multiple,
            profile=profile,
            wave=wave,
            triggered_only=triggered_only,
            market_filter=market_filter,
            min_reward_risk=min_reward_risk,
            require_ths_hot=require_ths_hot,
            require_positive_dde=require_positive_dde,
        )
        if candidate:
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            bool(item.get("triggered")),
            item.get("wave") == "二波",
            _number(item.get("score")),
            _number(item.get("amount")),
        ),
        reverse=True,
    )
    limit = max(1, min(int(limit), 200))
    selected = candidates[:limit]
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank
    dates = [
        _date((item.get("postclose_kline") or [])[-1])
        for item in main_board
        if isinstance(item.get("postclose_kline"), list) and item.get("postclose_kline")
    ]
    return {
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": max(dates, default=""),
        "source": "local_tdx_postclose",
        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605），排除 ST 与退市标的",
        "market_style": market,
        "thresholds": {
            "drawdown_min": drawdown_min,
            "volume_multiple": volume_multiple,
            "min_amount": min_amount,
            "amount_multiple": amount_multiple,
            "profile": profile,
            "wave": wave,
            "triggered_only": triggered_only,
            "market_filter": market_filter,
            "min_reward_risk": min_reward_risk,
            "require_ths_hot": require_ths_hot,
            "require_positive_dde": require_positive_dde,
        },
        "funnel": {
            "input": len(rows),
            "main_board": len(main_board),
            "history_ready": with_history,
            "structure_ready": structural,
            "ths_hot_total": len(hot_rows),
            "ths_hot_mainboard": sum(1 for code in hot_by_code if code in main_board_codes),
            "capital_ready": capital_ready,
            "matched": len(candidates),
            "returned": len(selected),
        },
        "rows": selected,
        "veto_stats": dict(VETO_STATS.most_common(8)),
        "methodology": [
            "人气前提：只从同花顺当日强势人气池中选股，并保留同花顺题材归因与大单净量证据。",
            "资金闸门：成交额至少1亿元、当日成交额达到近5日均值1.5倍、阳线上涨且大单净量为正。",
            "市场前提：强势样本以长期均线下方的超跌修复为主，并出现板块级修复广度。",
            "结构锚点：确认已知最低点 P0；取 P0 左侧第2根K线最高价为初始压力 R0。",
            "一波触发：收盘有效突破 R0，成交量达到固定倍量，并站上5日线；稳健版同时要求站上20日线。",
            "二波触发：一波后调整3–9个交易日，缩量且不破结构，再放量突破一波高点。",
            "风险纪律：只按短线反弹管理；结构破位、调整放量、时间超限、突破失败或盈亏比不足均放弃。",
        ],
        "threshold_status": "倍量、回撤与盈亏比是可回测参数；P0/R0和3–9日规则来自新模式资料。",
        "disclaimer": "仅供研究，不构成投资建议。超跌反弹不等于趋势反转。",
    }
