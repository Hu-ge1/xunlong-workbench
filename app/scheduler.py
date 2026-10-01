from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from .storage import Database


logger = logging.getLogger(__name__)

class LocalScheduler:
    """Minute-level local scheduler for the three documented workflows."""

    def __init__(
        self,
        database: Database,
        run_job: Callable[[str, bool], dict[str, Any]],
        run_maintenance: Callable[[str], dict[str, Any]] | None = None,
        interval_seconds: int = 20,
    ) -> None:
        self.database = database
        self.run_job = run_job
        self.run_maintenance = run_maintenance
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._maintenance_runs: set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xunlong-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                settings = self.database.get_settings()
                if not settings.get("auto_scheduler", True):
                    continue
                self._tick(datetime.now())
            except Exception:
                # Individual runs write their own failure record. The loop must stay alive.
                logger.exception("Scheduler tick failed")
                continue

    def _tick(self, now: datetime) -> None:
        run_date = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        current_minute = now.hour * 60 + now.minute

        def due_within_grace(hour: int, minute: int, grace: int = 10) -> bool:
            """Allow a short catch-up when a preceding synchronous job runs long."""

            scheduled = hour * 60 + minute
            return 0 <= current_minute - scheduled <= grace
        if now.weekday() < 5 and self.run_maintenance is not None:
            maintenance_task = (
                # The breadth refresh can block for over a minute.  Keep it
                # out of the 09:25-09:30 auction-final capture window.
                "preopen" if hhmm == "09:35" else
                # Catch up if the desktop app starts after the normal close.
                "daily" if 15 * 60 + 8 <= current_minute <= 17 * 60 else
                ""
            )
            maintenance_key = f"{run_date}:{maintenance_task}"
            if maintenance_task and maintenance_key not in self._maintenance_runs:
                self._maintenance_runs.add(maintenance_key)
                try:
                    self.run_maintenance(maintenance_task)
                except Exception:
                    # The 09:26 pre-scan still has Tencent/TDX fallbacks.
                    logger.exception("Scheduled maintenance failed: %s", maintenance_task)
        jobs = {job["id"]: job for job in self.database.list_jobs() if job["enabled"]}
        due: list[str] = []
        if now.weekday() < 5 and due_within_grace(9, 22, grace=2):
            # 竞价过程采样：09:20 后不可撤单为真实意图，取昨日首板池的虚拟
            # 撮合价，供 09:27 一进二评分卡的"竞价过程弱转强"路径因子。
            due.append("auction-trace")
        if now.weekday() < 5 and due_within_grace(9, 26, grace=4):
            # Capture the short-lived 09:25 terminal quote before the slower
            # dragon scan or breadth maintenance can block this scheduler.
            due.append("yijiner-scan")
        if now.weekday() < 5 and due_within_grace(9, 28):
            # Build today's dragon candidates after the terminal auction scan.
            due.append("morning-auction")
        if now.weekday() < 5 and due_within_grace(15, 5):
            due.append("overnight-pool")
        if now.weekday() < 5 and due_within_grace(15, 10):
            # 盘后生成次日低吸计划（素衣模式：趋势+人气+回调反包）。
            due.append("dixi-scan")
        if now.weekday() < 5 and due_within_grace(15, 1):
            due.append("afternoon-review")
        if now.weekday() == 5 and due_within_grace(8, 30):
            due.append("weekly-stock-info")
        for job_id in due:
            if job_id not in jobs or self.database.auto_job_already_ran(job_id, run_date, now=now):
                continue
            try:
                self.run_job(job_id, False)
            except Exception:
                logger.exception("Scheduled job failed: %s", job_id)
                continue
