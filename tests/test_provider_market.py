from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.providers import MarketDataProvider, ProviderError


class FixedWeekendDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 1, 10, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class FixedPostCloseDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 25, 21, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class FixedAuctionDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 25, 9, 20, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class FixedFinalAuctionDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 25, 9, 26, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class FixedPreopenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 25, 8, 29, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class ProviderMarketOverviewTests(unittest.TestCase):
    def test_old_live_auction_match_is_rejected(self):
        provider = MarketDataProvider(timeout=1)
        provider._reserve_ffd_call = Mock(return_value=False)
        provider._tencent_symbols = Mock(return_value={"sh600001": {
            "code": "600001", "price": 10.3, "auction_price": 10.3,
            "auction_volume_lots": 100, "auction_amount": 103_000,
            "quote_time": "20260825091500", "available": True,
        }})
        provider._eastmoney_auction_quotes = Mock(return_value={})
        try:
            with patch("app.providers.datetime", FixedFinalAuctionDateTime):
                row = provider.get_auction_quotes(["600001"])["600001"]
        finally:
            provider.close()
        self.assertFalse(row["available"])
        self.assertIn("older than two minutes", row["auction_rejected_reason"])

    def test_tencent_auction_turnover_uses_lots_when_amount_is_blank(self):
        provider = MarketDataProvider(timeout=1)
        fields = [""] * 53
        fields[1] = "测试股份"
        fields[3] = "10.3"
        fields[4] = "10.0"
        fields[6] = "100"
        fields[30] = "20260825092500"
        response = Mock(content=('v_sh600001="' + '~'.join(fields) + '";').encode("gbk"))
        provider._request = Mock(return_value=response)
        try:
            row = provider._tencent_symbols(["sh600001"])["sh600001"]
        finally:
            provider.close()
        self.assertEqual(row["auction_volume_lots"], 100)
        self.assertEqual(row["auction_amount"], 103_000)

    def test_call_auction_short_circuits_before_0915(self):
        provider = MarketDataProvider(timeout=1)
        provider.get_quotes = Mock(side_effect=AssertionError("pre-open must not request quotes"))
        provider._eastmoney_auction_quotes = Mock(side_effect=AssertionError("pre-open must not request quotes"))
        try:
            with patch("app.providers.datetime", FixedPreopenDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()

        self.assertFalse(rows["600001"]["available"])
        self.assertIn("not started", rows["600001"]["auction_rejected_reason"])
        provider.get_quotes.assert_not_called()

    def test_call_auction_prefers_ffd_terminal_asset(self):
        provider = MarketDataProvider(timeout=1)
        provider._reserve_ffd_call = Mock(return_value=True)
        provider._ffd.call = Mock(
            return_value={
                "data": {
                    "rows": [
                        {
                            "code": "600001.SH",
                            "name": "测试股份",
                            "pre_close": 10.0,
                            "auction_price": 10.3,
                            "auction_volume": 1000,
                            "auction_amount": 10300,
                            "auction_gain_pct": 3.0,
                            "asset_as_of": "2026-08-25T09:25:00+08:00",
                            "trade_date": "2026-08-25",
                            "data_status": "final",
                        }
                    ]
                }
            }
        )
        provider._tencent_symbols = Mock(side_effect=AssertionError("Tencent must not win over FFD"))
        provider._eastmoney_auction_quotes = Mock(
            side_effect=AssertionError("Eastmoney must not win over FFD")
        )
        try:
            with patch("app.providers.datetime", FixedPostCloseDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()

        row = rows["600001"]
        self.assertEqual(row["auction_source"], "ffd_market_microstructure")
        self.assertEqual(row["source"], "ffd_market_microstructure")
        self.assertEqual(row["auction_price"], 10.3)
        self.assertEqual(row["auction_volume_lots"], 10.0)
        self.assertTrue(row["available"])
        provider._ffd.call.assert_called_once()
        self.assertEqual(
            provider._ffd.call.call_args.args[1]["symbols"], ["600001.SH"]
        )

    def test_call_auction_rejects_unverified_tencent_quote_after_live_window(self):
        provider = MarketDataProvider(timeout=1)
        provider._reserve_ffd_call = Mock(return_value=True)
        provider._ffd.call = Mock(return_value={"data": {"rows": []}})
        provider._tencent_symbols = Mock(
            return_value={
                "sh600001": {
                    "code": "600001",
                    "price": 10.2,
                    "auction_price": 10.2,
                    "available": True,
                    "stale": False,
                    "source": "tencent_quote",
                    "trade_date": "2026-08-25",
                    "quote_time": "20260825092000",
                }
            }
        )
        provider._eastmoney_auction_quotes = Mock(
            side_effect=AssertionError("Eastmoney must remain the last fallback")
        )
        try:
            with patch("app.providers.datetime", FixedPostCloseDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()

        self.assertFalse(rows["600001"]["available"])
        self.assertIn("terminal marker", rows["600001"]["auction_rejected_reason"])
        provider._tencent_symbols.assert_called_once_with(["sh600001"])

    def test_call_auction_accepts_same_day_tencent_during_live_window(self):
        provider = MarketDataProvider(timeout=1)
        provider._tencent_symbols = Mock(
            return_value={
                "sh600001": {
                    "code": "600001",
                    "price": 10.2,
                    "auction_price": 10.2,
                    "available": True,
                    "stale": False,
                    "source": "tencent_quote",
                    "trade_date": "2026-08-25",
                    "quote_time": "20260825092000",
                }
            }
        )
        provider._eastmoney_auction_quotes = Mock(return_value={})
        try:
            with patch("app.providers.datetime", FixedAuctionDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()

        self.assertTrue(rows["600001"]["available"])
        self.assertEqual(rows["600001"]["auction_source"], "tencent_quote")

    def test_call_auction_complete_no_event_is_not_replaced_by_live_quote(self):
        provider = MarketDataProvider(timeout=1)
        provider._reserve_ffd_call = Mock(return_value=True)
        provider._ffd.call = Mock(
            return_value={
                "data": {
                    "rows": [
                        {
                            "code": "600001.SH",
                            "trade_date": "2026-08-25",
                            "data_status": "complete_no_event",
                        }
                    ]
                }
            }
        )
        provider.get_quotes = Mock(side_effect=AssertionError("No-event is authoritative"))
        provider._eastmoney_auction_quotes = Mock(
            side_effect=AssertionError("No-event is authoritative")
        )
        try:
            with patch("app.providers.datetime", FixedPostCloseDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()

        row = rows["600001"]
        self.assertEqual(row["auction_data_status"], "complete_no_event")
        self.assertFalse(row["available"])
        self.assertTrue(row["ffd_terminal"])

    def test_ffd_price_only_terminal_falls_through_to_tencent_turnover(self):
        provider = MarketDataProvider(timeout=1)
        provider._reserve_ffd_call = Mock(return_value=True)
        provider._ffd.call = Mock(return_value={"data": {"rows": [{
            "code": "600001.SH",
            "trade_date": "2026-08-25",
            "auction_price": 10.3,
            "auction_amount": 0,
            "data_status": "final",
        }]}})
        provider._tencent_symbols = Mock(return_value={"sh600001": {
            "code": "600001", "price": 10.3, "auction_price": 10.3,
            "auction_volume_lots": 100, "auction_amount": 103_000,
            "quote_time": "20260825092500", "available": True,
        }})
        try:
            with patch("app.providers.datetime", FixedFinalAuctionDateTime):
                rows = provider.get_auction_quotes(["600001"])
        finally:
            provider.close()
        self.assertEqual(rows["600001"]["auction_source"], "tencent_quote")
        self.assertEqual(rows["600001"]["auction_amount"], 103_000)

    def test_health_reports_ffd_fallback_as_degraded_but_operational(self):
        provider = MarketDataProvider(timeout=1)
        provider._read_ffd_state = Mock(
            return_value={"budget": {}, "market_daily": {}}
        )
        try:
            health = provider.health()
        finally:
            provider.close()

        self.assertTrue(health["ok"])
        self.assertTrue(health["operational"])
        self.assertTrue(health["degraded"])
        self.assertEqual(health["status"], "degraded")
        self.assertIn("FFD", health["degraded_reasons"][0])

    def test_network_windows_require_a_weekday(self):
        self.assertTrue(
            MarketDataProvider._is_live_index_window(datetime(2026, 7, 31, 9, 26))
        )
        self.assertTrue(
            MarketDataProvider._is_strategic_network_window(
                datetime(2026, 7, 31, 9, 28)
            )
        )
        self.assertFalse(
            MarketDataProvider._is_strategic_network_window(datetime(2026, 8, 1, 9, 28))
        )

    def test_weekend_overview_uses_tencent_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            vipdoc = Path(tmp)
            target = vipdoc / "sh" / "lday" / "sh000001.day"
            target.parent.mkdir(parents=True)
            target.write_bytes(
                struct.pack("<IIIIIfII", 20260730, 350000, 352000, 349000, 350000, 1.0, 100, 0)
                + struct.pack("<IIIIIfII", 20260731, 351000, 354000, 350000, 353500, 2.0, 200, 0)
            )
            provider = MarketDataProvider(timeout=1)
            provider.tdx_vipdoc = vipdoc
            # 9-18 起指数概览优先走 FFD;本测试验证周末兼容回退链
            # (腾讯最新快照 + 本地 TDX),需隔离 FFD 索引请求。
            provider._ffd_index_quotes = Mock(
                side_effect=ProviderError("FFD 索引在兼容性测试中应不可用")
            )
            provider._tencent_symbols = Mock(
                return_value={
                    "sh000001": {
                        "symbol": "sh000001",
                        "name": "上证指数",
                        "price": 3535.0,
                        "change_pct": 1.0,
                        "quote_time": "20260731150000",
                    }
                }
            )
            provider.get_northbound = Mock(side_effect=AssertionError("unexpected northbound call"))
            provider.get_ffd_market_breadth = Mock(return_value={})
            try:
                with patch("app.providers.datetime", FixedWeekendDateTime):
                    result = provider.get_market_overview()
            finally:
                provider.close()

            self.assertEqual(result["source"], "public_or_local_index_fallback")
            self.assertEqual(result["indices"][0]["price"], 3535.0)
            self.assertEqual(result["trade_date"], "2026-07-31")
            # 9-18 起回退概览按保守语义标记 data_delayed。
            self.assertTrue(result["data_delayed"])
            self.assertEqual(
                result["northbound_meta"]["source"],
                "not_requested_outside_strategic_window",
            )
            provider._tencent_symbols.assert_called_once()
            provider.get_northbound.assert_not_called()

    def test_stale_local_kline_is_replaced_by_newer_remote_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            vipdoc = Path(tmp)
            target = vipdoc / "sh" / "lday" / "sh600519.day"
            target.parent.mkdir(parents=True)
            target.write_bytes(
                struct.pack("<IIIIIfII", 20260821, 129150, 129150, 127201, 127283, 1.0, 100, 0)
            )
            payload = {
                "result": {
                    "data": [
                            {"day": "2026-08-24", "open": "1271.01", "close": "1304.66", "high": "1313.80", "low": "1270.33", "volume": "48440"},
                            {"day": "2026-08-25", "open": "1311.89", "close": "1304.00", "high": "1317.00", "low": "1301.11", "volume": "21111"},
                        ]
                }
            }
            response = Mock()
            response.json.return_value = payload
            provider = MarketDataProvider(timeout=1)
            provider.tdx_vipdoc = vipdoc
            # 9-18 起日K优先走 FFD 历史;本测试验证"过期本地K线被
            # 更新的远程会话替换"的兼容链路,需隔离 FFD 批量请求。
            provider._ffd_history_batch = Mock(
                side_effect=ProviderError("FFD 历史K线在兼容性测试中应不可用")
            )
            provider._request = Mock(return_value=response)
            try:
                with patch("app.providers.datetime", FixedPostCloseDateTime):
                    rows = provider.get_kline("600519", days=120)
            finally:
                provider.close()

            self.assertEqual(rows[-1]["date"], "2026-08-25")
            self.assertEqual(rows[-1]["source"], "sina_daily_kline")
            # 9-18 起回退结果按保守语义统一打 stale 标记(_meta.fallback),
            # 关键契约是:新会话数据(2026-08-25)确实替换了过期的本地文件。
            self.assertTrue(rows[-1]["stale"])
            provider._request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
