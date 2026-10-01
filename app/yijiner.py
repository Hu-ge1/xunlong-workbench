"""一进二（昨日首板 -> 今日二板）评分卡筛选。

规则来自桌面两份战法总结（龙场王哥《全网最全1进2讲解》与
「排账日记」《一进二战法.docx》量化评分卡）：

- 竞价爆量档位：竞价成交额 / 昨日全天成交额，10%/7%/5%/3%/2% 对应不同涨停概率区间
- 首板强势度：资金面 40% + 板块热度 30% + 市场情绪 20% + 消息面 10%
- 次日延续：竞价量比 35% + 高开幅度 20% + 爆量档 20% + 板块联动 15% + 市场宽度 10%
- 竞价量比 = 竞价成交额 / 昨日全天成交额 * 100
- 停机坪接力（正向）：首板次日高开 1~5% 且量比 1.5~5（量比缺失时按竞价额÷前5日分钟均额÷5折算）
- 死亡换手过滤：昨日换手 >60% 警惕，≥70% 十有八九见顶（50亿以内短线票口径）

涨停时间与封单额暂无数据源，相关因子按数据缺失处理并明确标注降级。
所有阈值均为经验参数（待回测），输出仅供研究，不构成投资建议。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import scoring


STRATEGY_VERSION = "yijiner-scorecard-v1-2026.09.06"

# 竞价爆量档位（竞价成交额 / 昨日全天成交额，百分比）
# 用昨日成交额做分母才能真实反映"竞价爆量"——除以流通市值对中大盘股天然偏低，
# 会导致几乎所有股票都无档位。阈值 2%~10% 与竞价量比理想区间 7%~12% 匹配。
TIER_THRESHOLDS = (
    (10.0, "S", "S级 · 很少见（竞价/昨日成交额≥10%，经验涨停概率≥95%）"),
    (7.0, "A", "A级 · 比较少（竞价/昨日成交额≥7%，经验涨停概率≥80%）"),
    (5.0, "B", "B级 · 较少见（竞价/昨日成交额≥5%，经验涨停概率≥70%）"),
    (3.0, "C", "C级 · 较常见（竞价/昨日成交额≥3%，经验涨停概率≥60%）"),
    (2.0, "D", "D级 · 经常见（竞价/昨日成交额≥2%，经验涨停概率≥55%）"),
)

TIER_PROBABILITY = {
    "S": (95, "很少见"),
    "A": (80, "比较少"),
    "B": (70, "较少见"),
    "C": (60, "较常见"),
    "D": (55, "经常见"),
    "": (None, "低于2%，不作为高概率信号"),
}

AUCTION_COMPONENT_WEIGHTS = {
    "auction_amount_ratio": 0.35,
    "auction_gap": 0.20,
    "auction_burst": 0.20,
    "sector_today": 0.15,
    "market_today": 0.10,
}

DEFAULT_PARAMS: dict[str, float] = {
    "min_auction_amount": 30_000_000.0,   # 竞价成交额下限（3000万，可调）
    "min_float_mcap": 2_000_000_000.0,    # 流通市值下限（20亿）
    "max_float_mcap": 7_000_000_000.0,    # 流通市值上限（70亿，超过扣分，>200亿剔除）
    "hard_max_float_mcap": 20_000_000_000.0,
    "auction_ratio_excellent": 10.0,
    "auction_ratio_strong": 7.0,
    "auction_ratio_watch": 5.0,
    "auction_ratio_observe": 3.0,
    "auction_amount_ratio_min": 5.0,      # 竞价量比理想区间下限（%）
    "auction_amount_ratio_max": 12.0,     # 竞价量比理想区间上限（%）
    "min_prev_amount": 300_000_000.0,     # 昨日成交额下限（3亿流动性）
    "limit_up_pct": 9.8,                  # 主板涨停判定（10%板）
    "min_prev_price": 3.0,                # 低价股下限（避免仙股）
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


def _bars(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in (snapshot.get("postclose_kline") or []) if isinstance(row, Mapping)]


def _closes(bars: list[Mapping[str, Any]]) -> list[float]:
    return [_number(row.get("close")) for row in bars]


def board_count(closes: list[float], *, limit_up_pct: float = 9.8, max_days: int = 10) -> int:
    """从最后一根K线往前数连续涨停天数（1 = 首板）。"""

    if len(closes) < 2:
        return 0
    count = 0
    index = len(closes) - 1
    while index > 0 and count < max_days:
        if _pct(closes[index], closes[index - 1]) >= limit_up_pct:
            count += 1
            index -= 1
        else:
            break
    return count


def _ma(closes: list[float], window: int) -> float:
    if len(closes) < window:
        return 0.0
    return sum(closes[-window:]) / window


def _tier(ratio_pct: float) -> tuple[str, str]:
    for threshold, key, label in TIER_THRESHOLDS:
        if ratio_pct >= threshold:
            return key, label
    return "", "未达观察档（<2%）"


def _score_with_weight(value: float, weight: float) -> tuple[float, float]:
    """返回（该权重档得分，实际计入的权重）。数据缺失时计入权重为 0，由调用方归一化。"""

    return value, weight


def compute_perf20(bars: list[dict[str, Any]], *, limit_up_pct: float = 9.8) -> dict[str, Any]:
    """个股近 20 个交易日表现：趋势、位置、股性、量能与回撤。

    全部来自已收盘 K 线，不含任何估算；不足 6 根 K 线返回空。
    """

    clean = [dict(row) for row in bars if isinstance(row, Mapping) and _number(row.get("close"))]
    if len(clean) < 6:
        return {}
    closes = [_number(row.get("close")) for row in clean]
    span = min(20, len(closes) - 1)
    window = clean[-(span + 1):]
    w_closes = closes[-(span + 1):]
    ret20 = (w_closes[-1] / w_closes[0] - 1.0) * 100.0
    highs = [_number(row.get("high"), cl) for row, cl in zip(window[1:], w_closes[1:])]
    lows = [_number(row.get("low"), cl) for row, cl in zip(window[1:], w_closes[1:])]
    high20 = max(highs)
    low20 = min(lows)
    pos_in_range = round((w_closes[-1] - low20) / (high20 - low20) * 100.0, 1) if high20 > low20 else 50.0
    daily: list[dict[str, Any]] = []
    limit_up_count = 0
    up_days = 0
    for index in range(1, len(window)):
        pct = (window[index]["close"] / window[index - 1]["close"] - 1.0) * 100.0
        if pct >= limit_up_pct:
            limit_up_count += 1
        if pct > 0:
            up_days += 1
        daily.append(
            {
                "date": str(window[index].get("date") or "")[:10],
                "pct": round(pct, 2),
                "close": round(window[index]["close"], 2),
            }
        )
    amounts = [_number(row.get("amount")) for row in clean]
    amounts = [a for a in amounts if a and a > 0]
    vol_5v20 = None
    vol_5v20_basis = None
    if len(amounts) >= 20:
        avg5 = sum(amounts[-5:]) / 5.0
        avg20 = sum(amounts[-20:]) / 20.0
        vol_5v20 = round(avg5 / avg20, 2) if avg20 > 0 else None
        vol_5v20_basis = "amount"
    else:
        # Tencent adjusted K-lines include volume but may omit historical
        # amount.  Volume is the correct non-estimated fallback for relative
        # activity; expose the basis so the UI never disguises the fallback.
        volumes = [_number(row.get("volume") or row.get("vol")) for row in clean]
        volumes = [value for value in volumes if value > 0]
        if len(volumes) >= 20:
            avg5 = sum(volumes[-5:]) / 5.0
            avg20 = sum(volumes[-20:]) / 20.0
            vol_5v20 = round(avg5 / avg20, 2) if avg20 > 0 else None
            vol_5v20_basis = "volume"
    peak = w_closes[0]
    max_drawdown = 0.0
    for cl in w_closes[1:]:
        peak = max(peak, cl)
        max_drawdown = min(max_drawdown, (cl / peak - 1.0) * 100.0)
    return {
        "ret20_pct": round(ret20, 2),
        "dist_high_pct": round((w_closes[-1] / high20 - 1.0) * 100.0, 2),
        "pos_in_range": pos_in_range,
        "limit_up_count": limit_up_count,
        "up_days": up_days,
        "vol_5v20": vol_5v20,
        "vol_5v20_basis": vol_5v20_basis,
        "max_drawdown_pct": round(max_drawdown, 2),
        "days": span,
        "daily": daily,
    }


def evaluate_candidate(
    snapshot: Mapping[str, Any],
    auction_quote: Mapping[str, Any] | None,
    *,
    params: Mapping[str, Any] | None = None,
    prev_limit_up_count: int = 0,
    industry_limit_up_count: int = 1,
    industry_strong_auction_count: int = 0,
    market_prev_limit_up: int = 0,
    market_auction_breadth: float = 0.0,
    limit_pool_row: Mapping[str, Any] | None = None,
    market_sealed_ratio: float | None = None,
    trace_low: float | None = None,
) -> dict[str, Any] | None:
    """对一只昨日首板股票做一进二评分；不满足硬性门槛返回 None。"""

    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update({k: v for k, v in params.items() if v is not None})
    code = str(snapshot.get("code") or "")
    name = str(snapshot.get("name") or "")
    if not is_main_board_stock(code, name):
        return None
    bars = _bars(snapshot)
    if len(bars) < 30:
        return None
    closes = _closes(bars)
    last = bars[-1]
    prev_amount = _number(last.get("amount"))
    prev_close = closes[-1]
    if min(prev_close, closes[-2]) <= 0 or prev_amount < float(cfg["min_prev_amount"]):
        return None
    prev_pct = _pct(closes[-1], closes[-2])
    if prev_pct < float(cfg["limit_up_pct"]):
        return None
    pool = dict(limit_pool_row or {})
    if pool.get("lianban_count") is not None:
        boards = int(_number(pool.get("lianban_count")))
    else:
        boards = board_count(closes, limit_up_pct=float(cfg["limit_up_pct"]))
    if boards != 1:
        return None  # 只做首板的一进二；连板票属于接力范畴
    price = closes[-1]
    if price < float(cfg["min_prev_price"]):
        return None

    float_mcap = _number(snapshot.get("float_market_cap"))
    if float_mcap <= 0:
        return None
    if float_mcap > float(cfg["hard_max_float_mcap"]):
        return None

    quote = dict(auction_quote or {})
    auction_available = bool(quote.get("available", True)) and bool(quote.get("auction_price"))
    auction_price = _number(quote.get("auction_price"))
    auction_amount = _number(quote.get("auction_amount"))
    last_close = _number(quote.get("last_close"), prev_close)
    auction_change = _pct(auction_price, last_close) if auction_price and last_close else _number(quote.get("change_pct"))

    # 昨日换手率（死亡换手过滤）与竞价量比（停机坪接力）。
    # 报价自带量比优先；FFD 竞价通道缺失时用「竞价额 ÷ 前5日分钟均额 ÷ 5」
    # 折算通达信式开盘量比（按竞价 5 分钟口径）。
    prev_turnover = None
    for key in ("turnover", "turnover_pct", "turnover_rate"):
        raw = last.get(key)
        if raw not in (None, ""):
            prev_turnover = _number(raw)
            break
    volume_ratio = _number(quote.get("volume_ratio")) or None
    if volume_ratio is None and auction_amount and prev_amount:
        amounts5 = [_number(row.get("amount")) for row in bars[-6:-1]]
        amounts5 = [a for a in amounts5 if a and a > 0]
        if amounts5:
            per_minute = (sum(amounts5) / len(amounts5)) / 240.0
            if per_minute > 0:
                volume_ratio = round(auction_amount / per_minute / 5.0, 2)
    parking_apron = bool(
        auction_available and volume_ratio is not None and auction_change is not None
        and 1.0 <= auction_change <= 5.0 and 1.5 <= volume_ratio <= 5.0
    )
    # 竞价过程弱转强（路径因子）：09:2x 采样曾明显水下，终态却健康高开。
    # 视频口径：从 -8%~-10% 拉到高开 +2~3% 最佳，3~4 偏高，5 以上就算高。
    path_weak_to_strong = bool(
        trace_low is not None and auction_available and auction_change is not None
        and trace_low <= -3.0 and 1.0 <= auction_change <= 7.0
    )
    path_fading = bool(
        trace_low is not None and auction_change is not None
        and trace_low >= 4.0 and auction_change <= trace_low - 3.0
    )

    # 一进二只找仍有换手空间的潜在涨停股；09:25 已封死一字板的不推送。
    auction_stage = str(quote.get("auction_stage") or "").lower()
    unmatched_sell = quote.get("auction_unmatched_sell_lots")
    if unmatched_sell is None:
        unmatched_sell = quote.get("unmatched_sell_lots")
    if (
        auction_stage in {"opening_call_auction_final", "final", "auction_final", "集合竞价终态"}
        and auction_change >= float(cfg["limit_up_pct"])
        and unmatched_sell is not None
        and _number(unmatched_sell) <= 0
    ):
        return None

    # 竞价量比（竞价成交额 / 昨日全天成交额 * 100，理想 7~12）—— 先算，用于档位判定
    auction_amount_ratio = auction_amount / prev_amount * 100.0 if auction_amount and prev_amount else 0.0

    # 竞价爆量档位（竞价成交额 / 昨日成交额）：反映竞价相对昨日全天的放量程度
    amount_to_mcap = auction_amount / float_mcap * 100.0 if auction_amount and float_mcap else 0.0  # 保留作参考指标
    tier_key, tier_label = _tier(auction_amount_ratio)
    probability_pct, probability_label = TIER_PROBABILITY.get(tier_key, (None, "待确认"))

    flags: list[str] = []
    missing: list[str] = []

    # ---------- 首板强势度（昨日） ----------
    # 资金面 40%：量能倍数、K线位置、均线、炸板次数、首板时间（封单额无数据源）
    volumes = [_number(row.get("volume") or row.get("vol")) for row in bars]
    baseline = [value for value in volumes[-6:-1] if value > 0]
    volume_multiple = volumes[-1] / (sum(baseline) / len(baseline)) if baseline else 0.0
    high_60 = max(_number(row.get("high"), close) for row, close in zip(bars[-60:], closes[-60:]))
    near_high = closes[-1] >= high_60 * 0.97
    ma5 = _ma(closes[:-1], 5)
    ma10 = _ma(closes[:-1], 10)
    ma20 = _ma(closes[:-1], 20)
    ma_ok = bool(ma5 and closes[-1] > ma5) and bool(ma10 and ma5 >= ma10)
    ma_bull = bool(ma20 and ma10 >= ma20 and closes[-1] > ma20)

    capital_components: list[dict[str, Any]] = []
    capital_weights: list[float] = []
    volume_score = _clamp((volume_multiple - 0.8) / (2.0 - 0.8) * 100.0)
    capital_components.append({"key": "volume", "label": "首板量能（前5日均量倍数）", "score": round(volume_score, 1), "evidence": f"{volume_multiple:.2f}x"})
    capital_weights.append(25.0)
    position_score = 100.0 if near_high else _clamp(_pct(closes[-1], high_60) + 100.0)
    capital_components.append({"key": "position", "label": "K线位置（是否创60日新高附近）", "score": round(position_score, 1), "evidence": f"距60日高点 {_pct(closes[-1], high_60):+.1f}%"})
    capital_weights.append(25.0)
    ma_score = 100.0 if (ma_ok and ma_bull) else 70.0 if ma_ok else 30.0
    capital_components.append({"key": "ma", "label": "均线支撑（5/10/20日线）", "score": round(ma_score, 1), "evidence": f"MA5 {ma5:.2f} / MA10 {ma10:.2f} / MA20 {ma20:.2f}"})
    capital_weights.append(20.0)
    open_count = _number(pool.get("open_count")) if pool else None
    if open_count is not None:
        zhaban_score = 100.0 if open_count == 0 else 65.0 if open_count == 1 else 30.0
        capital_components.append({"key": "open_count", "label": "炸板次数（FFD涨停池）", "score": round(zhaban_score, 1), "evidence": f"{int(open_count)} 次"})
        capital_weights.append(30.0)
    first_limit_time = str(pool.get("first_limit_time") or "") if pool else ""
    if first_limit_time:
        hour = int(first_limit_time[:2]) if first_limit_time[:2].isdigit() else 15
        time_score = 100.0 if first_limit_time <= "10:30" else 60.0 if first_limit_time < "14:00" else 30.0
        capital_components.append({"key": "first_limit_time", "label": "首板时间（早盘优于午后）", "score": round(time_score, 1), "evidence": first_limit_time})
        capital_weights.append(15.0)
    capital_missing = ["封单额（FFD 涨停池暂无此字段）"] if pool else ["炸板次数、首板时间（涨停池不可用）", "封单额（无数据源）"]
    capital_score = sum(s * w for s, w in zip([c["score"] for c in capital_components], capital_weights)) / sum(capital_weights)

    # 板块热度 30%：同行业昨日涨停家数
    sector_score = _clamp(40.0 + industry_limit_up_count * 20.0)
    sector_components = [
        {"key": "industry_limit_up", "label": "同行业昨日涨停家数", "score": round(sector_score, 1), "evidence": f"{industry_limit_up_count} 家"},
    ]
    sector_score_final = sector_score

    # 市场情绪 20%：优先用涨停池封板率，缺失时用涨停家数代理
    if market_sealed_ratio is not None:
        sentiment_score = _clamp(market_sealed_ratio * 100.0)
        sentiment_note = f"昨日封板率 {market_sealed_ratio:.0%}（涨停池口径）"
    else:
        sentiment_score = _clamp(market_prev_limit_up * 2.0) if market_prev_limit_up else 40.0
        sentiment_note = f"昨日全市场涨停 {market_prev_limit_up} 家（代理指标）"

    # 消息面 10%：涨停池官方题材 + 关联概念数量
    related_concepts = str(pool.get("related_concepts") or "") if pool else ""
    limit_reason = str(pool.get("limit_reason") or "") if pool else ""
    if limit_reason or related_concepts:
        concept_count = len([c for c in related_concepts.replace("；", "+").replace("+", ";").split(";") if c.strip()])
        news_score = _clamp(60.0 + concept_count * 10.0)
        news_note = f"官方题材：{limit_reason or '未披露'}" + (f"｜关联概念 {concept_count} 个" if concept_count else "")
    else:
        news_score = 50.0
        news_note = "涨停池无题材披露，默认中性"

    first_board_score = (
        capital_score * 0.40
        + sector_score_final * 0.30
        + sentiment_score * 0.20
        + news_score * 0.10
    )

    # ---------- 次日延续（今日竞价） ----------
    if auction_available:
        # 竞价量比 35%：7~12 理想，<5 或 >20 扣分
        if float(cfg["auction_amount_ratio_min"]) <= auction_amount_ratio <= float(cfg["auction_amount_ratio_max"]):
            auction_ratio_score = 100.0
        elif auction_amount_ratio > 20.0:
            auction_ratio_score = 40.0
            flags.append("竞价量比>20%，分歧过大（待回测）")
        elif auction_amount_ratio >= 5.0:
            auction_ratio_score = 75.0
        else:
            auction_ratio_score = _clamp(auction_amount_ratio / max(float(cfg["auction_amount_ratio_min"]), 0.1) * 70.0)
        # 竞价涨幅：高开 1%~7% 视为承接强
        if 1.0 <= auction_change <= 7.0:
            open_score = 100.0
        elif 0.0 < auction_change < 1.0:
            open_score = 60.0
        elif auction_change > 7.0:
            open_score = 55.0
            flags.append("竞价高开过猛（>7%），注意风险")
        else:
            open_score = 20.0
        auction_components = [
            {"key": "auction_amount_ratio", "label": "竞价量比（竞价额/昨日额，理想7%~12%）", "score": round(auction_ratio_score, 1), "evidence": f"{auction_amount_ratio:.2f}%"},
            {"key": "auction_gap", "label": "竞价高开幅度", "score": round(open_score, 1), "evidence": f"{auction_change:+.2f}%"},
            {"key": "auction_burst", "label": "竞价爆量档", "score": {"S": 100.0, "A": 90.0, "B": 70.0, "C": 50.0, "D": 35.0}.get(tier_key, 20.0), "evidence": tier_label},
        ]
        auction_missing: list[str] = []
    else:
        auction_components = []
        auction_missing = ["竞价数据不可用（非竞价时段或来源降级）"]
    # 竞价阶段权重合计（量比35 + 涨幅20 + 爆量20 + 板块联动15 + 市场情绪10）
    auction_stage_score: float | None
    if auction_available:
        sector_today_score = _clamp(50.0 + industry_strong_auction_count * 20.0)
        auction_components.append(
            {"key": "sector_today", "label": "同行业今日竞价强势家数（竞价涨幅>2%）", "score": round(sector_today_score, 1), "evidence": f"{industry_strong_auction_count} 家"}
        )
        market_today_score = _clamp(50.0 + market_auction_breadth * 2.0)
        auction_components.append(
            {"key": "market_today", "label": "今日竞价市场宽度（高开-低开家数差代理）", "score": round(market_today_score, 1), "evidence": f"宽度 {market_auction_breadth:+.0f}"}
        )
        auction_stage_score = sum(
            c["score"] * AUCTION_COMPONENT_WEIGHTS[c["key"]]
            for c in auction_components
        )
    else:
        auction_stage_score = None

    total_score: float | None
    coverage: float
    if auction_stage_score is None:
        # 无竞价数据时只输出首板强势度，总分降级为“昨日分”
        total_score = round(first_board_score, 1)
        coverage = 0.5
        flags.append("竞价数据缺失，当前仅展示首板强势度，不构成当日竞价结论")
    else:
        total_score = round(first_board_score * 0.55 + auction_stage_score * 0.45, 1)
        coverage = 1.0

    if float_mcap > float(cfg["max_float_mcap"]):
        flags.append(f"流通市值 {float_mcap / 1e8:.0f}亿 超过70亿，轿子偏重（待回测）")
    if auction_amount and auction_amount < float(cfg["min_auction_amount"]):
        flags.append("竞价成交额低于3000万门槛")
    if prev_turnover is not None and prev_turnover >= 70:
        flags.append(f"昨日换手{prev_turnover:.0f}%：死亡换手区（70%以上十有八九见顶，仅龙头换庄例外）")
    elif prev_turnover is not None and prev_turnover >= 60:
        flags.append(f"昨日换手{prev_turnover:.0f}%：换手警惕区（>60%）")
    if parking_apron:
        flags.append("停机坪接力：首板次日高开1~5%且量比1.5~5（正向信号）")
    if path_weak_to_strong:
        flags.append(f"竞价过程弱转强：早期约{trace_low:.1f}% → 终态{auction_change:+.1f}%（水下拉起，正向信号）")
    if path_fading:
        flags.append(f"竞价冲高回落：早期约{trace_low:.1f}% → 终态{auction_change:+.1f}%（谨慎）")

    decision = "watch"
    decision_reason = ""
    if auction_available:
        if tier_key in ("S", "A") and auction_amount >= float(cfg["min_auction_amount"]):
            decision = "candidate"
            decision_reason = "竞价爆量达 S/A 档（阈值待回测）"
        elif tier_key == "B" and 1.0 <= auction_change <= 7.0 and auction_stage_score and auction_stage_score >= 60:
            decision = "candidate"
            decision_reason = "B级爆量且竞价高开健康（阈值待回测）"
        else:
            decision_reason = "竞价爆量或高开幅度未达候选条件"

    return {
        "code": code,
        "name": name,
        "industry": str(snapshot.get("industry") or "其他"),
        "trade_date": str(last.get("date") or snapshot.get("as_of") or "")[:10],
        "prev_close": round(prev_close, 2),
        "prev_pct": round(prev_pct, 2),
        "boards": boards,
        "board_label": f"{boards}板",
        "price": round(price, 2),
        "float_mcap": round(float_mcap, 2),
        "prev_amount": round(prev_amount, 2),
        "auction_available": auction_available,
        "auction_source": str(quote.get("auction_source") or quote.get("source") or ""),
        "auction_price": round(auction_price, 2) if auction_price else None,
        "auction_change_pct": round(auction_change, 2) if auction_available else None,
        "auction_amount": round(auction_amount, 2) if auction_amount else None,
        "auction_amount_to_mcap_pct": round(amount_to_mcap, 2),
        "auction_amount_ratio_pct": round(auction_amount_ratio, 2),
        "prev_turnover_pct": round(prev_turnover, 2) if prev_turnover is not None else None,
        "volume_ratio": volume_ratio,
        "parking_apron": parking_apron,
        "trace_low_pct": round(trace_low, 2) if trace_low is not None else None,
        "path_weak_to_strong": path_weak_to_strong,
        "path_fading": path_fading,
        "perf20": compute_perf20(bars, limit_up_pct=float(cfg["limit_up_pct"])),
        "tier": tier_key,
        "tier_label": tier_label,
        "limit_up_probability_pct": probability_pct,
        "limit_up_probability_label": probability_label,
        "volume_multiple": round(volume_multiple, 2),
        "near_high": near_high,
        "ma_ok": ma_ok,
        "ma_bull": ma_bull,
        "first_board_score": round(first_board_score, 1),
        "auction_stage_score": round(auction_stage_score, 1) if auction_stage_score is not None else None,
        "score": total_score,
        "coverage": coverage,
        "breakdown": {
            "first_board": {
                "capital": {"weight": "40%", "score": round(capital_score, 1), "components": capital_components, "missing": capital_missing},
                "sector": {"weight": "30%", "score": round(sector_score_final, 1), "components": sector_components},
                "sentiment": {"weight": "20%", "score": round(sentiment_score, 1), "note": sentiment_note},
                "news": {"weight": "10%", "score": round(news_score, 1), "note": news_note},
            },
            "auction": {
                "weight": "45%",
                "score": round(auction_stage_score, 1) if auction_stage_score is not None else None,
                "components": auction_components,
                "missing": auction_missing,
            },
        },
        "open_count": int(open_count) if open_count is not None else None,
        "first_limit_time": first_limit_time or None,
        "limit_reason": limit_reason or None,
        "related_concepts": related_concepts or None,
        "decision": decision,
        "decision_reason": decision_reason,
        "risk_flags": flags,
        "missing_data": missing,
        "strategy_version": STRATEGY_VERSION,
    }


def screen_yijiner(
    snapshots: Iterable[Mapping[str, Any]],
    auction_quotes: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    limit: int = 50,
    params: Mapping[str, Any] | None = None,
    limit_pool: Mapping[str, Any] | None = None,
    trace_by_code: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """对全市场做一进二筛选：昨日首板 ∩ 今日竞价打分。"""

    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update({k: v for k, v in params.items() if v is not None})
    rows = [dict(item) for item in snapshots if isinstance(item, Mapping)]
    quotes = {
        str(key): dict(value)
        for key, value in (auction_quotes or {}).items()
        if isinstance(value, Mapping)
    }
    main_board = [item for item in rows if is_main_board_stock(item.get("code"), item.get("name"))]
    code_by = {str(item.get("code") or ""): item for item in main_board}

    # 昨日涨停统计（全市场口径 + 行业口径）
    limit_up_codes: list[str] = []
    industry_limit_up: dict[str, int] = {}
    dates: list[str] = []
    for item in rows:
        bars = _bars(item)
        if not bars:
            continue
        closes = _closes(bars)
        if len(closes) >= 2 and closes[-1] > 0 and closes[-2] > 0:
            if _pct(closes[-1], closes[-2]) >= float(cfg["limit_up_pct"]):
                code = str(item.get("code") or "")
                limit_up_codes.append(code)
                industry = str(item.get("industry") or "其他")
                industry_limit_up[industry] = industry_limit_up.get(industry, 0) + 1
            dates.append(str(bars[-1].get("date") or "")[:10])
    trade_date = max(dates, default="")
    pool_data = dict(limit_pool or {})
    pool_by_code = {
        str(k): dict(v)
        for k, v in (pool_data.get("by_code") or {}).items()
        if isinstance(v, Mapping)
    }
    pool_stats = dict(pool_data.get("stats") or {})
    first_board_codes: list[str] = []
    for code in limit_up_codes:
        item = code_by.get(code)
        if not item:
            continue
        pool_row = pool_by_code.get(code)
        if pool_row and pool_row.get("lianban_count") is not None:
            is_first_board = int(_number(pool_row.get("lianban_count"))) == 1
        else:
            is_first_board = board_count(_closes(_bars(item)), limit_up_pct=float(cfg["limit_up_pct"])) == 1
        if is_first_board:
            first_board_codes.append(code)
    market_sealed_ratio = None
    if pool_stats:
        sealed = _number(pool_stats.get("limit_up_count"))
        broken = _number(pool_stats.get("broken_count"))
        if sealed + broken > 0:
            market_sealed_ratio = sealed / (sealed + broken)

    # 今日竞价市场宽度（全部有竞价数据的股票高开-低开差，代理）
    gaps = [_number(q.get("change_pct")) for q in quotes.values() if q.get("change_pct") is not None]
    if not gaps and quotes:
        gaps = [
            _pct(_number(q.get("auction_price")), _number(q.get("last_close")))
            for q in quotes.values()
            if _number(q.get("auction_price")) and _number(q.get("last_close"))
        ]
    breadth = (sum(1 for g in gaps if g > 1.0) - sum(1 for g in gaps if g < -1.0)) if gaps else 0.0

    # 同行业今日竞价强势家数
    industry_strong: dict[str, int] = {}
    for code, quote in quotes.items():
        change = _number(quote.get("change_pct"))
        if not change and _number(quote.get("auction_price")) and _number(quote.get("last_close")):
            change = _pct(_number(quote.get("auction_price")), _number(quote.get("last_close")))
        if change > 2.0:
            industry = str(code_by.get(code, {}).get("industry") or "其他")
            industry_strong[industry] = industry_strong.get(industry, 0) + 1

    candidates: list[dict[str, Any]] = []
    for code in first_board_codes:
        item = code_by[code]
        industry = str(item.get("industry") or "其他")
        candidate = evaluate_candidate(
            item,
            quotes.get(code),
            params=cfg,
            industry_limit_up_count=max(industry_limit_up.get(industry, 1) - 1, 0),
            industry_strong_auction_count=industry_strong.get(industry, 0),
            market_prev_limit_up=len(limit_up_codes),
            market_auction_breadth=float(breadth),
            limit_pool_row=pool_by_code.get(code),
            market_sealed_ratio=market_sealed_ratio,
            trace_low=(trace_by_code or {}).get(code, {}).get("low_pct"),
        )
        if candidate:
            candidates.append(candidate)

    tier_rank = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "": 5}
    candidates.sort(
        key=lambda item: (tier_rank.get(str(item.get("tier")), 9), -_number(item.get("score")), -_number(item.get("prev_amount"))),
    )
    limit = max(1, min(int(limit), 200))
    selected = candidates[:limit]
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank

    tier_counts = {key: sum(1 for item in candidates if item.get("tier") == key) for key in ("S", "A", "B", "C", "D")}
    return {
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "source": "local_tdx_postclose + auction_quotes",
        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605），排除 ST 与退市标的",
        "thresholds": {k: v for k, v in cfg.items()},
        "market_state": {
            "prev_limit_up_count": len(limit_up_codes),
            "prev_first_board_count": len(first_board_codes),
            "auction_breadth": breadth,
            "auction_quotes_count": len(quotes),
            "auction_available": bool(quotes),
            "limit_pool": pool_stats,
            "market_sealed_ratio": market_sealed_ratio,
        },
        "tier_counts": tier_counts,
        "funnel": {
            "input": len(rows),
            "main_board": len(main_board),
            "prev_limit_up": len(limit_up_codes),
            "prev_first_boards": len(first_board_codes),
            "with_auction": sum(1 for item in candidates if item.get("auction_available")),
            "matched": len(candidates),
            "returned": len(selected),
        },
        "rows": selected,
        "methodology": [
            "标的池：昨日沪深主板首板涨停（FFD涨停池连板数=1，收盘涨幅≥9.8%兜底），排除 ST/退市与非主板。",
            "竞价爆量档位：竞价成交额÷昨日成交额；≥10% 很少见（经验涨停概率≥95%）、≥7% 比较少（≥80%）、≥5% 较少见（≥70%）、≥3% 较常见（≥60%）、≥2% 经常见（≥55%）。概率为经验分层，需回测验证。",
            "首板强势度（55%权重）：资金面40%（量能/K线位置/均线/炸板次数/首板时间）+ 板块热度30% + 情绪20%（封板率）+ 消息面10%（官方题材）。",
            "次日延续（45%权重）：竞价量比35%（理想7%~12%）+ 高开幅度20% + 爆量档20% + 板块联动15% + 市场竞价宽度10%。",
            "风险旗标：竞价量比>20%分歧过大、流通市值>70亿轿子重、高开>7%注意回封失败；昨日换手>60%警惕、≥70%死亡换手。",
            "正向信号：停机坪接力（首板次日高开1~5%且量比1.5~5），在建议中以正向旗标标注。",
            "路径因子：交易日09:22对首板池采样竞价快照，早期≤-3%而终态高开1~7%记为'竞价过程弱转强'（正向）；早期≥4%且回落3%以上记为'冲高回落'（谨慎）。",
            "数据源：流通市值与涨停池（炸板/首板时间/题材）来自 FFD；封单额属盘口级数据，FFD 暂无此字段，按缺失处理。",
        ],
        "threshold_status": "全部阈值来自战法总结的经验参数，待回测。",
        "disclaimer": "仅供研究，不构成投资建议。一进二炸板回撤风险高，务必设定止损。",
    }
