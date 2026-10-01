from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .scoring import RULEBOOK_VERSION, STRATEGY_VERSION, is_main_board_security


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class Database:
    """Small SQLite store with explicit JSON boundaries and per-call connections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._init_schema()
        self._seed_defaults()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS screen_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            run_type TEXT NOT NULL,
            market_score REAL NOT NULL DEFAULT 0,
            market_label TEXT NOT NULL DEFAULT '中性',
            market_note TEXT NOT NULL DEFAULT '',
            universe_count INTEGER NOT NULL DEFAULT 0,
            first_board_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            push_count INTEGER NOT NULL DEFAULT 0,
            highest_code TEXT NOT NULL DEFAULT '',
            highest_name TEXT NOT NULL DEFAULT '',
            highest_score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success',
            source TEXT NOT NULL DEFAULT 'live',
            message TEXT NOT NULL DEFAULT '',
            strategy_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES screen_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            industry TEXT NOT NULL DEFAULT '',
            zone INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL,
            gap_pct REAL,
            auction_amount REAL,
            decision TEXT NOT NULL,
            decision_reason TEXT NOT NULL DEFAULT '',
            signal TEXT NOT NULL DEFAULT '',
            breakdown_json TEXT NOT NULL DEFAULT '{}',
            gates_json TEXT NOT NULL DEFAULT '[]',
            snapshot_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, rank);

        CREATE TABLE IF NOT EXISTS yijiner_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            universe_count INTEGER NOT NULL DEFAULT 0,
            prev_limit_up_count INTEGER NOT NULL DEFAULT 0,
            first_board_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            candidate_decision_count INTEGER NOT NULL DEFAULT 0,
            auction_available_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success',
            source TEXT NOT NULL DEFAULT 'live',
            message TEXT NOT NULL DEFAULT '',
            strategy_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yijiner_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES yijiner_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            industry TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL,
            first_board_score REAL,
            auction_stage_score REAL,
            auction_amount REAL,
            auction_change_pct REAL,
            auction_amount_ratio_pct REAL,
            auction_amount_to_mcap_pct REAL,
            prev_turnover_pct REAL,
            volume_ratio REAL,
            parking_apron INTEGER,
            decision TEXT NOT NULL DEFAULT 'watch',
            decision_reason TEXT NOT NULL DEFAULT '',
            breakdown_json TEXT NOT NULL DEFAULT '{}',
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            snapshot_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_yijiner_scores_run ON yijiner_scores(run_id, rank);

        CREATE TABLE IF NOT EXISTS auction_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            sampled_at TEXT NOT NULL,
            price REAL,
            change_pct REAL,
            source TEXT,
            UNIQUE(trade_date, code, sampled_at)
        );
        CREATE INDEX IF NOT EXISTS idx_auction_traces_day ON auction_traces(trade_date, code);

        CREATE TABLE IF NOT EXISTS yijiner_outcomes (
            run_id INTEGER PRIMARY KEY REFERENCES yijiner_runs(id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dixi_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            universe_count INTEGER NOT NULL DEFAULT 0,
            amount_qualified_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            candidate_decision_count INTEGER NOT NULL DEFAULT 0,
            triggered_count INTEGER NOT NULL DEFAULT 0,
            market_shrink INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success',
            source TEXT NOT NULL DEFAULT 'live',
            message TEXT NOT NULL DEFAULT '',
            strategy_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dixi_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES dixi_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            industry TEXT NOT NULL DEFAULT '',
            buy_point TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL,
            volume_ratio REAL,
            activity_60d_limit_ups INTEGER,
            pullback_days INTEGER,
            pullback_rounds INTEGER,
            amount REAL,
            amount_rank INTEGER,
            hot_rank INTEGER,
            triggered INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL DEFAULT 'watch',
            decision_reason TEXT NOT NULL DEFAULT '',
            breakdown_json TEXT NOT NULL DEFAULT '{}',
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            snapshot_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_dixi_scores_run ON dixi_scores(run_id, rank);

        CREATE TABLE IF NOT EXISTS research_watchlist (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            analysis_json TEXT NOT NULL DEFAULT '{}',
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            analyzed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS backtest_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            category TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            pushed INTEGER NOT NULL DEFAULT 0,
            hit INTEGER,
            pnl_pct REAL,
            best_7d_pct REAL,
            score REAL,
            strategy_version TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'sample',
            note TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL UNIQUE,
            run_id INTEGER REFERENCES screen_runs(id) ON DELETE SET NULL,
            market_summary TEXT NOT NULL DEFAULT '',
            strategy_diagnosis TEXT NOT NULL DEFAULT '',
            advice TEXT NOT NULL DEFAULT '',
            stats_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            schedule TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            channel TEXT NOT NULL DEFAULT 'local',
            last_status TEXT NOT NULL DEFAULT 'waiting',
            last_run_at TEXT,
            next_run_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            run_date TEXT NOT NULL,
            run_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Idempotent column additions for databases created before the
        yijiner parking-apron / death-turnover upgrade."""
        migrations = (
            ("yijiner_scores", "prev_turnover_pct", "REAL"),
            ("yijiner_scores", "volume_ratio", "REAL"),
            ("yijiner_scores", "parking_apron", "INTEGER"),
        )
        with self.connect() as conn:
            for table, column, decl in migrations:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    def _seed_defaults(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        defaults = {
            "app_name": "寻龙工作台",
            "strategy_version": STRATEGY_VERSION,
            "technical_threshold": "10",
            "auction_threshold": "60",
            "scan_limit": "80",
            "rulebook_threshold": "62",
            "rulebook_push_threshold": "72",
            "demo_fallback": "true",
            "wecom_webhook": "",
            "home_channel": "",
            "auto_scheduler": "true",
            "astocklab_root": os.environ.get("ASTOCKLAB_ROOT", "").strip(),
        }
        jobs = [
            (
                "overnight-pool",
                "\u76d8\u540e\u5efa\u7acb\u9694\u591c\u5019\u9009\u6c60",
                "\u4ea4\u6613\u65e5 15:05",
                "\u901a\u8fbe\u4fe1\u677f\u5757\u8f6e\u52a8\u2192\u91cd\u70b9\u677f\u5757\u2192\u9694\u591c\u89c2\u5bdf\u6c60\uff0c\u4e0d\u76f4\u63a5\u63a8\u4e70\u5165",
            ),
            (
                "morning-auction",
                "每日擒龙预扫描",
                "交易日 09:26（供09:28微信推送）",
                "仅使用当日09:20-09:29实时行情与竞价数据，09:28由Hermes读取结果",
            ),
            (
                "yijiner-scan",
                "一进二竞价爆量扫描",
                "交易日 09:27",
                "对昨日首板涨停股结合当日竞价做评分卡筛选，结果记录在本地一进二页面",
            ),
            (
                "auction-trace",
                "竞价过程采样",
                "交易日 09:22",
                "对昨日首板池采样竞价早期价格，供一进二'竞价过程弱转强'路径因子",
            ),
            (
                "dixi-scan",
                "低吸计划生成",
                "交易日 15:10",
                "素衣不染尘模式：趋势+人气+回调反包，盘后生成次日低吸观察计划",
            ),
            (
                "afternoon-review",
                "每日收盘复盘",
                "交易日 15:01",
                "统计推送表现、市场概况与漏选原因",
            ),
            (
                "weekly-stock-info",
                "股票基础信息刷新",
                "每周六 08:30",
                "刷新股票名称、行业、流通股本和上市日期",
            ),
        ]
        with self._write_lock, self.connect() as conn:
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now),
                )
            # Upgrade databases created by the replica without overwriting a
            # user's Webhook or other local settings.  Historical rows remain
            # available for review but new runs carry the rulebook version.
            current_version = conn.execute(
                "SELECT value FROM settings WHERE key='strategy_version'"
            ).fetchone()
            if current_version and str(current_version[0]).startswith("replica-"):
                conn.execute(
                    "UPDATE settings SET value=?,updated_at=? WHERE key='strategy_version'",
                    (STRATEGY_VERSION, now),
                )
            for job_id, name, schedule, description in jobs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs(
                        id,name,schedule,description,enabled,channel,last_status,updated_at
                    ) VALUES(?,?,?,?,1,'wecom','waiting',?)
                    """,
                    (job_id, name, schedule, description, now),
                )
            # The legacy automatic overnight selector has been replaced by a
            # manual research watchlist. Keep its history, but never repopulate
            # the user-controlled pool in the background.
            conn.execute(
                "UPDATE jobs SET enabled=0,channel='local',updated_at=? WHERE id='overnight-pool'",
                (now,),
            )
            # 一进二/低吸计划仅本地研究记录，没有企业微信推送格式。
            conn.execute(
                "UPDATE jobs SET channel='local',updated_at=? WHERE id IN ('yijiner-scan','dixi-scan')",
                (now,),
            )
            conn.execute(
                """
                UPDATE jobs SET name=?,schedule=?,description=?,channel='local',updated_at=?
                WHERE id=?
                """,
                (
                    "每日擒龙预扫描",
                    "交易日 09:26（供09:28微信推送）",
                    "仅复核已有候选池，不重新扫描全市场；09:28由Hermes读取结果",
                    now,
                    "morning-auction",
                ),
            )
            count = conn.execute("SELECT COUNT(*) FROM backtest_records").fetchone()[0]
            if count == 0:
                self._seed_backtests(conn)
            runs = conn.execute("SELECT COUNT(*) FROM screen_runs").fetchone()[0]
            if runs == 0:
                self._seed_screen_run(conn)

    def _seed_backtests(self, conn: sqlite3.Connection) -> None:
        rows = [
            ("2026-06-16", "推送1", "300977", "深圳瑞捷", 1, 1, 36.9, 43.2, 70),
            ("2026-06-17", "最高分", "002446", "盛路通信", 0, None, -3.5, 3.2, 65),
            ("2026-06-18", "最高分", "002674", "兴业科技", 0, None, 4.2, 26.7, 70),
            ("2026-06-18", "推送1", "002642", "荣联科技", 1, 1, -1.3, 28.3, 70),
            ("2026-06-18", "推送2", "002579", "中京电子", 1, 1, 3.1, 19.8, 63),
            ("2026-06-22", "最高分", "002294", "信立泰", 0, None, -1.9, -0.3, 77),
            ("2026-06-22", "推送1", "603158", "腾龙股份", 1, 1, 25.2, 24.3, 67),
            ("2026-06-23", "推送1", "605117", "德业股份", 1, 1, 4.7, 9.0, 79),
            ("2026-06-24", "推送1", "605366", "宏柏新材", 1, 1, 12.5, 28.0, 62),
        ]
        conn.executemany(
            """
            INSERT INTO backtest_records(
                trade_date,category,code,name,pushed,hit,pnl_pct,best_7d_pct,score,
                strategy_version,source,note
            ) VALUES(?,?,?,?,?,?,?,?,?,'rulebook-v1-2026.07.18','calibration_sample',
                '基于用户提供截图录入的校准样例，不代表实时业绩')
            """,
            rows,
        )

    def _seed_screen_run(self, conn: sqlite3.Connection) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        metadata = {
            "is_sample": True,
            "notice": "基于用户截图录入的界面校准样例",
            "funnel": [
                {"key": "universe", "label": "全市场", "count": 3196},
                {"key": "risk_pass", "label": "风险否决后", "count": 61},
                {"key": "gates", "label": "事件确认闸门", "count": 16},
                {"key": "score", "label": "规则库综合", "count": 4},
                {"key": "push", "label": "最终推送", "count": 1},
            ],
        }
        cur = conn.execute(
            """
            INSERT INTO screen_runs(
                trade_date,run_type,market_score,market_label,market_note,
                universe_count,first_board_count,candidate_count,push_count,
                highest_code,highest_name,highest_score,status,source,message,
                strategy_version,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-06-09",
                "auction",
                2,
                "偏多",
                "市场温度适中，执行双区择优",
                3196,
                61,
                16,
                1,
                "603773",
                "沃格光电",
                67,
                "success",
                "calibration_sample",
                "历史校准样例（旧策略），新规则库运行不沿用科创板标的；仅供研究，不构成投资建议。",
                STRATEGY_VERSION,
                _json(metadata),
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO candidates(
                run_id,rank,code,name,industry,zone,score,gap_pct,auction_amount,
                decision,decision_reason,signal,breakdown_json,gates_json,snapshot_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                1,
                "603773",
                "沃格光电",
                "光学光电子",
                1,
                67,
                3.27,
                123000000,
                "push",
                "一区优先，评分及市场环境均通过",
                "偏多",
                _json({"Gap": 8, "MACD": 8, "KDJ": 7, "量比": 7, "市场": 6}),
                _json([]),
                _json({"is_sample": True}),
            ),
        )

    def get_settings(self, mask_secrets: bool = False) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            value: Any = row["value"]
            if value in {"true", "false"}:
                value = value == "true"
            elif row["key"] in {
                "technical_threshold",
                "auction_threshold",
                "scan_limit",
                "rulebook_threshold",
                "rulebook_push_threshold",
            }:
                try:
                    value = int(value)
                except ValueError:
                    pass
            if mask_secrets and row["key"] == "wecom_webhook" and value:
                value = f"{str(value)[:28]}...{str(value)[-6:]}"
            result[row["key"]] = value
        return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "app_name",
            "strategy_version",
            "technical_threshold",
            "auction_threshold",
            "scan_limit",
            "rulebook_threshold",
            "rulebook_push_threshold",
            "demo_fallback",
            "wecom_webhook",
            "home_channel",
            "auto_scheduler",
            "astocklab_root",
        }
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            for key, value in values.items():
                if key not in allowed:
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                conn.execute(
                    """
                    INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                    """,
                    (key, str(value), now),
                )
        return self.get_settings(mask_secrets=True)

    def list_research_watchlist(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_watchlist ORDER BY added_at DESC, code"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["analysis"] = _loads(item.pop("analysis_json", "{}"), {})
            result.append(item)
        return result

    def get_research_stock(self, code: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_watchlist WHERE code=?", (code,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["analysis"] = _loads(item.pop("analysis_json", "{}"), {})
        return item

    def upsert_research_stock(self, code: str, name: str = "", note: str = "") -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_watchlist(code,name,note,added_at,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    note=CASE WHEN excluded.note<>'' THEN excluded.note ELSE research_watchlist.note END,
                    updated_at=excluded.updated_at
                """,
                (code, name, note, now, now),
            )
        return self.get_research_stock(code) or {}

    def save_research_analysis(self, code: str, analysis: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE research_watchlist
                SET name=COALESCE(NULLIF(?,''),name),analysis_json=?,analyzed_at=?,updated_at=?
                WHERE code=?
                """,
                (analysis.get("name", ""), _json(analysis), now, now, code),
            )
        return self.get_research_stock(code) or {}

    def delete_research_stock(self, code: str) -> bool:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM research_watchlist WHERE code=?", (code,))
        return cursor.rowcount > 0

    def clear_research_watchlist(self) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM research_watchlist")
        return cursor.rowcount

    def create_screen_run(self, run: dict[str, Any], candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        candidate_list = list(candidates)
        highest = candidate_list[0] if candidate_list else {}
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO screen_runs(
                    trade_date,run_type,market_score,market_label,market_note,
                    universe_count,first_board_count,candidate_count,push_count,
                    highest_code,highest_name,highest_score,status,source,message,
                    strategy_version,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.get("trade_date", datetime.now().strftime("%Y-%m-%d")),
                    run.get("run_type", "technical"),
                    run.get("market_score", 0),
                    run.get("market_label", "中性"),
                    run.get("market_note", ""),
                    run.get("universe_count", len(candidate_list)),
                    run.get("first_board_count", 0),
                    len(candidate_list),
                    sum(1 for item in candidate_list if item.get("decision") == "push"),
                    highest.get("code", ""),
                    highest.get("name", ""),
                    highest.get("score", 0),
                    run.get("status", "success"),
                    run.get("source", "live"),
                    run.get("message", ""),
                    run.get("strategy_version", self.get_settings().get("strategy_version", STRATEGY_VERSION)),
                    _json(run.get("metadata", {})),
                    now,
                ),
            )
            run_id = int(cur.lastrowid)
            for rank, item in enumerate(candidate_list, start=1):
                conn.execute(
                    """
                    INSERT INTO candidates(
                        run_id,rank,code,name,industry,zone,score,gap_pct,auction_amount,
                        decision,decision_reason,signal,breakdown_json,gates_json,snapshot_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        rank,
                        item.get("code", ""),
                        item.get("name", ""),
                        item.get("industry", ""),
                        item.get("zone", 0),
                        item.get("score", 0),
                        item.get("gap_pct"),
                        item.get("auction_amount"),
                        item.get("decision", "watch"),
                        item.get("decision_reason", ""),
                        item.get("signal", ""),
                        _json(item.get("breakdown", {})),
                        _json(item.get("gates", [])),
                        _json(item.get("snapshot", {})),
                    ),
                )
        return self.get_screen_run(run_id) or {}

    def _run_row(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _loads(result.pop("metadata_json", "{}"), {})
        result["funnel"] = result["metadata"].get("funnel", [])
        result["first_boards"] = result.get("first_board_count", 0)
        candidates = conn.execute(
            "SELECT * FROM candidates WHERE run_id=? ORDER BY rank", (row["id"],)
        ).fetchall()
        result["candidates"] = []
        legacy_non_main_removed = 0
        for candidate in candidates:
            item = dict(candidate)
            if not is_main_board_security(item.get("code"), item.get("name")):
                legacy_non_main_removed += 1
                continue
            item["breakdown"] = _loads(item.pop("breakdown_json", "{}"), {})
            item["gates"] = _loads(item.pop("gates_json", "[]"), [])
            item["snapshot"] = _loads(item.pop("snapshot_json", "{}"), {})
            item["scores"] = item["breakdown"]
            item["pushed"] = item.get("decision") == "push"
            item["reason"] = item.get("decision_reason", "")
            item["change_pct"] = item["snapshot"].get("change_pct")
            result["candidates"].append(item)
        result["candidate_count"] = len(result["candidates"])
        result["push_count"] = sum(1 for item in result["candidates"] if item.get("decision") == "push")
        highest = result["candidates"][0] if result["candidates"] else {}
        result["highest_code"] = highest.get("code", "")
        result["highest_name"] = highest.get("name", "")
        result["highest_score"] = highest.get("score", 0)
        result["metadata"]["universe_scope"] = "沪深主板（000/001/002/003/600/601/603/605）"
        if legacy_non_main_removed:
            result["metadata"]["legacy_non_main_removed_count"] = legacy_non_main_removed
        legacy_key = "technical_threshold" if result.get("run_type") == "technical" else "auction_threshold"
        threshold = result["metadata"].get("threshold")
        if threshold is None:
            threshold = self.get_settings().get(legacy_key, 0)
        result["summary"] = {
            "total": result.get("universe_count", 0),
            "scanned": result.get("universe_count", 0),
            "first_boards": result.get("first_board_count", 0),
            "eligible_count": result["metadata"].get("eligible_count", result.get("universe_count", 0)),
            "excluded_star": result["metadata"].get("excluded_star_count", 0),
            "pre_filtered": result["metadata"].get(
                "snapshot_scored_count", result["metadata"].get("scan_limit", 0)
            ),
            "kline_confirmed": result["metadata"].get("data_rows", 0),
            "scored": result.get("candidate_count", 0),
            "strong": sum(1 for item in result["candidates"] if float(item.get("score") or 0) >= float(threshold or 0)),
            "pushed": result.get("push_count", 0),
            "min_score": threshold,
            "threshold_status": result["metadata"].get("threshold_status", "候选阈值，待回测"),
        }
        return result

    def get_latest_screen_run(self, run_type: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM screen_runs"
        args: tuple[Any, ...] = ()
        if run_type:
            sql += " WHERE run_type=?"
            args = (run_type,)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return self._run_row(row, conn) if row else None

    def get_screen_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM screen_runs WHERE id=?", (run_id,)).fetchone()
            return self._run_row(row, conn) if row else None

    def get_latest_candidate_pool_run(self) -> dict[str, Any] | None:
        """Return the latest first-round run, excluding confirmation-only runs."""

        excluded = ("morning-confirmation", "incremental-review")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM screen_runs
                WHERE run_type NOT IN (?, ?)
                ORDER BY id DESC LIMIT 1
                """,
                excluded,
            ).fetchone()
            return self._run_row(row, conn) if row else None

    def get_latest_overnight_pool_run(self) -> dict[str, Any] | None:
        """Prefer the reviewed overnight pool, then its first round."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM screen_runs ORDER BY id DESC LIMIT 100").fetchall()
            for row in rows:
                result = self._run_row(row, conn)
                stage = (result.get("metadata") or {}).get("pipeline_stage")
                if stage in {"overnight_second_round", "overnight_first_round"}:
                    return result
        return None

    def list_screen_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM screen_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._run_row(row, conn) for row in rows]

    def create_yijiner_run(self, run: dict[str, Any], scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        score_list = list(scores)
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO yijiner_runs(
                    trade_date,universe_count,prev_limit_up_count,first_board_count,
                    candidate_count,candidate_decision_count,auction_available_count,
                    status,source,message,strategy_version,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.get("trade_date", datetime.now().strftime("%Y-%m-%d")),
                    run.get("universe_count", 0),
                    run.get("prev_limit_up_count", 0),
                    run.get("first_board_count", 0),
                    len(score_list),
                    sum(1 for item in score_list if item.get("decision") == "candidate"),
                    run.get("auction_available_count", 0),
                    run.get("status", "success"),
                    run.get("source", "live"),
                    run.get("message", ""),
                    run.get("strategy_version", "yijiner-scorecard"),
                    _json(run.get("metadata", {})),
                    now,
                ),
            )
            run_id = int(cur.lastrowid)
            for item in score_list:
                conn.execute(
                    """
                    INSERT INTO yijiner_scores(
                        run_id,rank,code,name,industry,tier,score,first_board_score,
                        auction_stage_score,auction_amount,auction_change_pct,
                        auction_amount_ratio_pct,auction_amount_to_mcap_pct,
                        prev_turnover_pct,volume_ratio,parking_apron,
                        decision,decision_reason,breakdown_json,risk_flags_json,snapshot_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        item.get("rank", 0),
                        item.get("code", ""),
                        item.get("name", ""),
                        item.get("industry", ""),
                        item.get("tier", ""),
                        item.get("score", 0),
                        item.get("first_board_score"),
                        item.get("auction_stage_score"),
                        item.get("auction_amount"),
                        item.get("auction_change_pct"),
                        item.get("auction_amount_ratio_pct"),
                        item.get("auction_amount_to_mcap_pct"),
                        item.get("prev_turnover_pct"),
                        item.get("volume_ratio"),
                        1 if item.get("parking_apron") else 0,
                        item.get("decision", "watch"),
                        item.get("decision_reason", ""),
                        _json(item.get("breakdown", {})),
                        _json(item.get("risk_flags", [])),
                        _json(item.get("snapshot", {})),
                    ),
                )
        return self.get_yijiner_run(run_id) or {}

    def _yijiner_run_row(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _loads(result.pop("metadata_json", "{}"), {})
        scores = conn.execute(
            "SELECT * FROM yijiner_scores WHERE run_id=? ORDER BY rank", (row["id"],)
        ).fetchall()
        result["rows"] = []
        for candidate in scores:
            item = dict(candidate)
            item["breakdown"] = _loads(item.pop("breakdown_json", "{}"), {})
            item["risk_flags"] = _loads(item.pop("risk_flags_json", "[]"), [])
            item["snapshot"] = _loads(item.pop("snapshot_json", "{}"), {})
            result["rows"].append(item)
        result["candidate_count"] = len(result["rows"])
        result["candidate_decision_count"] = sum(
            1 for item in result["rows"] if item.get("decision") == "candidate"
        )
        return result

    def get_yijiner_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM yijiner_runs WHERE id=?", (run_id,)).fetchone()
            return self._yijiner_run_row(row, conn) if row else None

    def get_latest_yijiner_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM yijiner_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return self._yijiner_run_row(row, conn) if row else None

    def list_yijiner_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM yijiner_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._yijiner_run_row(row, conn) for row in rows]

    def save_auction_traces(self, trade_date: str, rows: Iterable[dict[str, Any]]) -> int:
        """Persist 09:2x auction path samples for the first-board watchlist."""
        payload = [dict(item) for item in rows if isinstance(item, dict) and item.get("code")]
        if not payload:
            return 0
        with self._write_lock, self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO auction_traces(
                    trade_date, code, sampled_at, price, change_pct, source
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        trade_date,
                        str(item["code"]),
                        str(item.get("sampled_at") or ""),
                        item.get("price"),
                        item.get("change_pct"),
                        str(item.get("source") or ""),
                    )
                    for item in payload
                ],
            )
        return len(payload)

    def get_auction_traces(self, trade_date: str) -> dict[str, dict[str, Any]]:
        """Return {code: {low_pct, first_pct, first_at, samples}} for one date."""
        result: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT code, sampled_at, change_pct FROM auction_traces WHERE trade_date=? ORDER BY sampled_at",
                (trade_date,),
            ).fetchall()
        for row in rows:
            code = str(row["code"])
            change = row["change_pct"]
            entry = result.setdefault(code, {"low_pct": None, "first_pct": None, "first_at": "", "samples": 0})
            entry["samples"] += 1
            if change is not None:
                change = float(change)
                if entry["low_pct"] is None or change < entry["low_pct"]:
                    entry["low_pct"] = change
            if not entry["first_at"]:
                entry["first_at"] = str(row["sampled_at"])
                entry["first_pct"] = float(change) if change is not None else None
        return result

    def save_yijiner_outcome(self, run_id: int, payload: dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO yijiner_outcomes(run_id, payload_json, captured_at) VALUES(?,?,?)",
                (run_id, _json(payload), now),
            )

    def get_yijiner_outcome(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json, captured_at FROM yijiner_outcomes WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        payload = _loads(row["payload_json"], {})
        if isinstance(payload, dict):
            payload["captured_at"] = row["captured_at"]
        return payload or None

    def create_dixi_run(self, run: dict[str, Any], scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        score_list = list(scores)
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO dixi_runs(
                    trade_date,universe_count,amount_qualified_count,candidate_count,
                    candidate_decision_count,triggered_count,market_shrink,
                    status,source,message,strategy_version,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.get("trade_date", datetime.now().strftime("%Y-%m-%d")),
                    run.get("universe_count", 0),
                    run.get("amount_qualified_count", 0),
                    len(score_list),
                    sum(1 for item in score_list if item.get("decision") == "candidate"),
                    run.get("triggered_count", 0),
                    1 if run.get("market_shrink") else 0,
                    run.get("status", "success"),
                    run.get("source", "live"),
                    run.get("message", ""),
                    run.get("strategy_version", "dixi-trend-pullback"),
                    _json(run.get("metadata", {})),
                    now,
                ),
            )
            run_id = int(cur.lastrowid)
            for item in score_list:
                conn.execute(
                    """
                    INSERT INTO dixi_scores(
                        run_id,rank,code,name,industry,buy_point,score,volume_ratio,
                        activity_60d_limit_ups,pullback_days,pullback_rounds,amount,
                        amount_rank,hot_rank,triggered,decision,decision_reason,
                        breakdown_json,risk_flags_json,snapshot_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        item.get("rank", 0),
                        item.get("code", ""),
                        item.get("name", ""),
                        item.get("industry", ""),
                        item.get("buy_point", ""),
                        item.get("score", 0),
                        item.get("volume_ratio"),
                        item.get("activity_60d_limit_ups"),
                        item.get("pullback_days"),
                        item.get("pullback_rounds"),
                        item.get("amount"),
                        item.get("amount_rank"),
                        item.get("hot_rank"),
                        1 if item.get("triggered") else 0,
                        item.get("decision", "watch"),
                        item.get("decision_reason", ""),
                        _json(item.get("breakdown", {})),
                        _json(item.get("risk_flags", [])),
                        _json(item.get("snapshot", {})),
                    ),
                )
        return self.get_dixi_run(run_id) or {}

    def _dixi_run_row(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _loads(result.pop("metadata_json", "{}"), {})
        result["market_shrink"] = bool(result.get("market_shrink"))
        scores = conn.execute(
            "SELECT * FROM dixi_scores WHERE run_id=? ORDER BY rank", (row["id"],)
        ).fetchall()
        result["rows"] = []
        for candidate in scores:
            item = dict(candidate)
            item["breakdown"] = _loads(item.pop("breakdown_json", "{}"), {})
            item["risk_flags"] = _loads(item.pop("risk_flags_json", "[]"), [])
            item["snapshot"] = _loads(item.pop("snapshot_json", "{}"), {})
            item["triggered"] = bool(item.get("triggered"))
            result["rows"].append(item)
        result["candidate_count"] = len(result["rows"])
        result["candidate_decision_count"] = sum(
            1 for item in result["rows"] if item.get("decision") == "candidate"
        )
        return result

    def get_dixi_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dixi_runs WHERE id=?", (run_id,)).fetchone()
            return self._dixi_run_row(row, conn) if row else None

    def get_latest_dixi_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dixi_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return self._dixi_run_row(row, conn) if row else None

    def list_dixi_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dixi_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._dixi_run_row(row, conn) for row in rows]

    def list_backtests(self, limit: int = 200) -> dict[str, Any]:
        with self.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM backtest_records ORDER BY trade_date DESC,id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        pushed = [row for row in rows if row["pushed"]]
        hit_rows = [row for row in pushed if row["hit"] is not None]
        stats = {
            "trading_days": len({row["trade_date"] for row in rows}),
            "push_count": len(pushed),
            "hit_count": sum(1 for row in hit_rows if row["hit"]),
            "hit_rate": round(
                100 * sum(1 for row in hit_rows if row["hit"]) / len(hit_rows), 1
            )
            if hit_rows
            else 0,
            "avg_pnl_pct": round(
                sum(float(row["pnl_pct"] or 0) for row in pushed) / len(pushed), 2
            )
            if pushed
            else 0,
            "avg_best_7d_pct": round(
                sum(float(row["best_7d_pct"] or 0) for row in pushed) / len(pushed), 2
            )
            if pushed
            else 0,
            "sample_notice": "含截图校准样例，不代表实时或未来收益",
        }
        stats["trade_days"] = stats["trading_days"]
        stats["hits"] = stats["hit_count"]
        stats["avg_return"] = stats["avg_pnl_pct"]
        stats["average_return"] = stats["avg_pnl_pct"]
        for row in rows:
            row["best_7d"] = row.get("best_7d_pct")
            row["total_return"] = row.get("pnl_pct")
        return {"records": rows, "stats": stats}

    def add_backtest_records(self, records: Iterable[dict[str, Any]]) -> int:
        rows = list(records)
        if not rows:
            return 0
        with self._write_lock, self.connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO backtest_records(
                        trade_date,category,code,name,pushed,hit,pnl_pct,best_7d_pct,
                        score,strategy_version,source,note
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row.get("trade_date", ""),
                        row.get("category", "推送"),
                        row.get("code", ""),
                        row.get("name", ""),
                        1 if row.get("pushed") else 0,
                        None if row.get("hit") is None else (1 if row.get("hit") else 0),
                        row.get("pnl_pct"),
                        row.get("best_7d_pct"),
                        row.get("score"),
                        row.get("strategy_version", STRATEGY_VERSION),
                        row.get("source", "computed"),
                        row.get("note", ""),
                    ),
                )
        return len(rows)

    def upsert_review(self, review: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews(
                    trade_date,run_id,market_summary,strategy_diagnosis,advice,stats_json,created_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    run_id=excluded.run_id,
                    market_summary=excluded.market_summary,
                    strategy_diagnosis=excluded.strategy_diagnosis,
                    advice=excluded.advice,
                    stats_json=excluded.stats_json,
                    created_at=excluded.created_at
                """,
                (
                    review["trade_date"],
                    review.get("run_id"),
                    review.get("market_summary", ""),
                    review.get("strategy_diagnosis", ""),
                    review.get("advice", ""),
                    _json(review.get("stats", {})),
                    now,
                ),
            )
        return self.get_latest_review() or {}

    def get_latest_review(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM reviews ORDER BY trade_date DESC LIMIT 1").fetchone()
        if not row:
            return None
        result = dict(row)
        result["stats"] = _loads(result.pop("stats_json", "{}"), {})
        return result

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def update_job(self, job_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"enabled", "channel"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get_job(job_id)
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*updates.values(), job_id),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {**dict(row), "enabled": bool(row["enabled"])} if row else None

    def start_job_run(self, job_id: str, manual: bool = False) -> dict[str, Any]:
        now = datetime.now()
        # The auction scan may initially find no verified match and need a
        # bounded second attempt. Give each automatic attempt its own key;
        # other daily jobs retain their one-run-per-day key.
        suffix = ('manual-' + now.strftime('%H%M%S%f') if manual else
                  'auto-' + now.strftime('%H%M%S%f') if job_id == 'yijiner-scan' else 'auto')
        run_key = f"{job_id}:{now.strftime('%Y-%m-%d')}:{suffix}"
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO job_runs(job_id,run_date,run_key,status,started_at)
                VALUES(?,?,?,?,?)
                """,
                (job_id, now.strftime("%Y-%m-%d"), run_key, "running", now.isoformat(timespec="seconds")),
            )
            conn.execute(
                "UPDATE jobs SET last_status='running',last_run_at=?,updated_at=? WHERE id=?",
                (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), job_id),
            )
        return {"id": int(cur.lastrowid), "run_key": run_key, "status": "running"}

    def finish_job_run(
        self, run_id: int, job_id: str, status: str, summary: str, result: dict[str, Any]
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE job_runs SET status=?,summary=?,result_json=?,finished_at=? WHERE id=?
                """,
                (status, summary, _json(result), now, run_id),
            )
            conn.execute(
                "UPDATE jobs SET last_status=?,updated_at=? WHERE id=?",
                (status, now, job_id),
            )

    def auto_job_already_ran(self, job_id: str, run_date: str, now: datetime | None = None) -> bool:
        run_key = f"{job_id}:{run_date}:auto"
        with self.connect() as conn:
            if job_id == "yijiner-scan":
                row = conn.execute(
                    """SELECT status, started_at, result_json FROM job_runs
                       WHERE job_id=? AND run_date=? AND (run_key=? OR run_key LIKE ?)
                       ORDER BY id DESC LIMIT 1""",
                    (job_id, run_date, run_key, run_key + "-%"),
                ).fetchone()
                if not row:
                    return False
                result = _loads(row["result_json"], {})
                if row["status"] in {"running", "success"} or result.get("persisted") is True:
                    return True
                # Avoid hammering sources every 20 seconds, but retry a
                # warning/failed auction before its short capture window ends.
                attempted = datetime.fromisoformat(row["started_at"])
                return ((now or datetime.now()) - attempted).total_seconds() < 120
            return bool(
                conn.execute("SELECT 1 FROM job_runs WHERE run_key=?", (run_key,)).fetchone()
            )

    def recent_job_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = _loads(item.pop("result_json", "{}"), {})
            result.append(item)
        return result

    @staticmethod
    def next_scheduled_time(job_id: str, now: datetime | None = None) -> str:
        now = now or datetime.now()
        if job_id == "weekly-stock-info":
            days = (5 - now.weekday()) % 7
            target = (now + timedelta(days=days)).replace(hour=8, minute=30, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=7)
            return target.isoformat(timespec="minutes")
        schedules = {
            "auction-trace": (9, 22),
            "morning-auction": (9, 26),
            "yijiner-scan": (9, 27),
            "afternoon-review": (15, 1),
            "overnight-pool": (15, 5),
            "dixi-scan": (15, 10),
        }
        hour, minute = schedules.get(job_id, (15, 1))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
        return target.isoformat(timespec="minutes")
