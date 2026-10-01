"""Classic A-share strategy templates from the comprehensive stock selection framework.

Each strategy function accepts a snapshot (latest quote) and rows (K-line history)
and returns a dict with 'triggered' (bool), 'signal' (bullish/neutral/bearish),
and 'evidence' (list of conditions that were met/failed).
"""

from __future__ import annotations

from math import isnan
from typing import Any, Iterable, Mapping, Sequence


def _n(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if not isnan(v) and v > 0 else default
    except (TypeError, ValueError):
        return default


def _ma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ---------------------------------------------------------------------------
# Strategy 1: 放量上涨 (Volume Breakout)
# ---------------------------------------------------------------------------

def volume_breakout(snapshot: Mapping[str, Any] | None,
                    rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """放量上涨: day-over-day gain < 2% OR close < open, amount >= 2亿,
    volume >= 2x 5-day average volume."""
    snapshot = snapshot or {}
    rows_list = list(rows or [])
    if len(rows_list) < 6:
        return {"triggered": False, "signal": "neutral", "evidence": ["K线数据不足(需≥6日)"]}

    latest = rows_list[-1]
    previous = rows_list[-2]
    close = _n(snapshot.get("price")) or _n(latest.get("close"))
    prev_close = _n(previous.get("close"))
    day_change = (close / prev_close - 1) * 100 if prev_close else 0
    open_price = _n(snapshot.get("open")) or _n(latest.get("open"))
    amount = _n(snapshot.get("amount")) or _n(latest.get("amount"))

    evidence: list[str] = []
    conditions_met = 0

    # Condition 1: day change < 2% OR close <= open
    if day_change < 2.0 or close <= open_price:
        conditions_met += 1
        evidence.append(f"涨幅{day_change:+.1f}%<2%或收于开盘以下 ✓")
    else:
        evidence.append(f"涨幅{day_change:+.1f}%≥2%且收阳 ✗")

    # Condition 2: amount >= 2亿
    if amount >= 200_000_000:
        conditions_met += 1
        evidence.append(f"成交额{amount/1e8:.1f}亿≥2亿 ✓")
    else:
        evidence.append(f"成交额{amount/1e8:.2f}亿<2亿 ✗")

    # Condition 3: volume >= 2x 5-day average
    vols = [_n(r.get("volume")) for r in rows_list[-6:]]
    avg_vol = sum(vols[:-1]) / 5.0 if vols[:-1] else 0
    today_vol = vols[-1]
    if avg_vol > 0 and today_vol >= avg_vol * 2.0:
        conditions_met += 1
        evidence.append(f"成交量{today_vol/avg_vol:.1f}x5日均量 ✓")
    else:
        evidence.append(f"成交量不足2倍均量 ✗" if avg_vol > 0 else "均量数据不足 ✗")

    triggered = conditions_met == 3
    return {
        "triggered": triggered,
        "signal": "bullish" if triggered else "neutral",
        "conditions_met": conditions_met,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Strategy 2: 均线多头 (MA Bullish Alignment)
# ---------------------------------------------------------------------------

def ma_bullish_alignment(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """均线多头: MA30 rising over the last 30 days, with MA30/MA30_30d_ago > 1.2."""
    rows_list = list(rows or [])
    if len(rows_list) < 60:
        return {"triggered": False, "signal": "neutral", "evidence": ["K线不足60日"]}

    closes = [_n(r.get("close")) for r in rows_list]
    evidence: list[str] = []

    # MA30 at four points: 30, 20, 10, 0 days ago
    ma30_now = _ma(closes, 30)
    ma30_10d = _ma(closes[:-10], 30) if len(closes) > 40 else None
    ma30_20d = _ma(closes[:-20], 30) if len(closes) > 50 else None
    ma30_30d = _ma(closes[:-30], 30) if len(closes) > 60 else None

    if None in (ma30_now, ma30_10d, ma30_20d, ma30_30d):
        return {"triggered": False, "signal": "neutral", "evidence": ["MA30数据不足"]}

    # Condition 1: MA30 rising at each checkpoint
    rising = ma30_30d < ma30_20d < ma30_10d < ma30_now
    if rising:
        evidence.append(f"MA30持续上升 ✓")
    else:
        evidence.append(f"MA30未持续上升 ✗")

    # Condition 2: MA30_now / MA30_30d_ago > 1.2
    ratio = ma30_now / ma30_30d
    if ratio > 1.2:
        evidence.append(f"MA30增长{ratio:.2f}x>1.2 ✓")
    else:
        evidence.append(f"MA30增长{ratio:.2f}x≤1.2 ✗")

    triggered = rising and ratio > 1.2
    return {
        "triggered": triggered,
        "signal": "bullish" if triggered else "neutral",
        "evidence": evidence,
        "ma30_now": round(ma30_now, 2),
        "ratio": round(ratio, 3),
    }


# ---------------------------------------------------------------------------
# Strategy 3: 停机坪 (Parking Apron)
# ---------------------------------------------------------------------------

def parking_apron(snapshot: Mapping[str, Any] | None,
                  rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """停机坪: A limit-up day within last 15 days, followed by 3 days of
    high-open, positive close, within 5% daily range."""
    snapshot = snapshot or {}
    rows_list = list(rows or [])
    if len(rows_list) < 20:
        return {"triggered": False, "signal": "neutral", "evidence": ["K线不足20日"]}

    evidence: list[str] = []
    closes = [_n(r.get("close")) for r in rows_list]
    opens = [_n(r.get("open")) for r in rows_list]
    volumes = [_n(r.get("volume")) for r in rows_list]

    # Find the limit-up day within last 15
    limit_up_idx = None
    for i in range(max(0, len(closes) - 15), len(closes)):
        if i == 0:
            continue
        change = (closes[i] / closes[i - 1] - 1) * 100
        if change >= 9.5:
            # Check volume breakout
            if i >= 5:
                avg_vol = sum(volumes[i - 5:i]) / 5.0
                if volumes[i] >= avg_vol * 1.5:
                    limit_up_idx = i
                    break

    if limit_up_idx is None:
        evidence.append("近15日无放量涨停 ✗")
        return {"triggered": False, "signal": "neutral", "evidence": evidence}

    evidence.append(f"第{-len(closes)+limit_up_idx}日涨停(放量) ✓")

    # Check the 3 days after limit-up
    if limit_up_idx + 3 >= len(closes):
        evidence.append("涨停后不足3个交易日 ✗")
        return {"triggered": False, "signal": "neutral", "evidence": evidence}

    apron_ok = True
    for offset in range(1, 4):
        idx = limit_up_idx + offset
        if idx >= len(closes):
            break
        day_open = opens[idx]
        day_close = closes[idx]
        prev_close = closes[idx - 1]
        if prev_close == 0:
            continue

        gap = (day_open / prev_close - 1) * 100
        change = (day_close / day_open - 1) * 100

        if gap <= 0:
            evidence.append(f"第{offset}日未高开 ✗")
            apron_ok = False
        elif abs(change) > 5.0:
            evidence.append(f"第{offset}日振幅{change:+.1f}%>5% ✗")
            apron_ok = False
        else:
            evidence.append(f"第{offset}日高开{gap:+.1f}%,收{change:+.1f}% ✓")

    return {
        "triggered": apron_ok,
        "signal": "bullish" if apron_ok else "neutral",
        "evidence": evidence,
        "limit_up_date_idx": limit_up_idx,
    }


# ---------------------------------------------------------------------------
# Strategy 4: 突破平台 (Platform Breakout)
# ---------------------------------------------------------------------------

def platform_breakout(snapshot: Mapping[str, Any] | None,
                      rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """突破平台: close >= MA60 > open on some day within 60 days, with volume breakout,
    and prior price stayed within -5%~+20% of MA60."""
    rows_list = list(rows or [])
    if len(rows_list) < 61:
        return {"triggered": False, "signal": "neutral", "evidence": ["K线不足61日"]}

    closes = [_n(r.get("close")) for r in rows_list]
    opens = [_n(r.get("open")) for r in rows_list]
    volumes = [_n(r.get("volume")) for r in rows_list]
    evidence: list[str] = []

    # Find breakout day: close >= MA60 > open
    breakout_idx = None
    for i in range(60, len(closes)):
        ma60 = _ma(closes[:i + 1], 60)
        if ma60 is None:
            continue
        if closes[i] >= ma60 > opens[i]:
            # Check volume condition
            if i >= 5:
                avg_vol = sum(volumes[i - 5:i]) / 5.0
                if volumes[i] >= avg_vol * 1.5:
                    breakout_idx = i
                    break

    if breakout_idx is None:
        evidence.append("近60日无放量突破MA60 ✗")
        return {"triggered": False, "signal": "neutral", "evidence": evidence}

    evidence.append(f"第{-len(closes)+breakout_idx}日突破MA60(放量) ✓")

    # Check prior deviation range
    deviation_ok = True
    for i in range(max(0, breakout_idx - 60), breakout_idx):
        ma60_i = _ma(closes[:i + 1], 60)
        if ma60_i is None or ma60_i == 0:
            continue
        dev = (closes[i] / ma60_i - 1) * 100
        if dev < -5.0:
            evidence.append(f"突破前有日偏离MA60{dev:+.1f}%<-5% ✗")
            deviation_ok = False
            break
        if dev > 20.0:
            evidence.append(f"突破前有日偏离MA60{dev:+.1f}%>20% ✗")
            deviation_ok = False
            break

    if deviation_ok:
        evidence.append("突破前价格在MA60的-5%~+20%区间 ✓")

    return {
        "triggered": deviation_ok,
        "signal": "bullish" if deviation_ok else "neutral",
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Strategy 5: 海龟交易法则 (Turtle Trading)
# ---------------------------------------------------------------------------

def turtle_breakout(rows: Iterable[Mapping[str, Any]] | None, period: int = 60) -> dict[str, Any]:
    """海龟交易法则: latest close is the highest close in the last `period` days."""
    rows_list = list(rows or [])
    if len(rows_list) < period:
        return {"triggered": False, "signal": "neutral", "evidence": [f"K线不足{period}日"]}

    closes = [_n(r.get("close")) for r in rows_list]
    latest = closes[-1]
    highest = max(closes[-period:])

    triggered = latest >= highest
    return {
        "triggered": triggered,
        "signal": "bullish" if triggered else "neutral",
        "evidence": [f"收盘{latest:.2f}{'>=' if triggered else '<'}近{period}日最高{highest:.2f}"],
        "close": latest,
        "period_high": highest,
    }


# ---------------------------------------------------------------------------
# Strategy 6: 回踩年线 (Pullback to 250-day MA)
# ---------------------------------------------------------------------------

def ma250_pullback(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """回踩年线: price broke above MA250, pulled back to MA250 with shrinking volume,
    then recovered. Bullish continuation."""
    rows_list = list(rows or [])
    if len(rows_list) < 310:  # 250 + 60 for context
        return {"triggered": False, "signal": "neutral", "evidence": [f"K线不足310日(需250+60)"]}

    closes = [_n(r.get("close")) for r in rows_list]
    volumes = [_n(r.get("volume")) for r in rows_list]
    evidence: list[str] = []

    # Find the highest close in last 60 days
    recent = closes[-60:]
    high_idx_relative = recent.index(max(recent))
    high_idx = len(closes) - 60 + high_idx_relative

    # Split into before/after the high
    before = closes[:high_idx]
    after = closes[high_idx:]

    if len(before) < 250:
        evidence.append("高点前数据不足250日 ✗")
        return {"triggered": False, "signal": "neutral", "evidence": evidence}

    ma250_before_break = _ma(before, 250)
    if ma250_before_break is None:
        return {"triggered": False, "signal": "neutral", "evidence": ["MA250计算失败"]}

    # Check: price broke above MA250 in the "before" period
    crossed = False
    for i in range(250, len(before)):
        ma250_i = _ma(closes[:i + 1], 250)
        if ma250_i is None:
            continue
        if closes[i - 1] < ma250_i and closes[i] >= ma250_i:
            crossed = True
            break

    if not crossed:
        evidence.append("未检测到向上突破年线 ✗")
        return {"triggered": False, "signal": "neutral", "evidence": evidence}

    evidence.append("检测到向上突破年线 ✓")

    # Check: after period stays above MA250
    after_above = all(
        closes[high_idx + j] > (_ma(closes[:high_idx + j + 1], 250) or 0)
        for j in range(len(after))
        if _ma(closes[:high_idx + j + 1], 250) is not None
    )
    if after_above:
        evidence.append("高点后持续在年线上方 ✓")
    else:
        evidence.append("高点后有跌破年线 ✗")

    # Check: pullback to low within 10-50 days after high, with volume shrink
    low_idx = None
    for j in range(10, min(50, len(after))):
        actual_idx = high_idx + j
        if actual_idx >= len(closes):
            break
        if closes[actual_idx] == min(after):
            low_idx = actual_idx
            break

    if low_idx:
        vol_high = volumes[high_idx]
        vol_low = volumes[low_idx]
        price_ratio = closes[low_idx] / closes[high_idx]
        if vol_high > vol_low * 2:
            evidence.append(f"回踩缩量(高峰量/低谷量={vol_high/vol_low:.1f}) ✓")
        else:
            evidence.append(f"回踩未明显缩量 ✗")
        if price_ratio < 0.8:
            evidence.append(f"回踩幅度{(1-price_ratio)*100:.0f}%>20% ✓")
        else:
            evidence.append(f"回踩幅度不足 ✗")

    triggered = crossed and after_above
    return {
        "triggered": triggered,
        "signal": "bullish" if triggered else "neutral",
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Strategy 7: 无大幅回撤 (No Large Drawdown)
# ---------------------------------------------------------------------------

def no_large_drawdown(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """无大幅回撤: 60-day gain < 60%, no single-day drop > 7%, no 2-day cumulative > 10%."""
    rows_list = list(rows or [])
    if len(rows_list) < 61:
        return {"triggered": False, "signal": "neutral", "evidence": ["K线不足61日"]}

    closes = [_n(r.get("close")) for r in rows_list]
    opens = [_n(r.get("open")) for r in rows_list]
    evidence: list[str] = []

    # Condition 1: 60-day gain < 60%
    gain_60 = (closes[-1] / closes[-61] - 1) * 100
    if gain_60 < 60:
        evidence.append(f"60日涨幅{gain_60:.1f}%<60% ✓")
    else:
        evidence.append(f"60日涨幅{gain_60:.1f}%≥60% ✗")

    # Condition 2: no single day drop > 7%
    max_drop = 0.0
    for i in range(max(1, len(closes) - 60), len(closes)):
        day_drop = (closes[i] / closes[i - 1] - 1) * 100
        if day_drop < max_drop:
            max_drop = day_drop
    if max_drop > -7.0:
        evidence.append(f"无单日跌幅>7%(最大{max_drop:.1f}%) ✓")
    else:
        evidence.append(f"有单日跌幅{max_drop:.1f}%>7% ✗")

    # Condition 3: no high-open-low-close drop > 7%
    hlc_drop_ok = True
    for i in range(max(0, len(closes) - 60), len(closes)):
        if opens[i] > 0 and closes[i] > 0:
            hlc = (closes[i] / opens[i] - 1) * 100
            if hlc < -7.0:
                hlc_drop_ok = False
                break
    evidence.append("无高开低走>7%" if hlc_drop_ok else "有高开低走>7%事件 ✗")

    triggered = gain_60 < 60 and max_drop > -7.0 and hlc_drop_ok
    return {
        "triggered": triggered,
        "signal": "bullish" if triggered else "neutral",
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Unified runner
# ---------------------------------------------------------------------------

STRATEGIES = {
    "volume_breakout": (volume_breakout, "放量上涨"),
    "ma_bullish": (ma_bullish_alignment, "均线多头"),
    "parking_apron": (parking_apron, "停机坪"),
    "platform_breakout": (platform_breakout, "突破平台"),
    "turtle": (turtle_breakout, "海龟交易"),
    "ma250_pullback": (ma250_pullback, "回踩年线"),
    "no_large_drawdown": (no_large_drawdown, "无大幅回撤"),
}


def run_all(snapshot: Mapping[str, Any] | None,
            rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Run all strategy detectors and return aggregated results."""
    results: dict[str, Any] = {}
    triggered_count = 0
    for name, (func, label) in STRATEGIES.items():
        try:
            result = func(snapshot, rows)
            result["label"] = label
            results[name] = result
            if result.get("triggered"):
                triggered_count += 1
        except Exception as exc:
            results[name] = {
                "triggered": False, "signal": "neutral",
                "evidence": [f"计算异常: {exc}"], "label": label,
            }
    return {
        "strategies": results,
        "triggered_count": triggered_count,
        "triggered": [name for name, r in results.items() if r.get("triggered")],
    }
