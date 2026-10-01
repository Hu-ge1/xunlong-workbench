"""
Overseas market data fetcher for the Six-Dimensional Timing Model (六维择时 Signal 6).

Uses yfinance (free) to fetch key overseas indices relevant to A-share risk:
- 费城半导体指数 (^SOX) — core semiconductor sentiment
- 韩国KOSPI (^KS11) — Korea chip/tech exposure
- 日经225 (^N225) — Japan tech/manufacturing
- 纳斯达克100 (^NDX) — US tech proxy
- 标普500 (^GSPC) — broad US risk appetite

All data is fetched at A-share pre-market (before 9:00 AM Beijing time),
so it qualifies as "已知数据" under the look-ahead bias constraint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

YFINANCE_AVAILABLE = False
try:
    import yfinance as yf

    YFINANCE_AVAILABLE = True
except ImportError:
    logger.warning("yfinance not installed; overseas shock factor disabled")

# ── Retry config ────────────────────────────────────────────────────
_MAX_RETRIES = 1
_RETRY_DELAY = 1.0
_TICKER_DELAY = 0.05
_REQUEST_TIMEOUT = 5
_CACHE_TTL_SECONDS = 30 * 60
_snapshot_cache: dict[tuple[str, ...], tuple[float, dict[str, dict[str, Any]]]] = {}

# ── Index definitions ──────────────────────────────────────────────
# Codes are yfinance tickers. We fetch latest 2-day data to compute
# daily change percentage and flag extreme single-day moves.

OVERSEAS_INDICES: dict[str, dict[str, Any]] = {
    "SOX": {
        "ticker": "^SOX",
        "name_cn": "费城半导体",
        "region": "US",
        "threshold_severe": -4.0,  # single-day drop below this = severe shock
        "threshold_warning": -2.5,  # single-day drop below this = warning
        "timezone": "US/Eastern",
        "trading_hours": "prev_close ~09:30–16:00 ET",
    },
    "KS11": {
        "ticker": "^KS11",
        "name_cn": "韩国KOSPI",
        "region": "KR",
        "threshold_severe": -5.0,
        "threshold_warning": -2.5,
        "timezone": "Asia/Seoul",
        "trading_hours": "09:00–15:30 KST",
    },
    "N225": {
        "ticker": "^N225",
        "name_cn": "日经225",
        "region": "JP",
        "threshold_severe": -4.0,
        "threshold_warning": -2.5,
        "timezone": "Asia/Tokyo",
        "trading_hours": "09:00–15:00 JST",
    },
    "IXIC": {
        "ticker": "^IXIC",
        "name_cn": "纳斯达克综合",
        "region": "US",
        "threshold_severe": -3.5,
        "threshold_warning": -2.0,
        "timezone": "US/Eastern",
        "trading_hours": "09:30–16:00 ET",
    },
}

# Mapping from A-share strategy style to relevant overseas indices
STYLE_OVERSEAS_MAP: dict[str, list[str]] = {
    "半导体": ["SOX", "KS11"],
    "科技": ["SOX", "IXIC", "N225"],
    "芯片": ["SOX", "KS11"],
    "AI": ["SOX", "IXIC"],
    "新能源": ["KS11", "N225"],
    "default": ["SOX", "KS11", "N225", "IXIC"],
}


def is_available() -> bool:
    """Check if yfinance is installed and functional."""
    return YFINANCE_AVAILABLE


def fetch_overseas_snapshot(
    codes: list[str] | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, dict[str, Any]]:
    """Fetch latest price and daily change for specified overseas indices.

    Args:
        codes: List of index keys (e.g. ['SOX', 'KS11']). Defaults to all.
        use_cache: If True, reuses cached results within the same session.

    Returns:
        Dict keyed by index code, each with:
        - price: latest close (float or None)
        - change_pct: daily % change (float or None)
        - prev_close: previous session close
        - name_cn: Chinese display name
        - region: US/KR/JP
        - available: whether data was successfully fetched
        - error: error message if fetch failed
    """
    if not YFINANCE_AVAILABLE:
        return {
            code: {
                "price": None,
                "change_pct": None,
                "prev_close": None,
                "name_cn": info["name_cn"],
                "region": info["region"],
                "available": False,
                "error": "yfinance not installed",
            }
            for code, info in OVERSEAS_INDICES.items()
            if codes is None or code in codes
        }

    target_codes = codes or list(OVERSEAS_INDICES.keys())
    cache_key = tuple(target_codes)
    cached = _snapshot_cache.get(cache_key)
    if use_cache and cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return {key: dict(value) for key, value in cached[1].items()}
    results: dict[str, dict[str, Any]] = {}

    for code in target_codes:
        info = OVERSEAS_INDICES.get(code)
        if info is None:
            results[code] = {
                "price": None,
                "change_pct": None,
                "prev_close": None,
                "name_cn": code,
                "region": "unknown",
                "available": False,
                "error": f"Unknown index code: {code}",
            }
            continue

        try:
            ticker = yf.Ticker(info["ticker"])
            # Fetch with retry to handle transient rate limits
            hist = None
            last_error = ""
            for attempt in range(_MAX_RETRIES):
                try:
                    hist = ticker.history(period="5d", timeout=_REQUEST_TIMEOUT)
                    break
                except Exception as e:
                    last_error = str(e)
                    if "Rate limited" in last_error or "Too Many" in last_error:
                        wait = _RETRY_DELAY * (2 ** attempt)
                        logger.warning("Rate limited for %s, retrying in %.1fs (attempt %d/%d)",
                                       code, wait, attempt + 1, _MAX_RETRIES)
                        time.sleep(wait)
                    else:
                        raise
            time.sleep(_TICKER_DELAY)  # polite delay between tickers
            if hist is None or hist.empty:
                results[code] = _make_error(info, "No data returned from yfinance")
                continue

            if len(hist) < 2:
                # Only one data point — can't compute daily change
                latest = hist.iloc[-1]
                price = round(float(latest["Close"]), 2)
                results[code] = _make_partial(info, price)
                continue

            latest = hist.iloc[-1]
            previous = hist.iloc[-2]
            price = round(float(latest["Close"]), 2)
            prev_close = round(float(previous["Close"]), 2)
            change_pct = round((price / prev_close - 1.0) * 100.0, 2) if prev_close else None

            results[code] = {
                "price": price,
                "change_pct": change_pct,
                "prev_close": prev_close,
                "name_cn": info["name_cn"],
                "region": info["region"],
                "ticker": info["ticker"],
                "available": True,
                "error": None,
                "threshold_warning": info["threshold_warning"],
                "threshold_severe": info["threshold_severe"],
            }
            logger.info(
                "Overseas %s (%s): %.2f (%.2f%%)",
                code, info["name_cn"], price, change_pct or 0.0,
            )

        except Exception as exc:
            logger.warning("Overseas fetch failed for %s: %s", code, exc)
            results[code] = _make_error(info, str(exc))

    if use_cache:
        _snapshot_cache[cache_key] = (
            time.monotonic(),
            {key: dict(value) for key, value in results.items()},
        )
    return results


def _make_error(info: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "price": None,
        "change_pct": None,
        "prev_close": None,
        "name_cn": info["name_cn"],
        "region": info["region"],
        "available": False,
        "error": error,
    }


def _make_partial(info: dict[str, Any], price: float) -> dict[str, Any]:
    return {
        "price": price,
        "change_pct": None,
        "prev_close": None,
        "name_cn": info["name_cn"],
        "region": info["region"],
        "available": True,
        "error": "Insufficient history for daily change",
    }
