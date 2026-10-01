from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.providers import MarketDataProvider
from app.qmt import QmtUnavailable


class FixedAuctionDateTime(datetime):
    """周二 09:26 —— 盘中竞价窗口,实时行情优先级场景。"""

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 15, 9, 26, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class FixedPostCloseDateTime(datetime):
    """周二 21:00 —— 收盘后,最新已完结交易日为当日。"""

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 15, 21, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


def _tick_row(code: str, price: float, last_close: float) -> dict:
    return {
        "code": code,
        "name": code,
        "price": price,
        "last_close": last_close,
        "previous_close": last_close,
        "open": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "volume": 1_000_000.0,
        "volume_lots": 10_000.0,
        "amount": price * 1_000_000.0,
        "amount_wan": price * 1_000_000.0 / 10_000.0,
        "change": price - last_close,
        "change_pct": round((price / last_close - 1) * 100, 4),
        "quote_time": "2026-09-15 09:26:03",
        "data_as_of": "2026-09-15 09:26:03",
        "trade_date": "2026-09-15",
        "available": True,
        "source": "qmt_tick",
        "stale": False,
    }


class FakeQmt:
    """鸭子类型的 QmtMarketData 替身,从不触碰真实 xtquant。"""

    def __init__(
        self,
        *,
        universe=None,
        ticks=None,
        klines=None,
        auction=None,
        enabled=True,
        kline_enabled=True,
        fail=False,
    ):
        self.enabled = enabled
        self.kline_enabled = kline_enabled
        self.universe = universe or []
        self.ticks = ticks or {}
        self.klines = klines or {}
        self.auction = auction or {}
        self.fail = fail
        self.universe_calls = 0
        self.tick_calls = 0
        self.auction_calls = 0
        self.kline_calls = 0

    def _maybe_fail(self) -> None:
        if self.fail:
            raise QmtUnavailable("QMT 客户端离线(测试桩)")

    def universe_rows(self):
        self.universe_calls += 1
        self._maybe_fail()
        return list(self.universe)

    def full_tick(self, codes):
        self.tick_calls += 1
        self._maybe_fail()
        wanted = set(codes)
        return {code: dict(row) for code, row in self.ticks.items() if code in wanted}

    def auction_rows(self, codes, *, now=None):
        self.auction_calls += 1
        self._maybe_fail()
        wanted = set(codes)
        return {code: dict(row) for code, row in self.auction.items() if code in wanted}

    def daily_kline(self, code, days, expected_date=None):
        self.kline_calls += 1
        self._maybe_fail()
        return list(self.klines.get(code) or [])

    def health(self):
        return {
            "enabled": self.enabled,
            "kline_enabled": self.kline_enabled,
            "library_path": "",
            "client_connected": not self.fail,
            "connected_checked_at": "",
            "last_success": "",
            "last_error": "",
            "import_error": "",
        }

    def close(self):
        return None


class QmtBridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.provider = MarketDataProvider(timeout=1)
        # 让宇宙快照/FFD 状态文件全部落在临时目录,保持测试无副作用。
        self.provider.data_dir = self.tmp_path
        self.provider.ffd_state_path = self.tmp_path / "ffd_state.json"

    def tearDown(self):
        self.provider.close()
        self._tmp.cleanup()

    def _enable(self, fake: FakeQmt) -> None:
        self.provider.qmt_enabled = True
        self.provider.qmt = fake

    def test_universe_prefers_qmt_when_client_online(self):
        fake = FakeQmt(
            universe=[
                {"code": "600000", "name": "浦发银行"},
                {"code": "000001", "name": "平安银行"},
            ],
            ticks={
                "600000": _tick_row("600000", 10.0, 9.9),
                "000001": _tick_row("000001", 11.0, 10.8),
            },
        )
        self._enable(fake)
        self.provider.FFD_DIRECT_MIN_USABLE_ROWS = 2
        self.provider._ffd_direct_universe = Mock(
            side_effect=AssertionError("QMT 在线时不得消耗 FFD 全市场调用")
        )
        with patch("app.providers.datetime", FixedPostCloseDateTime):
            rows = self.provider.get_market_universe()
        self.assertEqual({row["code"] for row in rows}, {"600000", "000001"})
        self.assertTrue(all(row.get("source") == "qmt_universe" for row in rows))
        self.assertEqual(fake.universe_calls, 1)
        self.assertEqual(fake.tick_calls, 1)
        self.provider._ffd_direct_universe.assert_not_called()

    def test_universe_falls_back_to_ffd_when_qmt_offline(self):
        fake = FakeQmt(fail=True)
        self._enable(fake)
        self.provider._ffd_direct_universe = Mock(
            return_value=[{"code": "600000", "name": "浦发银行", "price": 10.0}]
        )
        with patch("app.providers.datetime", FixedPostCloseDateTime):
            rows = self.provider.get_market_universe()
        self.assertEqual(fake.universe_calls, 1)
        self.assertEqual([row["code"] for row in rows], ["600000"])

    def test_quotes_use_qmt_without_spending_ffd_budget(self):
        fake = FakeQmt(ticks={"600519": _tick_row("600519", 1500.0, 1480.0)})
        self._enable(fake)
        self.provider._ffd_quote_snapshot = Mock(
            side_effect=AssertionError("QMT 在线时不得消耗 FFD 报价调用")
        )
        self.provider._reserve_ffd_call = Mock(return_value=True)
        with patch("app.providers.datetime", FixedAuctionDateTime):
            rows = self.provider.get_quotes(["600519"], force=True)
        self.assertEqual(rows["600519"]["price"], 1500.0)
        self.assertEqual(rows["600519"]["source"], "qmt_tick")
        self.assertEqual(fake.tick_calls, 1)
        self.provider._reserve_ffd_call.assert_not_called()

    def test_quotes_fall_back_to_ffd_when_qmt_offline(self):
        fake = FakeQmt(fail=True)
        self._enable(fake)
        self.provider._ffd_quote_snapshot = Mock(
            return_value={"sh600519": {"code": "600519", "price": 1500.0, "available": True}}
        )
        with patch("app.providers.datetime", FixedAuctionDateTime):
            rows = self.provider.get_quotes(["600519"], force=True)
        self.assertEqual(rows["600519"]["price"], 1500.0)
        self.provider._ffd_quote_snapshot.assert_called_once()

    def test_auction_quotes_qmt_covers_all_skips_ffd(self):
        auction_row = _tick_row("600001", 10.3, 10.0)
        auction_row.update(
            {
                "auction_price": 10.3,
                "auction_amount": 10_300_000.0,
                "auction_stage": "opening_call_auction_final",
                "auction_data_status": "final",
                "auction_source": "qmt_tick",
            }
        )
        fake = FakeQmt(auction={"600001": auction_row})
        self._enable(fake)
        self.provider._reserve_ffd_call = Mock(return_value=True)
        self.provider._ffd.call = Mock(
            side_effect=AssertionError("QMT 覆盖全部代码时不得消耗 FFD 预算")
        )
        with patch("app.providers.datetime", FixedAuctionDateTime):
            rows = self.provider.get_auction_quotes(["600001"])
        self.assertEqual(rows["600001"]["auction_source"], "qmt_tick")
        self.assertEqual(rows["600001"]["auction_price"], 10.3)
        self.assertEqual(fake.auction_calls, 1)
        self.provider._reserve_ffd_call.assert_not_called()

    def test_auction_quotes_fall_back_to_ffd_when_qmt_offline(self):
        fake = FakeQmt(fail=True)
        self._enable(fake)
        self.provider._reserve_ffd_call = Mock(return_value=True)
        self.provider._ffd.call = Mock(
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
                            "asset_as_of": "2026-09-15T09:25:00+08:00",
                            "trade_date": "2026-09-15",
                            "data_status": "final",
                        }
                    ]
                }
            }
        )
        with patch("app.providers.datetime", FixedAuctionDateTime):
            rows = self.provider.get_auction_quotes(["600001"])
        self.assertEqual(rows["600001"]["auction_price"], 10.3)
        self.assertEqual(fake.auction_calls, 1)
        self.provider._reserve_ffd_call.assert_called_once()

    def test_kline_prefers_qmt_local_bars(self):
        fake = FakeQmt(
            klines={
                "600000": [
                    {
                        "date": "20260915",
                        "open": 10.0,
                        "close": 10.2,
                        "high": 10.3,
                        "low": 9.9,
                        "volume": 1_000_000.0,
                        "amount": 10_200_000.0,
                        "change_pct": 2.0,
                        "source": "qmt_daily_kline",
                    }
                ]
            }
        )
        self._enable(fake)
        self.provider._ffd_history_batch = Mock(
            side_effect=AssertionError("QMT 在线时不得消耗 FFD 历史K线调用")
        )
        with patch("app.providers.datetime", FixedPostCloseDateTime):
            rows = self.provider.get_kline("600000", days=10)
        self.assertEqual(rows[-1]["source"], "qmt_daily_kline")
        self.assertEqual(fake.kline_calls, 1)
        self.provider._ffd_history_batch.assert_not_called()

    def test_kline_falls_back_to_ffd_when_qmt_offline(self):
        fake = FakeQmt(fail=True)
        self._enable(fake)
        self.provider._ffd_history_batch = Mock(
            return_value={
                "600000.SH": [
                    {
                        "date": "20260915",
                        "open": 10.0,
                        "close": 10.2,
                        "high": 10.3,
                        "low": 9.9,
                        "volume": 1_000_000.0,
                        "amount": 10_200_000.0,
                        "change_pct": 2.0,
                        "source": "ffd_quote_history",
                    }
                ]
            }
        )
        with patch("app.providers.datetime", FixedPostCloseDateTime):
            rows = self.provider.get_kline("600000", days=10)
        self.assertTrue(rows)
        self.assertEqual(fake.kline_calls, 1)
        self.provider._ffd_history_batch.assert_called_once()

    def test_health_reports_qmt_block(self):
        # conftest 默认关闭桥接:健康块必须存在且 enabled=False,保证
        # 测试环境与本地 QMT 客户端隔离。
        health = self.provider.health()
        self.assertIn("qmt", health)
        self.assertFalse(health["qmt"]["enabled"])


if __name__ == "__main__":
    unittest.main()
