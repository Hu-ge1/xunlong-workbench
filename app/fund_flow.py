"""
Institutional flow & coverage data for Xunlong workbench.

Integrates three data sources from akshare (free):
- ETF daily fund flow → 散户情绪 extreme detection (108选6 Factor 3)
- Analyst coverage → 机构关注度 filter (108选6 Factor 2)
- Fund portfolio holdings → 基金共识 direction (108选6 Factor 4)

All data caches locally to avoid repeated API calls within the same session.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

AKSHARE_AVAILABLE = False
try:
    import akshare as ak

    AKSHARE_AVAILABLE = True
except ImportError:
    logger.warning("akshare not installed; fund_flow module disabled")

# ── Simple in-memory cache ──────────────────────────────────────────
_cache: dict[str, tuple[Any, datetime]] = {}
_CACHE_TTL = timedelta(hours=2)

# ── Industry → ETF mapping ──────────────────────────────────────────
# Key industries and their representative ETFs for flow tracking
INDUSTRY_ETF_MAP: dict[str, list[str]] = {
    "半导体": ["512480", "512760", "159995"],
    "芯片": ["512760", "159995", "159801"],
    "电子": ["159997", "515260", "515050"],
    "医药": ["512010", "159929", "512170"],
    "医疗": ["512170", "159828", "159883"],
    "消费": ["159928", "510630", "512690"],
    "食品饮料": ["515170", "159843"],
    "白酒": ["512690"],
    "军工": ["512660", "512710"],
    "新能源": ["516160", "159875", "516850"],
    "光伏": ["515790", "159857"],
    "电池": ["159755", "159840"],
    "汽车": ["516110", "159889"],
    "通信": ["515880", "515050"],
    "计算机": ["512720", "159998"],
    "软件": ["159852", "515230"],
    "传媒": ["512980", "159805"],
    "游戏": ["159869", "516010"],
    "银行": ["512800", "512700"],
    "券商": ["512880", "512000", "159841"],
    "保险": ["512070"],
    "房地产": ["512200", "515060"],
    "有色": ["512400", "159871"],
    "煤炭": ["515220"],
    "钢铁": ["515210"],
    "化工": ["159870", "516020"],
    "电力": ["159611", "561560", "561700"],
    "农业": ["159825", "159865"],
    "基建": ["516950", "159619"],
    "交通运输": ["159662"],
    "央企": ["512960", "510060"],
    "红利": ["510880", "515080"],
    "科创": ["588000", "588050", "588080", "588180"],
    "创业板": ["159915", "159949", "159922"],
}


def _cached(key: str, fetcher, ttl: timedelta = _CACHE_TTL):
    """Simple TTL cache wrapper."""
    now = datetime.now()
    if key in _cache:
        data, ts = _cache[key]
        if now - ts < ttl:
            return data
    data = fetcher()
    _cache[key] = (data, now)
    return data


# ══════════════════════════════════════════════════════════════════════
# ETF Fund Flow — 散户情绪极端检测
# ══════════════════════════════════════════════════════════════════════

def fetch_etf_flow() -> dict[str, dict[str, Any]]:
    """Fetch daily ETF NAV/price data for all tracked ETFs.

    Returns dict keyed by ETF code with:
    - name, nav, price, change_pct, discount_pct
    """
    if not AKSHARE_AVAILABLE:
        return {}

    def _fetch():
        try:
            df = ak.fund_etf_fund_daily_em()
        except Exception as e:
            logger.warning("ETF flow fetch failed: %s", e)
            return {}

        result: dict[str, dict[str, Any]] = {}
        for _, row in df.iterrows():
            code = str(row.get("基金代码", ""))
            if not code:
                continue
            try:
                result[code] = {
                    "name": str(row.get("基金简称", "")),
                    "type": str(row.get("类型", "")),
                    "price": float(row.get("市价", 0) or 0),
                    "change_pct": float(str(row.get("增长率", "0%")).replace("%", "")),
                    "discount_pct": float(str(row.get("折价率", "0%")).replace("%", "")),
                }
            except (ValueError, TypeError):
                continue
        logger.info("ETF flow: %d ETFs loaded", len(result))
        return result

    return _cached("etf_flow", _fetch)


def compute_industry_retail_sentiment(
    etf_flow: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute retail sentiment by industry from ETF flow data.

    For each industry, aggregates the ETF flow signals to determine:
    - avg_change: average ETF NAV change (%)
    - extreme_buy: whether flow is extremely positive (散户疯狂买)
    - extreme_sell: whether flow is extremely negative (散户疯狂卖)
    - sentiment_score: 0 = extreme sell, 1 = extreme buy, 0.5 = neutral

    Based on the 微笑曲线 logic: BOTH extremes are opportunities.
    """
    if etf_flow is None:
        etf_flow = fetch_etf_flow()

    if not etf_flow:
        return {}

    results: dict[str, dict[str, Any]] = {}
    for industry, etf_codes in INDUSTRY_ETF_MAP.items():
        changes = []
        names = []
        for code in etf_codes:
            data = etf_flow.get(code)
            if data and data["change_pct"] != 0:
                changes.append(data["change_pct"])
                names.append(data["name"])

        if len(changes) < 1:
            continue

        avg_change = sum(changes) / len(changes)
        max_change = max(changes)
        min_change = min(changes)

        # Extreme detection: |change| > 2% = significant retail activity
        extreme_buy = any(c > 2.0 for c in changes)
        extreme_sell = any(c < -2.0 for c in changes)
        is_extreme = extreme_buy or extreme_sell

        # Map to 0-1 scale, then fold at middle (微笑曲线)
        # Score of 1.0 = most extreme (either direction)
        max_abs = max(abs(max_change), abs(min_change), 0.01)
        raw_score = min(1.0, max_abs / 5.0)  # saturate at 5%

        results[industry] = {
            "avg_change_pct": round(avg_change, 2),
            "max_change_pct": round(max_change, 2),
            "min_change_pct": round(min_change, 2),
            "etf_count": len(changes),
            "extreme_buy": extreme_buy,
            "extreme_sell": extreme_sell,
            "is_extreme": is_extreme,
            "extreme_score": round(abs(raw_score - 0.5) * 2, 3),  # 0=neutral, 1=extreme
            "etf_names": names,
        }

    logger.info("Retail sentiment: %d industries computed", len(results))
    return results


# ══════════════════════════════════════════════════════════════════════
# Analyst Coverage — 机构关注度
# ══════════════════════════════════════════════════════════════════════

def fetch_analyst_coverage() -> dict[str, Any]:
    """Fetch analyst rankings and aggregate by industry.

    Returns:
        analysts: list of top analysts with performance
        industry_coverage: count of analysts covering each industry
    """
    if not AKSHARE_AVAILABLE:
        return {"analysts": [], "industry_coverage": {}}

    def _fetch():
        try:
            df = ak.stock_analyst_rank_em()
        except Exception as e:
            logger.warning("Analyst rank fetch failed: %s", e)
            return {"analysts": [], "industry_coverage": {}}

        analysts = []
        industry_count: dict[str, int] = {}

        for _, row in df.iterrows():
            name = str(row.get("分析师名称", ""))
            unit = str(row.get("分析师单位", ""))
            industry = str(row.get("行业", ""))
            ret_12m = float(row.get("12个月收益率", 0) or 0)
            ret_3m = float(row.get("3个月收益率", 0) or 0)
            stock_name = str(row.get("2024最新个股评级-股票名称", ""))
            stock_code = str(row.get("2024最新个股评级-股票代码", ""))

            analysts.append({
                "name": name, "unit": unit, "industry": industry,
                "ret_12m": round(ret_12m, 1), "ret_3m": round(ret_3m, 1),
                "pick": stock_name, "pick_code": stock_code,
            })

            if industry:
                industry_count[industry] = industry_count.get(industry, 0) + 1

        logger.info("Analyst coverage: %d analysts, %d industries", len(analysts), len(industry_count))
        return {"analysts": analysts, "industry_coverage": industry_count}

    return _cached("analyst_coverage", _fetch)


def compute_industry_coverage_score(
    coverage: dict[str, Any] | None = None,
    min_analysts: int = 3,
) -> dict[str, float]:
    """Score industries by analyst coverage depth.

    Returns dict of industry → coverage_score (0-1).
    Industries with < min_analysts are flagged as uncovered.
    """
    if coverage is None:
        coverage = fetch_analyst_coverage()

    industry_count = coverage.get("industry_coverage", {})
    if not industry_count:
        return {}

    max_count = max(industry_count.values()) if industry_count else 1
    return {
        industry: round(count / max_count, 3)
        for industry, count in industry_count.items()
        if count >= min_analysts
    }


# ══════════════════════════════════════════════════════════════════════
# Fund Holdings Consensus — 基金共识方向
# ══════════════════════════════════════════════════════════════════════

def fetch_fund_holdings(year: str = "2026") -> list[dict[str, Any]]:
    """Fetch latest quarterly fund holdings.

    Returns list of holding records with stock code, weight, market value.
    """
    if not AKSHARE_AVAILABLE:
        return []

    def _fetch():
        try:
            df = ak.fund_portfolio_hold_em(date=year)
        except Exception as e:
            logger.warning("Fund holdings fetch failed: %s", e)
            return []

        holdings = []
        for _, row in df.iterrows():
            try:
                holdings.append({
                    "stock_code": str(row.get("股票代码", "")),
                    "stock_name": str(row.get("股票名称", "")),
                    "weight_pct": float(row.get("占净值比例", 0) or 0),
                    "shares": float(row.get("持股数", 0) or 0),
                    "market_value": float(row.get("持仓市值", 0) or 0),
                    "quarter": str(row.get("季度", "")),
                })
            except (ValueError, TypeError):
                continue

        logger.info("Fund holdings: %d records loaded", len(holdings))
        return holdings

    return _cached(f"fund_holdings_{year}", _fetch)


def compute_fund_consensus(
    holdings: list[dict[str, Any]] | None = None,
    min_funds: int = 3,
    top_n: int = 20,
) -> dict[str, dict[str, Any]]:
    """Aggregate fund holdings to find consensus picks.

    Groups by stock code, counts how many funds hold it,
    and computes the total weight.

    Returns dict of stock_code → {name, fund_count, total_weight, avg_weight}
    """
    if holdings is None:
        holdings = fetch_fund_holdings()

    if not holdings:
        return {}

    # Aggregate by stock
    stock_map: dict[str, dict[str, Any]] = {}
    for h in holdings:
        code = h["stock_code"]
        if code not in stock_map:
            stock_map[code] = {
                "name": h["stock_name"],
                "fund_count": 0,
                "total_weight": 0.0,
                "weights": [],
            }
        stock_map[code]["fund_count"] += 1
        stock_map[code]["total_weight"] += h["weight_pct"]
        stock_map[code]["weights"].append(h["weight_pct"])

    # Filter and score
    results: dict[str, dict[str, Any]] = {}
    for code, data in stock_map.items():
        if data["fund_count"] < min_funds:
            continue
        results[code] = {
            "name": data["name"],
            "fund_count": data["fund_count"],
            "total_weight": round(data["total_weight"], 1),
            "avg_weight": round(data["total_weight"] / data["fund_count"], 2),
        }

    # Sort by fund_count descending, return top_n
    sorted_items = sorted(results.items(), key=lambda x: x[1]["fund_count"], reverse=True)
    return dict(sorted_items[:top_n])


# ══════════════════════════════════════════════════════════════════════
# Combined: Full institutional flow snapshot
# ══════════════════════════════════════════════════════════════════════

def fetch_institutional_snapshot() -> dict[str, Any]:
    """Fetch all three data sources in one call.

    Returns:
        etf_flow: raw ETF data
        retail_sentiment: industry-level retail sentiment
        analyst_coverage: analyst rankings + industry coverage
        fund_consensus: top consensus stock picks
    """
    etf = fetch_etf_flow()
    sentiment = compute_industry_retail_sentiment(etf)
    coverage = fetch_analyst_coverage()
    consensus = compute_fund_consensus()

    return {
        "etf_flow": {"total_etfs": len(etf)},
        "retail_sentiment": sentiment,
        "analyst_coverage": {
            "total_analysts": len(coverage.get("analysts", [])),
            "industries_covered": len(coverage.get("industry_coverage", {})),
        },
        "fund_consensus": consensus,
        "available": AKSHARE_AVAILABLE,
        "fetched_at": datetime.now().isoformat(),
    }
