import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.storage import Database


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_secret_masking(self):
        settings = self.db.get_settings()
        self.assertEqual(settings["app_name"], "寻龙工作台")
        self.db.update_settings({"wecom_webhook": "https://example.invalid/very-long-secret-token"})
        masked = self.db.get_settings(mask_secrets=True)["wecom_webhook"]
        self.assertIn("...", masked)
        self.assertNotIn("secret-token", masked)

    def test_morning_job_precomputes_before_0928_delivery(self):
        job = self.db.get_job("morning-auction")
        self.assertEqual(job["name"], "每日擒龙预扫描")
        self.assertIn("09:26", job["schedule"])
        self.assertIn("09:28", job["schedule"])
        next_run = self.db.next_scheduled_time(
            "morning-auction", datetime(2026, 7, 27, 8, 0)
        )
        self.assertEqual(next_run, "2026-07-27T09:26")

    def test_each_intraday_job_reports_its_real_next_run_time(self):
        now = datetime(2026, 7, 27, 8, 0)
        expected = {
            "auction-trace": "2026-07-27T09:22",
            "morning-auction": "2026-07-27T09:26",
            "yijiner-scan": "2026-07-27T09:27",
            "afternoon-review": "2026-07-27T15:01",
            "overnight-pool": "2026-07-27T15:05",
            "dixi-scan": "2026-07-27T15:10",
        }
        for job_id, next_run in expected.items():
            with self.subTest(job_id=job_id):
                self.assertEqual(self.db.next_scheduled_time(job_id, now), next_run)

    def test_legacy_screen_results_hide_non_main_board_candidates(self):
        created = self.db.create_screen_run(
            {
                "trade_date": "2026-08-01",
                "run_type": "dragon",
                "universe_count": 3,
                "metadata": {"threshold": 62},
            },
            [
                {"code": "600001", "name": "主板测试", "score": 80, "decision": "watch"},
                {"code": "300001", "name": "创业板测试", "score": 79, "decision": "watch"},
                {"code": "920130", "name": "北交所测试", "score": 78, "decision": "watch"},
            ],
        )
        loaded = self.db.get_screen_run(created["id"])
        self.assertEqual([item["code"] for item in loaded["candidates"]], ["600001"])
        self.assertEqual(loaded["candidate_count"], 1)
        self.assertEqual(loaded["metadata"]["legacy_non_main_removed_count"], 2)

    def test_screen_run_round_trip(self):
        saved = self.db.create_screen_run(
            {
                "trade_date": "2026-07-17",
                "run_type": "technical",
                "universe_count": 24,
                "metadata": {"funnel": [{"label": "前置过滤", "count": 24}]},
            },
            [
                {
                    "code": "600001",
                    "name": "测试股份",
                    "score": 11,
                    "decision": "push",
                    "breakdown": {"ma5": 3},
                    "gates": [],
                    "snapshot": {"source": "fixture"},
                }
            ],
        )
        self.assertEqual(saved["push_count"], 1)
        self.assertEqual(saved["candidates"][0]["breakdown"]["ma5"], 3)
        self.assertEqual(self.db.get_latest_screen_run("technical")["id"], saved["id"])

    def test_research_watchlist_round_trip(self):
        saved = self.db.upsert_research_stock("600001", "主板测试", "观察财报")
        self.assertEqual(saved["note"], "观察财报")
        analyzed = self.db.save_research_analysis("600001", {"name": "主板测试", "score": 80})
        self.assertEqual(analyzed["analysis"]["score"], 80)
        self.assertEqual(len(self.db.list_research_watchlist()), 1)
        self.assertTrue(self.db.delete_research_stock("600001"))
        self.assertEqual(self.db.list_research_watchlist(), [])

    def test_backtest_statistics_only_count_pushes(self):
        payload = self.db.list_backtests()
        self.assertGreater(payload["stats"]["push_count"], 0)
        self.assertLessEqual(payload["stats"]["hit_rate"], 100)


if __name__ == "__main__":
    unittest.main()
