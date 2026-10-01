from __future__ import annotations

import app.dixi
from app.dixi import evaluate_candidate, screen_universe


def uptrend_snapshot(
    code: str = "600001",
    name: str = "趋势强票",
    industry: str = "电力设备",
    *,
    days: int = 70,
    base: float = 10.0,
    daily: float = 1.004,
    amount: float = 600_000_000.0,
    pullback_days: int = 0,
    today_rebound: bool = True,
    today_open_gap: float = 0.995,
) -> dict:
    bars = []
    price = base
    for index in range(days - 1):
        if index in (18, 19):
            price *= 1.10  # 注入两次涨停，满足"股性活跃"（60日涨停≥2）
        bars.append(
            {
                "date": f"2026-06-{index % 28 + 1:02d}",
                "open": price * 0.999,
                "high": price * 1.012,
                "low": price * 0.995,
                "close": price,
                "volume": 800_000,
                "amount": amount * 0.9,
            }
        )
        price *= daily
    if pullback_days:
        for index in range(pullback_days):
            price *= 0.985
            bars.append(
                {
                    "date": f"2026-08-{index + 1:02d}",
                    "open": price * 1.002,
                    "high": price * 1.004,
                    "low": price * 0.98,
                    "close": price,
                    "volume": 400_000,
                    "amount": amount * 0.5,
                }
            )
    if today_rebound:
        open_price = price * today_open_gap
        close = max(price * 1.02, bars[-1]["close"] * 1.001)
        bars.append(
            {
                "date": "2026-08-20",
                "open": open_price,
                "high": close * 1.005,
                "low": open_price * 0.995,
                "close": close,
                "volume": 1_600_000,
                "amount": amount,
            }
        )
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "amount": bars[-1]["amount"],
        "postclose_kline": bars,
    }


def test_dip_buy_triggered_on_pullback_wrap():
    snap = uptrend_snapshot(pullback_days=3)
    row = evaluate_candidate(snap, amount_rank=50)
    assert row is not None
    assert row["buy_point"] in ("回调反包", "均线低吸", "急跌拉回")
    assert row["amount_rank"] == 50
    assert row["activity_60d_limit_ups"] >= 2 or row["activity_60d_limit_ups"] == 0


def test_trend_break_and_liquidity_gates():
    # 流动性不足被剔除
    poor = uptrend_snapshot(code="600002", amount=80_000_000.0)
    assert evaluate_candidate(poor, amount_rank=800) is None
    # 趋势破位（连续大跌 6%）被剔除
    broken = uptrend_snapshot(code="600003")
    broken["postclose_kline"][-1]["close"] = broken["postclose_kline"][-2]["close"] * 0.90
    broken["postclose_kline"][-1]["open"] = broken["postclose_kline"][-2]["close"] * 0.91
    broken["postclose_kline"][-1]["high"] = broken["postclose_kline"][-1]["close"] * 1.001
    assert evaluate_candidate(broken, amount_rank=10) is None


def test_screen_universe_pipeline_and_rank():
    snaps = [
        uptrend_snapshot(pullback_days=3),
        uptrend_snapshot(code="600002", name="无回调趋势票"),
        uptrend_snapshot(code="600003", name="成交额不足", amount=50_000_000.0),
    ]
    result = screen_universe(snaps, limit=20)
    codes = [item["code"] for item in result["rows"]]
    assert "600001" in codes
    assert "600003" not in codes
    ranks = [item["amount_rank"] for item in result["rows"]]
    assert all(rank > 0 for rank in ranks)
    assert result["methodology"]


def test_auction_verify_verdicts():
    rows = [
        {"code": "600001", "name": "符合", "buy_point": "回调反包", "score": 80, "price": 10.0},
        {"code": "600002", "name": "低开放弃", "buy_point": "均线低吸", "score": 60, "price": 10.0},
        {"code": "600003", "name": "高开谨慎", "buy_point": "急跌观察", "score": 55, "price": 10.0},
        {"code": "600004", "name": "无数据", "buy_point": "急跌观察", "score": 50, "price": 10.0},
    ]
    quotes = {
        "600001": {"auction_price": 10.2, "last_close": 10.0, "available": True},   # +2%
        "600002": {"auction_price": 9.6, "last_close": 10.0, "available": True},    # -4%
        "600003": {"auction_price": 10.9, "last_close": 10.0, "available": True},   # +9%
    }
    out = {item["code"]: item for item in app.dixi.auction_verify(rows, quotes)}
    assert "符合计划" in out["600001"]["verdict"]
    assert "放弃" in out["600002"]["verdict"]
    assert "谨慎" in out["600003"]["verdict"]
    assert out["600004"]["available"] is False
    # 符合计划的排最前
    assert out["600001"] == app.dixi.auction_verify(rows, quotes)[0]
