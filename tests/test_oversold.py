from __future__ import annotations

from app.oversold import evaluate_candidate, is_main_board_stock, screen_universe


MARKET = {"supportive": True, "score": 18, "label": "超跌修复占优"}
THS_HOT = {
    "code": "600001",
    "rank": 8,
    "reason": "超跌反弹+测试题材",
    "turnover": 6.8,
    "large_order_net_ratio": 1.6,
}


def first_wave_bars():
    rows = []
    price = 20.0
    for index in range(46):
        previous = price
        price *= 0.99
        rows.append(
            {
                "date": f"2026{5 + index // 28:02d}{1 + index % 28:02d}",
                "open": previous,
                "high": previous * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000,
                "amount": 80_000_000,
            }
        )
    for index in range(13):
        close = price * (1.003 + index * 0.0015)
        rows.append(
            {
                "date": f"202607{index + 1:02d}",
                "open": close * 0.997,
                "high": close * 1.01,
                "low": close * 0.995,
                "close": close,
                "volume": 1_000_000,
                "amount": 80_000_000,
            }
        )
    p0_index = min(range(len(rows)), key=lambda pos: rows[pos]["low"])
    r0 = rows[p0_index - 2]["high"]
    rows.append(
        {
            "date": "20260714",
            "open": r0 * 0.985,
            "high": r0 * 1.035,
            "low": r0 * 0.98,
            "close": r0 * 1.025,
            "volume": 2_600_000,
            "amount": 120_000_000,
        }
    )
    return rows


def second_wave_bars():
    rows = first_wave_bars()
    first_close = rows[-1]["close"]
    rows.extend(
        [
            {"date": "20260715", "open": first_close, "high": first_close * 1.08, "low": first_close * 0.99, "close": first_close * 1.06, "volume": 2_000_000, "amount": 100_000_000},
            {"date": "20260716", "open": first_close * 1.04, "high": first_close * 1.05, "low": first_close * 1.01, "close": first_close * 1.03, "volume": 900_000, "amount": 80_000_000},
            {"date": "20260717", "open": first_close * 1.03, "high": first_close * 1.04, "low": first_close * 1.00, "close": first_close * 1.02, "volume": 800_000, "amount": 80_000_000},
            {"date": "20260718", "open": first_close * 1.02, "high": first_close * 1.04, "low": first_close * 1.00, "close": first_close * 1.03, "volume": 700_000, "amount": 80_000_000},
            {"date": "20260719", "open": first_close * 1.03, "high": first_close * 1.05, "low": first_close * 1.01, "close": first_close * 1.04, "volume": 650_000, "amount": 80_000_000},
            {"date": "20260720", "open": first_close * 1.05, "high": first_close * 1.12, "low": first_close * 1.04, "close": first_close * 1.10, "volume": 2_600_000, "amount": 130_000_000},
        ]
    )
    return rows


def test_main_board_scope_excludes_growth_star_st_and_delisting():
    assert is_main_board_stock("600001", "测试股份")
    assert is_main_board_stock("002001", "测试股份")
    assert not is_main_board_stock("300001", "测试股份")
    assert not is_main_board_stock("688001", "测试股份")
    assert not is_main_board_stock("600001", "*ST测试")
    assert not is_main_board_stock("600001", "测试退")


def test_first_wave_uses_p0_r0_and_volume_breakout():
    candidate = evaluate_candidate(
        {"code": "600001", "name": "测试股份", "industry": "测试", "amount": 120_000_000},
        first_wave_bars(),
        market=MARKET,
        popularity=THS_HOT,
        min_reward_risk=0.0,
    )
    assert candidate is not None
    assert candidate["wave"] == "一波"
    assert candidate["triggered"] is True
    assert candidate["price"] > candidate["r0"] > candidate["p0"]
    assert candidate["volume_multiple"] >= 2.0
    assert candidate["above_ma5"] is True
    assert candidate["above_ma20"] is True
    assert candidate["ths_hot"] is True
    assert candidate["amount_multiple"] >= 1.5


def test_second_wave_requires_3_to_9_day_contracting_pullback():
    candidate = evaluate_candidate(
        {"code": "600001", "name": "测试股份", "industry": "测试", "amount": 130_000_000},
        second_wave_bars(),
        market=MARKET,
        popularity=THS_HOT,
        wave="second",
        min_reward_risk=0.0,
    )
    assert candidate is not None
    assert candidate["wave"] == "二波"
    assert 3 <= candidate["adjustment_days"] <= 9
    assert candidate["adjustment_shrink_ratio"] < 0.8


def test_screen_universe_filters_scope_and_exposes_market_style():
    bars = first_wave_bars()
    snapshots = [
        {"code": "600001", "name": "主板一只", "industry": "测试", "amount": 120_000_000, "postclose_kline": bars},
        {"code": "300001", "name": "创业板一只", "industry": "测试", "amount": 120_000_000, "postclose_kline": bars},
    ]
    result = screen_universe(
        snapshots,
        hot_stocks=[THS_HOT],
        limit=20,
        market_filter=False,
        min_reward_risk=0.0,
    )
    assert result["funnel"]["input"] == 2
    assert result["funnel"]["main_board"] == 1
    assert result["funnel"]["structure_ready"] == 1
    assert result["rows"][0]["code"] == "600001"
    assert "oversold_share" in result["market_style"]


def test_rejects_stock_outside_ths_hot_pool():
    candidate = evaluate_candidate(
        {"code": "600001", "name": "测试股份", "industry": "测试", "amount": 120_000_000},
        first_wave_bars(),
        market=MARKET,
        min_reward_risk=0.0,
    )
    assert candidate is None


def test_rejects_negative_ths_large_order_flow():
    candidate = evaluate_candidate(
        {"code": "600001", "name": "测试股份", "industry": "测试", "amount": 120_000_000},
        first_wave_bars(),
        market=MARKET,
        popularity={**THS_HOT, "large_order_net_ratio": -0.2},
        min_reward_risk=0.0,
    )
    assert candidate is None
