import tempfile
import unittest
from pathlib import Path

from app import scoring
from app.providers import filter_stock_universe
from app.services import XunlongService
from app.storage import Database


def _rows(count: int = 80):
    rows = []
    price = 10.0
    for index in range(count):
        previous = price
        price *= 1.004
        rows.append(
            {
                "date": f"2026-01-{index + 1:02d}",
                "open": previous,
                "high": price * 1.01,
                "low": previous * 0.99,
                "close": price,
                "volume": 1_000_000,
                "amount": 20_000_000,
                "turnover": 3.0,
            }
        )
    return rows


class RulebookTests(unittest.TestCase):
    def test_universe_filter_keeps_growth_boards_and_excludes_star(self):
        rows = [
            {"code": "300001", "name": "创业板测试"},
            {"code": "301001", "name": "创业板测试2"},
            {"code": "600001", "name": "主板测试"},
            {"code": "688001", "name": "科创测试"},
            {"code": "689001", "name": "科创测试2"},
            {"code": "000001", "name": "测试ETF"},
        ]
        kept, stats = filter_stock_universe(rows)
        self.assertEqual({item["code"] for item in kept}, {"300001", "301001", "600001"})
        self.assertEqual(stats["star_excluded"], 2)
        self.assertTrue(scoring.is_a_share_security("300730", "科创信息"))
        self.assertFalse(scoring.is_star_security("300730", "科创信息"))

    def test_strategy_main_board_scope_excludes_growth_star_and_beijing(self):
        self.assertTrue(scoring.is_main_board_security("000001", "平安银行"))
        self.assertTrue(scoring.is_main_board_security("002001", "新和成"))
        self.assertTrue(scoring.is_main_board_security("600000", "浦发银行"))
        self.assertFalse(scoring.is_main_board_security("300001", "创业板测试"))
        self.assertFalse(scoring.is_main_board_security("301001", "创业板测试2"))
        self.assertFalse(scoring.is_main_board_security("688001", "科创板测试"))
        self.assertFalse(scoring.is_main_board_security("920130", "北交所测试"))

    def test_missing_kline_cannot_be_pushed(self):
        result = scoring.rulebook_score(
            {
                "code": "600001",
                "name": "测试股份",
                "price": 10,
                "amount": 20_000_000,
                "turnover_pct": 3,
                "pe_ttm": 15,
                "pb": 1.2,
            },
            [],
            {"score": 4, "label": "偏多"},
            "trend",
        )
        self.assertIsNone(result["trigger"]["confirmed"])
        self.assertFalse(result["push_eligible"])

    def test_dragon_mode_requires_emotion_mainline_leader_and_trigger(self):
        rows = _rows()
        # The calibrated model deliberately rejects a low-efficiency daily bar.
        # Make this fixture a genuine strong close so it remains a valid push test.
        latest = rows[-1]
        latest["open"] = latest["close"] * 0.98
        latest["low"] = latest["open"]
        latest["high"] = latest["close"] * 1.001
        phase = {"phase": "复苏", "breadth_ratio": 0.52}
        board = {"name": "机器人", "avg_change_pct": 3.0, "up_ratio": 0.75}
        result = scoring.dragon_score(
            {"code": "300001", "name": "测试股份", "industry": "机器人"},
            rows,
            phase,
            board,
            leader_rank=1,
            leader_count=8,
        )
        self.assertEqual(result["triggers"]["selected"], "板块回流")
        self.assertTrue(result["push_eligible"])
        self.assertEqual(result["leader"]["rank"], 1)

    def test_dragon_mode_vetoes_retreat(self):
        result = scoring.dragon_score(
            {"code": "600001", "industry": "机器人"},
            _rows(),
            {"phase": "退潮", "breadth_ratio": 0.2},
            {"name": "机器人", "avg_change_pct": 3, "up_ratio": 0.8},
            leader_rank=1,
            leader_count=6,
        )
        self.assertFalse(result["eligible"])
        self.assertFalse(result["push_eligible"])

    def test_dragon_phase_requires_confirmed_retreat_structure(self):
        snapshots = [{"code": f"600{i:03d}", "change_pct": -6 if i < 35 else -1} for i in range(100)]
        phase = scoring.dragon_market_phase({"score": -8, "breadth": {"ratio": 0.2}}, snapshots)
        self.assertEqual(phase["phase"], "退潮")
        self.assertEqual(phase["action"], "禁止开仓")
        self.assertGreater(phase["sample_size"], 0)

    def test_dragon_phase_does_not_use_index_score_alone(self):
        snapshots = [{"code": f"600{i:03d}", "change_pct": 1 if i < 55 else -1} for i in range(100)]
        phase = scoring.dragon_market_phase({"score": -8, "breadth": {"ratio": 0.55}}, snapshots)
        self.assertNotEqual(phase["phase"], "退潮")

    def test_dragon_phase_low_data_waits_for_confirmation(self):
        phase = scoring.dragon_market_phase({"score": -10, "breadth": {"ratio": 0.1}}, [{"change_pct": -8}])
        self.assertEqual(phase["confidence"], "low")
        self.assertNotEqual(phase["phase"], "退潮")

    def test_dragon_trigger_uses_live_quote_after_completed_kline(self):
        rows = _rows()
        rows[-1]["close"] = rows[-2]["close"] * 0.97
        result = scoring.dragon_score(
            {
                "code": "600001",
                "price": rows[-1]["close"] * 1.03,
                "last_close": rows[-1]["close"],
                "change_pct": 3.0,
                "industry": "测试行业",
            },
            rows,
            {"phase": "复苏"},
            {"name": "测试行业", "avg_change_pct": 2, "up_ratio": 0.8, "rank": 1},
            leader_rank=1,
            leader_count=5,
        )
        self.assertTrue(result["triggers"]["weak_to_strong"])
        self.assertTrue(result["data_coverage"]["live_quote_used"])

    def test_star_is_rejected_before_individual_analysis(self):
        class Provider:
            def get_quote(self, code):
                raise AssertionError("provider must not be called for STAR")

        with tempfile.TemporaryDirectory() as directory:
            service = XunlongService(Provider(), Database(Path(directory) / "rulebook.db"))
            with self.assertRaisesRegex(ValueError, "科创板"):
                service.stock_analysis("688001")

    def test_run_metadata_reports_full_pool_and_exclusions(self):
        class Provider:
            def get_market_overview(self):
                return {"indices": [], "source": "fixture"}

            def get_kline(self, code, days=120, **kwargs):
                return _rows()

            def get_market_universe(self, limit=None):
                return [
                    {"code": "300001", "name": "创业板测试", "amount": 20_000_000, "turnover": 3},
                    {"code": "600001", "name": "主板测试", "amount": 20_000_000, "turnover": 3},
                    {"code": "688001", "name": "科创测试", "amount": 20_000_000, "turnover": 3},
                ]

            def get_market_universe_stats(self):
                return {"input": 3, "kept": 2, "star_excluded": 1, "non_stock_excluded": 0}

            def health(self):
                return {"status": "ok", "sources": {}}

        with tempfile.TemporaryDirectory() as directory:
            service = XunlongService(Provider(), Database(Path(directory) / "run.db"))
            result = service.run_screener("rulebook", limit=12)
            self.assertEqual(result["universe_count"], 1)
            self.assertEqual(result["metadata"]["excluded_non_main_board_count"], 1)
            self.assertEqual(result["metadata"]["excluded_star_count"], 1)
            self.assertTrue(all(scoring.is_main_board_security(item["code"], item["name"]) for item in result["candidates"]))
            self.assertEqual(result["metadata"]["rulebook_version"], scoring.RULEBOOK_VERSION)


if __name__ == "__main__":
    unittest.main()
