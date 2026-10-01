from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from app.providers import MarketDataProvider


class FakeFFD:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_name = ""
        self.last_arguments = {}

    def call(self, name, arguments):
        self.calls += 1
        self.last_name = name
        self.last_arguments = arguments
        return self.payload

    def close(self):
        return None


def provider_with_state(tmp_path: Path) -> MarketDataProvider:
    provider = MarketDataProvider(timeout=1)
    provider.ffd_state_path = tmp_path / "ffd_state.json"
    provider._ffd_daily_call_limit = 6
    return provider


def test_ffd_columnar_payload_is_normalised():
    rows = MarketDataProvider._ffd_rows(
        {"data": {"ts_code": ["000001.SZ", "600000.SH"], "close": [10.1, None]}}
    )
    assert rows == [
        {"ts_code": "000001.SZ", "close": 10.1},
        {"ts_code": "600000.SH", "close": None},
    ]


def test_ffd_breadth_mapping_and_local_budget(tmp_path):
    provider = provider_with_state(tmp_path)
    provider._ffd = FakeFFD(
        {
            "raw_rows": [
                {
                    "trade_date": "2026-07-23",
                    "total_count": 5000,
                    "up_count": 3000,
                    "down_count": 1800,
                    "flat_count": 200,
                    "limit_up_count": 80,
                    "limit_down_count": 5,
                    "median_pct_chg": 0.8,
                    "avg_pct_chg": 1.1,
                }
            ]
        }
    )
    result = provider.get_ffd_market_breadth(force=True)
    assert result["source"] == "ffd_market_breadth"
    assert result["ratio"] == 0.625
    assert result["limit_up_count"] == 80
    assert provider.health()["ffd"]["calls_today"] == 1
    provider.close()


def test_universe_keeps_l2_missing_as_null(tmp_path):
    provider = provider_with_state(tmp_path)
    # 9-18 起全市场宇宙优先走 FFD 直连。本测试验证的是"本地 TDX 宇宙 +
    # FFD 日线参考合并"的兼容链路,必须把 FFD 网络与真实宇宙快照隔离。
    provider.data_dir = tmp_path
    provider.sync_ffd_market_daily = Mock(side_effect=RuntimeError("测试中禁用 FFD 同步"))
    provider._request = Mock(side_effect=RuntimeError("测试中禁用远程请求"))
    provider._tdx_quote_universe = lambda: [
        {
            "code": "000001",
            "name": "平安银行",
            "price": 10.0,
            "change_pct": 1.0,
            "amount": 1000000,
        }
    ]
    provider._cached_ffd_daily_rows = lambda: [
        {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "trade_date": "2026-07-22",
            "close": 9.9,
        }
    ]
    rows = provider.get_market_universe(force=True)
    assert rows[0]["source"] == "tdx_postclose_day+ffd_daily_reference"
    assert rows[0]["ffd_daily"]["close"] == 9.9
    assert rows[0]["l2_available"] is False
    assert rows[0]["l2_ten_level"] is None
    assert rows[0]["l2_imbalance"] is None
    assert rows[0]["l2_score_eligible"] is False
    provider.close()


def test_ffd_daily_universe_is_date_validated(tmp_path):
    provider = provider_with_state(tmp_path)
    provider._write_ffd_state(
        {
            "market_daily": {
                "trade_date": "2026-07-22",
                "rows": [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "trade_date": "2026-07-22",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.2,
                        "pre_close": 10.0,
                        "change": 0.2,
                        "pct_chg": 2.0,
                        "vol": 100,
                        "amount": 200000000,
                    }
                ],
            }
        }
    )
    rows = provider._ffd_daily_universe("2026-07-22")
    assert rows[0]["code"] == "000001"
    assert rows[0]["source"] == "ffd_market_daily_universe"
    assert rows[0]["ffd_trade_date"] == "2026-07-22"
    assert provider._ffd_daily_universe("2026-07-23") == []
    provider.close()


def test_float_mcap_reuses_persisted_market_daily_after_restart(tmp_path):
    provider = provider_with_state(tmp_path)
    provider._ffd = FakeFFD({})
    provider._write_ffd_state(
        {
            "market_daily": {
                "trade_date": "2026-09-18",
                "rows": [
                    {"ts_code": "000001.SZ", "float_market_cap": 123_000_000_000},
                    {"ts_code": "600000.SH", "float_mcap": 45_600_000_000},
                    {"ts_code": "bad", "float_market_cap": 1},
                ],
            }
        }
    )

    result = provider.get_ffd_float_mcap("2026-09-18")

    assert result == {"000001": 123_000_000_000.0, "600000": 45_600_000_000.0}
    assert provider._ffd.calls == 0
    saved = provider._read_ffd_state()
    assert saved["float_mcap_cache"]["trade_date"] == "2026-09-18"
    assert saved["float_mcap_cache"]["map"] == result
    provider.close()


def test_hydrate_postclose_klines_uses_remote_fallback(tmp_path):
    provider = provider_with_state(tmp_path)
    # 9-18 起先尝试 FFD 批量K线;模拟批量不可用,逼出 per-code 回退链路。
    provider.get_klines = Mock(side_effect=RuntimeError("测试中禁用 FFD 批量K线"))
    provider.get_kline = lambda code, days=120: [
        {"date": "2026-07-21", "close": 10.0, "source": "sina_daily_kline"},
        {"date": "2026-07-22", "close": 10.2, "source": "sina_daily_kline"},
    ]
    rows = provider.hydrate_postclose_klines(
        [{"code": "000001", "name": "平安银行", "amount": 1000, "postclose_kline": []}],
        max_symbols=1,
    )
    assert len(rows[0]["postclose_kline"]) == 2
    assert rows[0]["postclose_kline"][-1]["amount"] == 1000
    assert rows[0]["kline_source"] == "sina_daily_kline"
    provider.close()


def test_market_news_prefers_ffd_and_maps_public_fields(tmp_path):
    provider = provider_with_state(tmp_path)
    provider._ffd = FakeFFD(
        {
            "raw_rows": [
                {
                    "news_id": 42,
                    "headline": "芯片行业出现新进展",
                    "facts": "核心事实",
                    "insight": "关注产业链传导",
                    "sector": "科技",
                    "sector_tags": ["半导体"],
                    "market_tags": ["cn"],
                    "pub_dt": "2026-08-08T08:00:00+00:00",
                    "source_label": "公开新闻源",
                    "score": 8,
                    "sentiment_label": "偏利好",
                }
            ]
        }
    )
    rows = provider.get_market_news(limit=5)
    assert provider._ffd.last_name == "ffd_news_latest"
    assert rows[0]["title"] == "芯片行业出现新进展"
    assert rows[0]["content"] == "核心事实 关注产业链传导"
    assert rows[0]["publisher"] == "公开新闻源"
    assert rows[0]["source"] == "ffd_news"
    assert rows[0]["sector_tags"] == ["半导体"]
    provider.close()


def test_stock_news_uses_ffd_keyword_search(tmp_path):
    provider = provider_with_state(tmp_path)
    provider.get_quote = lambda code: {"code": code, "name": "平安银行"}
    provider._ffd = FakeFFD(
        {
            "items": [
                {
                    "news_id": 7,
                    "normalized_title": "公司公告摘要",
                    "facts": "公告事实",
                    "pub_dt": "2026-08-08T08:00:00+00:00",
                    "source_label": "公开新闻源",
                }
            ]
        }
    )
    rows = provider.get_stock_news("000001", limit=3)
    assert provider._ffd.last_name == "ffd_news_search"
    assert provider._ffd.last_arguments["q"] == "000001 平安银行"
    assert rows[0]["title"] == "公司公告摘要"
    assert rows[0]["source"] == "ffd_news"
    provider.close()
