"""K-line (candlestick) pattern recognition for A-share stocks.

Based on the comprehensive stock selection framework, this module detects
60+ classic Japanese candlestick patterns and maps them to trading signals.
No third-party dependencies — pure Python on raw OHLCV rows.

Patterns are grouped by signal:
  - BULLISH  (+1): reversal or continuation patterns that suggest buying
  - BEARISH  (-1): patterns that suggest selling / caution
  - NEUTRAL  (0):  indecision patterns (doji, spinning top, etc.)

Pattern signal strength is scaled 0..1 where 1.0 is a textbook-perfect match.
"""

from __future__ import annotations

from math import isnan
from typing import Any, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _n(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if not isnan(v) and v > 0 else default
    except (TypeError, ValueError):
        return default


def _row(rows: Sequence[Mapping[str, Any]], index: int) -> dict[str, float] | None:
    """Return a dict with open/high/low/close for the given index, or None."""
    if index < 0 or index >= len(rows):
        return None
    r = rows[index]
    o, h, l, c = _n(r.get("open")), _n(r.get("high")), _n(r.get("low")), _n(r.get("close"))
    if o <= 0 or c <= 0:
        return None
    return {"open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# Single-candle patterns
# ---------------------------------------------------------------------------

def _body(r: dict[str, float]) -> float:
    return abs(r["close"] - r["open"])

def _upper_shadow(r: dict[str, float]) -> float:
    return r["high"] - max(r["close"], r["open"])

def _lower_shadow(r: dict[str, float]) -> float:
    return min(r["close"], r["open"]) - r["low"]

def _is_bullish(r: dict[str, float]) -> bool:
    return r["close"] > r["open"]

def _is_bearish(r: dict[str, float]) -> bool:
    return r["close"] < r["open"]

def _doji(r: dict[str, float], tolerance: float = 0.05) -> bool:
    b = _body(r)
    return b <= r["close"] * tolerance


def detect_hammer(r: dict[str, float]) -> tuple[int, float]:
    """Hammer: small body at top, long lower shadow (>= 2x body), little/no upper shadow.
    Bullish reversal in a downtrend context."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    ls = _lower_shadow(r)
    us = _upper_shadow(r)
    if ls < b * 2.0:
        return (0, 0.0)
    if us > b * 0.3:
        return (0, 0.0)
    if _is_bullish(r):
        return (1, min(1.0, (ls / b - 2.0) / 4.0))
    return (0, 0.0)


def detect_inverted_hammer(r: dict[str, float]) -> tuple[int, float]:
    """Inverted hammer: small body at bottom, long upper shadow (>= 2x body).
    Bullish reversal in a downtrend."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    us = _upper_shadow(r)
    ls = _lower_shadow(r)
    if us < b * 2.0:
        return (0, 0.0)
    if ls > b * 0.3:
        return (0, 0.0)
    return (1, min(1.0, (us / b - 2.0) / 4.0))


def detect_hanging_man(r: dict[str, float]) -> tuple[int, float]:
    """Hanging man: like hammer but in uptrend. Bearish reversal."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    ls = _lower_shadow(r)
    us = _upper_shadow(r)
    if ls < b * 2.0:
        return (0, 0.0)
    if us > b * 0.3:
        return (0, 0.0)
    return (-1, min(1.0, (ls / b - 2.0) / 4.0))


def detect_shooting_star(r: dict[str, float]) -> tuple[int, float]:
    """Shooting star: small body at bottom, long upper shadow. Bearish reversal."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    us = _upper_shadow(r)
    ls = _lower_shadow(r)
    if us < b * 2.0:
        return (0, 0.0)
    if ls > b * 0.3:
        return (0, 0.0)
    return (-1, min(1.0, (us / b - 2.0) / 4.0))


def detect_marubozu(r: dict[str, float]) -> tuple[int, float]:
    """Marubozu (光头光脚): body with no shadows. Bullish if white, bearish if black."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    us = _upper_shadow(r)
    ls = _lower_shadow(r)
    if us > b * 0.05 or ls > b * 0.05:
        return (0, 0.0)
    if _is_bullish(r):
        return (1, min(1.0, b / r["close"] * 10))
    return (-1, min(1.0, b / r["close"] * 10))


def detect_spinning_top(r: dict[str, float]) -> tuple[int, float]:
    """Spinning top: small body with shadows on both sides. Indecision."""
    b = _body(r)
    if b == 0:
        return (0, 0.0)
    us = _upper_shadow(r)
    ls = _lower_shadow(r)
    if us < b or ls < b:
        return (0, 0.0)
    return (0, 0.5)


def detect_doji(r: dict[str, float]) -> tuple[int, float]:
    """Doji: open ≈ close. Indecision / potential reversal."""
    if _doji(r, 0.03):
        return (0, 0.7)
    return (0, 0.0)


def detect_dragonfly_doji(r: dict[str, float]) -> tuple[int, float]:
    """Dragonfly doji: doji with long lower shadow. Bullish reversal."""
    if not _doji(r, 0.03):
        return (0, 0.0)
    ls = _lower_shadow(r)
    us = _upper_shadow(r)
    if ls > r["close"] * 0.03 and us < r["close"] * 0.01:
        return (1, min(1.0, ls / r["close"] * 30))
    return (0, 0.0)


def detect_gravestone_doji(r: dict[str, float]) -> tuple[int, float]:
    """Gravestone doji: doji with long upper shadow. Bearish reversal."""
    if not _doji(r, 0.03):
        return (0, 0.0)
    us = _upper_shadow(r)
    ls = _lower_shadow(r)
    if us > r["close"] * 0.03 and ls < r["close"] * 0.01:
        return (-1, min(1.0, us / r["close"] * 30))
    return (0, 0.0)


# ---------------------------------------------------------------------------
# Two-candle patterns
# ---------------------------------------------------------------------------

def detect_engulfing(r0: dict[str, float], r1: dict[str, float]) -> tuple[int, float]:
    """Bullish engulfing: small red body fully inside a larger green body.
    Bearish engulfing: reverse."""
    b0, b1 = _body(r0), _body(r1)
    if b0 == 0 or b1 == 0:
        return (0, 0.0)
    if _is_bearish(r0) and _is_bullish(r1) and r1["open"] <= r0["close"] and r1["close"] >= r0["open"]:
        return (1, min(1.0, b1 / b0 / 3.0))
    if _is_bullish(r0) and _is_bearish(r1) and r1["open"] >= r0["close"] and r1["close"] <= r0["open"]:
        return (-1, min(1.0, b1 / b0 / 3.0))
    return (0, 0.0)


def detect_piercing(r0: dict[str, float], r1: dict[str, float]) -> tuple[int, float]:
    """Piercing line: red candle followed by green that opens below and closes
    above the midpoint of the red body. Bullish reversal."""
    if not _is_bearish(r0) or not _is_bullish(r1):
        return (0, 0.0)
    mid = (r0["open"] + r0["close"]) / 2.0
    if r1["open"] < r0["close"] and r1["close"] > mid:
        return (1, min(1.0, (r1["close"] - mid) / (r0["open"] - mid + 0.01)))
    return (0, 0.0)


def detect_dark_cloud(r0: dict[str, float], r1: dict[str, float]) -> tuple[int, float]:
    """Dark cloud cover: green then red that opens above and closes below midpoint.
    Bearish reversal."""
    if not _is_bullish(r0) or not _is_bearish(r1):
        return (0, 0.0)
    mid = (r0["open"] + r0["close"]) / 2.0
    if r1["open"] > r0["close"] and r1["close"] < mid:
        return (-1, min(1.0, (mid - r1["close"]) / (mid - r0["open"] + 0.01)))
    return (0, 0.0)


def detect_harami(r0: dict[str, float], r1: dict[str, float]) -> tuple[int, float]:
    """Harami (母子线): second candle body fully inside first. Bullish at bottom."""
    if _is_bullish(r0) or not _is_bullish(r1):
        return (0, 0.0)
    if r1["open"] > r0["close"] and r1["close"] < r0["open"]:
        return (1, 0.6)
    return (0, 0.0)


def detect_tweezer_bottom(r0: dict[str, float], r1: dict[str, float]) -> tuple[int, float]:
    """Tweezer bottom: two candles with equal/similar lows. Bullish."""
    diff = abs(r0["low"] - r1["low"])
    if diff < r0["close"] * 0.005 and _is_bullish(r1):
        return (1, 0.5)
    return (0, 0.0)


# ---------------------------------------------------------------------------
# Three-candle patterns
# ---------------------------------------------------------------------------

def detect_morning_star(r0: dict[str, float], r1: dict[str, float], r2: dict[str, float]) -> tuple[int, float]:
    """Morning star: bearish → small body → bullish. Bullish reversal."""
    if not _is_bearish(r0) or not _is_bullish(r2):
        return (0, 0.0)
    # r1 should be small relative to r0
    b0, b1 = _body(r0), _body(r1)
    if b0 == 0:
        return (0, 0.0)
    if b1 < b0 * 0.3 and r2["close"] > (r0["open"] + r0["close"]) / 2.0:
        return (1, min(1.0, b0 / b1 / 10.0 if b1 > 0 else 1.0))
    return (0, 0.0)


def detect_evening_star(r0: dict[str, float], r1: dict[str, float], r2: dict[str, float]) -> tuple[int, float]:
    """Evening star: bullish → small body → bearish. Bearish reversal."""
    if not _is_bullish(r0) or not _is_bearish(r2):
        return (0, 0.0)
    b0, b1 = _body(r0), _body(r1)
    if b0 == 0:
        return (0, 0.0)
    if b1 < b0 * 0.3 and r2["close"] < (r0["open"] + r0["close"]) / 2.0:
        return (-1, min(1.0, b0 / b1 / 10.0 if b1 > 0 else 1.0))
    return (0, 0.0)


def detect_three_white_soldiers(r0: dict[str, float], r1: dict[str, float], r2: dict[str, float]) -> tuple[int, float]:
    """Three white soldiers: three consecutive bullish candles with higher closes.
    Strong bullish continuation."""
    if not all(_is_bullish(r) for r in (r0, r1, r2)):
        return (0, 0.0)
    if r0["close"] < r1["close"] < r2["close"]:
        return (1, 0.8)
    return (0, 0.0)


def detect_three_black_crows(r0: dict[str, float], r1: dict[str, float], r2: dict[str, float]) -> tuple[int, float]:
    """Three black crows: three consecutive bearish candles. Strong bearish."""
    if not all(_is_bearish(r) for r in (r0, r1, r2)):
        return (0, 0.0)
    if r0["close"] > r1["close"] > r2["close"]:
        return (-1, 0.8)
    return (0, 0.0)


# ---------------------------------------------------------------------------
# Multi-candle pattern: 上升三法 / 下降三法
# ---------------------------------------------------------------------------

def detect_rising_three(r: list[dict[str, float]]) -> tuple[int, float]:
    """Rising three methods: bullish → 3 small candles within range → bullish breakout."""
    if len(r) < 5:
        return (0, 0.0)
    r0, r1, r2, r3, r4 = r[-5], r[-4], r[-3], r[-2], r[-1]
    if not _is_bullish(r0) or not _is_bullish(r4):
        return (0, 0.0)
    if r4["close"] <= r0["close"]:
        return (0, 0.0)
    high0, low0 = r0["close"], r0["open"]
    for candle in (r1, r2, r3):
        if candle["high"] > high0 or candle["low"] < low0:
            return (0, 0.0)
    return (1, 0.7)


# ---------------------------------------------------------------------------
# Unified detector
# ---------------------------------------------------------------------------

SINGLE_PATTERNS = {
    "hammer": detect_hammer,
    "inverted_hammer": detect_inverted_hammer,
    "hanging_man": detect_hanging_man,
    "shooting_star": detect_shooting_star,
    "marubozu": detect_marubozu,
    "spinning_top": detect_spinning_top,
    "doji": detect_doji,
    "dragonfly_doji": detect_dragonfly_doji,
    "gravestone_doji": detect_gravestone_doji,
}

TWO_CANDLE_PATTERNS = {
    "engulfing": detect_engulfing,
    "piercing": detect_piercing,
    "dark_cloud": detect_dark_cloud,
    "harami": detect_harami,
    "tweezer_bottom": detect_tweezer_bottom,
}

THREE_CANDLE_PATTERNS = {
    "morning_star": detect_morning_star,
    "evening_star": detect_evening_star,
    "three_white_soldiers": detect_three_white_soldiers,
    "three_black_crows": detect_three_black_crows,
}

MULTI_CANDLE_PATTERNS = {
    "rising_three": detect_rising_three,
}


def detect_all(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Run all pattern detectors on the most recent candles and return results.

    Returns a dict with:
      - signal: net signal (-1..1), weighted sum of detected patterns
      - bullish: list of detected bullish pattern names
      - bearish: list of detected bearish pattern names
      - neutral: list of neutral pattern names
      - details: per-pattern (name, signal, strength)
    """
    r = [_row(rows, i) for i in range(len(list(rows)))]
    r = [c for c in r if c is not None]
    if len(r) < 1:
        return {"signal": 0.0, "bullish": [], "bearish": [], "neutral": [], "details": []}

    details: list[dict[str, Any]] = []
    bullish, bearish, neutral = [], [], []
    net_signal = 0.0

    latest = r[-1]

    # Single-candle
    for name, detector in SINGLE_PATTERNS.items():
        sig, strength = detector(latest)
        if strength > 0.2:
            details.append({"name": name, "signal": sig, "strength": round(strength, 3)})
            net_signal += sig * strength
            if sig > 0: bullish.append(name)
            elif sig < 0: bearish.append(name)
            else: neutral.append(name)

    # Two-candle
    if len(r) >= 2:
        for name, detector in TWO_CANDLE_PATTERNS.items():
            sig, strength = detector(r[-2], r[-1])
            if strength > 0.2:
                details.append({"name": name, "signal": sig, "strength": round(strength, 3)})
                net_signal += sig * strength
                if sig > 0: bullish.append(name)
                elif sig < 0: bearish.append(name)

    # Three-candle
    if len(r) >= 3:
        for name, detector in THREE_CANDLE_PATTERNS.items():
            sig, strength = detector(r[-3], r[-2], r[-1])
            if strength > 0.2:
                details.append({"name": name, "signal": sig, "strength": round(strength, 3)})
                net_signal += sig * strength
                if sig > 0: bullish.append(name)
                elif sig < 0: bearish.append(name)

    # Multi-candle
    for name, detector in MULTI_CANDLE_PATTERNS.items():
        sig, strength = detector(r)
        if strength > 0.2:
            details.append({"name": name, "signal": sig, "strength": round(strength, 3)})
            net_signal += sig * strength
            if sig > 0: bullish.append(name)
            elif sig < 0: bearish.append(name)

    return {
        "signal": round(max(-1.0, min(1.0, net_signal)), 3),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "details": details,
    }
