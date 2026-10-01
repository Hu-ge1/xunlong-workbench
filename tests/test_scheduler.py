from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from app.scheduler import LocalScheduler
from app.storage import Database


class SchedulerTests(unittest.TestCase):
    def test_terminal_auction_scan_runs_before_morning_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "scheduler.db")
            run_job = Mock(return_value={"status": "success"})
            scheduler = LocalScheduler(database, run_job)

            scheduler._tick(datetime(2026, 7, 27, 9, 20))
            run_job.assert_not_called()

            with database.connect() as conn:
                conn.execute(
                    "INSERT INTO job_runs(job_id,run_date,run_key,status,started_at) VALUES(?,?,?,?,?)",
                    ("auction-trace", "2026-07-27", "auction-trace:2026-07-27:auto", "success", "2026-07-27T09:22:00"),
                )
            scheduler._tick(datetime(2026, 7, 27, 9, 26))
            run_job.assert_called_once_with("yijiner-scan", False)

            run_job.reset_mock()
            scheduler._tick(datetime(2026, 7, 27, 9, 28))
            self.assertEqual(run_job.call_args_list[0].args, ("yijiner-scan", False))
            self.assertEqual(run_job.call_args_list[1].args, ("morning-auction", False))

    def test_yijiner_catches_up_when_morning_job_blocks_past_0927(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "scheduler.db")
            with database.connect() as conn:
                for job_id in ("auction-trace", "morning-auction"):
                    conn.execute(
                        "INSERT INTO job_runs(job_id,run_date,run_key,status,started_at) VALUES(?,?,?,?,?)",
                        (job_id, "2026-07-27", f"{job_id}:2026-07-27:auto", "success", "2026-07-27T09:26:00"),
                    )
            run_job = Mock(return_value={"status": "success"})
            scheduler = LocalScheduler(database, run_job)

            scheduler._tick(datetime(2026, 7, 27, 9, 29))
            run_job.assert_called_once_with("yijiner-scan", False)

    def test_morning_jobs_do_not_run_long_after_grace_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "scheduler.db")
            run_job = Mock(return_value={"status": "success"})
            scheduler = LocalScheduler(database, run_job)

            scheduler._tick(datetime(2026, 7, 27, 10, 0))
            run_job.assert_not_called()

    def test_auction_warning_retries_after_two_minutes_but_stops_after_valid_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "scheduler.db")
            with database.connect() as conn:
                conn.execute(
                    """INSERT INTO job_runs(job_id,run_date,run_key,status,started_at,result_json)
                       VALUES(?,?,?,?,?,?)""",
                    ("yijiner-scan", "2026-07-27", "yijiner-scan:2026-07-27:auto",
                     "warning", "2026-07-27T09:26:00", '{"persisted":false}'),
                )
            run_job = Mock(return_value={"status": "success"})
            scheduler = LocalScheduler(database, run_job)
            scheduler._tick(datetime(2026, 7, 27, 9, 27))
            run_job.assert_not_called()
            scheduler._tick(datetime(2026, 7, 27, 9, 28))
            self.assertEqual(run_job.call_args_list[0].args, ("yijiner-scan", False))

            with database.connect() as conn:
                conn.execute(
                    """INSERT INTO job_runs(job_id,run_date,run_key,status,started_at,result_json)
                       VALUES(?,?,?,?,?,?)""",
                    ("yijiner-scan", "2026-07-27", "yijiner-scan:2026-07-27:auto-092800",
                     "warning", "2026-07-27T09:28:00", '{"persisted":true}'),
                )
            run_job.reset_mock()
            scheduler._tick(datetime(2026, 7, 27, 9, 30))
            self.assertNotIn(("yijiner-scan", False), [call.args for call in run_job.call_args_list])

    def test_missed_postclose_baseline_refresh_catches_up_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "scheduler.db")
            maintenance = Mock(return_value={"ok": True})
            scheduler = LocalScheduler(database, Mock(), run_maintenance=maintenance)
            scheduler._tick(datetime(2026, 7, 27, 15, 36))
            scheduler._tick(datetime(2026, 7, 27, 15, 37))
            maintenance.assert_called_once_with("daily")


if __name__ == "__main__":
    unittest.main()
