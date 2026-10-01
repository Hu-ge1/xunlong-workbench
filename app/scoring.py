"""Deterministic indicators and scoring for the Xunlong workbench.

The module deliberately has no framework or third-party dependencies.  Public
functions accept sequences of mapping-like OHLCV rows.  Common English and
Chinese field names are supported; rows with no valid positive close are
ignored.  Inputs are expected in chronological order.

The scores are research signals, not forecasts or investment advice.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, log10, sqrt
import re
from statistics import fmean, pstdev
from typing import Any, Iterable, Mapping, Sequence


# The rulebook is intentionally versioned separately from the application.  A
# run can therefore be compared with the source rules even when the UI changes.
RULEBOOK_VERSION = "stock-selection-rulebook-v1"
STRATEGY_VERSION = "rulebook-v1-2026.07.18"
PIPELINE_VERSION = "candidate-pipeline-v1-2026.08.01"

# The requested universe is the A-share market, with one explicit exception:
# STAR-board securities are never eligible.  Keep this predicate in the
# scoring module as well as the provider so offline/fallback data cannot bypass
# the policy.
STAR_CODE_PREFIXES = ("688", "689")
MAIN_BOARD_CODE_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
A_SHARE_CODE_PREFIXES = (
    "000", "001", "002", "003", "004", "005", "006", "300", "301", "302", "303", "600", "601", "603", "605",
    "430", "440", "830", "831", "832", "833", "834", "835", "836", "837",
    "838", "839", "870", "871", "872", "873", "920",
)


def _normalise_security_code(value: Any) -> str:
    """Return a six-digit code when one is present, otherwise an empty string."""

    text = str(value or "").strip().lower()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return match.group(1) if match else ""


def is_star_security(code: Any, name: Any = "") -> bool:
    """Whether a security belongs to the excluded STAR board."""

    normalised = _normalise_security_code(code)
    label = str(name or "").upper()
    # Code prefixes are authoritative.  Name matching is deliberately narrow:
    # a legitimate non-STAR stock can contain the characters “科创” in its
    # company name (for example, 科创信息).
    return normalised.startswith(STAR_CODE_PREFIXES) or "科创板" in label


def is_a_share_security(code: Any, name: Any = "", *, include_star: bool = False) -> bool:
    """Return true for supported A-share stock codes (not funds/indices)."""

    normalised = _normalise_security_code(code)
    if not normalised or not normalised.startswith(A_SHARE_CODE_PREFIXES):
        return False
    if not include_star and is_star_security(normalised, name):
        return False
    label = str(name or "").upper()
    # The clist endpoint can mix in funds, bonds and index products.  These are
    # not stock candidates even when their code happens to share a prefix.
    if any(token in label for token in ("ETF", "LOF", "基金", "债", "指数", "转债")):
        return False
    return True


def is_main_board_security(code: Any, name: Any = "") -> bool:
    """Return true only for Shanghai/Shenzhen main-board common stocks."""

    normalised = _normalise_security_code(code)
    return bool(
        normalised.startswith(MAIN_BOARD_CODE_PREFIXES)
        and is_a_share_security(normalised, name)
    )


RULEBOOK_MODES: tuple[str, ...] = ("value", "growth", "trend", "event")
DRAGON_MODE = "dragon"
DRAGON_VERSION = "dragon-v1.1-2026.07.23-sixdim"


RULEBOOK_CONFIG: dict[str, Any] = {
    "threshold": 62.0,
    "push_threshold": 72.0,
    "max_push": 3,
    "min_amount": 5_000_000.0,
    "min_turnover": 0.3,
    "market_block_score": -6,
    "mode_weights": {
        "value": {"valuation": 40.0, "quality": 30.0, "growth": 15.0, "style": 10.0, "technical": 5.0},
        "growth": {"growth": 45.0, "expectation": 25.0, "quality": 10.0, "funds": 10.0, "trend": 10.0},
        "trend": {"momentum": 35.0, "indicators": 25.0, "volume": 20.0, "liquidity": 10.0, "volatility": 10.0},
        "event": {"event": 40.0, "expectation": 30.0, "chase_risk": 15.0, "volume": 10.0, "liquidity": 5.0},
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "strategy_version": STRATEGY_VERSION,
    "missing_optional_pass": True,
    "gates": {
        "allowed_code_prefixes": A_SHARE_CODE_PREFIXES,
        "min_price": 2.0,
        "max_price": 150.0,
        "min_market_cap": 1_500_000_000.0,
        "max_market_cap": 200_000_000_000.0,
        "min_gap_pct": -5.5,
        "max_gap_pct": 8.0,
        "min_auction_amount": 5_000_000.0,
        "min_volume_ratio": 0.8,
        "max_volume_ratio": 8.0,
        "min_turnover_pct": 0.5,
        "max_turnover_pct": 30.0,
        "max_amplitude_pct": 18.0,
        "max_kdj_j": 115.0,
        "max_previous_gain_pct": 15.0,
        "max_consecutive_up_days": 4,
        "latest_seal_time": "14:45",
        "max_board_count": 1,
    },
    "auction": {
        "weights": {
            "gap": 15.0,
            "volume_ratio": 12.0,
            "auction_amount": 10.0,
            "turnover": 8.0,
            "amplitude": 8.0,
            "ma_structure": 12.0,
            "kdj": 8.0,
            "macd": 10.0,
            "boll": 8.0,
            "board_sector": 9.0,
        },
        "missing_dimension_score": 0.45,
        "zone_1_min": 72.0,
        "zone_2_min": 60.0,
        "market_block_score": -8,
        "gap_shape": (-5.5, 0.3, 3.5, 8.0),
        "volume_ratio_shape": (0.8, 1.5, 4.0, 8.0),
        "turnover_shape": (0.5, 2.0, 12.0, 30.0),
        "amplitude_shape": (0.0, 2.0, 10.0, 18.0),
        "auction_amount_target": 100_000_000.0,
    },
}


_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "trade_date", "datetime", "time", "日期", "交易日期"),
    "open": ("open", "open_price", "开盘", "开盘价"),
    "high": ("high", "high_price", "最高", "最高价"),
    "low": ("low", "low_price", "最低", "最低价"),
    "close": ("close", "close_price", "latest", "price", "收盘", "收盘价", "最新价"),
    "volume": ("volume", "vol", "成交量"),
    "amount": ("amount", "turnover_amount", "成交额"),
    "turnover": ("turnover", "turnover_rate", "turnover_pct", "换手率"),
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "").replace("%", "")
            if not cleaned or cleaned in {"--", "None", "null", "nan"}:
                return None
            value = cleaned
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _field(row: Mapping[str, Any], canonical: str) -> Any:
    return _first(row, *_ALIASES[canonical])


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None or not isfinite(value) else round(value, digits)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    result = deepcopy(dict(base))
    if not override:
        return result
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _normalize_rows(rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows or []):
        if not isinstance(raw, Mapping):
            continue
        close = _number(_field(raw, "close"))
        if close is None or close <= 0:
            continue
        open_price = _number(_field(raw, "open")) or close
        high = _number(_field(raw, "high")) or max(open_price, close)
        low = _number(_field(raw, "low")) or min(open_price, close)
        high = max(high, open_price, close, low)
        low = min(low, open_price, close, high)
        normalized.append(
            {
                "date": str(_field(raw, "date") or index),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": max(0.0, _number(_field(raw, "volume")) or 0.0),
                "amount": max(0.0, _number(_field(raw, "amount")) or 0.0),
                "turnover": _number(_field(raw, "turnover")),
                "raw": raw,
            }
        )
    return normalized


def _sma_series(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        return result
    window_sum = 0.0
    for index, value in enumerate(values):
        window_sum += value
        if index >= period:
            window_sum -= values[index - period]
        if index >= period - 1:
            result[index] = window_sum / period
    return result


def _ema_series(values: Sequence[float], period: int) -> list[float | None]:
    if not values or period <= 0:
        return [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    ema = float(values[0])
    result: list[float | None] = [ema]
    for value in values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
        result.append(ema)
    return result


def _rsi_series(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period or period <= 0:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = fmean(gains[:period])
    avg_loss = fmean(losses[:period])
    result[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        result[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _kdj_series(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 9
) -> tuple[list[float], list[float], list[float]]:
    k_value = 50.0
    d_value = 50.0
    k_series: list[float] = []
    d_series: list[float] = []
    j_series: list[float] = []
    for index, close in enumerate(closes):
        start = max(0, index - period + 1)
        highest = max(highs[start : index + 1])
        lowest = min(lows[start : index + 1])
        rsv = 50.0 if highest == lowest else (close - lowest) / (highest - lowest) * 100.0
        k_value = 2.0 / 3.0 * k_value + 1.0 / 3.0 * rsv
        d_value = 2.0 / 3.0 * d_value + 1.0 / 3.0 * k_value
        k_series.append(k_value)
        d_series.append(d_value)
        j_series.append(3.0 * k_value - 2.0 * d_value)
    return k_series, d_series, j_series


def _boll_series(values: Sequence[float], period: int = 20, width: float = 2.0) -> dict[str, list[float | None]]:
    middle = _sma_series(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    bandwidth: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        mean = middle[index]
        deviation = pstdev(values[index - period + 1 : index + 1])
        assert mean is not None
        upper[index] = mean + width * deviation
        lower[index] = mean - width * deviation
        bandwidth[index] = 0.0 if mean == 0 else (upper[index] - lower[index]) / mean * 100.0
    return {"middle": middle, "upper": upper, "lower": lower, "bandwidth": bandwidth}


def _atr_series(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if not closes:
        return result
    true_ranges = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    if len(true_ranges) < period:
        return result
    atr = fmean(true_ranges[:period])
    result[period - 1] = atr
    for index in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[index]) / period
        result[index] = atr
    return result


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True)
class _IndicatorBundle:
    rows: list[dict[str, Any]]
    closes: list[float]
    highs: list[float]
    lows: list[float]
    volumes: list[float]
    ma: dict[int, list[float | None]]
    ema12: list[float | None]
    ema26: list[float | None]
    dif: list[float | None]
    dea: list[float | None]
    macd_hist: list[float | None]
    rsi6: list[float | None]
    rsi14: list[float | None]
    k: list[float]
    d: list[float]
    j: list[float]
    boll: dict[str, list[float | None]]
    atr14: list[float | None]
    volume_ratio: dict[int, list[float | None]]


def _indicator_bundle(rows: Iterable[Mapping[str, Any]] | None) -> _IndicatorBundle:
    clean = _normalize_rows(rows)
    closes = [row["close"] for row in clean]
    highs = [row["high"] for row in clean]
    lows = [row["low"] for row in clean]
    volumes = [row["volume"] for row in clean]
    ma = {period: _sma_series(closes, period) for period in (5, 10, 20, 60)}
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif = [
        None if fast is None or slow is None else fast - slow
        for fast, slow in zip(ema12, ema26)
    ]
    dif_values = [value if value is not None else 0.0 for value in dif]
    dea = _ema_series(dif_values, 9)
    macd_hist = [
        None if value is None or signal is None else 2.0 * (value - signal)
        for value, signal in zip(dif, dea)
    ]
    k, d, j = _kdj_series(highs, lows, closes) if closes else ([], [], [])
    volume_ratio: dict[int, list[float | None]] = {}
    for period in (5, 10, 20):
        series: list[float | None] = [None] * len(volumes)
        for index, volume in enumerate(volumes):
            start = max(0, index - period)
            prior = [item for item in volumes[start:index] if item > 0]
            if prior:
                series[index] = _safe_ratio(volume, fmean(prior))
        volume_ratio[period] = series
    return _IndicatorBundle(
        rows=clean,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        ma=ma,
        ema12=ema12,
        ema26=ema26,
        dif=dif,
        dea=dea,
        macd_hist=macd_hist,
        rsi6=_rsi_series(closes, 6),
        rsi14=_rsi_series(closes, 14),
        k=k,
        d=d,
        j=j,
        boll=_boll_series(closes),
        atr14=_atr_series(highs, lows, closes),
        volume_ratio=volume_ratio,
    )


def standard_indicators(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Return full-history standard indicator series and a compact latest view."""

    bundle = _indicator_bundle(rows)
    if not bundle.rows:
        return {"count": 0, "latest": {}, "series": {}}
    index = len(bundle.rows) - 1
    series = {
        "ma5": bundle.ma[5],
        "ma10": bundle.ma[10],
        "ma20": bundle.ma[20],
        "ma60": bundle.ma[60],
        "ema12": bundle.ema12,
        "ema26": bundle.ema26,
        "macd_dif": bundle.dif,
        "macd_dea": bundle.dea,
        "macd_hist": bundle.macd_hist,
        "rsi6": bundle.rsi6,
        "rsi14": bundle.rsi14,
        "kdj_k": bundle.k,
        "kdj_d": bundle.d,
        "kdj_j": bundle.j,
        "boll_mid": bundle.boll["middle"],
        "boll_upper": bundle.boll["upper"],
        "boll_lower": bundle.boll["lower"],
        "boll_bandwidth": bundle.boll["bandwidth"],
        "atr14": bundle.atr14,
        "volume_ratio_5": bundle.volume_ratio[5],
        "volume_ratio_10": bundle.volume_ratio[10],
        "volume_ratio_20": bundle.volume_ratio[20],
    }
    latest = {name: _round(values[index]) for name, values in series.items()}
    latest["date"] = bundle.rows[index]["date"]
    latest["close"] = bundle.rows[index]["close"]
    return {"count": len(bundle.rows), "latest": latest, "series": series}


def _technical_from_bundle(bundle: _IndicatorBundle) -> dict[str, Any]:
    if not bundle.rows:
        return {
            "strategy_version": STRATEGY_VERSION,
            "total": 0,
            "label": "数据不足",
            "components": {"ma5": 0, "kdj": 0, "macd": 0, "ma10": 0},
            "evidence": ["没有可用的 OHLC 收盘数据"],
            "indicators": {},
            "data_points": 0,
        }

    index = len(bundle.rows) - 1
    close = bundle.closes[index]
    evidence: list[str] = []

    def ma_component(period: int) -> int:
        current = bundle.ma[period][index]
        if current is None:
            evidence.append(f"MA{period}: 需要至少 {period} 条数据")
            return 0
        previous = bundle.ma[period][index - 1] if index > 0 else None
        distance = (close / current - 1.0) * 100.0 if current else 0.0
        score = 2 if distance > 0.2 else -2 if distance < -0.2 else 0
        if previous is not None:
            score += 1 if current > previous else -1 if current < previous else 0
        score = int(_clamp(score, -3, 3))
        direction = "上方" if close >= current else "下方"
        slope = "上行" if previous is not None and current > previous else "下行" if previous is not None and current < previous else "走平"
        evidence.append(f"MA{period}: 收盘位于均线{direction}，均线{slope}，得分 {score:+d}")
        return score

    ma5_score = ma_component(5)
    ma10_score = ma_component(10)

    kdj_score = 0
    if len(bundle.rows) >= 9 and bundle.j:
        k_value, d_value, j_value = bundle.k[index], bundle.d[index], bundle.j[index]
        previous_j = bundle.j[index - 1] if index > 0 else j_value
        kdj_score += 1 if k_value >= d_value else -1
        kdj_score += 1 if j_value >= 50.0 else -1
        kdj_score += 1 if j_value > previous_j else -1 if j_value < previous_j else 0
        if j_value > 110.0:
            kdj_score = min(kdj_score, 1)
        elif j_value < -10.0:
            kdj_score = max(kdj_score, -1)
        kdj_score = int(_clamp(kdj_score, -3, 3))
        evidence.append(
            f"KDJ: K={k_value:.1f}, D={d_value:.1f}, J={j_value:.1f}，得分 {kdj_score:+d}"
        )
    else:
        evidence.append("KDJ: 需要至少 9 条数据")

    macd_score = 0
    dif = bundle.dif[index]
    dea = bundle.dea[index]
    hist = bundle.macd_hist[index]
    previous_hist = bundle.macd_hist[index - 1] if index > 0 else None
    if len(bundle.rows) >= 26 and dif is not None and dea is not None and hist is not None:
        macd_score += 1 if dif >= dea else -1
        macd_score += 1 if dif >= 0 else -1
        if previous_hist is not None:
            macd_score += 1 if hist > previous_hist else -1 if hist < previous_hist else 0
        macd_score = int(_clamp(macd_score, -3, 3))
        evidence.append(
            f"MACD: DIF={dif:.4f}, DEA={dea:.4f}, 柱={hist:.4f}，得分 {macd_score:+d}"
        )
    else:
        evidence.append("MACD: 需要至少 26 条数据")

    components = {"ma5": ma5_score, "kdj": kdj_score, "macd": macd_score, "ma10": ma10_score}
    total = sum(components.values())
    if total >= 8:
        label = "强多"
    elif total >= 3:
        label = "偏多"
    elif total <= -8:
        label = "强空"
    elif total <= -3:
        label = "偏空"
    else:
        label = "中性"
    indicators = {
        "close": _round(close),
        "ma5": _round(bundle.ma[5][index]),
        "ma10": _round(bundle.ma[10][index]),
        "ma20": _round(bundle.ma[20][index]),
        "ma60": _round(bundle.ma[60][index]),
        "kdj": {"k": _round(bundle.k[index]), "d": _round(bundle.d[index]), "j": _round(bundle.j[index])},
        "macd": {"dif": _round(dif), "dea": _round(dea), "hist": _round(hist)},
        "rsi6": _round(bundle.rsi6[index]),
        "rsi14": _round(bundle.rsi14[index]),
        "boll": {
            "upper": _round(bundle.boll["upper"][index]),
            "middle": _round(bundle.boll["middle"][index]),
            "lower": _round(bundle.boll["lower"][index]),
        },
        "atr14": _round(bundle.atr14[index]),
        "volume_ratios": {
            "vr5": _round(bundle.volume_ratio[5][index]),
            "vr10": _round(bundle.volume_ratio[10][index]),
            "vr20": _round(bundle.volume_ratio[20][index]),
        },
    }
    return {
        "strategy_version": STRATEGY_VERSION,
        "total": total,
        "label": label,
        "components": components,
        "evidence": evidence,
        "indicators": indicators,
        "data_points": len(bundle.rows),
    }


def technical_score(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Return the MA5/KDJ/MACD/MA10 four-dimensional score (-12..12)."""

    return _technical_from_bundle(_indicator_bundle(rows))


def trend_score_series(rows: Iterable[Mapping[str, Any]] | None, n: int = 10) -> list[int]:
    """Return the latest *n* rolling four-dimensional technical scores."""

    clean = _normalize_rows(rows)
    if n <= 0 or not clean:
        return []
    start = max(0, len(clean) - n)
    scores: list[int] = []
    for index in range(start, len(clean)):
        prefix = [row["raw"] for row in clean[: index + 1]]
        scores.append(int(technical_score(prefix)["total"]))
    return scores


def market_score(index_rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Classify an index into a -10..10 market regime and 0.90..1.10 coefficient."""

    bundle = _indicator_bundle(index_rows)
    if not bundle.rows:
        return {
            "score": 0,
            "label": "中性",
            "coefficient": 1.0,
            "evidence": ["指数数据不足，使用中性系数"],
            "data_points": 0,
        }
    index = len(bundle.rows) - 1
    close = bundle.closes[index]
    score = 0
    evidence: list[str] = []
    for period, points in ((5, 2), (10, 2), (20, 1)):
        average = bundle.ma[period][index]
        if average is not None:
            contribution = points if close >= average else -points
            score += contribution
            evidence.append(f"指数收盘{'站上' if contribution > 0 else '跌破'} MA{period}: {contribution:+d}")
    ma5 = bundle.ma[5][index]
    ma10 = bundle.ma[10][index]
    if ma5 is not None and ma10 is not None:
        contribution = 1 if ma5 >= ma10 else -1
        score += contribution
        evidence.append(f"MA5 {'高于' if contribution > 0 else '低于'} MA10: {contribution:+d}")
    hist = bundle.macd_hist[index]
    previous_hist = bundle.macd_hist[index - 1] if index > 0 else None
    if len(bundle.rows) >= 26 and hist is not None:
        contribution = 1 if hist >= 0 else -1
        if previous_hist is not None:
            contribution += 1 if hist > previous_hist else -1 if hist < previous_hist else 0
        score += contribution
        evidence.append(f"MACD 柱方向及动量: {contribution:+d}")
    if index > 0 and bundle.closes[index - 1] > 0:
        day_change = (close / bundle.closes[index - 1] - 1.0) * 100.0
        contribution = 1 if day_change >= 0.5 else -1 if day_change <= -0.5 else 0
        score += contribution
        evidence.append(f"指数单日涨跌 {day_change:+.2f}%: {contribution:+d}")
    rsi14 = bundle.rsi14[index]
    if rsi14 is not None:
        contribution = 1 if rsi14 >= 55 else -1 if rsi14 <= 45 else 0
        score += contribution
        evidence.append(f"RSI14={rsi14:.1f}: {contribution:+d}")
    score = int(_clamp(score, -10, 10))
    label = "强多" if score >= 6 else "偏多" if score >= 2 else "强空" if score <= -6 else "偏空" if score <= -2 else "中性"
    return {
        "score": score,
        "label": label,
        "coefficient": round(_clamp(1.0 + score / 100.0, 0.90, 1.10), 3),
        "evidence": evidence,
        "data_points": len(bundle.rows),
        "date": bundle.rows[index]["date"],
    }


def _rulebook_metric(snapshot: Mapping[str, Any], *names: str) -> float | None:
    """Read a numeric rulebook field from common flat or nested containers."""

    value = _number(_first(snapshot, *names))
    if value is not None:
        return value
    for container_name in ("financial", "fundamentals", "prediction", "event", "factors"):
        container = snapshot.get(container_name)
        if isinstance(container, Mapping):
            value = _number(_first(container, *names))
            if value is not None:
                return value
    return None


def _rulebook_flag(snapshot: Mapping[str, Any], *names: str) -> bool | None:
    value = _first(snapshot, *names)
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"", "none", "null", "unknown", "未知"}:
            return None
        if lowered in {"0", "false", "no", "n", "否", "正常", "交易"}:
            return False
        if lowered in {"1", "true", "yes", "y", "是", "停牌", "退市"}:
            return True
    return bool(value)


def evaluate_rulebook_risk(
    snapshot: Mapping[str, Any] | None,
    rows: Iterable[Mapping[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the rulebook's hard veto layer before any ranking score."""

    snapshot = snapshot or {}
    cfg = _deep_merge(RULEBOOK_CONFIG, config)
    bundle = _indicator_bundle(rows)
    code = _normalise_security_code(_first(snapshot, "code", "symbol", "stock_code", "代码"))
    name = str(_first(snapshot, "name", "stock_name", "名称") or "")
    upper_name = name.upper().replace(" ", "")
    hard_flags: list[str] = []
    soft_flags: list[str] = []

    if is_star_security(code, name):
        hard_flags.append("科创板已按系统规则强制排除")
    elif not is_a_share_security(code, name):
        hard_flags.append("不属于支持的A股股票范围")
    if "ST" in upper_name or "退" in name:
        hard_flags.append("ST、退市整理或退市标的")
    if _rulebook_flag(snapshot, "is_suspended", "suspended", "停牌"):
        hard_flags.append("停牌或不可交易")
    # User/news providers may supply these explicit risk flags.  They are
    # deliberately hard vetoes: a favorable technical score must never average
    # away a material disclosure, regulatory, delisting or liquidity risk.
    explicit_risk_fields = (
        ("major_risk", "重大风险"),
        ("regulatory_risk", "监管风险"),
        ("earnings_warning", "业绩风险"),
        ("delisting_risk", "退市风险"),
        ("unlock_risk", "解禁风险"),
        ("liquidity_risk", "流动性风险"),
        ("user_risk", "用户标记风险"),
    )
    for field, label in explicit_risk_fields:
        if _rulebook_flag(snapshot, field, label):
            hard_flags.append(f"{label}，硬性否决")
    trade_status = str(_first(snapshot, "trade_status", "status", "交易状态") or "").strip()
    if trade_status and any(token in trade_status for token in ("停牌", "退市", "不可交易")):
        hard_flags.append(f"交易状态异常：{trade_status}")

    latest_close = bundle.closes[-1] if bundle.closes else None
    price = _rulebook_metric(snapshot, "price", "latest", "close", "最新价") or latest_close
    if price is None or price <= 0:
        if snapshot.get("catalogue_source"):
            soft_flags.append("静态代码目录无实时价格，等待K线确认")
        else:
            hard_flags.append("缺少有效价格数据")

    amount = _rulebook_metric(snapshot, "amount", "turnover_amount", "成交额")
    if amount is None and bundle.rows:
        amount = bundle.rows[-1].get("amount") or None
    turnover = _rulebook_metric(snapshot, "turnover_pct", "turnover", "turnover_rate", "换手率")
    if amount is not None and 0 < amount < float(cfg["min_amount"]):
        hard_flags.append("流动性不足：成交额低于候选下限")
    elif amount in (None, 0):
        soft_flags.append("成交额缺失，流动性待核验")
    if turnover is not None and 0 <= turnover < float(cfg["min_turnover"]):
        hard_flags.append("流动性不足：换手率过低")
    elif turnover is None:
        soft_flags.append("换手率缺失，流动性待核验")

    pe = _rulebook_metric(snapshot, "pe_ttm", "pe", "市盈率")
    pb = _rulebook_metric(snapshot, "pb", "市净率")
    if pe == 0:
        pe = None
    if pb == 0:
        pb = None
    if pe is not None and pe <= 0:
        soft_flags.append("PE为负值或无经济意义，不计入低估值加分")
    if pb is not None and pb <= 0:
        soft_flags.append("PB为负值或无经济意义，不计入低估值加分")

    debt = _rulebook_metric(snapshot, "debt_ratio", "asset_liability_ratio", "资产负债率")
    pledge = _rulebook_metric(snapshot, "pledge_ratio", "股权质押率")
    cashflow_ratio = _rulebook_metric(snapshot, "cashflow_profit_ratio", "ocf_net_profit", "经营现金流净利比")
    goodwill_ratio = _rulebook_metric(snapshot, "goodwill_ratio", "商誉占比")
    unlock_ratio = _rulebook_metric(snapshot, "unlock_ratio", "解禁比例")
    industry = str(_first(snapshot, "industry", "sector", "行业") or "")
    special_industry = any(token in industry for token in ("银行", "地产", "房地产", "保险"))
    if debt is not None and debt >= 60 and not special_industry:
        soft_flags.append("资产负债率高于候选参考值60%")
    if pledge is not None and pledge >= 10:
        soft_flags.append("股权质押率高于候选参考值10%")
    if cashflow_ratio is not None and cashflow_ratio < 1:
        soft_flags.append("经营现金流/净利润低于候选参考值1")
    if goodwill_ratio is not None and goodwill_ratio >= 30:
        soft_flags.append("商誉占比较高")
    if unlock_ratio is not None and unlock_ratio >= 10:
        soft_flags.append("近期解禁压力较高")

    return {
        "eligible": not hard_flags,
        "hard_veto": bool(hard_flags),
        "hard_flags": list(dict.fromkeys(hard_flags)),
        "soft_flags": list(dict.fromkeys(soft_flags)),
        "checked": {
            "code": code,
            "price": _round(price),
            "amount": amount,
            "turnover_pct": turnover,
            "pe_ttm": pe,
            "pb": pb,
            "debt_ratio": debt,
            "pledge_ratio": pledge,
            "cashflow_profit_ratio": cashflow_ratio,
            "goodwill_ratio": goodwill_ratio,
            "unlock_ratio": unlock_ratio,
        },
    }


def board_rotation_score(
    boards: Iterable[Mapping[str, Any]] | None,
    *,
    lookback_days: int = 10,
) -> dict[str, Any]:
    """Rank board rotation using current breadth plus optional ten-day history.

    Providers may expose ``history``/``returns_10d`` on each board.  When the
    history is unavailable the result remains explicit instead of pretending
    that a live one-day ranking is a ten-day trend.
    """

    rows = [dict(item) for item in (boards or []) if isinstance(item, Mapping)]
    ranked: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or row.get("board") or row.get("industry") or "").strip()
        if not name:
            continue
        change = _number(row.get("change_pct", row.get("avg_change_pct")))
        velocity = _number(row.get("velocity_pct"))
        inflow = _number(row.get("main_net_inflow", row.get("main_net")))
        up = _number(row.get("up_count"))
        down = _number(row.get("down_count"))
        breadth = None if up is None or down is None or up + down <= 0 else up / (up + down)
        history = row.get("history")
        returns_10d = _number(row.get("returns_10d", row.get("change_10d_pct")))
        if returns_10d is None and isinstance(history, Sequence) and history:
            points = [_number(item.get("change_pct") if isinstance(item, Mapping) else item) for item in history[-lookback_days:]]
            points = [value for value in points if value is not None]
            returns_10d = sum(points) if points else None
        live_strength = 0.0
        if change is not None:
            live_strength += _clamp((change + 5.0) / 15.0) * 45.0
        if velocity is not None:
            live_strength += _clamp((velocity + 2.0) / 6.0) * 15.0
        if breadth is not None:
            live_strength += _clamp(breadth) * 25.0
        if inflow is not None:
            live_strength += (50.0 + 50.0 * inflow / (abs(inflow) + 1_000_000_000.0)) * 0.15
        history_strength = None if returns_10d is None else _clamp((returns_10d + 15.0) / 45.0) * 100.0
        strength = live_strength if history_strength is None else live_strength * 0.55 + history_strength * 0.45
        state = "加速" if strength >= 72 else "强势轮动" if strength >= 58 else "观察" if strength >= 42 else "退潮"
        defensive_terms = ("\u94f6\u884c", "\u4fdd\u9669", "\u516c\u7528\u4e8b\u4e1a", "\u71c3\u6c14", "\u7535\u529b", "\u9ad8\u80a1\u606f", "\u7164\u70ad")
        offensive_terms = ("\u4eba\u5de5\u667a\u80fd", "AI", "\u82af\u7247", "\u534a\u5bfc\u4f53", "\u96c6\u6210\u7535\u8def", "\u8f6f\u4ef6", "\u4e91", "\u901a\u4fe1", "\u7535\u5b50", "\u673a\u5668\u4eba", "\u81ea\u52a8\u5316", "\u7b97\u529b", "\u4e92\u8054\u7f51", "\u4f4e\u7a7a")
        is_defensive = any(term in name for term in defensive_terms)
        is_offensive = any(term.lower() in name.lower() for term in offensive_terms)
        if returns_10d is not None and returns_10d >= 15 and change is not None and change <= -1:
            stage = "\u9ad8\u4f4d\u5206\u6b67"
        elif change is not None and change >= 3 and (returns_10d is None or returns_10d < 5):
            stage = "\u4e8b\u4ef6\u53d1\u9175"
        elif change is not None and change >= 2 and (returns_10d is None or returns_10d >= 5):
            stage = "\u4e3b\u7ebf\u52a0\u901f"
        elif returns_10d is not None and returns_10d >= 5 and change is not None and -2 <= change < 1:
            stage = "\u5f3a\u52bf\u56de\u8e29"
        elif is_defensive and strength >= 50:
            stage = "\u9632\u5fa1\u627f\u63a5"
        else:
            stage = "\u8f6e\u52a8\u89c2\u5bdf"
        ranked.append({
            "name": name,
            "rank": 0,
            "strength": round(strength, 1),
            "state": state,
            "change_pct": _round(change),
            "returns_10d_pct": _round(returns_10d),
            "breadth_ratio": _round(breadth),
            "history_available": returns_10d is not None,
            "lookback_days": lookback_days,
            "stage": stage,
            "style": "defensive" if is_defensive else "offensive" if is_offensive else "neutral",
        })
    ranked.sort(key=lambda item: (item["strength"], item["change_pct"] or -999), reverse=True)
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
    return {
        "version": PIPELINE_VERSION,
        "lookback_days": lookback_days,
        "available": bool(ranked),
        "history_available": any(item["history_available"] for item in ranked),
        "boards": ranked,
        "top": ranked[:10],
        "note": "近10日历史缺失时仅使用当前板块强弱，不能视为完整轮动结论。" if ranked and not any(item["history_available"] for item in ranked) else "板块状态由近10日变化、当日涨跌、宽度和资金代理合成。",
    }


def confirm_existing_candidate(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    rows: Iterable[Mapping[str, Any]] | None,
    market_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify an already selected candidate at the next morning auction.

    This function never creates a new candidate.  It only combines the stored
    first-round score, technical evidence and available auction fields, with
    hard risk vetoes applied first.
    """

    previous = dict(previous or {})
    current = dict(current or {})
    snapshot = {**previous, **current}
    technical = technical_score(rows)
    technical_norm = _clamp((float(technical.get("total", 0)) + 12.0) / 24.0) * 100.0
    # Ordinary ``price``/``open`` fields are not proof of an opening auction.
    # The previous implementation treated any quote snapshot as auction data,
    # which let a cached 16:14 post-close row pass the morning confirmation.
    auction_fields = any(
        current.get(key) not in (None, "", 0)
        for key in (
            "auction_price", "auction_amount", "auction_volume_lots",
            "auction_data_status", "auction_source", "indicative_price",
        )
    )
    auction_source = str(current.get("auction_source") or current.get("source") or "")
    current_trade_date = str(current.get("trade_date") or "")[:10]
    today = datetime.now().strftime("%Y-%m-%d")
    auction_available = bool(
        auction_fields
        and current.get("available", True)
        and not current.get("stale")
        and current_trade_date == today
        and auction_source
        and str(current.get("auction_data_status") or "").lower()
        not in {"complete_no_event", "no_event", "终态无成交"}
    )
    gates = evaluate_auction_gates(snapshot, rows) if auction_available else []
    auction = auction_score(snapshot, rows, market_context or {"score": 0}) if auction_available else None
    prior_score = _number(previous.get("score")) or 0.0
    auction_value = _number((auction or {}).get("score"))
    if auction_value is None:
        auction_value = 50.0
    blended = round(prior_score * 0.55 + technical_norm * 0.20 + auction_value * 0.25, 1)
    risk = evaluate_rulebook_risk(snapshot, rows)
    failed = [gate["name"] for gate in gates if not gate.get("pass")]
    # A recommendation cannot be confirmed from a negative/underwater opening
    # or from a quote whose date/source cannot be verified.  This is a hard
    # direction and freshness gate, not a soft score penalty.
    auction_price = _number(current.get("auction_price"))
    previous_close = _number(current.get("last_close") or current.get("prev_close") or previous.get("last_close"))
    gap_pct = (
        (auction_price / previous_close - 1.0) * 100.0
        if auction_price is not None and previous_close and previous_close > 0
        else None
    )
    direction_failed = []
    if auction_available and gap_pct is None:
        direction_failed.append("竞价缺少昨收，无法验证方向")
    elif auction_available and gap_pct is not None and gap_pct < 0:
        direction_failed.append(f"竞价低于昨收{gap_pct:.2f}%")
    live_change = _number(current.get("change_pct"))
    if auction_available and live_change is not None and live_change < -0.5:
        direction_failed.append(f"当日涨跌{live_change:+.2f}%偏弱")
    if direction_failed:
        failed.extend(direction_failed)
    market_score = _number((market_context or {}).get("score")) or 0.0
    market_blocked = market_score <= -6 or "退潮" in str((market_context or {}).get("emotion_phase", ""))
    technical_ok = bool(technical.get("data_points", 0) >= 20 and technical.get("total", 0) >= 0)
    confirmed = bool(
        auction is not None
        and risk.get("eligible")
        and not failed
        and technical_ok
        and not market_blocked
        and blended >= 72
    )
    if not risk.get("eligible"):
        reason = "重大风险硬性否决"
    elif not auction_available:
        reason = "当日竞价数据未验证（来源/日期/终态缺失），仅保留观察"
    elif failed:
        reason = f"竞价确认未通过：{', '.join(failed[:3])}"
    elif market_blocked:
        reason = "市场情绪退潮或弱势，停止新增推送"
    elif not technical_ok:
        reason = "技术面未形成均线、MACD、KDJ等一致确认"
    elif confirmed:
        reason = "前一轮候选通过技术面与竞价增量复核"
    else:
        reason = "复核分数未达到推送线，继续观察"
    return {
        "pipeline_version": PIPELINE_VERSION,
        "confirmed": confirmed,
        "decision": "push" if confirmed else "watch",
        "reason": reason,
        "score": blended,
        "prior_score": prior_score,
        "technical_score": technical_norm,
        "auction_score": auction_value if auction is not None else None,
        "technical": technical,
        "auction": auction,
        "gates": gates,
        "risk": risk,
        "market_blocked": market_blocked,
        "data_available": auction_available,
        "auction_source": auction_source or None,
        "auction_trade_date": current_trade_date or None,
        "gap_pct": round(gap_pct, 3) if gap_pct is not None else None,
        "direction_failed": direction_failed,
    }


def _factor(score: float | None, value: Any, evidence: str) -> dict[str, Any]:
    return {
        "score": None if score is None else round(_clamp(float(score), 0.0, 100.0), 2),
        "value": value,
        "available": score is not None,
        "evidence": evidence,
    }


def _band_score(value: float | None, bands: Sequence[tuple[float, float]]) -> float | None:
    if value is None:
        return None
    for upper, score in bands:
        if value <= upper:
            return score
    return bands[-1][1] if bands else None


def _positive_threshold_score(value: float | None, low: float, target: float, high: float) -> float | None:
    if value is None:
        return None
    if value <= low:
        return max(0.0, 35.0 * (value - low + max(abs(low), 1.0)) / max(abs(low), 1.0))
    if value >= high:
        return 100.0
    if value <= target:
        return 35.0 + 35.0 * (value - low) / max(target - low, 1e-12)
    return 70.0 + 30.0 * (value - target) / max(high - target, 1e-12)


def _weighted_mode(
    mode: str,
    weights: Mapping[str, float],
    factors: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    total_weight = sum(max(0.0, float(value)) for value in weights.values()) or 1.0
    used_weight = 0.0
    weighted_score = 0.0
    detail: dict[str, Any] = {}
    for factor_name, weight_value in weights.items():
        weight = max(0.0, float(weight_value))
        item = dict(factors.get(factor_name) or _factor(None, None, "数据待补充"))
        item["weight"] = weight
        detail[factor_name] = item
        if item.get("available") and item.get("score") is not None:
            used_weight += weight
            weighted_score += float(item["score"]) * weight
    raw = weighted_score / used_weight if used_weight else 50.0
    coverage = used_weight / total_weight
    # Missing factors are excluded from the denominator, then the result is
    # shrunk towards neutral.  This prevents a single favourable quote field
    # from masquerading as a fully evidenced stock thesis.
    adjusted = 50.0 + (raw - 50.0) * coverage
    return {
        "mode": mode,
        "score": round(_clamp(adjusted, 0.0, 100.0), 1),
        "raw_score": round(_clamp(raw, 0.0, 100.0), 1),
        "coverage": round(coverage, 3),
        "factors": detail,
    }


def _rulebook_series_metrics(rows: Iterable[Mapping[str, Any]] | None) -> tuple[_IndicatorBundle, dict[str, Any]]:
    bundle = _indicator_bundle(rows)
    if not bundle.rows:
        return bundle, {}
    index = len(bundle.rows) - 1
    close = bundle.closes[index]

    def period_return(days: int) -> float | None:
        if len(bundle.closes) <= days or bundle.closes[-days - 1] <= 0:
            return None
        return (close / bundle.closes[-days - 1] - 1.0) * 100.0

    daily_returns = [
        bundle.closes[i] / bundle.closes[i - 1] - 1.0
        for i in range(1, len(bundle.closes))
        if bundle.closes[i - 1] > 0
    ]
    volatility = pstdev(daily_returns[-60:]) * sqrt(252.0) * 100.0 if len(daily_returns) >= 10 else None
    volume_ratio = bundle.volume_ratio[5][index]
    rsi = bundle.rsi14[index]
    dif = bundle.dif[index]
    dea = bundle.dea[index]
    ma5 = bundle.ma[5][index]
    ma20 = bundle.ma[20][index]
    return bundle, {
        "return_1m": period_return(20),
        "return_3m": period_return(60),
        "volatility_annual_pct": volatility,
        "volume_ratio_5": volume_ratio,
        "rsi14": rsi,
        "macd_dif": dif,
        "macd_dea": dea,
        "macd_positive_cross": bool(dif is not None and dea is not None and dif > dea and dif > 0),
        "close_above_ma5": bool(ma5 is not None and close >= ma5),
        "close_above_ma20": bool(ma20 is not None and close >= ma20),
        "latest_close": close,
    }


def rulebook_score(
    snapshot: Mapping[str, Any] | None,
    rows: Iterable[Mapping[str, Any]] | None,
    market_context: Mapping[str, Any] | float | int | None = None,
    mode: str = "balanced",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one candidate with the v1 value/growth/trend/event rulebook.

    Numeric cut-offs in the source document are candidate thresholds (evidence
    grade B/C), not promises.  Missing factors are exposed in coverage and
    shrink scores towards neutral instead of being silently treated as passes.
    """

    snapshot = snapshot or {}
    cfg = _deep_merge(RULEBOOK_CONFIG, config)
    rows_list = list(rows or [])
    risk = evaluate_rulebook_risk(snapshot, rows_list, cfg)
    bundle, series = _rulebook_series_metrics(rows_list)
    technical = _technical_from_bundle(bundle) if bundle.rows else {
        "total": 0,
        "label": "数据不足",
        "components": {},
        "evidence": ["K线数据待补充"],
        "indicators": {},
        "data_points": 0,
    }

    pe = _rulebook_metric(snapshot, "pe_ttm", "pe", "市盈率")
    pb = _rulebook_metric(snapshot, "pb", "市净率")
    dividend = _rulebook_metric(snapshot, "dividend_yield", "股息率")
    valuation_scores: list[float] = []
    if pe is not None:
        valuation_scores.append(
            8.0 if pe <= 0 else float(_band_score(pe, ((12, 95), (20, 82), (30, 68), (45, 50), (70, 30), (float("inf"), 15))) or 0)
        )
    if pb is not None:
        valuation_scores.append(
            8.0 if pb <= 0 else float(_band_score(pb, ((1, 92), (2, 78), (4, 62), (7, 42), (float("inf"), 22))) or 0)
        )
    if dividend is not None:
        valuation_scores.append(_clamp(dividend / 5.0) * 100.0)
    valuation = fmean(valuation_scores) if valuation_scores else None

    roe = _rulebook_metric(snapshot, "roe", "roe_pct", "净资产收益率")
    gross_margin = _rulebook_metric(snapshot, "gross_margin", "gross_margin_pct", "毛利率")
    net_margin = _rulebook_metric(snapshot, "net_margin", "net_margin_pct", "净利率")
    cashflow_ratio = _rulebook_metric(snapshot, "cashflow_profit_ratio", "ocf_net_profit", "经营现金流净利比")
    debt = _rulebook_metric(snapshot, "debt_ratio", "asset_liability_ratio", "资产负债率")
    quality_scores = [
        value
        for value in (
            _positive_threshold_score(roe, 0, 15, 30),
            _positive_threshold_score(gross_margin, 0, 20, 50),
            _positive_threshold_score(net_margin, 0, 10, 25),
            _positive_threshold_score(cashflow_ratio, 0, 1, 2),
            None if debt is None else _clamp((80.0 - debt) / 60.0) * 100.0,
        )
        if value is not None
    ]
    quality = fmean(quality_scores) if quality_scores else None

    profit_growth = _rulebook_metric(snapshot, "net_profit_growth", "profit_growth", "扣非净利润同比", "净利润同比")
    revenue_growth = _rulebook_metric(snapshot, "revenue_growth", "revenue_yoy", "营业收入同比", "营收同比")
    eps_growth = _rulebook_metric(snapshot, "eps_growth", "eps_yoy", "EPS同比")
    growth_scores = [
        value
        for value in (
            _positive_threshold_score(profit_growth, -20, 30, 80),
            _positive_threshold_score(revenue_growth, -10, 25, 60),
            _positive_threshold_score(eps_growth, -20, 30, 80),
        )
        if value is not None
    ]
    growth = fmean(growth_scores) if growth_scores else None

    forecast_upgrades = _rulebook_metric(snapshot, "forecast_upgrades", "profit_forecast_upgrades", "盈利预测上调次数")
    target_upside = _rulebook_metric(snapshot, "target_upside_pct", "target_price_upside", "目标价空间")
    expectation_scores = [
        value
        for value in (
            None if forecast_upgrades is None else _clamp(forecast_upgrades / 3.0) * 100.0,
            _positive_threshold_score(target_upside, -10, 20, 50),
        )
        if value is not None
    ]
    expectation = fmean(expectation_scores) if expectation_scores else None

    amount = _rulebook_metric(snapshot, "amount", "turnover_amount", "成交额")
    if amount is None and bundle.rows:
        amount = bundle.rows[-1].get("amount") or None
    turnover = _rulebook_metric(snapshot, "turnover_pct", "turnover", "turnover_rate", "换手率")
    liquidity_scores = [
        value
        for value in (
            None if amount is None else _clamp(log10(max(amount, 1.0)) / 10.0) * 100.0,
            None if turnover is None else _clamp(turnover / 5.0) * 100.0,
        )
        if value is not None
    ]
    liquidity = fmean(liquidity_scores) if liquidity_scores else None

    main_net = _rulebook_metric(snapshot, "main_net", "total_main_net", "main_fund_net", "主力净流入")
    institution_net = _rulebook_metric(snapshot, "institution_net", "lhb_institution_net", "龙虎榜机构净买")
    financing_change = _rulebook_metric(snapshot, "financing_change_pct", "margin_change_pct", "融资余额变化")
    fund_scores = [
        value
        for value in (
            None if main_net is None else 50.0 + 50.0 * (main_net / (abs(main_net) + 50_000_000.0)),
            None if institution_net is None else 50.0 + 50.0 * (institution_net / (abs(institution_net) + 50_000_000.0)),
            _positive_threshold_score(financing_change, -10, 0, 15),
        )
        if value is not None
    ]
    funds = fmean(fund_scores) if fund_scores else None

    return_1m = series.get("return_1m")
    return_3m = series.get("return_3m")
    if return_1m is None:
        return_1m = _rulebook_metric(snapshot, "change_20d_pct", "change_1m_pct", "1月涨幅")
    if return_3m is None:
        return_3m = _rulebook_metric(snapshot, "change_60d_pct", "change_3m_pct", "3月涨幅")
    momentum_scores = [
        value
        for value in (
            _positive_threshold_score(return_1m, -20, 8, 30),
            _positive_threshold_score(return_3m, -30, 15, 60),
        )
        if value is not None
    ]
    momentum = fmean(momentum_scores) if momentum_scores else None

    rsi = series.get("rsi14")
    macd_positive = series.get("macd_positive_cross") if series else None
    indicator_scores: list[float] = []
    if rsi is not None:
        indicator_scores.append(90.0 if 50 <= rsi <= 70 else 65.0 if 40 <= rsi < 50 or 70 < rsi <= 78 else 30.0)
    if macd_positive is not None:
        indicator_scores.append(90.0 if macd_positive else 30.0)
    if technical.get("data_points", 0):
        indicator_scores.append(_clamp((float(technical.get("total", 0)) + 12.0) / 24.0) * 100.0)
    indicators = fmean(indicator_scores) if indicator_scores else None

    volume_ratio = series.get("volume_ratio_5")
    if volume_ratio is None:
        volume_ratio = _rulebook_metric(snapshot, "volume_ratio", "量比")
    if volume_ratio is not None and volume_ratio <= 0:
        volume_ratio = None
    volume_score = None if volume_ratio is None else (
        95.0 if 1.5 <= volume_ratio <= 3.5 else 72.0 if 1.0 <= volume_ratio < 1.5 or 3.5 < volume_ratio <= 5 else 38.0
    )
    volatility = series.get("volatility_annual_pct")
    volatility_score = None if volatility is None else _clamp((55.0 - volatility) / 35.0) * 100.0

    mcap = _rulebook_metric(snapshot, "mcap", "market_cap", "total_market_cap", "总市值")
    style_score = None if mcap is None else (
        82.0 if 5_000_000_000 <= mcap <= 100_000_000_000 else 65.0 if 1_500_000_000 <= mcap <= 300_000_000_000 else 42.0
    )

    event_raw = _rulebook_metric(snapshot, "event_score", "earnings_event_score", "事件强度")
    earnings_surprise = _rulebook_metric(snapshot, "earnings_surprise_pct", "业绩预告超预期")
    lhb_net = _rulebook_metric(snapshot, "lhb_net", "dragon_tiger_net", "龙虎榜净买入")
    event_scores = [
        value
        for value in (
            None if event_raw is None else (_clamp(event_raw if event_raw <= 1 else event_raw / 100.0) * 100.0),
            _positive_threshold_score(earnings_surprise, -20, 20, 80),
            None if lhb_net is None else 50.0 + 50.0 * (lhb_net / (abs(lhb_net) + 50_000_000.0)),
        )
        if value is not None
    ]
    event_score = fmean(event_scores) if event_scores else None
    prior_event_gain = _rulebook_metric(snapshot, "event_prior_10d_gain", "prior_10d_gain", "事件前10日涨幅")
    if prior_event_gain is None:
        prior_event_gain = return_1m
    chase_risk = None if prior_event_gain is None else _clamp((35.0 - max(prior_event_gain, -10.0)) / 45.0) * 100.0

    factors = {
        "valuation": _factor(valuation, {"pe_ttm": pe, "pb": pb, "dividend_yield": dividend}, "PE/PB负值不作低估值；正值按区间候选评分"),
        "quality": _factor(quality, {"roe": roe, "gross_margin": gross_margin, "cashflow_profit_ratio": cashflow_ratio, "debt_ratio": debt}, "质量参考ROE、利润率、现金流与负债"),
        "growth": _factor(growth, {"profit_growth": profit_growth, "revenue_growth": revenue_growth, "eps_growth": eps_growth}, "成长参考净利、营收和EPS增速"),
        "style": _factor(style_score, mcap, "行业/市值风格适配的简化代理"),
        "technical": _factor(indicators, technical.get("total"), "技术只作入场确认，不替代收益逻辑"),
        "expectation": _factor(expectation, {"forecast_upgrades": forecast_upgrades, "target_upside_pct": target_upside}, "盈利预测上调与目标空间"),
        "funds": _factor(funds, {"main_net": main_net, "institution_net": institution_net, "financing_change_pct": financing_change}, "主力、机构与融资变化"),
        "trend": _factor(momentum, {"return_1m": return_1m, "return_3m": return_3m}, "1月/3月动量候选评分"),
        "momentum": _factor(momentum, {"return_1m": return_1m, "return_3m": return_3m}, "1月/3月动量候选评分"),
        "indicators": _factor(indicators, {"rsi14": rsi, "macd_positive_cross": macd_positive}, "RSI、MACD和均线一致性"),
        "volume": _factor(volume_score, volume_ratio, "量能相对5日均量，1.5倍为候选参考"),
        "liquidity": _factor(liquidity, {"amount": amount, "turnover_pct": turnover}, "成交额与换手率"),
        "volatility": _factor(volatility_score, volatility, "20-60日年化波动率，低波动优先"),
        "event": _factor(event_score, {"event_score": event_raw, "earnings_surprise_pct": earnings_surprise, "lhb_net": lhb_net}, "业绩、龙虎榜和已结构化事件"),
        "chase_risk": _factor(chase_risk, prior_event_gain, "事件前涨幅越大，追高风险得分越低"),
    }

    modes = {
        mode_name: _weighted_mode(mode_name, weights, factors)
        for mode_name, weights in cfg["mode_weights"].items()
    }
    aliases = {"technical": "trend", "auction": "event", "composite": "balanced", "rulebook": "balanced"}
    requested_mode = aliases.get(str(mode or "balanced").lower(), str(mode or "balanced").lower())
    if requested_mode in RULEBOOK_MODES:
        selected_mode = requested_mode
        selection_reason = "按用户/任务指定模式评分"
    else:
        selected_mode = max(
            RULEBOOK_MODES,
            key=lambda item: (modes[item]["score"] + modes[item]["coverage"] * 4.0, modes[item]["coverage"]),
        )
        selection_reason = "在四类模式中选择得分与数据覆盖更匹配者"

    market_value, coefficient, market_label = _market_context(market_context)
    breadth = market_context.get("breadth", {}) if isinstance(market_context, Mapping) else {}
    advance = _number(_first(breadth, "advance", "rise", "up")) if isinstance(breadth, Mapping) else None
    decline = _number(_first(breadth, "decline", "fall", "down")) if isinstance(breadth, Mapping) else None
    breadth_ratio = advance / (advance + decline) if advance is not None and decline is not None and advance + decline > 0 else None
    explicit_phase = str(_first(market_context, "emotion_phase", "phase", "market_phase") or "") if isinstance(market_context, Mapping) else ""
    retreat = market_value <= int(cfg["market_block_score"]) or (breadth_ratio is not None and breadth_ratio < 0.30) or "退潮" in explicit_phase
    selected = modes[selected_mode]
    soft_penalty = min(24.0, len(risk["soft_flags"]) * 4.0)
    adjusted_score = _clamp(float(selected["score"]) * coefficient - soft_penalty, 0.0, 100.0)
    if risk["hard_veto"]:
        adjusted_score = 0.0

    trigger_conditions: list[dict[str, Any]] = []
    if series:
        trigger_conditions.extend(
            [
                {"name": "RSI 50-70", "pass": bool(rsi is not None and 50 <= rsi <= 70), "value": _round(rsi)},
                {"name": "MACD零轴上方转强", "pass": bool(series.get("macd_positive_cross")), "value": {"dif": _round(series.get("macd_dif")), "dea": _round(series.get("macd_dea"))}},
                {"name": "量能达到5日均量1.5倍", "pass": bool(volume_ratio is not None and volume_ratio >= 1.5), "value": _round(volume_ratio)},
                {"name": "收盘站上MA5与MA20", "pass": bool(series.get("close_above_ma5") and series.get("close_above_ma20")), "value": series.get("latest_close")},
            ]
        )
    trigger_passed = sum(1 for item in trigger_conditions if item["pass"])
    if not trigger_conditions:
        trigger_ok: bool | None = None
    elif selected_mode in {"trend", "event"}:
        # The rulebook explicitly says inconsistent indicators are not a trade:
        # trend/event entries need RSI, MACD, volume and price structure to
        # agree, rather than passing on two convenient technical signals.
        trigger_ok = bool(all(item["pass"] for item in trigger_conditions))
    else:
        trigger_ok = trigger_passed >= 2
    valuation_invalid = (pe is not None and pe <= 0) or (pb is not None and pb <= 0)
    severe_financial_risk = any(
        phrase in "；".join(risk["soft_flags"])
        for phrase in ("资产负债率高", "股权质押率高", "经营现金流/净利润低")
    )
    push_eligible = bool(
        risk["eligible"]
        and adjusted_score >= float(cfg["push_threshold"])
        and not (retreat and selected_mode in {"trend", "event"})
        and trigger_ok is True
        and not valuation_invalid
        and not severe_financial_risk
    )

    if risk["hard_veto"]:
        label = "风险否决"
    elif adjusted_score >= float(cfg["push_threshold"]):
        label = "强候选" if push_eligible else "高分观察"
    elif adjusted_score >= float(cfg["threshold"]):
        label = "候选"
    elif adjusted_score >= 52:
        label = "观察"
    else:
        label = "暂不入选"
    if retreat:
        position = "总仓0-10%，趋势/事件接力停止"
    elif market_value >= 6:
        position = "总仓30-70%，单股通常不超过30%"
    elif market_value >= 1:
        position = "总仓10-30%，确认后再加仓"
    else:
        position = "总仓0-20%，等待环境确认"

    mode_labels = {"value": "价值型", "growth": "成长型", "trend": "趋势型", "event": "事件驱动型"}
    return {
        "strategy_version": STRATEGY_VERSION,
        "rulebook_version": RULEBOOK_VERSION,
        "score": round(adjusted_score, 1),
        "raw_score": selected["raw_score"],
        "label": label,
        "selected_mode": selected_mode,
        "selected_mode_label": mode_labels[selected_mode],
        "selection_reason": selection_reason,
        "mode_scores": modes,
        "breakdown": {
            name: {
                "score": value["score"],
                "max_score": 100,
                "coverage": value["coverage"],
                "reason": "规则库模式评分（候选阈值，待回测）",
            }
            for name, value in modes.items()
        },
        "factors": factors,
        "risk": risk,
        "eligible": risk["eligible"],
        "push_eligible": push_eligible,
        "market": {
            "score": market_value,
            "label": market_label,
            "coefficient": round(coefficient, 3),
            "breadth_ratio": _round(breadth_ratio),
            "emotion_phase": explicit_phase or ("退潮/弱势" if retreat else "待确认"),
            "short_term_blocked": retreat,
        },
        "trigger": {
            "confirmed": trigger_ok,
            "passed": trigger_passed,
            "total": len(trigger_conditions),
            "conditions": trigger_conditions,
            "note": "竞价和开盘触发只验证预案，不单独构成买入理由。",
        },
        "position": position,
        "exit_plan": [
            "逻辑失效或市场退潮优先退出",
            "板块走弱、龙头断板且次日不修复时退出",
            "短线1-3个交易日不兑现时执行时间止损",
            "技术止损按波动率与平台位校准，不机械套用固定百分比",
        ],
        "data_coverage": {
            "selected_mode": selected["coverage"],
            "kline_points": len(bundle.rows),
            "missing_factors": [name for name, item in factors.items() if not item["available"]],
            "threshold_status": "候选阈值，需样本外回测和实盘偏差复核",
        },
        "technical": technical,
        "evidence": [
            f"采用{mode_labels[selected_mode]}，模式得分{selected['score']:.1f}，覆盖率{selected['coverage']:.0%}",
            f"市场环境{market_label}，系数{coefficient:.2f}",
            *(risk["hard_flags"] or risk["soft_flags"][:2]),
        ],
    }


def _cross_index_resonance_score(
    indices: Sequence[Mapping[str, Any]], weight: float = 10.0
) -> dict[str, Any]:
    """Score synchronized decline across major indices — Signal 5: Cross-index Resonance."""
    if not indices:
        return {"points": 0.0, "tag": "数据缺失", "available": False, "declining": 0, "monitored": 0, "severe_declining": 0, "detail": ""}
    KEY_INDICES = {
        "000001": "上证", "399001": "深证", "399006": "创业板",
        "000300": "沪深300", "000688": "科创50", "000905": "中证500",
    }
    declining = severe = 0
    details: list[str] = []
    for idx in indices:
        code = str(idx.get("code", ""))
        if code not in KEY_INDICES:
            continue
        change = _number(idx.get("change_pct")) or 0.0
        name = KEY_INDICES[code]
        if change < -3:
            severe += 1
        if change < 0:
            declining += 1
            details.append(f"{name}{change:+.1f}%")
    monitored = sum(1 for idx in indices if str(idx.get("code", "")) in KEY_INDICES)
    if monitored < 3:
        return {"points": 0.5 * weight, "tag": "指数不足", "available": False, "declining": declining, "monitored": monitored, "severe_declining": severe, "detail": ""}
    if severe >= 3:
        points, tag = weight * 0.08, f"系统性Risk-off({declining}/{monitored}跌,{severe}重)"
    elif declining >= 5:
        points, tag = weight * 0.12, f"高度共振({declining}/{monitored}同步下跌)"
    elif declining >= 4:
        points, tag = weight * 0.25, f"强共振({declining}/{monitored}同步下跌)"
    elif declining >= 3:
        points, tag = weight * 0.50, f"中度共振({declining}/{monitored}下跌)"
    elif declining >= 2:
        points, tag = weight * 0.75, f"轻度共振({declining}/{monitored}下跌)"
    elif declining == 1:
        points, tag = weight * 0.90, f"个别走弱({declining}/{monitored})"
    else:
        points, tag = weight, f"无共振({monitored}指数全涨)"
    return {"points": round(points, 1), "tag": tag, "available": True, "declining": declining, "monitored": monitored, "severe_declining": severe, "detail": "；".join(details) if details else "无显著下跌指数"}


def _high_beta_weakness_score(
    indices: Sequence[Mapping[str, Any]], weight: float = 8.0
) -> dict[str, Any]:
    """Score high-beta underperformance vs HS300 — Signal 2: Leader Sector Breakdown."""
    idx_map: dict[str, tuple[str, float]] = {}
    for idx in indices:
        code = str(idx.get("code", ""))
        change = _number(idx.get("change_pct"))
        if code and change is not None:
            idx_map[code] = (str(idx.get("name", code)), change)
    cyb = idx_map.get("399006")
    kc = idx_map.get("000688")
    hs300 = idx_map.get("000300")
    if not cyb or not hs300:
        return {"points": 0.5 * weight, "tag": "高beta/基准缺失", "available": False, "diff_vs_hs300": None, "detail": ""}
    diff = cyb[1] - hs300[1]
    if kc:
        diff = min(diff, kc[1] - hs300[1])
    if diff >= 2:
        points, tag = weight, f"高beta领涨({diff:+.1f}% vs HS300)"
    elif diff >= 0:
        points, tag = weight * 0.85, f"高beta偏强({diff:+.1f}% vs HS300)"
    elif diff >= -1:
        points, tag = weight * 0.65, f"高beta略弱({diff:+.1f}% vs HS300)"
    elif diff >= -2:
        points, tag = weight * 0.40, f"高beta走弱({diff:+.1f}% vs HS300)⚠️"
    elif diff >= -4:
        points, tag = weight * 0.20, f"高beta显著跑输({diff:+.1f}% vs HS300)🔴"
    else:
        points, tag = weight * 0.08, f"高beta被抛售({diff:+.1f}% vs HS300)🚨"
    detail = f"创业板{cyb[1]:+.1f}%"
    if kc:
        detail += f"，科创50{kc[1]:+.1f}%"
    detail += f" vs 沪深300{hs300[1]:+.1f}%"
    return {"points": round(points, 1), "tag": tag, "available": True, "diff_vs_hs300": round(diff, 2), "detail": detail}


def _breadth_velocity_score(
    current_ratio: float | None, prev_ratio: float | None, weight: float = 8.0
) -> dict[str, Any]:
    """Score breadth change velocity — Signal 3: Breadth Decay Velocity."""
    if current_ratio is None:
        return {"points": 0.0, "tag": "当前广度缺失", "available": False, "delta": None, "prev_ratio": None, "current_ratio": None}
    if prev_ratio is None:
        return {"points": weight * 0.70, "tag": "无历史对比(首日)", "available": True, "delta": None, "prev_ratio": None, "current_ratio": round(current_ratio, 4)}
    delta_pct = (current_ratio - prev_ratio) * 100
    if delta_pct >= 5:
        points, tag = weight, f"广度扩张(+{delta_pct:.0f}pp)"
    elif delta_pct >= 0:
        points, tag = weight * 0.85, f"广度稳定({delta_pct:+.0f}pp)"
    elif delta_pct >= -10:
        points, tag = weight * 0.65, f"广度微缩({delta_pct:.0f}pp)"
    elif delta_pct >= -20:
        points, tag = weight * 0.35, f"广度恶化({delta_pct:.0f}pp)⚠️"
    elif delta_pct >= -30:
        points, tag = weight * 0.15, f"广度崩塌({delta_pct:.0f}pp)🔴"
    else:
        points, tag = weight * 0.05, f"广度雪崩({delta_pct:.0f}pp)🚨"
    return {"points": round(points, 1), "tag": tag, "available": True, "delta": round(delta_pct, 1), "prev_ratio": round(prev_ratio, 4), "current_ratio": round(current_ratio, 4)}


def _overseas_shock_score(
    overseas: Mapping[str, Any] | None, weight: float = 8.0
) -> dict[str, Any]:
    """Score overseas market shock — Signal 6: Overseas Shock.

    Detects extreme negative moves in overseas markets that occurred
    BEFORE A-share opening (legitimate pre-market data, no look-ahead).
    Based on Simon's 六维择时 Signal 6: Global Shock.

    Scoring logic (lower = more risk):
    - No overseas data → neutral (0.6 × weight)
    - All indices normal → full points
    - One index warning → 70%
    - Multiple warnings or one severe → 30–50%
    - Multiple severe declines → 8–15% (strong shock)
    """
    if not overseas:
        return {
            "points": weight * 0.60,
            "tag": "海外数据缺失(中性)",
            "available": False,
            "severe_count": 0,
            "warning_count": 0,
            "details": [],
        }

    severe_count = 0
    warning_count = 0
    details: list[str] = []

    for code, data in overseas.items():
        if not isinstance(data, Mapping):
            continue
        if not data.get("available"):
            continue
        change = data.get("change_pct")
        if change is None:
            continue
        name = data.get("name_cn", code)
        severe_th = data.get("threshold_severe", -4.0)
        warn_th = data.get("threshold_warning", -2.5)

        if change <= severe_th:
            severe_count += 1
            details.append(f"{name}{change:+.1f}%❗")
        elif change <= warn_th:
            warning_count += 1
            details.append(f"{name}{change:+.1f}%⚠️")
        else:
            details.append(f"{name}{change:+.1f}%")

    fetched = sum(1 for v in overseas.values() if isinstance(v, Mapping) and v.get("available"))
    if fetched == 0:
        return {
            "points": weight * 0.55,
            "tag": "海外数据全部获取失败",
            "available": False,
            "severe_count": 0,
            "warning_count": 0,
            "details": [],
        }

    if severe_count >= 3:
        points, tag = weight * 0.08, f"海外全面冲击({severe_count}严重)"
    elif severe_count >= 2:
        points, tag = weight * 0.15, f"海外双重冲击({severe_count}严重)"
    elif severe_count >= 1 and warning_count >= 2:
        points, tag = weight * 0.22, f"海外严重+预警({severe_count}重+{warning_count}警)"
    elif severe_count >= 1:
        points, tag = weight * 0.35, f"海外单指数严重冲击({details[0] if details else ''})"
    elif warning_count >= 3:
        points, tag = weight * 0.40, f"海外多指数预警({warning_count}个)"
    elif warning_count >= 2:
        points, tag = weight * 0.55, f"海外双指数预警({warning_count}个)"
    elif warning_count >= 1:
        points, tag = weight * 0.75, f"海外单指数预警"
    else:
        points, tag = weight, f"海外无冲击({fetched}指数正常)"

    return {
        "points": round(points, 1),
        "tag": tag,
        "available": True,
        "severe_count": severe_count,
        "warning_count": warning_count,
        "fetched": fetched,
        "detail": "；".join(details) if details else "无显著海外冲击",
    }


def _crowding_fragility_score(
    index_kline: Sequence[Mapping[str, Any]] | None,
    breadth_ratio: float | None,
    weight: float = 6.0,
) -> dict[str, Any]:
    """Score crowding fragility — Signal 1: Crowding Fragility.

    Detects when the market index is near its 20-day high but breadth is
    abnormally narrow (few stocks participating). This is a "yellow light"
    signal — not an immediate block, but a warning that the market is
    increasingly dependent on a handful of leaders.

    Based on Simon's 六维择时: 指数创新高但近8成股票下跌.
    """
    if not index_kline:
        return {"points": 0.5 * weight, "tag": "K线数据缺失", "available": False,
                "near_high": False, "high_pct": None, "detail": ""}

    # Extract closes from kline rows
    closes: list[float] = []
    for row in index_kline:
        if isinstance(row, Mapping):
            val = _number(row.get("close")) or _number(row.get("Close"))
            if val is not None and val > 0:
                closes.append(val)

    if len(closes) < 20:
        return {"points": 0.5 * weight, "tag": f"K线不足({len(closes)}条)", "available": False,
                "near_high": False, "high_pct": None, "detail": ""}

    # 20-day high and current position
    high_20 = max(closes[-20:])
    current = closes[-1]
    pct_from_high = (current / high_20 - 1.0) * 100.0 if high_20 > 0 else 0.0

    # "Near high" = within 3% of 20-day peak
    near_high = pct_from_high > -3.0

    # Breadth check
    breadth_low = breadth_ratio is not None and breadth_ratio < 0.55
    breadth_very_low = breadth_ratio is not None and breadth_ratio < 0.35

    detail = f"距20日高{pct_from_high:+.1f}%，广度={breadth_ratio:.1%}" if breadth_ratio is not None else f"距20日高{pct_from_high:+.1f}%"

    if near_high and breadth_very_low:
        points, tag = weight * 0.12, f"高度拥挤⚠️(高+广度{breadth_ratio:.0%})"
    elif near_high and breadth_low:
        points, tag = weight * 0.30, f"拥挤脆弱(高+广度{breadth_ratio:.0%})"
    elif near_high:
        points, tag = weight * 0.65, f"高位运行(广度尚可)"
    elif breadth_very_low:
        points, tag = weight * 0.40, f"广度极弱({breadth_ratio:.0%})"
    elif breadth_low:
        points, tag = weight * 0.70, f"广度偏弱(非高位)"
    else:
        points, tag = weight, f"无拥挤(距高{pct_from_high:+.1f}%)"

    return {
        "points": round(points, 1), "tag": tag, "available": True,
        "near_high": near_high, "pct_from_high": round(pct_from_high, 1),
        "high_20": round(high_20, 2), "detail": detail,
    }


def _liquidity_compression_score(
    index_kline: Sequence[Mapping[str, Any]] | None,
    weight: float = 7.0,
) -> dict[str, Any]:
    """Score liquidity compression — Signal 4: Liquidity Compression.

    Tracks consecutive volume contraction combined with price decline.
    In Simon's model, liquidity compression alone is NOT bearish —
    it becomes dangerous only when paired with price deterioration.

    "缩量本身不等于看空。真正危险的是：价格趋势向下 + 高beta跑输 +
     市场广度恶化 + 同时成交额连续下降。"
    """
    if not index_kline:
        return {"points": 0.5 * weight, "tag": "K线数据缺失", "available": False,
                "consecutive_down": 0, "volume_trend": "", "detail": ""}

    # Extract close and volume (amount) from kline
    closes: list[float] = []
    amounts: list[float] = []
    for row in index_kline:
        if isinstance(row, Mapping):
            c = _number(row.get("close")) or _number(row.get("Close"))
            a = _number(row.get("amount")) or _number(row.get("Amount")) or _number(row.get("volume"))
            if c is not None and c > 0:
                closes.append(c)
                amounts.append(a if a is not None and a > 0 else 0.0)

    if len(closes) < 10:
        return {"points": 0.5 * weight, "tag": f"K线不足({len(closes)}条)", "available": False,
                "consecutive_down": 0, "volume_trend": "", "detail": ""}

    # Count consecutive volume contraction days (last 5 sessions)
    consecutive_down = 0
    for i in range(len(amounts) - 1, max(len(amounts) - 6, 0), -1):
        if i > 0 and amounts[i] > 0 and amounts[i - 1] > 0 and amounts[i] < amounts[i - 1]:
            consecutive_down += 1
        else:
            break

    # Price trend over same period
    lookback = min(5, len(closes) - 1)
    price_change = (closes[-1] / closes[-lookback - 1] - 1.0) * 100.0 if closes[-lookback - 1] > 0 else 0.0
    price_falling = price_change < 0

    # Volume trend description
    if consecutive_down >= 4:
        vol_tag = f"连缩{consecutive_down}天"
    elif consecutive_down >= 3:
        vol_tag = f"缩量{consecutive_down}天"
    elif consecutive_down >= 2:
        vol_tag = f"微缩{consecutive_down}天"
    else:
        vol_tag = "正常"

    detail = f"成交额{vol_tag}，5日价格{price_change:+.1f}%"

    # Scoring: penalize when BOTH volume contracting AND price falling
    if consecutive_down >= 4 and price_falling:
        points, tag = weight * 0.15, f"流动性枯竭(连缩{consecutive_down}天+价格下跌)🔴"
    elif consecutive_down >= 3 and price_falling:
        points, tag = weight * 0.30, f"流动性恶化(缩量{consecutive_down}天+价格下跌)⚠️"
    elif consecutive_down >= 2 and price_falling:
        points, tag = weight * 0.50, f"流动性收缩({vol_tag}+价格跌)"
    elif consecutive_down >= 4:
        points, tag = weight * 0.60, f"持续缩量(价格企稳)"
    elif consecutive_down >= 2:
        points, tag = weight * 0.75, f"轻度缩量({vol_tag})"
    elif price_falling and price_change < -5:
        points, tag = weight * 0.45, f"放量下跌⚠️"
    else:
        points, tag = weight, f"流动性正常"

    return {
        "points": round(points, 1), "tag": tag, "available": True,
        "consecutive_down": consecutive_down, "volume_trend": vol_tag,
        "price_change_5d": round(price_change, 1), "detail": detail,
    }


def intraday_q_switch(
    indices: Sequence[Mapping[str, Any]] | None = None,
    candidate_pool: Sequence[Mapping[str, Any]] | None = None,
    *,
    high_beta_threshold: float = -2.5,
    broad_index_threshold: float = -1.5,
    min_resonance_count: int = 3,
    gap_down_ratio_threshold: float = 0.70,
) -> dict[str, Any]:
    """Intraday emergency circuit breaker — Signal: 9:35 Q-Switch.

    Designed to be called at approximately 9:35 AM after market open.
    Checks whether an emergency stop is warranted based on real-time
    index data and candidate pool behavior.

    Conditions (all must be met at 9:35):
    1. High-beta index (创业板/科创50) decline > high_beta_threshold from prev close
    2. Broad index (深证成指/中证1000) decline > broad_index_threshold
    3. At least min_resonance_count core indices at new intraday lows
    4. Candidate pool gap-down ratio exceeds gap_down_ratio_threshold

    Based on Simon's 六维择时 intraday Q-Switch.
    触发后：取消买单、停止新增多头、降低高beta持仓。
    """
    conditions: list[dict[str, Any]] = []
    triggered_count = 0

    if not indices:
        return {"triggered": False, "reason": "无实时指数数据", "conditions": [], "actions": []}

    idx_map = {str(i.get("code", "")): i for i in indices if isinstance(i, Mapping)}

    # Condition 1: High-beta index decline
    hb_hit = False
    for code in ("399006", "000688"):  # 创业板, 科创50
        idx = idx_map.get(code)
        if idx:
            chg = _number(idx.get("change_pct"))
            if chg is not None and chg <= high_beta_threshold:
                hb_hit = True
                break
    conditions.append({"name": "高beta急跌", "met": hb_hit,
                       "threshold": f"任一指数≤{high_beta_threshold}%",
                       "value": _number(idx_map.get("399006", {}).get("change_pct")) if idx_map.get("399006") else None})
    if hb_hit:
        triggered_count += 1

    # Condition 2: Broad index decline
    broad_hit = False
    for code in ("399001", "000905"):  # 深证成指, 中证1000 (中证500 as proxy)
        idx = idx_map.get(code)
        if idx:
            chg = _number(idx.get("change_pct"))
            if chg is not None and chg <= broad_index_threshold:
                broad_hit = True
                break
    conditions.append({"name": "大盘指数跟跌", "met": broad_hit,
                       "threshold": f"任一指数≤{broad_index_threshold}%",
                       "value": _number(idx_map.get("399001", {}).get("change_pct")) if idx_map.get("399001") else None})
    if broad_hit:
        triggered_count += 1

    # Condition 3: Multi-index resonance
    CORE = ("000001", "399001", "399006", "000300", "000688", "000905")
    declining = sum(1 for c in CORE if idx_map.get(c) and (_number(idx_map[c].get("change_pct")) or 0) < -1.0)
    resonance_hit = declining >= min_resonance_count
    conditions.append({"name": "多指数共振下跌", "met": resonance_hit,
                       "threshold": f"≥{min_resonance_count}个核心指数跌超1%",
                       "value": declining})
    if resonance_hit:
        triggered_count += 1

    # Condition 4: Candidate pool gap-down
    pool_hit = False
    gap_ratio = None
    if candidate_pool:
        pool_changes = [_number(r.get("change_pct")) for r in candidate_pool
                        if isinstance(r, Mapping) and r.get("change_pct") not in (None, "")]
        if pool_changes:
            gap_down = sum(1 for v in pool_changes if v < -2)
            gap_ratio = gap_down / len(pool_changes) if pool_changes else 0
            pool_hit = gap_ratio >= gap_down_ratio_threshold
    conditions.append({"name": "候选池大面积低开", "met": pool_hit,
                       "threshold": f"低开>2%比例≥{gap_down_ratio_threshold:.0%}",
                       "value": round(gap_ratio, 3) if gap_ratio is not None else None})
    if pool_hit:
        triggered_count += 1

    # Decision
    triggered = triggered_count >= 3  # Need 3 out of 4
    if triggered:
        reason = f"Q-Switch触发({triggered_count}/4条件满足)"
        actions = [
            "立即取消所有未完成买单",
            "停止当天新增多头仓位",
            "降低前一日高beta持仓至目标仓位50%以下",
        ]
    elif triggered_count >= 2:
        reason = f"Q-Switch警告({triggered_count}/4条件接近触发)"
        actions = ["暂停新增买入，观察至9:45"]
    else:
        reason = f"Q-Switch正常({triggered_count}/4)"
        actions = []

    return {
        "triggered": triggered,
        "triggered_count": triggered_count,
        "reason": reason,
        "conditions": conditions,
        "actions": actions,
        "as_of": "9:35盘中检查",
    }


def _retail_extreme_score(
    retail_sentiment: Mapping[str, Any] | None,
    weight: float = 6.0,
) -> dict[str, Any]:
    """Score retail sentiment extremes — 108选6 Factor 3: Retail Flow.

    Based on the 微笑曲线: both extreme buying AND extreme selling
    by retail investors signal opportunity. The middle (tepid) is worst.
    Score = 1 - |sentiment - 0.5| * 2 → 1 at extremes, 0 at neutral.
    """
    if not retail_sentiment:
        return {"points": weight * 0.60, "tag": "散户数据缺失", "available": False,
                "extreme_count": 0, "avg_extreme_score": None}

    scores = []
    extreme_count = 0
    for v in retail_sentiment.values():
        if isinstance(v, Mapping):
            es = v.get("extreme_score")
            if es is not None:
                scores.append(float(es))
                if v.get("is_extreme"):
                    extreme_count += 1

    if not scores:
        return {"points": weight * 0.55, "tag": "无有效散户数据", "available": False,
                "extreme_count": 0, "avg_extreme_score": None}

    avg_score = sum(scores) / len(scores)

    # Higher avg extreme score = more industries at extremes = more opportunity
    if avg_score >= 0.5:
        points, tag = weight, f"散户情绪高度极端(均{avg_score:.2f}/{extreme_count}行业)"
    elif avg_score >= 0.35:
        points, tag = weight * 0.80, f"散户情绪较极端(均{avg_score:.2f}/{extreme_count}行业)"
    elif avg_score >= 0.20:
        points, tag = weight * 0.55, f"散户情绪温和(均{avg_score:.2f})"
    else:
        points, tag = weight * 0.35, f"散户情绪平淡(均{avg_score:.2f}，缺乏机会)"

    return {
        "points": round(points, 1), "tag": tag, "available": True,
        "extreme_count": extreme_count, "avg_extreme_score": round(avg_score, 3),
        "industries_tracked": len(scores),
    }


def dragon_market_phase(market_context: Mapping[str, Any] | None, snapshots: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Classify emotion from breadth, limit structure and data quality."""
    context = market_context or {}
    breadth = context.get("breadth") if isinstance(context, Mapping) else {}
    ratio = _number(_first(breadth or {}, "ratio", "breadth_ratio")) if isinstance(breadth, Mapping) else None
    indices = context.get("indices") if isinstance(context, Mapping) else None
    prev_breadth_ratio = _number(context.get("prev_breadth_ratio")) if isinstance(context, Mapping) else None
    overseas = context.get("overseas") if isinstance(context, Mapping) else None
    index_kline = context.get("index_kline") if isinstance(context, Mapping) else None
    retail_sentiment = context.get("retail_sentiment") if isinstance(context, Mapping) else None
    rows = [row for row in snapshots if isinstance(row, Mapping)]
    changes = [_number(row.get("change_pct")) or 0.0 for row in rows if row.get("change_pct") not in (None, "")]
    if ratio is None and changes:
        ratio = sum(1 for value in changes if value > 0) / max(1, sum(1 for value in changes if value != 0))
    score = (_number(context.get("score")) or 0.0) if isinstance(context, Mapping) else 0.0
    sample_size = len(changes)
    positive = sum(1 for value in changes if value >= 5)
    negative = sum(1 for value in changes if value <= -5)
    median_change = sorted(changes)[len(changes) // 2] if changes else None

    def explicit(*names: str) -> int | None:
        value = _first(context, *names) if isinstance(context, Mapping) else None
        if value in (None, "") and isinstance(breadth, Mapping):
            value = _first(breadth, *names)
        try:
            return max(0, int(float(value))) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    limit_up = explicit("limit_up_count", "涨停数", "涨停家数")
    limit_down = explicit("limit_down_count", "跌停数", "跌停家数")
    inferred_up = inferred_down = 0
    for row in rows:
        change = _number(row.get("change_pct")) or 0.0
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        threshold = 4.8 if "ST" in name.upper() else (19.5 if code.startswith(("300", "301")) else 9.5)
        if change >= threshold:
            inferred_up += 1
        if change <= -threshold:
            inferred_down += 1
    limit_up = inferred_up if limit_up is None else limit_up
    limit_down = inferred_down if limit_down is None else limit_down
    strong_up_ratio = positive / sample_size if sample_size else None
    strong_down_ratio = negative / sample_size if sample_size else None
    confidence = "high" if sample_size >= 500 else "medium" if sample_size >= 100 else "low"
    enough = sample_size >= 100 and ratio is not None
    median = median_change or 0.0

    # --- Multi-factor emotion score (0–100 scale) ---
    # Replaces the three hard-wired binary conditions with a weighted scoring
    # approach inspired by McClellan breadth, AD-line momentum, and common
    # Chinese quant frameworks (涨跌比 + 强势股比 + 涨跌停结构 + 中位数涨跌).
    emotion_score = 0.0
    max_possible = 0.0
    breakdown: list[dict[str, Any]] = []

    # Factor 1: Breadth ratio (weight: 35 points)
    if ratio is not None and enough:
        w = 35.0
        max_possible += w
        if ratio >= 0.65:
            points = w
            tag = "广度强势"
        elif ratio >= 0.50:
            points = w * 0.75
            tag = "广度偏强"
        elif ratio >= 0.38:
            points = w * 0.50
            tag = "广度中性"
        elif ratio >= 0.25:
            points = w * 0.30
            tag = "广度偏弱"
        elif ratio >= 0.18:
            points = w * 0.12
            tag = "广度弱势"
        else:
            points = w * 0.05
            tag = "广度极弱"
        emotion_score += points
        breakdown.append({"factor": "breadth", "value": round(ratio, 4), "points": round(points, 1), "tag": tag})

    # Factor 2: Strong-move asymmetry (weight: 25 points)
    # Uses ±5% moves to gauge conviction, not just direction.
    if positive is not None and negative is not None and sample_size and (positive + negative) > 0:
        w = 25.0
        max_possible += w
        if negative == 0 and positive > 0:
            points = w
            tag = "无强势下跌"
        elif positive > negative * 2.5:
            points = w * 0.85
            tag = "强势股碾压"
        elif positive > negative * 1.5:
            points = w * 0.65
            tag = "强势股占优"
        elif positive > negative:
            points = w * 0.45
            tag = "强势股略多"
        elif positive == negative:
            points = w * 0.30
            tag = "强势股均衡"
        elif negative <= positive * 1.5:
            points = w * 0.18
            tag = "强势股偏弱"
        else:
            points = w * 0.06
            tag = "强势股碾压式弱势"
        emotion_score += points
        breakdown.append({"factor": "strong_asymmetry", "value": f"up={positive},down={negative}", "points": round(points, 1), "tag": tag})

    # Factor 3: Limit-up / limit-down structure (weight: 20 points)
    if limit_up is not None and limit_down is not None and (limit_up + limit_down) > 0:
        w = 20.0
        max_possible += w
        if limit_up >= 50 and limit_down < 10:
            points = w
            tag = "涨停潮"
        elif limit_up > limit_down * 3:
            points = w * 0.85
            tag = "涨停主导"
        elif limit_up > limit_down * 1.5:
            points = w * 0.62
            tag = "涨停偏多"
        elif limit_up > limit_down:
            points = w * 0.42
            tag = "涨跌停接近"
        elif limit_down <= limit_up * 1.5:
            points = w * 0.22
            tag = "跌停偏多"
        elif limit_down <= limit_up * 3:
            points = w * 0.12
            tag = "跌停主导"
        else:
            points = w * 0.04
            tag = "跌停潮/恐慌"
        emotion_score += points
        breakdown.append({"factor": "limit_structure", "value": f"up={limit_up},down={limit_down}", "points": round(points, 1), "tag": tag})

    # Factor 4: Median daily change (weight: 10 points)
    # The median stock move reveals whether the "average stock" is participating.
    if median is not None and enough:
        w = 10.0
        max_possible += w
        if median > 1.5:
            points = w
            tag = "普涨强劲"
        elif median > 0.6:
            points = w * 0.75
            tag = "温和普涨"
        elif median > 0.0:
            points = w * 0.55
            tag = "微幅偏涨"
        elif median > -0.6:
            points = w * 0.35
            tag = "微幅偏跌"
        elif median > -1.5:
            points = w * 0.18
            tag = "温和普跌"
        else:
            points = w * 0.05
            tag = "普跌加速"
        emotion_score += points
        breakdown.append({"factor": "median_change", "value": round(median, 2), "points": round(points, 1), "tag": tag})

    # Factor 5: Market technical score contribution (weight: 10 points)
    if enough:
        w = 10.0
        max_possible += w
        clamped = max(-10.0, min(10.0, score))
        points = w * (clamped + 10.0) / 20.0  # map [-10,10] to [0,w]
        emotion_score += points
        breakdown.append({"factor": "technical_score", "value": round(score, 1), "points": round(points, 1), "tag": f"技术评分{score:+.0f}"})

    # --- Six-Dimensional Timing Risk Layer (六维择时) ---
    breadth_vel = _breadth_velocity_score(ratio, prev_breadth_ratio, weight=8.0)
    resonance = _cross_index_resonance_score(indices or [], weight=10.0)
    hb_weak = _high_beta_weakness_score(indices or [], weight=8.0)

    # Factor 6: Breadth velocity (weight: 8) — Signal 3: Breadth Decay
    if breadth_vel.get("available"):
        max_possible += 8.0
        emotion_score += breadth_vel["points"]
        breakdown.append({"factor": "breadth_velocity", "value": breadth_vel.get("delta"), "points": breadth_vel["points"], "tag": breadth_vel["tag"]})

    # Factor 7: Cross-index resonance (weight: 10) — Signal 5: Cross-index Resonance
    if resonance.get("available"):
        max_possible += 10.0
        emotion_score += resonance["points"]
        breakdown.append({"factor": "cross_index_resonance", "value": f"{resonance.get('declining',0)}/{resonance.get('monitored',0)}跌", "points": resonance["points"], "tag": resonance["tag"], "detail": resonance.get("detail", "")})

    # Factor 8: High-beta weakness (weight: 8) — Signal 2: Leader Sector Breakdown
    if hb_weak.get("available"):
        max_possible += 8.0
        emotion_score += hb_weak["points"]
        breakdown.append({"factor": "high_beta_weakness", "value": hb_weak.get("diff_vs_hs300"), "points": hb_weak["points"], "tag": hb_weak["tag"], "detail": hb_weak.get("detail", "")})

    # Factor 9: Overseas shock (weight: 8) — Signal 6: Global Shock
    oseas = _overseas_shock_score(overseas, weight=8.0)
    if oseas.get("available"):
        max_possible += 8.0
        emotion_score += oseas["points"]
        breakdown.append({"factor": "overseas_shock", "value": f"严重{oseas.get('severe_count',0)}/预警{oseas.get('warning_count',0)}", "points": oseas["points"], "tag": oseas["tag"], "detail": oseas.get("detail", "")})

    # Factor 10: Crowding fragility (weight: 6) — Signal 1: Crowding Fragility
    crowd = _crowding_fragility_score(index_kline, ratio, weight=6.0)
    if crowd.get("available"):
        max_possible += 6.0
        emotion_score += crowd["points"]
        breakdown.append({"factor": "crowding_fragility", "value": crowd.get("pct_from_high"), "points": crowd["points"], "tag": crowd["tag"], "detail": crowd.get("detail", "")})

    # Factor 11: Liquidity compression (weight: 7) — Signal 4: Liquidity Compression
    liquid = _liquidity_compression_score(index_kline, weight=7.0)
    if liquid.get("available"):
        max_possible += 7.0
        emotion_score += liquid["points"]
        breakdown.append({"factor": "liquidity_compression", "value": liquid.get("consecutive_down"), "points": liquid["points"], "tag": liquid["tag"], "detail": liquid.get("detail", "")})

    # Factor 12: Retail sentiment extreme (weight: 6) — 108选6 Factor 3: Retail Flow
    retail = _retail_extreme_score(retail_sentiment, weight=6.0)
    if retail.get("available"):
        max_possible += 6.0
        emotion_score += retail["points"]
        breakdown.append({"factor": "retail_extreme", "value": retail.get("extreme_count"), "points": retail["points"], "tag": retail["tag"]})

    # Normalize to 0-100 when some factors are missing
    normalized_score = (emotion_score / max_possible * 100.0) if max_possible > 0 else 50.0

    evidence = [f"breadth={ratio:.1%}" if ratio is not None else "breadth=unknown",
                f"sample={sample_size}", f"limit_up={limit_up},limit_down={limit_down}",
                f"strong_up={positive},strong_down={negative}",
                f"median={median:+.2f}%" if median is not None else "median=unknown",
                f"resonance={resonance.get('declining',0)}/{resonance.get('monitored',0)}同步跌" if resonance.get("available") else "resonance=unknown",
                f"hb_weakness={hb_weak.get('diff_vs_hs300','--')}%" if hb_weak.get("available") else "hb_weakness=unknown",
                f"breadth_vel={breadth_vel.get('delta','--')}pp" if breadth_vel.get("available") and breadth_vel.get("delta") is not None else "",
                f"overseas={oseas.get('severe_count',0)}重/{oseas.get('warning_count',0)}警" if oseas.get("available") else "overseas=unknown",
                f"crowd={crowd.get('tag','')}" if crowd.get("available") else "",
                f"liquid={liquid.get('volume_trend','')}" if liquid.get("available") else "",
                f"retail_extreme={retail.get('extreme_count',0)}行业" if retail.get("available") else "",
                f"emotion_score={normalized_score:.0f}"]

    # --- Phase classification from normalized score ---
    if not enough or sample_size < 100:
        phase, action = "冰点", "等待数据确认"
    elif normalized_score >= 85:
        phase, action = "高潮", "只做核心，严禁中位股"
    elif normalized_score >= 60:
        phase, action = "主升", "允许核心确认"
    elif normalized_score >= 42:
        phase, action = "复苏", "只做弱转强/回流"
    elif normalized_score >= 26:
        phase, action = "低迷", "轻仓试探，严格止损"
    elif normalized_score >= 14:
        phase, action = "退潮", "禁止开仓"
    else:
        phase, action = "冰点", "等待首个有效回流"
    return {
        "phase": phase, "action": action, "breadth_ratio": ratio, "score": score,
        "sample_size": sample_size, "limit_up_count": limit_up, "limit_down_count": limit_down,
        "strong_up_count": positive, "strong_down_count": negative,
        "strong_up_ratio": strong_up_ratio, "strong_down_ratio": strong_down_ratio,
        "median_change": median_change, "confidence": confidence, "evidence": evidence,
        "emotion_score": round(normalized_score, 1),
        "emotion_breakdown": breakdown,
        "timing_risk": {
            "breadth_velocity": breadth_vel,
            "cross_index_resonance": resonance,
            "high_beta_weakness": hb_weak,
            "overseas_shock": oseas,
            "crowding_fragility": crowd,
            "liquidity_compression": liquid,
            "retail_extreme": retail,
        },
    }


def dragon_board_strength(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rank industry/concept groups from the available full-market snapshot."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in snapshots:
        board = str(_first(row, "concept", "theme", "industry", "sector", "board") or "").strip()
        if board:
            groups.setdefault(board, []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for name, rows in groups.items():
        changes = [_number(row.get("change_pct")) or 0.0 for row in rows if row.get("change_pct") not in (None, "")]
        result[name] = {
            "name": name, "count": len(rows),
            "avg_change_pct": round(fmean(changes), 2) if changes else None,
            "up_ratio": round(sum(1 for value in changes if value > 0) / max(1, len(changes)), 3),
            "coherence": (lambda c: round(1.0 - (pstdev(c) / (abs(fmean(c)) + 0.01)), 3) if len(c) >= 3 and fmean(c) != 0 else None)(changes) if changes else None,
            "leader_concentration": round(max(changes) / (sum(abs(c) for c in changes) + 0.01), 3) if changes else None,
            "diffusion": round(sum(1 for c in changes if c > 0) / max(1, len(changes)), 3) if changes else None,
            "leader_code": max(rows, key=lambda row: _number(row.get("change_pct")) or 0.0).get("code", "") if rows else "",
        }
    ranked = sorted(result.values(), key=lambda item: ((_number(item.get("avg_change_pct")) or -99), (_number(item.get("up_ratio")) or 0)), reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
        item["total_boards"] = len(ranked)
    return result


# ── Portfolio Beta Alignment ────────────────────────────────────────

STRATEGY_PROFILES: dict[str, dict[str, float]] = {
    "default": {
        "breadth_velocity": 1.0, "cross_index_resonance": 1.0,
        "high_beta_weakness": 1.0, "overseas_shock": 1.0,
        "crowding_fragility": 1.0, "liquidity_compression": 1.0,
    },
    "半导体": {
        "breadth_velocity": 0.8, "cross_index_resonance": 1.0,
        "high_beta_weakness": 1.3, "overseas_shock": 1.5,
        "crowding_fragility": 1.2, "liquidity_compression": 0.8,
    },
    "科技成长": {
        "breadth_velocity": 0.9, "cross_index_resonance": 1.1,
        "high_beta_weakness": 1.4, "overseas_shock": 1.3,
        "crowding_fragility": 1.1, "liquidity_compression": 0.9,
    },
    "小盘股": {
        "breadth_velocity": 1.3, "cross_index_resonance": 0.9,
        "high_beta_weakness": 0.7, "overseas_shock": 0.6,
        "crowding_fragility": 0.8, "liquidity_compression": 1.4,
    },
    "价值蓝筹": {
        "breadth_velocity": 0.8, "cross_index_resonance": 1.0,
        "high_beta_weakness": 0.5, "overseas_shock": 0.7,
        "crowding_fragility": 0.6, "liquidity_compression": 1.0,
    },
    "事件驱动": {
        "breadth_velocity": 1.0, "cross_index_resonance": 1.2,
        "high_beta_weakness": 1.0, "overseas_shock": 0.8,
        "crowding_fragility": 0.9, "liquidity_compression": 0.9,
    },
}


def get_beta_aligned_weights(strategy_profile: str = "default") -> dict[str, float]:
    """Return timing factor weight multipliers for a given strategy profile."""
    return STRATEGY_PROFILES.get(strategy_profile, STRATEGY_PROFILES["default"])


def _realized_volatility(closes: Sequence[float], period: int = 20) -> float | None:
    """Annualized realized volatility from daily close returns."""
    if len(closes) < period + 1:
        return None
    returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(len(closes) - period, len(closes))]
    if len(returns) < 2:
        return None
    daily_std = pstdev(returns)
    return daily_std * sqrt(252) if daily_std > 0 else None


def compute_adaptive_position(
    closes: Sequence[float],
    emotion_score: float,
    *,
    target_annual_vol: float = 0.18,
    vol_floor: float = 0.05,
    position_cap: float = 1.0,
) -> dict[str, Any]:
    """Compute adaptive position sizing: 最终仓位 = 波动率目标仓位 × 市场状态乘数.

    Based on Simon's 六维择时 position management:
    - Vol_target_pos = target_vol / realized_vol (capped at 1.0)
    - State_mult = emotion_score / 100 (clamped to [0.05, 1.0])
    - Final = vol_target_pos × state_mult

    Returns a dict with both percentage and explanatory breakdown.
    """
    realized_vol = _realized_volatility(closes)
    if realized_vol is None or realized_vol <= 0:
        # Fallback: use static phase-based position
        return {
            "adaptive_position_pct": None,
            "adaptive_position": "N/A（波动率数据不足）",
            "realized_vol": None,
            "vol_target_position": None,
            "state_multiplier": None,
            "available": False,
        }

    # Vol-target position: scale down when vol is high
    vol_target_pos = min(position_cap, target_annual_vol / realized_vol)
    # State multiplier: lower when market is risky
    state_mult = max(vol_floor, min(1.0, emotion_score / 100.0))
    # Final adaptive position
    adaptive_pct = vol_target_pos * state_mult

    return {
        "adaptive_position_pct": round(adaptive_pct * 100, 0),
        "adaptive_position": f"{adaptive_pct * 100:.0f}%",
        "realized_vol": round(realized_vol * 100, 1),
        "vol_target_position": round(vol_target_pos * 100, 0),
        "state_multiplier": round(state_mult, 2),
        "formula": f"{vol_target_pos*100:.0f}% × {state_mult:.2f} = {adaptive_pct*100:.0f}%",
        "available": True,
    }


def dragon_score(snapshot: Mapping[str, Any] | None, rows: Iterable[Mapping[str, Any]] | None,
                 phase: Mapping[str, Any] | None, board: Mapping[str, Any] | None,
                 *, leader_rank: int = 99, leader_count: int = 0) -> dict[str, Any]:
    """Independent 擒龙 decision: emotion -> mainline -> leader -> trigger."""
    snapshot = snapshot or {}
    normalized = _normalize_rows(rows)
    risk = evaluate_rulebook_risk(snapshot, normalized)
    closes = [item["close"] for item in normalized]
    latest = normalized[-1] if normalized else {}
    previous = normalized[-2] if len(normalized) > 1 else {}
    live_close = _number(snapshot.get("price"))
    live_change = _number(snapshot.get("change_pct"))
    live_quote_used = live_close is not None and live_change is not None and snapshot.get("change_pct") not in (None, "")
    latest_close = live_close if live_quote_used else (_number(latest.get("close")) or live_close or 0.0)
    prev_close = _number(snapshot.get("last_close")) if live_quote_used else _number(previous.get("close"))
    prev_close = prev_close or latest_close
    day_change = live_change if live_quote_used else ((latest_close / prev_close - 1) * 100 if prev_close else 0.0)
    indicator_closes = [*closes, latest_close] if live_quote_used else closes
    ma5 = fmean(indicator_closes[-5:]) if len(indicator_closes) >= 5 else None
    ma20 = fmean(indicator_closes[-20:]) if len(indicator_closes) >= 20 else None
    if live_quote_used and len(closes) >= 20 and closes[-20]:
        ret20 = (latest_close / closes[-20] - 1) * 100
    elif len(closes) >= 21 and closes[-21]:
        ret20 = (latest_close / closes[-21] - 1) * 100
    else:
        ret20 = _number(snapshot.get("change_20d_pct")) or 0.0
    if live_quote_used and len(closes) >= 2 and closes[-2]:
        prior_change = (closes[-1] / closes[-2] - 1) * 100
    else:
        prior_change = ((closes[-2] / closes[-3] - 1) * 100) if len(closes) >= 3 and closes[-3] else 0
    weak_to_strong = prior_change <= -2 and day_change >= 2 and (ma5 is None or latest_close >= ma5)
    first_divergence = prior_change >= 5 and -3 <= day_change <= 2 and (ma20 is None or latest_close >= ma20)
    reflow = bool(board and (_number(board.get("avg_change_pct")) or -99) >= 1 and latest_close >= (ma5 or latest_close) and day_change >= 0)
    # 价强量弱 (Price Strong, Volume Weak) — Factor 5 from 108选6 sector rotation
    price_strong = latest_close >= (ma5 or latest_close) and day_change > 0
    vr = _number(snapshot.get("volume_ratio")) or _number(snapshot.get("vr"))
    to = _number(snapshot.get("turnover_pct")) or _number(snapshot.get("turnover"))
    vol_moderate = (vr is None or vr < 2.5) and (to is None or to < 15)
    not_extended = ret20 < 30
    price_strong_vol_weak = price_strong and vol_moderate and not_extended and (ma20 is None or latest_close >= ma20)
    # 烂板回封 (Board-break recovery) — leader microstructure trigger
    intraday_drawdown = (lowest - latest_close) / max(0.01, highest) * 100 if (highest := max(latest.get("high", latest_close), latest_close)) > 0 and (lowest := min(latest.get("low", latest_close), latest_close)) > 0 else 0
    board_break_recovery = (intraday_drawdown <= -5 if intraday_drawdown else False) and close_position >= 0.75 if (close_position := ((latest_close - lowest) / (highest - lowest)) if highest > lowest else 0.5) >= 0.75 else False
    trigger = "弱转强" if weak_to_strong else "首次分歧" if first_divergence else "板块回流" if reflow else "烂板回封" if board_break_recovery else "价强量弱" if price_strong_vol_weak else "无有效买点"
    resilience = 80 if ret20 >= 10 and day_change >= -1 else 55 if ret20 >= 0 else 25
    board_change = _number(board.get("avg_change_pct")) if board else None
    board_up_ratio = _number(board.get("up_ratio")) if board else None
    mainline = min(100, max(0, 50 + (board_change if board_change is not None else -5) * 8 + (board_up_ratio if board_up_ratio is not None else 0.5) * 30)) if board else 0
    # Fund consensus boost: stocks held by many funds get +5-15 bonus
    fund_consensus = (phase or {}).get("fund_consensus") if isinstance(phase, Mapping) else None
    consensus_boost = 0
    if fund_consensus and isinstance(fund_consensus, Mapping):
        stock_code = str(snapshot.get("code", ""))
        fc = fund_consensus.get(stock_code)
        if fc and isinstance(fc, Mapping):
            consensus_boost = min(15, max(0, fc.get("fund_count", 0) * 2))
    leader = max(0, 100 - max(0, leader_rank - 1) * 12) if leader_count else 0
    role = "龙头" if leader_rank == 1 else "中军" if leader_rank <= 3 else "前排梯队" if leader_rank <= 6 else "中位股"
    topping_risk = leader_rank == 1 and ret20 >= 40 and day_change < 0
    middle_negative = 2 <= leader_rank <= 10 and day_change <= -5
    negative_feedback = ret20 < -12 or topping_risk or middle_negative or (ma20 is not None and latest_close < ma20 and day_change < -3)
    phase_name = str((phase or {}).get("phase", "冰点"))
    board_rank = int(_number((board or {}).get("rank")) or (1 if board else 999))
    mainline_veto = board_rank > 10
    # 冰点 → hard veto; 退潮 → hard veto; 低迷 → observation only (no push)
    phase_veto = phase_name in {"冰点", "退潮"}
    veto = phase_veto or negative_feedback or not risk["eligible"] or not normalized or not board or mainline_veto
    score = round(mainline * .30 + leader * .30 + resilience * .15 + (100 if trigger != "无有效买点" else 15) * .25, 1)
    # ---- Calibrated v2.0 scoring (2026-07-23) ----
    # Evidence: score bands 90+ avg 2.24% < 50-70 avg 9.32%. High scores overfitted to board momentum.
    # Five components: micro quality 20% + dampened mainline 25% + capped leader 20% + trigger quality 20% + continuous resilience 15%
    import math
    mq_score = (microstructure_quality(normalized)).get("score", 50)
    # Dampen extreme mainline: above 90 gets diminishing returns (board too hot)
    mainline_dampened = mainline if mainline <= 85 else 85 + (mainline - 85) * 0.35 if mainline <= 95 else 88.5 + (mainline - 95) * 0.15
    # Cap leader at 75 — weak board #1 shouldn't dominate
    leader_capped = min(75, leader)
    # Trigger quality weighting (backtest shows 弱转强 outperforms 板块回流)
    _trigger_weights = {"弱转强": 100, "烂板回封": 90, "首次分歧": 80, "板块回流": 60, "价强量弱": 50, "无有效买点": 15}
    trigger_quality = _trigger_weights.get(trigger, 15)
    # Continuous resilience: smooth instead of 80/55/25
    resilience_continuous = min(100, max(10, 50 + ret20 * (2 if ret20 >= 0 else 2.5)))
    score = round(mq_score * .20 + mainline_dampened * .25 + leader_capped * .20 + trigger_quality * .20 + resilience_continuous * .15, 1)
    score = min(100, score + consensus_boost)
    push_ok = not veto and trigger != "无有效买点" and score >= 72 and (
        phase_name in {"复苏", "主升"}
        or (phase_name == "高潮" and leader_rank == 1)
        or (phase_name == "低迷" and leader_rank == 1 and score >= 82)
    )
    position = {
        "冰点": "0%", "退潮": "0%", "低迷": "5%-15%", "复苏": "10%-30%",
        "主升": "30%-60%", "高潮": "0%-20%，仅核心",
    }.get(phase_name, "0%-10%")
    # Adaptive position: 最终仓位 = 波动率目标 × 市场状态乘数
    emotion_score_val = float((phase or {}).get("emotion_score", 50.0))
    adaptive_pos = compute_adaptive_position(closes, emotion_score_val)
    return {
        "strategy_version": DRAGON_VERSION, "mode": DRAGON_MODE, "score": score,
        "label": "擒龙推送" if push_ok else "擒龙观察" if not veto else "擒龙否决",
        "eligible": not veto, "push_eligible": push_ok, "risk": risk, "position": position,
        "adaptive_position": adaptive_pos,
        "emotion": dict(phase or {}), "board": dict(board or {}),
        "leader": {"rank": leader_rank, "count": leader_count, "score": round(leader, 1), "role": role, "topping_risk": topping_risk, "middle_negative_feedback": middle_negative, "drive_ratio": board_up_ratio},
        "triggers": {"weak_to_strong": weak_to_strong, "first_divergence": first_divergence, "reflow": reflow, "price_strong_vol_weak": price_strong_vol_weak, "board_break_recovery": board_break_recovery, "selected": trigger},
        "negative_feedback": negative_feedback,
        "breakdown": {"emotion": phase_name, "mainline": round(mainline, 1), "leader": round(leader, 1), "resilience": resilience, "trigger": trigger},
        "micro_quality": microstructure_quality(normalized),
        "evidence": [f"情绪周期：{phase_name}", f"主线板块：{board.get('name') if board else '缺失'}", f"地位：{role}，区间排名{leader_rank}/{leader_count or '--'}", f"买点：{trigger}"],
        "data_coverage": {"kline_points": len(normalized), "live_quote_used": live_quote_used, "board_available": bool(board), "leader_available": bool(leader_count)},
    }


def _snapshot_number(snapshot: Mapping[str, Any], *names: str) -> float | None:
    return _number(_first(snapshot, *names))


def _snapshot_bool(snapshot: Mapping[str, Any], *names: str) -> bool | None:
    value = _first(snapshot, *names)
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "是", "涨停", "首板"}:
            return True
        if lowered in {"0", "false", "no", "n", "否"}:
            return False
    return bool(value)


def _time_minutes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace("：", ":")
    try:
        if ":" in text:
            hour, minute = text.split(":", 1)
            return int(hour) * 60 + int(minute[:2])
        digits = "".join(char for char in text if char.isdigit())
        if len(digits) >= 4:
            return int(digits[-4:-2]) * 60 + int(digits[-2:])
    except ValueError:
        return None
    return None


def _optional_result(
    name: str,
    value: Any,
    predicate: bool | None,
    pass_reason: str,
    fail_reason: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    if predicate is None:
        passed = bool(config.get("missing_optional_pass", True))
        reason = "字段缺失，按可选项放行" if passed else "字段缺失"
    else:
        passed = bool(predicate)
        reason = pass_reason if passed else fail_reason
    return {"name": name, "pass": passed, "reason": reason, "value": value}


def evaluate_auction_gates(
    snapshot: Mapping[str, Any] | None,
    rows: Iterable[Mapping[str, Any]] | None,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate seventeen named, auditable hard gates for a first-board candidate."""

    snapshot = snapshot or {}
    cfg = _deep_merge(DEFAULT_CONFIG, config)
    gates_cfg = cfg["gates"]
    bundle = _indicator_bundle(rows)
    latest = bundle.rows[-1] if bundle.rows else None
    previous_close = _snapshot_number(snapshot, "prev_close", "previous_close", "昨收", "昨收价")
    if previous_close is None and latest:
        previous_close = latest["close"]
    auction_price = _snapshot_number(snapshot, "auction_price", "open", "open_price", "竞价价", "开盘价")
    gap_pct = _snapshot_number(snapshot, "gap_pct", "gap", "竞价涨幅", "高开幅度")
    if gap_pct is None and auction_price is not None and previous_close:
        gap_pct = (auction_price / previous_close - 1.0) * 100.0
    code = str(_first(snapshot, "code", "symbol", "股票代码", "代码") or "")
    name = str(_first(snapshot, "name", "stock_name", "股票名称", "名称") or "")
    market_cap = _snapshot_number(snapshot, "market_cap", "total_market_cap", "总市值", "市值")
    auction_amount = _snapshot_number(snapshot, "auction_amount", "amount", "竞价额", "竞价金额")
    volume_ratio = _snapshot_number(snapshot, "volume_ratio", "vr", "量比")
    turnover = _snapshot_number(snapshot, "turnover", "turnover_pct", "turnover_rate", "换手率")
    amplitude = _snapshot_number(snapshot, "amplitude", "amplitude_pct", "振幅")
    if amplitude is None and latest and latest["close"]:
        amplitude = (latest["high"] - latest["low"]) / latest["close"] * 100.0
    is_st = _snapshot_bool(snapshot, "is_st", "st", "是否ST")
    is_suspended = _snapshot_bool(snapshot, "is_suspended", "suspended", "停牌")
    is_first_board = _snapshot_bool(snapshot, "is_first_board", "first_board", "首板")
    board_count = _snapshot_number(snapshot, "board_count", "consecutive_boards", "连板数")
    seal_time = _first(snapshot, "seal_time", "first_seal_time", "封板时间", "首次封板时间")
    sector_resonance = _snapshot_number(snapshot, "sector_resonance", "industry_resonance", "板块共振")
    previous_gain = _snapshot_number(snapshot, "previous_gain_pct", "prev_pct", "前日涨幅")
    if previous_gain is None and len(bundle.rows) >= 2:
        previous_gain = (bundle.closes[-1] / bundle.closes[-2] - 1.0) * 100.0
    consecutive_up = _snapshot_number(snapshot, "consecutive_up_days", "连续上涨天数")
    if consecutive_up is None and bundle.closes:
        consecutive_up = 0
        for index in range(len(bundle.closes) - 1, 0, -1):
            if bundle.closes[index] > bundle.closes[index - 1]:
                consecutive_up += 1
            else:
                break
    j_value = bundle.j[-1] if bundle.j else None

    results: list[dict[str, Any]] = []
    required_ok = auction_price is not None and auction_price > 0 and previous_close is not None and previous_close > 0
    results.append({"name": "required_data", "pass": required_ok, "reason": "竞价价和昨收完整" if required_ok else "缺少有效竞价价或昨收", "value": {"auction_price": auction_price, "prev_close": previous_close}})
    eligible = (
        not bool(is_st)
        and not bool(is_suspended)
        and "退" not in name
        and not is_star_security(code, name)
        and is_a_share_security(code, name)
    )
    results.append({"name": "eligible_security", "pass": eligible, "reason": "非 ST、非退市且未停牌" if eligible else "ST、退市或停牌证券", "value": {"is_st": is_st, "is_suspended": is_suspended, "name": name}})
    allowed_prefixes = tuple(gates_cfg["allowed_code_prefixes"])
    board_ok = (code.startswith(allowed_prefixes) and not is_star_security(code, name)) if code else None
    results.append(_optional_result("board_scope", code or None, board_ok, "属于A股全市场范围且已排除科创板", "不属于A股范围或属于科创板", cfg))
    first_board_ok = None if is_first_board is None and board_count is None else bool(is_first_board if is_first_board is not None else board_count == 1) and (board_count is None or board_count <= gates_cfg["max_board_count"])
    results.append(_optional_result("previous_first_board", {"is_first_board": is_first_board, "board_count": board_count}, first_board_ok, "前一交易日为首板", "非首板或已连板", cfg))
    price_valid = auction_price is not None and auction_price > 0 and (previous_close is None or auction_price <= previous_close * 1.25)
    results.append({"name": "auction_price_valid", "pass": price_valid, "reason": "竞价价格有效" if price_valid else "竞价价格缺失或异常", "value": auction_price})
    price_range_ok = None if auction_price is None else gates_cfg["min_price"] <= auction_price <= gates_cfg["max_price"]
    results.append(_optional_result("price_range", auction_price, price_range_ok, "股价位于允许区间", "股价超出允许区间", cfg))
    cap_ok = None if market_cap is None else gates_cfg["min_market_cap"] <= market_cap <= gates_cfg["max_market_cap"]
    results.append(_optional_result("market_cap_range", market_cap, cap_ok, "市值位于允许区间", "市值超出允许区间", cfg))
    gap_ok = None if gap_pct is None else gates_cfg["min_gap_pct"] <= gap_pct <= gates_cfg["max_gap_pct"]
    results.append(_optional_result("gap_range", _round(gap_pct), gap_ok, "竞价 Gap 位于六区间有效范围", "真低开水下或高开陷阱区", cfg))
    amount_ok = None if auction_amount is None else auction_amount >= gates_cfg["min_auction_amount"]
    results.append(_optional_result("auction_liquidity", auction_amount, amount_ok, "竞价成交额达标", "竞价成交额不足", cfg))
    vr_ok = None if volume_ratio is None else gates_cfg["min_volume_ratio"] <= volume_ratio <= gates_cfg["max_volume_ratio"]
    results.append(_optional_result("volume_ratio", volume_ratio, vr_ok, "量比位于复合阈值范围", "量比过低或过热", cfg))
    turnover_ok = None if turnover is None else gates_cfg["min_turnover_pct"] <= turnover <= gates_cfg["max_turnover_pct"]
    results.append(_optional_result("turnover", turnover, turnover_ok, "换手率位于有效范围", "换手率过低或过高", cfg))
    amplitude_ok = None if amplitude is None else 0 <= amplitude <= gates_cfg["max_amplitude_pct"]
    results.append(_optional_result("amplitude", _round(amplitude), amplitude_ok, "振幅未触发异常过滤", "振幅过大", cfg))
    momentum_ok = (
        (j_value is None or j_value <= gates_cfg["max_kdj_j"])
        and (previous_gain is None or previous_gain <= gates_cfg["max_previous_gain_pct"])
        and (consecutive_up is None or consecutive_up <= gates_cfg["max_consecutive_up_days"])
    )
    results.append({"name": "momentum_sanity", "pass": momentum_ok, "reason": "J 值、前日涨幅和连涨组合未过热" if momentum_ok else "高位 J 值或连续暴涨组合过热", "value": {"kdj_j": _round(j_value), "previous_gain_pct": _round(previous_gain), "consecutive_up_days": consecutive_up}})
    latest_seal = _time_minutes(gates_cfg["latest_seal_time"])
    seal_minutes = _time_minutes(seal_time)
    quality_ok = None if seal_minutes is None and sector_resonance is None else bool((seal_minutes is not None and latest_seal is not None and seal_minutes <= latest_seal) or (sector_resonance is not None and sector_resonance > 0))
    results.append(_optional_result("first_board_quality", {"seal_time": seal_time, "sector_resonance": sector_resonance}, quality_ok, "封板时间或板块共振达标", "封板过晚且无板块共振", cfg))
    # Gate 15: Auction overheat — gap too high AND volume too high = 已抢跑
    overheat = None if gap_pct is None or volume_ratio is None else not (gap_pct >= 5.0 and volume_ratio >= 4.0)
    results.append(_optional_result("auction_overheat", {"gap_pct": _round(gap_pct), "volume_ratio": volume_ratio}, overheat, "竞价未过热（未同时高开+爆量）", "竞价过热：高开且爆量，资金已抢跑", cfg))

    # Gate 16 (NEW): Auction scramble — morning grab detection
    # Triggered when gap > 1% AND volume_ratio > 2 AND auction_amount > 10M
    # This signals institutional money flowing in during call auction.
    scramble_ok = None
    if gap_pct is not None and volume_ratio is not None and auction_amount is not None:
        is_scramble = gap_pct > 1.0 and volume_ratio > 2.0 and auction_amount > 10_000_000
        scramble_ok = True  # scramble is a positive signal, not a gate failure
        scramble_tag = "检测到竞价抢筹信号：高开+放量+大额竞价" if is_scramble else "未检测到明显抢筹"
    else:
        scramble_tag = "竞价数据不全，无法判断抢筹"
    results.append(_optional_result("auction_scramble",
        {"gap": _round(gap_pct), "vr": volume_ratio, "amount": auction_amount},
        scramble_ok, scramble_tag, "竞价数据异常", cfg))

    # Gate 17 (NEW): 停机坪 relay — gap continuation after a limit-up day
    # Pattern: yesterday was a limit-up (first board), today gaps up 1-5% with
    # reasonable volume (not overheated). This is the "parking apron" strategy
    # from the comprehensive stock selection framework.
    apron_ok = None
    if is_first_board and gap_pct is not None and volume_ratio is not None:
        apron_detected = 1.0 <= gap_pct <= 5.0 and 1.5 <= volume_ratio <= 5.0
        apron_ok = True  # apron is a bonus signal, not a hard gate
        apron_tag = "停机坪接力确认：首板次日竞价高开1-5%且量能合理" if apron_detected else "首板次日竞价不符合停机坪模式"
    elif not is_first_board:
        apron_tag = "非首板，不适用停机坪检测"
    else:
        apron_tag = "竞价数据不全，无法判断停机坪"
    results.append(_optional_result("parking_apron",
        {"is_first_board": is_first_board, "gap": _round(gap_pct), "vr": volume_ratio},
        apron_ok, apron_tag, "竞价数据异常", cfg))

    assert len(results) == 17
    return results


def _trapezoid(value: float, shape: Sequence[float]) -> float:
    a, b, c, d = (float(item) for item in shape)
    if value <= a or value >= d:
        return 0.0
    if b <= value <= c:
        return 1.0
    if value < b:
        return _clamp((value - a) / (b - a)) if b != a else 1.0
    return _clamp((d - value) / (d - c)) if d != c else 1.0


def _market_context(context: Mapping[str, Any] | float | int | None) -> tuple[int, float, str]:
    if isinstance(context, Mapping):
        score = int(_number(_first(context, "score", "market_score")) or 0)
        coefficient = _number(_first(context, "coefficient", "coeff", "market_coefficient"))
        coefficient = coefficient if coefficient is not None else 1.0 + score / 100.0
        label = str(_first(context, "label", "market_label") or "中性")
    elif isinstance(context, (int, float)) and isfinite(float(context)):
        score = int(_clamp(float(context), -10, 10))
        coefficient = 1.0 + score / 100.0
        label = "强多" if score >= 6 else "偏多" if score >= 2 else "强空" if score <= -6 else "偏空" if score <= -2 else "中性"
    else:
        score, coefficient, label = 0, 1.0, "中性"
    return score, _clamp(float(coefficient), 0.90, 1.10), label


def auction_score(
    snapshot: Mapping[str, Any] | None,
    rows: Iterable[Mapping[str, Any]] | None,
    market_context: Mapping[str, Any] | float | int | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable ten-dimensional auction score on a 0..100 scale."""

    snapshot = snapshot or {}
    cfg = _deep_merge(DEFAULT_CONFIG, config)
    auction_cfg = cfg["auction"]
    bundle = _indicator_bundle(rows)
    tech = _technical_from_bundle(bundle)
    gates = evaluate_auction_gates(snapshot, rows, cfg)
    latest = bundle.rows[-1] if bundle.rows else None
    previous_close = _snapshot_number(snapshot, "prev_close", "previous_close", "昨收", "昨收价") or (latest["close"] if latest else None)
    auction_price = _snapshot_number(snapshot, "auction_price", "open", "open_price", "竞价价", "开盘价")
    gap = _snapshot_number(snapshot, "gap_pct", "gap", "竞价涨幅", "高开幅度")
    if gap is None and auction_price is not None and previous_close:
        gap = (auction_price / previous_close - 1.0) * 100.0
    volume_ratio = _snapshot_number(snapshot, "volume_ratio", "vr", "量比")
    auction_amount = _snapshot_number(snapshot, "auction_amount", "amount", "竞价额", "竞价金额")
    turnover = _snapshot_number(snapshot, "turnover", "turnover_pct", "turnover_rate", "换手率")
    amplitude = _snapshot_number(snapshot, "amplitude", "amplitude_pct", "振幅")
    if amplitude is None and latest and latest["close"]:
        amplitude = (latest["high"] - latest["low"]) / latest["close"] * 100.0
    missing_score = float(auction_cfg["missing_dimension_score"])

    dimension_values: dict[str, tuple[float, Any, str]] = {}
    dimension_values["gap"] = (missing_score if gap is None else _trapezoid(gap, auction_cfg["gap_shape"]), _round(gap), "Gap 六区间连续评分")
    dimension_values["volume_ratio"] = (missing_score if volume_ratio is None else _trapezoid(volume_ratio, auction_cfg["volume_ratio_shape"]), volume_ratio, "量比复合区间")
    if auction_amount is None or auction_amount <= 0:
        amount_norm = missing_score
    else:
        minimum = max(1.0, float(cfg["gates"]["min_auction_amount"]))
        target = max(minimum * 1.01, float(auction_cfg["auction_amount_target"]))
        amount_norm = _clamp(log10(max(auction_amount, minimum) / minimum + 1.0) / log10(target / minimum + 1.0))
    dimension_values["auction_amount"] = (amount_norm, auction_amount, "竞价成交额对数归一")
    dimension_values["turnover"] = (missing_score if turnover is None else _trapezoid(turnover, auction_cfg["turnover_shape"]), turnover, "换手率有效区间")
    dimension_values["amplitude"] = (missing_score if amplitude is None else _trapezoid(amplitude, auction_cfg["amplitude_shape"]), _round(amplitude), "振幅有效区间")
    ma_points = tech["components"]["ma5"] + tech["components"]["ma10"]
    dimension_values["ma_structure"] = ((_clamp((ma_points + 6.0) / 12.0)), ma_points, "MA5/MA10 位置与斜率")
    kdj_points = tech["components"]["kdj"]
    kdj_norm = _clamp((kdj_points + 3.0) / 6.0)
    if bundle.j and bundle.j[-1] > 110.0:
        kdj_norm = min(kdj_norm, 0.35)
    dimension_values["kdj"] = (kdj_norm, _round(bundle.j[-1]) if bundle.j else None, "K/D 关系、J 位置与斜率")
    macd_points = tech["components"]["macd"]
    dimension_values["macd"] = (_clamp((macd_points + 3.0) / 6.0), macd_points, "DIF/DEA、零轴和柱动量")
    boll_norm = missing_score
    boll_value: Any = None
    if bundle.rows:
        index = len(bundle.rows) - 1
        mid = bundle.boll["middle"][index]
        upper = bundle.boll["upper"][index]
        lower = bundle.boll["lower"][index]
        close = bundle.closes[index]
        if mid is not None and upper is not None and lower is not None:
            if close < lower:
                boll_norm = 0.15
            elif close < mid:
                boll_norm = 0.35 + 0.25 * _clamp((close - lower) / max(mid - lower, 1e-12))
            elif close <= upper:
                position = _clamp((close - mid) / max(upper - mid, 1e-12))
                boll_norm = 0.70 + 0.30 * (1.0 - abs(position - 0.65) / 0.65)
            else:
                boll_norm = 0.60 if macd_points > 0 else 0.25
            boll_value = {"close": _round(close), "lower": _round(lower), "mid": _round(mid), "upper": _round(upper)}
    dimension_values["boll"] = (_clamp(boll_norm), boll_value, "BOLL 位置、突破和极端超涨")
    seal_time = _time_minutes(_first(snapshot, "seal_time", "first_seal_time", "封板时间", "首次封板时间"))
    resonance = _snapshot_number(snapshot, "sector_resonance", "industry_resonance", "板块共振")
    sector_limit_ups = _snapshot_number(snapshot, "sector_limit_up_count", "industry_limit_up_count", "板块涨停数")
    quality = 0.45
    if seal_time is not None:
        quality += 0.25 if seal_time <= 10 * 60 + 30 else 0.15 if seal_time <= 13 * 60 + 30 else 0.0
    if resonance is not None:
        quality += 0.25 * _clamp(resonance if resonance <= 1 else resonance / 10.0)
    elif sector_limit_ups is not None:
        quality += 0.25 * _clamp(sector_limit_ups / 4.0)
    board_count = _snapshot_number(snapshot, "board_count", "consecutive_boards", "连板数")
    if board_count is not None and board_count != 1:
        quality -= 0.30
    dimension_values["board_sector"] = (_clamp(quality), {"seal_time": _first(snapshot, "seal_time", "first_seal_time", "封板时间", "首次封板时间"), "sector_resonance": resonance, "sector_limit_up_count": sector_limit_ups}, "首板质量与板块共振")

    configured_weights = auction_cfg["weights"]
    expected_dimensions = tuple(DEFAULT_CONFIG["auction"]["weights"].keys())
    weights = {name: max(0.0, float(configured_weights.get(name, 0.0))) for name in expected_dimensions}
    weight_total = sum(weights.values()) or 1.0
    breakdown: dict[str, dict[str, Any]] = {}
    raw_score = 0.0
    evidence: list[str] = []
    for name in expected_dimensions:
        normalized, value, reason = dimension_values[name]
        max_points = weights[name] / weight_total * 100.0
        points = _clamp(normalized) * max_points
        raw_score += points
        breakdown[name] = {"score": round(points, 2), "max_score": round(max_points, 2), "normalized": round(_clamp(normalized), 4), "value": value, "reason": reason}
        evidence.append(f"{name}: {points:.1f}/{max_points:.1f} - {reason}")

    market_value, coefficient, market_label = _market_context(market_context)
    adjusted_score = round(_clamp(raw_score * coefficient, 0.0, 100.0), 1)
    failed_gates = [gate for gate in gates if not gate["pass"]]
    if failed_gates:
        zone = "excluded"
    elif market_value <= int(auction_cfg["market_block_score"]):
        zone = "blocked"
    elif adjusted_score >= float(auction_cfg["zone_1_min"]):
        zone = "zone1"
    elif adjusted_score >= float(auction_cfg["zone_2_min"]):
        zone = "zone2"
    else:
        zone = "watch"
    return {
        "strategy_version": str(cfg.get("strategy_version", STRATEGY_VERSION)),
        "score": adjusted_score,
        "raw_score": round(raw_score, 1),
        "zone": zone,
        "eligible": not failed_gates and zone in {"zone1", "zone2"},
        "market": {"score": market_value, "label": market_label, "coefficient": round(coefficient, 3)},
        "breakdown": breakdown,
        "gates": gates,
        "failed_gates": [gate["name"] for gate in failed_gates],
        "evidence": evidence,
        "technical": tech,
    }


def _phase_label(bundle: _IndicatorBundle, index: int) -> tuple[str, list[str]]:
    close = bundle.closes[index]
    previous = bundle.closes[index - 1] if index > 0 else close
    daily_return = (close / previous - 1.0) * 100.0 if previous else 0.0
    start = max(0, index - 4)
    five_day_return = (close / bundle.closes[start] - 1.0) * 100.0 if bundle.closes[start] else 0.0
    volume_ratio = bundle.volume_ratio[5][index] or 1.0
    ma5 = bundle.ma[5][index]
    ma10 = bundle.ma[10][index]
    ma20 = bundle.ma[20][index]
    hist = bundle.macd_hist[index]
    previous_hist = bundle.macd_hist[index - 1] if index > 0 else hist
    crossed_ma5 = index > 0 and ma5 is not None and bundle.ma[5][index - 1] is not None and previous <= bundle.ma[5][index - 1] and close > ma5
    evidence = [f"日涨跌 {daily_return:+.2f}%", f"5日变化 {five_day_return:+.2f}%", f"量比 {volume_ratio:.2f}"]

    recent_start = max(0, index - 19)
    recent_high = max(bundle.highs[recent_start : index + 1])
    recent_low = min(bundle.lows[recent_start : index + 1])
    range_position = 0.5 if recent_high == recent_low else (close - recent_low) / (recent_high - recent_low)
    if daily_return <= -2.0 and volume_ratio >= 1.35 and range_position >= 0.55:
        return "出货", evidence + ["高位放量回落"]
    if ma5 is not None and ma10 is not None and close > ma5 > ma10 and (hist or 0.0) > 0 and (five_day_return >= 3.0 or daily_return >= 1.2):
        return "主升", evidence + ["价格与短均线多头排列，MACD 柱为正"]
    if crossed_ma5 and hist is not None and previous_hist is not None and hist > previous_hist:
        return "反转", evidence + ["上穿 MA5 且 MACD 动量改善"]
    if daily_return <= -1.0 and ma5 is not None and close < ma5 and (ma20 is None or close >= ma20 * 0.95) and volume_ratio <= 1.45:
        return "洗盘", evidence + ["缩量或温和放量回踩短均线"]
    near_ma20 = ma20 is not None and abs(close / ma20 - 1.0) <= 0.04
    if abs(five_day_return) <= 4.0 and volume_ratio <= 1.20 and (near_ma20 or range_position <= 0.55):
        return "吸筹", evidence + ["低波动、量能收敛并靠近中期成本"]
    return "整理", evidence + ["未形成单一强趋势条件"]


def deep_analysis(rows: Iterable[Mapping[str, Any]] | None, max_days: int = 30) -> dict[str, Any]:
    """Split the latest window into deterministic, non-overlapping market phases."""

    clean = _normalize_rows(rows)
    if max_days <= 0 or not clean:
        return {
            "strategy_version": STRATEGY_VERSION,
            "rows_used": 0,
            "phases": [],
            "conclusion": {"bias": "数据不足", "summary": "没有足够的有效 K 线", "conditions": []},
        }
    clean = clean[-max_days:]
    bundle = _indicator_bundle([row["raw"] for row in clean])
    labels: list[str] = []
    daily_evidence: list[list[str]] = []
    for index in range(len(bundle.rows)):
        label, evidence = _phase_label(bundle, index)
        labels.append(label)
        daily_evidence.append(evidence)
    # Remove isolated one-day noise when both neighbours agree.
    smoothed = labels[:]
    for index in range(1, len(labels) - 1):
        if labels[index - 1] == labels[index + 1] != labels[index]:
            smoothed[index] = labels[index - 1]

    phases: list[dict[str, Any]] = []
    start = 0
    for index in range(1, len(smoothed) + 1):
        if index < len(smoothed) and smoothed[index] == smoothed[start]:
            continue
        end = index - 1
        start_price = bundle.closes[start]
        end_price = bundle.closes[end]
        volumes = [value for value in bundle.volume_ratio[5][start : end + 1] if value is not None]
        phases.append(
            {
                "stage": smoothed[start],
                "start_date": bundle.rows[start]["date"],
                "end_date": bundle.rows[end]["date"],
                "days": end - start + 1,
                "start_price": _round(start_price),
                "end_price": _round(end_price),
                "return_pct": round((end_price / start_price - 1.0) * 100.0, 2) if start_price else 0.0,
                "average_volume_ratio": round(fmean(volumes), 2) if volumes else None,
                "evidence": daily_evidence[end],
            }
        )
        start = index

    latest_stage = phases[-1]["stage"]
    latest_close = bundle.closes[-1]
    ma10 = bundle.ma[10][-1]
    ma20 = bundle.ma[20][-1]
    boll_upper = bundle.boll["upper"][-1]
    support = ma10 or ma20 or min(bundle.lows[-5:])
    resistance = boll_upper or max(bundle.highs[-10:])
    if latest_stage == "主升":
        bias = "偏强"
        summary = "短均线与 MACD 动量保持多头，当前属于主升条件区。"
    elif latest_stage == "反转":
        bias = "转强观察"
        summary = "价格刚出现反转证据，仍需收盘和量能确认。"
    elif latest_stage == "吸筹":
        bias = "蓄势"
        summary = "价格和量能收敛，处于成本区附近的蓄势阶段。"
    elif latest_stage == "洗盘":
        bias = "震荡"
        summary = "短线回踩但尚未确认趋势破坏，应观察支撑与量能。"
    elif latest_stage == "出货":
        bias = "风险"
        summary = "高位放量回落特征较强，风险条件优先。"
    else:
        bias = "中性"
        summary = "当前未形成一致的趋势证据。"
    conditions = [
        f"延续条件：收盘维持 {support:.2f} 上方且 MACD 柱不连续走弱。",
        f"突破条件：放量站稳 {resistance:.2f} 后再确认趋势延续。",
        f"风险条件：收盘有效跌破 {support:.2f} 或出现高位放量长阴。",
    ]
    return {
        "strategy_version": STRATEGY_VERSION,
        "rows_used": len(bundle.rows),
        "window": {"start_date": bundle.rows[0]["date"], "end_date": bundle.rows[-1]["date"]},
        "phases": phases,
        "conclusion": {
            "bias": bias,
            "stage": latest_stage,
            "summary": summary,
            "latest_close": _round(latest_close),
            "support": _round(support),
            "resistance": _round(resistance),
            "conditions": conditions,
            "disclaimer": "仅供研究，不构成投资建议。",
        },
    }


def _self_test() -> None:
    rows: list[dict[str, Any]] = []
    price = 10.0
    for day in range(80):
        drift = 0.04 + (0.06 if day > 55 else 0.0) + (0.12 if day % 7 == 0 else -0.02)
        open_price = price
        price = max(1.0, price + drift)
        rows.append(
            {
                "date": f"2026-{(day // 28) + 1:02d}-{(day % 28) + 1:02d}",
                "open": open_price,
                "high": max(open_price, price) * 1.012,
                "low": min(open_price, price) * 0.988,
                "close": price,
                "volume": 1_000_000 + day * 8_000,
            }
        )
    indicators = standard_indicators(rows)
    tech = technical_score(rows)
    market = market_score(rows)
    snapshot = {
        "code": "603001",
        "name": "示例股份",
        "is_first_board": True,
        "board_count": 1,
        "prev_close": rows[-1]["close"],
        "auction_price": rows[-1]["close"] * 1.025,
        "market_cap": 8_000_000_000,
        "auction_amount": 50_000_000,
        "volume_ratio": 2.1,
        "turnover": 4.2,
        "amplitude": 6.0,
        "seal_time": "10:12",
        "sector_resonance": 0.8,
    }
    auction = auction_score(snapshot, rows, market)
    analysis = deep_analysis(rows)
    assert indicators["count"] == 80 and indicators["latest"]["ma60"] is not None
    assert -12 <= tech["total"] <= 12 and len(trend_score_series(rows)) == 10
    assert len(auction["gates"]) == 15 and len(auction["breakdown"]) == 10
    assert 0 <= auction["score"] <= 100 and analysis["rows_used"] == 30
    for left, right in zip(analysis["phases"], analysis["phases"][1:]):
        assert left["end_date"] < right["start_date"]
    print("scoring self-test passed")


def microstructure_quality(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    tail_minutes: int = 15,
) -> dict[str, Any]:
    """Assess intraday quality: is this real strength or just a good show?

    Computes four metrics from intraday OHLCV data:
    1. Path efficiency: net advance / total distance (0-1, higher=steadier)
    2. Close position: (close-low)/(high-low) (0-1, higher=stronger finish)
    3. Volume efficiency: abs(change_pct) / turnover_pct (higher=more efficient)
    4. Tail contribution: last N minutes gain / total gain (lower=more genuine)

    Based on Simon's microstructure framework.
    """
    clean = _normalize_rows(rows)
    if len(clean) < 2:
        return {"available": False, "quality": "数据不足", "score": 50}

    latest = clean[-1]
    close = latest["close"]
    open_price = latest["open"]
    high = latest["high"]
    low = latest["low"]
    volume = latest.get("volume", 0)
    amount = latest.get("amount", 0)

    # 1. Path efficiency: close-to-close net / sum of absolute ticks
    # Simplified: use daily OHLC as proxy for intraday path
    net_advance = abs(close - open_price)
    total_range = high - low
    path_efficiency = net_advance / total_range if total_range > 0 else 1.0

    # 2. Close position
    close_position = (close - low) / (high - low) if high > low else 0.5

    # 3. Volume efficiency
    day_change = (close / open_price - 1.0) * 100 if open_price > 0 else 0.0
    turnover = latest.get("turnover")
    vol_eff = abs(day_change) / max(0.01, turnover) if turnover and turnover > 0 else None

    # 4. Tail contribution (approximated — needs intraday bars for precision)
    # With daily OHLC only: use close-vs-high proximity as proxy
    # close near high on up day = sustained strength; close far from high = tail lift
    tail_proxy = 1.0 - close_position if day_change > 0 else 0.5

    # ── Quality assessment ──────────────────────────────────────────
    quality_score = 50.0
    flags: list[str] = []

    # Path efficiency
    if path_efficiency >= 0.7:
        quality_score += 15
        flags.append("路径高效")
    elif path_efficiency >= 0.4:
        quality_score += 5
    else:
        quality_score -= 10
        flags.append("路径低效(多空分歧)")

    # Close position
    if close_position >= 0.8:
        quality_score += 15
        flags.append("收于高位")
    elif close_position >= 0.5:
        quality_score += 5
    else:
        quality_score -= 10
        flags.append("收于低位(追高被埋)")

    # Volume efficiency
    if vol_eff is not None:
        if vol_eff >= 0.5:
            quality_score += 10
            flags.append("量价高效")
        elif vol_eff >= 0.15:
            quality_score += 3
        else:
            quality_score -= 8
            flags.append("量价低效(换手大涨幅小)")

    # Tail contribution proxy
    if close_position < 0.5 and day_change > 3:
        quality_score -= 12
        flags.append("尾盘突击嫌疑")

    quality_score = int(max(10, min(90, quality_score)))

    # Character classification
    if path_efficiency >= 0.65 and close_position >= 0.75:
        character = "老黄牛"
    elif path_efficiency <= 0.35:
        character = "戏精"
    elif vol_eff is not None and vol_eff < 0.2 and abs(day_change) < 3:
        character = "装忙"
    elif close_position < 0.4 and day_change > 3:
        character = "下班突击"
    else:
        character = "普通"
    return {
        "available": True,
        "quality": "强" if quality_score >= 70 else "中" if quality_score >= 40 else "弱",
        "score": quality_score,
        "character": character,
        "metrics": {
            "path_efficiency": round(path_efficiency, 3),
            "close_position": round(close_position, 3),
            "volume_efficiency": round(vol_eff, 3) if vol_eff is not None else None,
            "tail_proxy": round(tail_proxy, 3),
        },
        "flags": flags,
        "day_change_pct": round(day_change, 2),
    }


if __name__ == "__main__":
    _self_test()
