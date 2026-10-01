import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.services import XunlongService, _filter_same_day_auction_quotes, _present_yijiner_run
from app.storage import Database


def kline(code, limit_up=False):
    rows = []
    price = 10.0
    for index in range(80):
        open_price = price
        rate = 0.006
        if limit_up and index == 79:
            rate = 0.10
        price *= 1 + rate
        rows.append(
            {
                "date": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
                "open": open_price,
                "high": price * 1.01,
                "low": open_price * 0.99,
                "close": price,
                "volume": 2_000_000 + index * 50_000,
                "amount": 40_000_000,
                "turnover": 4,
            }
        )
    return rows


class FakeProvider:
    def __init__(self):
        self.rows = {f"600{i:03d}": kline(f"600{i:03d}", limit_up=i == 1) for i in range(1, 25)}

    def get_market_overview(self):
        return {
            "indices": [{"code": "000001", "name": "上证指数", "price": 3500, "change_pct": 0.8}],
            "source": "fixture",
            "as_of": "2026-07-17 09:26:00",
        }

    def get_market_universe(self, limit=None):
        rows = []
        for index, code in enumerate(self.rows):
            latest = self.rows[code][-1]
            rows.append(
                {
                    "code": code,
                    "name": f"测试{index + 1}",
                    "industry": "测试行业",
                    "open": latest["close"] * 1.02,
                    "amount": 120_000_000,
                    "market_cap": 8_000_000_000,
                    "volume_ratio": 2.2,
                    "turnover": 4.1,
                    "amplitude": 6.5,
                }
            )
        return rows[:limit]

    def get_kline(self, code, days=120, *args, **kwargs):
        if code == "000001":
            return kline(code)
        return self.rows.get(code, [])[-days:]

    def get_quote(self, code):
        row = self.rows[code][-1]
        return {"code": code, "name": "测试股份", "price": row["close"], "change_pct": 1.2}

    def get_quotes(self, codes):
        result = {}
        for code in codes:
            close = self.rows[code][-1]["close"]
            auction_price = close * 1.03
            result[code] = {
                "code": code,
                "name": "测试股份",
                "price": auction_price,
                "last_close": close,
                "open": auction_price,
                "change_pct": 3.0,
                "amount_wan": 1234.0,
                "bids": [
                    {"price": auction_price, "volume": 500},
                    {"price": auction_price * 0.999, "volume": 300},
                ],
                "asks": [
                    {"price": auction_price, "volume": 400},
                    {"price": auction_price * 1.001, "volume": 100},
                ],
                "quote_time": "20260901092003",
                "data_as_of": "2026-09-01T09:20:03+08:00",
                "trade_date": "2026-09-01",
                "source": "fixture_quote",
                "available": True,
            }
        return result

    def get_stock_info(self, code):
        return {"code": code, "name": "测试股份", "industry": "测试行业"}

    def get_boards(self, code):
        return {"boards": [{"name": "测试概念", "change_pct": 1.8}]}

    def get_fund_flow(self, code):
        return {"total_main_net": 20_000_000, "points": 120}

    def get_local_prediction(self, code):
        return {}

    def get_stock_news(self, code):
        return []

    def health(self):
        return {"status": "healthy", "sources": []}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "service.db")
        self.service = XunlongService(FakeProvider(), self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_pipeline_and_morning_confirmation_are_pool_only(self):
        first = self.service.run_screener("technical", 24)
        self.assertEqual(first["metadata"]["pipeline_stage"], "candidate_pool")
        self.assertIn("board_rotation", first["metadata"])
        confirmed = self.service.morning_confirmation(first)
        self.assertEqual(confirmed["metadata"]["pipeline_stage"], "morning_confirmation")
        self.assertEqual(confirmed["metadata"]["source_run_id"], first["id"])
        self.assertEqual(confirmed["metadata"]["new_symbols_added"], 0)
    def test_technical_screen_and_stock_analysis(self):
        result = self.service.run_screener("technical", 24)
        self.assertEqual(result["run_type"], "technical")
        self.assertEqual(result["universe_count"], 24)
        self.assertTrue(result["candidates"])
        analysis = self.service.stock_analysis("600001")
        self.assertEqual(len(analysis["trend_scores"]), 10)
        self.assertEqual(analysis["code"], "600001")

    def test_manual_research_pool_add_analyze_and_remove(self):
        added = self.service.add_research_stock("600001", "重点观察")
        self.assertEqual(added["code"], "600001")
        with self.assertRaisesRegex(ValueError, "沪深主板"):
            self.service.add_research_stock("300001")
        analyzed = self.service.analyze_research_stock("600001")
        self.assertEqual(analyzed["analysis"]["code"], "600001")
        self.assertIn("news", analyzed["analysis"])
        self.assertIn("technical", analyzed["analysis"])
        self.assertIn("financial", analyzed["analysis"])
        self.assertTrue(self.service.remove_research_stock("600001"))
        self.assertEqual(self.service.research_pool()["count"], 0)

    def test_batch_import_extracts_deduplicates_and_rejects_non_main_board(self):
        result = self.service.import_research_stocks(
            "600001 测试一\nSH600002, 600001\n300001 创业板\n无效文本",
            "复制导入",
        )
        self.assertEqual(result["detected_count"], 3)
        self.assertEqual(result["added_codes"], ["600001", "600002"])
        self.assertEqual(result["rejected"], [{"code": "300001", "reason": "非沪深主板"}])
        self.assertEqual(self.service.research_pool()["count"], 2)

        repeated = self.service.import_research_stocks("600001\t600002")
        self.assertEqual(repeated["added_count"], 0)
        self.assertEqual(repeated["updated_count"], 2)

    def test_auction_screen_is_auditable(self):
        result = self.service.run_screener("auction", 24)
        self.assertGreaterEqual(result["first_board_count"], 1)
        if result["candidates"]:
            self.assertEqual(len(result["candidates"][0]["gates"]), 17)

    def test_yijiner_rejects_previous_day_and_unverified_off_window_quotes(self):
        previous_day = {
            "600001": {
                "auction_price": 11.0,
                "trade_date": "2026-09-17",
                "available": True,
                "auction_source": "tencent_quote",
            }
        }
        accepted, state = _filter_same_day_auction_quotes(
            previous_day, datetime(2026, 9, 18, 9, 20)
        )
        self.assertEqual(accepted, {})
        self.assertFalse(state["valid"])

        ordinary_close = {
            "600001": {
                "auction_price": 11.0,
                "trade_date": "2026-09-18",
                "available": True,
                "auction_source": "tencent_quote",
            }
        }
        accepted, state = _filter_same_day_auction_quotes(
            ordinary_close, datetime(2026, 9, 18, 15, 5)
        )
        self.assertEqual(accepted, {})
        self.assertIn("非竞价窗口且缺少终态标记", state["rejected_reasons"])

        terminal = {
            "600001": {
                "auction_price": 11.0,
                "trade_date": "2026-09-18",
                "available": True,
                "auction_source": "ffd_market_microstructure",
                "auction_data_status": "final",
                "auction_amount": 10_300,
            }
        }
        accepted, state = _filter_same_day_auction_quotes(
            terminal, datetime(2026, 9, 18, 15, 5)
        )
        self.assertIn("600001", accepted)
        self.assertTrue(state["valid"])

    def test_price_only_auction_quote_is_not_treated_as_complete_data(self):
        accepted, state = _filter_same_day_auction_quotes(
            {"600001": {
                "trade_date": "2026-09-18",
                "auction_price": 10.3,
                "auction_amount": 0,
                "available": True,
            }},
            datetime(2026, 9, 18, 9, 20),
        )
        self.assertEqual(accepted, {})
        self.assertFalse(state["valid"])
        self.assertEqual(state["rejected_reasons"]["缺少竞价成交额，仅有价格快照"], 1)

    def test_legacy_premarket_yijiner_run_is_safely_presented(self):
        run = {
            "trade_date": "2026-09-17",
            "created_at": "2026-09-18T08:29:20",
            "auction_available_count": 1,
            "metadata": {"market_state": {"auction_breadth": 1, "auction_available": True}},
            "rows": [{
                "first_board_score": 82.5,
                "score": 70.0,
                "tier": "B",
                "auction_stage_score": 55.0,
                "auction_change_pct": 10.0,
                "decision": "candidate",
                "risk_flags": ["竞价高开过猛"],
                "snapshot": {"auction_available": True},
            }],
        }
        shown = _present_yijiner_run(run)
        self.assertEqual(shown["scan_date"], "2026-09-18")
        self.assertEqual(shown["base_trade_date"], "2026-09-17")
        self.assertFalse(shown["metadata"]["auction_validation"]["valid"])
        self.assertEqual(shown["metadata"]["market_state"]["auction_breadth"], None)
        self.assertEqual(shown["rows"][0]["score"], 82.5)
        self.assertFalse(shown["rows"][0]["snapshot"]["auction_available"])

    def test_premarket_yijiner_preview_does_not_replace_latest_run(self):
        snapshot = {
            "code": "600001",
            "name": "测试首板",
            "industry": "测试行业",
            "float_market_cap": 3_000_000_000,
            "postclose_kline": [
                {"date": "2026-09-16", "close": 10.0, "volume": 1_000_000, "amount": 300_000_000},
                {"date": "2026-09-17", "close": 11.0, "volume": 2_000_000, "amount": 500_000_000},
            ],
        }
        self.service.provider.get_market_universe = Mock(return_value=[snapshot])

        result = self.service.yijiner_scan(now=datetime(2026, 9, 18, 8, 29))

        self.assertEqual(result["status"], "waiting")
        self.assertFalse(result["persisted"])
        self.assertIsNone(self.db.get_latest_yijiner_run())
        self.service.provider.get_market_universe.assert_not_called()

    def test_explicit_premarket_preview_refreshes_latest_completed_session(self):
        snapshot = {
            "code": "600001",
            "name": "测试首板",
            "industry": "测试行业",
            "float_market_cap": 3_000_000_000,
            "postclose_kline": [
                {"date": "2026-09-17", "close": 10.0, "volume": 1_000_000, "amount": 300_000_000},
                {"date": "2026-09-18", "close": 11.0, "volume": 2_000_000, "amount": 500_000_000},
            ],
        }
        self.service.provider.get_market_universe = Mock(return_value=[snapshot])

        result = self.service.yijiner_premarket_preview(now=datetime(2026, 9, 21, 8, 29))

        self.assertTrue(result["preview"])
        self.assertFalse(result["persisted"])
        self.assertEqual(result["scan_date"], "2026-09-21")
        self.assertEqual(result["base_trade_date"], "2026-09-18")
        self.service.provider.get_market_universe.assert_called_once_with(limit=None)
        self.assertIsNone(self.db.get_latest_yijiner_run())

    def test_duplicate_yijiner_scan_is_rejected_without_provider_work(self):
        self.service.provider.get_market_universe = Mock(side_effect=AssertionError("duplicate scan must not fetch"))
        self.service._yijiner_scan_lock.acquire()
        try:
            result = self.service.yijiner_scan(now=datetime(2026, 9, 18, 9, 27))
        finally:
            self.service._yijiner_scan_lock.release()

        self.assertEqual(result["status"], "busy")
        self.assertFalse(result["persisted"])
        self.service.provider.get_market_universe.assert_not_called()

    def test_valid_terminal_yijiner_scan_is_persisted_with_both_dates(self):
        snapshot = {
            "code": "600001",
            "name": "测试首板",
            "industry": "测试行业",
            "float_market_cap": 3_000_000_000,
            "postclose_kline": [
                {"date": "2026-09-16", "close": 10.0, "volume": 1_000_000, "amount": 300_000_000},
                {"date": "2026-09-17", "close": 11.0, "volume": 2_000_000, "amount": 500_000_000},
            ],
        }
        self.service.provider.get_market_universe = Mock(return_value=[snapshot])
        self.service.provider.get_auction_quotes = Mock(return_value={
            "600001": {
                "auction_price": 11.33,
                "last_close": 11.0,
                "auction_amount": 60_000_000,
                "trade_date": "2026-09-18",
                "auction_source": "ffd_market_microstructure",
                "auction_data_status": "final",
                "available": True,
                "stale": False,
            }
        })

        result = self.service.yijiner_scan(now=datetime(2026, 9, 18, 15, 5))
        stored = self.db.get_latest_yijiner_run()

        self.assertTrue(result["persisted"])
        self.assertEqual(stored["trade_date"], "2026-09-18")
        self.assertEqual(stored["metadata"]["scan_date"], "2026-09-18")
        self.assertEqual(stored["metadata"]["base_trade_date"], "2026-09-17")

    def test_live_auction_overlay_updates_only_existing_candidates(self):
        run = self.service.run_screener("technical", 24)
        live = self.service.live_auction_candidates(
            run["id"],
            now=datetime(2026, 9, 1, 9, 20, 4),
        )
        self.assertEqual(live["run_id"], run["id"])
        self.assertEqual(live["session"], "auction")
        self.assertTrue(live["active"])
        self.assertEqual(live["candidate_count"], len(run["candidates"]))
        self.assertAlmostEqual(live["candidates"][0]["gap_pct"], 3.0)
        self.assertEqual(live["candidates"][0]["auction_amount"], 12_340_000)
        self.assertEqual(live["candidates"][0]["unmatched_direction"], "买方")

    def test_live_auction_uses_verified_ffd_match_amount_and_volume(self):
        base = self.service.provider

        def ffd_quotes(codes):
            rows = {}
            for code in codes:
                close = base.rows[code][-1]["close"]
                rows[code] = {
                    "code": code,
                    "name": "测试股份",
                    "price": close * 1.03,
                    "last_close": close,
                    "open": close * 1.03,
                    "auction_price": close * 1.03,
                    "auction_volume_lots": 10,
                    "auction_amount": 10300,
                    "auction_unmatched_buy_lots": 500,
                    "auction_unmatched_sell_lots": 100,
                    "quote_time": "2026-09-01T09:25:00+08:00",
                    "data_as_of": "2026-09-01T09:25:00+08:00",
                    "trade_date": "2026-09-01",
                    "source": "ffd_market_microstructure",
                    "auction_source": "ffd_market_microstructure",
                    "available": True,
                    "stale": False,
                }
            return rows

        base.get_auction_quotes = Mock(side_effect=ffd_quotes)
        run = self.service.run_screener("technical", 24)
        live = self.service.live_auction_candidates(
            run["id"],
            now=datetime(2026, 9, 1, 9, 26, 0),
        )
        row = live["candidates"][0]
        self.assertEqual(row["source"], "ffd_market_microstructure")
        self.assertEqual(row["matched_volume_lots"], 10)
        self.assertEqual(row["auction_amount"], 10300)
        self.assertEqual(row["matched_amount"], 10300)
        self.assertEqual(row["unmatched_direction"], "买方")

    def test_dragon_scan_persists_breadth_for_next_run(self):
        result = self.service.run_screener("dragon", 24)
        self.assertIn("breadth_ratio", result["metadata"])
        self.assertIn("dragon_emotion", result["metadata"])
        # market_context must pass the index K-line into the 12-factor timing
        # layer; the fixture has enough rows for crowding/liquidity checks.
        emotion = result["candidates"][0]["snapshot"]["rulebook"]["emotion"]
        self.assertTrue(emotion["timing_risk"]["crowding_fragility"]["available"])
        self.assertTrue(emotion["timing_risk"]["liquidity_compression"]["available"])

    def test_latest_review_exposes_full_seven_step_report(self):
        self.service.run_screener("dragon", 24)
        with patch("app.review.LOG_DIR", self.temp.name):
            review = self.service.latest_review()
        self.assertEqual(review["report_format"], "seven-step-review-v1")
        self.assertIn("## 01 市场情绪", review["report_markdown"])
        self.assertIn("## 07 次日预案", review["report_markdown"])

    def test_bot_command(self):
        response = self.service.bot_command("600001")
        self.assertEqual(response["type"], "stock")
        self.assertIn("仅供研究", response["text"])
        self.assertFalse(response["delivery"]["sent"])

    def test_bot_command_delivers_response_to_wecom(self):
        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
        self.db.update_settings({"wecom_webhook": webhook})
        provider_response = Mock(ok=True, status_code=200)
        provider_response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        with patch("app.services.requests.post", return_value=provider_response) as post:
            response = self.service.bot_command("状态")
        self.assertEqual(response["type"], "status")
        self.assertTrue(response["delivery"]["sent"])
        self.assertEqual(
            post.call_args.kwargs["json"]["text"]["content"], response["text"]
        )

    def test_wecom_message_success_and_application_error(self):
        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
        self.db.update_settings({"wecom_webhook": webhook})
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        with patch("app.services.requests.post", return_value=response) as post:
            result = self.service.send_test_message("测试消息")
        self.assertTrue(result["sent"])
        post.assert_called_once_with(
            webhook,
            json={"msgtype": "text", "text": {"content": "测试消息"}},
            timeout=12,
        )

        response.json.return_value = {"errcode": 93000, "errmsg": "invalid webhook"}
        with patch("app.services.requests.post", return_value=response):
            result = self.service.send_test_message("测试消息")
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "invalid webhook")

    def test_wecom_rejects_non_official_webhook(self):
        self.db.update_settings({"wecom_webhook": "http://127.0.0.1/internal"})
        with patch("app.services.requests.post") as post:
            result = self.service.send_test_message("测试消息")
        self.assertFalse(result["sent"])
        self.assertIn("地址无效", result["reason"])
        post.assert_not_called()

    def test_manual_job_delivers_to_wecom(self):
        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
        self.db.update_settings({"wecom_webhook": webhook})
        self.db.update_job("morning-auction", {"channel": "wecom"})
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        with patch("app.services.requests.post", return_value=response) as post:
            result = self.service.run_job("morning-auction")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["result"]["delivery"]["sent"])
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()

