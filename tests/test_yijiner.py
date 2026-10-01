from __future__ import annotations

from app import yijiner


def first_board_snapshot(
    code: str = "600001",
    name: str = "测试首板",
    industry: str = "电力设备",
    *,
    limit_up_pct: float = 10.0,
    float_mcap: float = 3_000_000_000.0,
    prev_amount: float = 500_000_000.0,
    base_price: float = 10.0,
    days: int = 40,
) -> dict:
    bars = []
    price = base_price
    for index in range(days - 1):
        bars.append(
            {
                "date": f"2026-08-{index + 1:02d}",
                "open": price,
                "high": price * 1.02,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000,
                "amount": prev_amount * 0.8,
            }
        )
    close = price * (1 + limit_up_pct / 100.0)
    bars.append(
        {
            "date": "2026-09-05",
            "open": price * 1.01,
            "high": close * 1.001,
            "low": price * 1.005,
            "close": close,
            "volume": 2_000_000,
            "amount": prev_amount,
        }
    )
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "float_market_cap": float_mcap,
        "postclose_kline": bars,
    }


def auction_quote(
    code: str = "600001",
    *,
    auction_price: float = 11.55,
    last_close: float = 11.0,
    auction_amount: float = 50_000_000.0,
) -> dict:
    return {
        "code": code,
        "name": "测试首板",
        "auction_price": auction_price,
        "last_close": last_close,
        "auction_amount": auction_amount,
        "change_pct": (auction_price / last_close - 1.0) * 100.0,
        "available": True,
        "source": "test",
    }


def test_board_count_counts_consecutive_limit_ups():
    closes = [10.0, 11.0, 12.1, 13.31]
    assert yijiner.board_count(closes) == 3
    assert yijiner.board_count([10.0, 10.5]) == 0


def test_auction_tier_thresholds():
    snapshot = first_board_snapshot()
    # 竞价额 6% 昨日成交额 -> B 档；竞价额/流通市值=1% 仅作参考
    quote = auction_quote(auction_amount=30_000_000.0)
    row = yijiner.evaluate_candidate(snapshot, quote)
    assert row is not None
    assert row["tier"] == "B"
    assert row["auction_amount_to_mcap_pct"] == 1.0


def test_auction_stage_uses_documented_component_weights():
    snapshot = first_board_snapshot()
    row = yijiner.evaluate_candidate(snapshot, auction_quote())
    assert row is not None
    # amount ratio=10% (100), gap=5% (100), burst=10% S档 (100),
    # sector=50 and market breadth=50.
    expected = 100 * 0.35 + 100 * 0.20 + 100 * 0.20 + 50 * 0.15 + 50 * 0.10
    assert row["auction_stage_score"] == expected


def test_d_tier_sorts_before_unranked_candidates():
    snapshots = [
        first_board_snapshot(code="600001", name="D档"),
        first_board_snapshot(code="600002", name="未入档"),
    ]
    quotes = {
        "600001": auction_quote("600001", auction_amount=12_000_000.0),  # 2.4% -> D档
        "600002": auction_quote("600002", auction_amount=8_000_000.0),   # 1.6% -> 无档
    }
    result = yijiner.screen_yijiner(snapshots, quotes)
    assert [row["tier"] for row in result["rows"][:2]] == ["D", ""]


def test_first_board_only_and_main_board_filter():
    snapshot = first_board_snapshot()
    row = yijiner.evaluate_candidate(snapshot, auction_quote())
    assert row is not None and row["boards"] == 1
    # 非主板被剔除
    cyb = first_board_snapshot(code="300001")
    assert yijiner.evaluate_candidate(cyb, auction_quote(code="300001")) is None
    # ST 被剔除
    st = first_board_snapshot(name="ST测试")
    assert yijiner.evaluate_candidate(st, auction_quote()) is None
    # 连板票不是一进二标的（再追加一根涨停 => 二板）
    two_board = first_board_snapshot()
    two_board["postclose_kline"].append(
        {
            "date": "2026-09-06",
            "open": two_board["postclose_kline"][-1]["close"] * 1.01,
            "high": two_board["postclose_kline"][-1]["close"] * 1.102,
            "low": two_board["postclose_kline"][-1]["close"] * 1.008,
            "close": two_board["postclose_kline"][-1]["close"] * 1.1,
            "volume": 2_000_000,
            "amount": 600_000_000.0,
        }
    )
    assert yijiner.evaluate_candidate(two_board, auction_quote()) is None


def test_without_auction_data_score_degrades():
    snapshot = first_board_snapshot()
    row = yijiner.evaluate_candidate(snapshot, None)
    assert row is not None
    assert row["auction_available"] is False
    assert row["auction_stage_score"] is None
    assert any("竞价" in flag for flag in row["risk_flags"])


def test_screen_yijiner_pipeline():
    snapshots = [
        first_board_snapshot(),
        first_board_snapshot(code="600002", name="同行业首板", industry="电力设备"),
        first_board_snapshot(code="600003", name="其他行业", industry="酿酒"),
        {
            "code": "600999",
            "name": "未涨停",
            "industry": "酿酒",
            "float_market_cap": 3_000_000_000.0,
            "postclose_kline": first_board_snapshot()["postclose_kline"][:-1],
        },
    ]
    quotes = {
        "600001": auction_quote("600001", auction_amount=400_000_000.0),
        "600002": auction_quote("600002", auction_amount=8_000_000.0),
    }
    result = yijiner.screen_yijiner(snapshots, quotes)
    codes = [item["code"] for item in result["rows"]]
    assert "600001" in codes and "600002" in codes
    # 600003 无竞价数据也会出现，但必须标记缺失与降级
    no_quote = next(item for item in result["rows"] if item["code"] == "600003")
    assert no_quote["auction_available"] is False
    assert no_quote["auction_stage_score"] is None
    funnel = result["funnel"]
    assert funnel["prev_first_boards"] == 3
    assert result["market_state"]["prev_limit_up_count"] == 3
    top = result["rows"][0]
    # 600001 竞价额占昨日成交额 80% 应为 S 档并排在第一
    assert top["code"] == "600001"
    assert top["tier"] == "S"
    assert top["decision"] == "candidate"
    assert result["tier_counts"]["S"] == 1


def test_limit_pool_enriches_scoring():
    snapshot = first_board_snapshot()
    pool_row = {
        "ts_code": "600001.SH",
        "name": "测试首板",
        "pool_type": "limit_up",
        "lianban_count": 1,
        "open_count": 0,
        "first_limit_time": "09:31:00",
        "limit_reason": "AI算力+机器人",
        "related_concepts": "算力;机器人",
    }
    row = yijiner.evaluate_candidate(snapshot, auction_quote(), limit_pool_row=pool_row)
    assert row is not None
    assert row["open_count"] == 0
    assert row["first_limit_time"] == "09:31:00"
    assert row["limit_reason"] == "AI算力+机器人"
    capital = row["breakdown"]["first_board"]["capital"]
    keys = {c["key"] for c in capital["components"]}
    assert {"open_count", "first_limit_time"} <= keys
    assert capital["missing"] == ["封单额（FFD 涨停池暂无此字段）"]
    two = dict(pool_row, lianban_count=2)
    assert yijiner.evaluate_candidate(snapshot, auction_quote(), limit_pool_row=two) is None


def test_screen_yijiner_uses_pool_sealed_ratio():
    snapshots = [first_board_snapshot()]
    pool = {
        "by_code": {
            "600001": {
                "ts_code": "600001.SH",
                "pool_type": "limit_up",
                "lianban_count": 1,
                "open_count": 0,
                "first_limit_time": "09:35:00",
                "limit_reason": "测试题材",
                "related_concepts": "测试",
            }
        },
        "stats": {"limit_up_count": 40, "broken_count": 10, "multi_board_count": 5, "board_ladder": {"首板": 35}},
    }
    result = yijiner.screen_yijiner(snapshots, {"600001": auction_quote()}, limit_pool=pool)
    assert abs(result["market_state"]["market_sealed_ratio"] - 0.8) < 1e-6
    row = result["rows"][0]
    assert row["open_count"] == 0
    sentiment = row["breakdown"]["first_board"]["sentiment"]
    assert "封板率" in sentiment["note"]


def test_path_weak_to_strong_trace_factor():
    """09:2x 采样水下、终态健康高开 => 竞价过程弱转强；早期高开回落 => 冲高回落。"""
    snapshot = first_board_snapshot()
    row = yijiner.evaluate_candidate(snapshot, auction_quote(), trace_low=-6.0)
    assert row is not None
    assert row["trace_low_pct"] == -6.0
    assert row["path_weak_to_strong"] is True
    assert row["path_fading"] is False
    assert any("竞价过程弱转强" in flag for flag in row["risk_flags"])

    fading = yijiner.evaluate_candidate(
        snapshot, auction_quote(auction_price=10.9, last_close=11.0), trace_low=6.5
    )
    assert fading["path_weak_to_strong"] is False
    assert fading["path_fading"] is True
    assert any("冲高回落" in flag for flag in fading["risk_flags"])

    no_trace = yijiner.evaluate_candidate(snapshot, auction_quote())
    assert no_trace["trace_low_pct"] is None
    assert no_trace["path_weak_to_strong"] is False


def test_compute_perf20_metrics():
    """20日表现：涨幅/距高点/涨停计数/量比/回撤可算且方向正确。"""
    bars = []
    price = 10.0
    for index in range(40):
        bars.append(
            {
                "date": f"2026-{8 + index // 28:02d}-{index % 28 + 1:02d}",
                "open": price,
                "high": price * 1.02,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000,
                "amount": 200_000_000.0,
            }
        )
        price *= 1.02
    bars[-1]["close"] = price  # 最后一天平稳
    perf = yijiner.compute_perf20(bars)
    assert perf["days"] == 20
    assert perf["ret20_pct"] > 40  # 每日+2% 复利约+48%
    assert perf["dist_high_pct"] <= 0
    assert perf["pos_in_range"] >= 80
    assert perf["limit_up_count"] == 0
    assert perf["vol_5v20"] == 1.0
    assert perf["vol_5v20_basis"] == "amount"
    assert perf["max_drawdown_pct"] <= 0
    assert len(perf["daily"]) == 20
    assert yijiner.compute_perf20(bars[:3]) == {}


def test_compute_perf20_falls_back_to_volume_when_amount_is_missing():
    bars = []
    for index in range(40):
        bars.append(
            {
                "date": f"2026-08-{index + 1:02d}",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 2_000_000 if index >= 35 else 1_000_000,
                "amount": 0,
            }
        )

    perf = yijiner.compute_perf20(bars)

    assert perf["vol_5v20"] == 1.6
    assert perf["vol_5v20_basis"] == "volume"
