from __future__ import annotations

import math
import re
from copy import deepcopy
from threading import Lock
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from . import dixi, maifu, oversold, scoring, yijiner
from .patterns import detect_all as detect_kline_patterns
from .providers import MarketDataProvider, filter_stock_universe
from .storage import Database
from .strategies import run_all as run_strategies


DISCLAIMER = "仅供研究，不构成投资建议。"

_OVERSOLD_CACHE: dict[tuple, tuple[datetime, dict[str, Any]]] = {}
_OVERSOLD_CACHE_TTL = 300  # 超跌反弹筛选结果缓存 5 分钟（全市场评估约 10~40 秒）


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _date_part(value: Any) -> str:
    """Extract an ISO trading date without guessing from an undated quote."""

    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
    return "-".join(match.groups()) if match else ""


def _filter_same_day_auction_quotes(
    quotes: dict[str, dict[str, Any]], current_time: datetime
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Keep only genuine same-session auction observations.

    Tencent is useful only while the opening auction is live. Outside that
    window we require an explicit terminal-auction marker, otherwise an
    ordinary daily/quote snapshot can be mistaken for today's auction.
    """

    today = current_time.strftime("%Y-%m-%d")
    minute = current_time.hour * 60 + current_time.minute
    live_window = current_time.weekday() < 5 and 9 * 60 + 15 <= minute <= 9 * 60 + 30
    accepted: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    final_statuses = {"final", "complete", "completed", "终态"}
    final_stages = {"opening_call_auction_final", "final", "auction_final", "集合竞价终态"}

    for code, raw in quotes.items():
        quote = dict(raw) if isinstance(raw, dict) else {}
        quote_date = next(
            (
                value
                for value in (
                    _date_part(quote.get("trade_date")),
                    _date_part(quote.get("data_as_of")),
                    _date_part(quote.get("quote_time")),
                )
                if value
            ),
            "",
        )
        status = str(quote.get("auction_data_status") or "").strip().lower()
        stage = str(quote.get("auction_stage") or "").strip().lower()
        terminal = bool(quote.get("ffd_terminal")) or status in final_statuses or stage in final_stages
        if quote_date != today:
            rejected[str(code)] = "非当日报价"
        elif bool(quote.get("stale")) or not bool(quote.get("available", bool(quote))):
            rejected[str(code)] = "报价不可用或已过期"
        elif _number(quote.get("auction_price")) <= 0:
            rejected[str(code)] = "缺少竞价价格"
        elif status == "snapshot_only":
            rejected[str(code)] = "普通行情快照不是竞价终态"
        elif not live_window and not terminal:
            rejected[str(code)] = "非竞价窗口且缺少终态标记"
        elif _number(quote.get("auction_amount")) <= 0:
            rejected[str(code)] = "缺少竞价成交额，仅有价格快照"
        else:
            accepted[str(code)] = quote

    return accepted, {
        "valid": bool(accepted),
        "scan_date": today,
        "live_window": live_window,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "rejected_reasons": dict(sorted({reason: list(rejected.values()).count(reason) for reason in set(rejected.values())}.items())),
    }


def _present_yijiner_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose separate scan/base dates and safely downgrade legacy pre-auction runs."""

    if not run:
        return run
    result = deepcopy(run)
    metadata = result.setdefault("metadata", {})
    created_at = str(result.get("created_at") or "")
    scan_date = str(metadata.get("scan_date") or _date_part(created_at) or result.get("trade_date") or "")
    base_date = str(metadata.get("base_trade_date") or result.get("trade_date") or "")
    result["scan_date"] = scan_date
    result["base_trade_date"] = base_date

    validation = metadata.get("auction_validation")
    if not isinstance(validation, dict):
        created_minute = None
        time_match = re.search(r"T(\d{2}):(\d{2})", created_at)
        if time_match:
            created_minute = int(time_match.group(1)) * 60 + int(time_match.group(2))
        validation = {
            "valid": created_minute is None or created_minute >= 9 * 60 + 15,
            "legacy_inferred": True,
        }
        metadata["auction_validation"] = validation

    # Older runs accepted same-day price-only Tencent snapshots as a complete
    # auction.  They had zero matched turnover for every row, so presenting
    # their tier/auction score as verified would be misleading.
    if validation.get("valid") and result.get("rows") and not any(
        _number(row.get("auction_amount")) > 0 for row in result["rows"]
    ):
        validation = {**validation, "valid": False, "reason": "竞价成交额未验证"}
        metadata["auction_validation"] = validation

    if not bool(validation.get("valid")):
        market_state = metadata.setdefault("market_state", {})
        market_state.update({"auction_breadth": None, "auction_quotes_count": 0, "auction_available": False})
        result["auction_available_count"] = 0
        for row in result.get("rows") or []:
            row["tier"] = ""
            row["score"] = row.get("first_board_score")
            row["auction_stage_score"] = None
            row["auction_amount"] = None
            row["auction_change_pct"] = None
            row["auction_amount_ratio_pct"] = 0.0
            row["auction_amount_to_mcap_pct"] = 0.0
            row["decision"] = "watch"
            row["decision_reason"] = "扫描发生在竞价开始前，无有效当日竞价"
            snapshot = row.setdefault("snapshot", {})
            snapshot["auction_available"] = False
            flags = row.setdefault("risk_flags", [])
            flags[:] = [flag for flag in flags if "竞价" not in str(flag)]
            flags.append("无有效当日竞价数据，仅展示昨日首板分")
    return result


def _normalize_rows(value: Any) -> tuple[list[dict[str, Any]], int, str]:
    if isinstance(value, list):
        source = str(value[0].get("source") or "live") if value and isinstance(value[0], dict) else "live"
        return value, len(value), source
    if not isinstance(value, dict):
        return [], 0, "unavailable"
    rows = value.get("stocks") or value.get("items") or value.get("data") or []
    return list(rows), int(value.get("total") or len(rows)), str(value.get("source") or "live")


def _health_source_rows(health: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize provider health into rows shared by overview and settings."""

    source_state = health.get("sources")
    items = source_state.items() if isinstance(source_state, dict) else []
    rows: list[dict[str, Any]] = []
    for name, item in items:
        if not isinstance(item, dict):
            continue
        last_error = item.get("last_error") or {}
        healthy = bool(item.get("ok", True))
        rows.append(
            {
                "name": name,
                "status": "ok" if healthy else "error",
                "healthy": healthy,
                "updated_at": item.get("last_success")
                or (last_error.get("time") if isinstance(last_error, dict) else last_error),
                "message": item.get("last_error_message")
                or (last_error.get("message") if isinstance(last_error, dict) else "")
                or "数据适配器可用",
            }
        )
    ffd = health.get("ffd") if isinstance(health.get("ffd"), dict) else {}
    if ffd.get("enabled"):
        ready = ffd.get("daily_baseline_status") == "ready"
        rows.append(
            {
                "name": "ffd_daily_baseline",
                "status": "ok" if ready else "degraded",
                "healthy": ready,
                "degraded": not ready,
                "updated_at": ffd.get("daily_baseline_date"),
                "message": (
                    f"FFD 日线基线已就绪（{int(ffd.get('daily_baseline_rows') or 0)} 条）"
                    if ready
                    else "FFD 日线基线未就绪，已回退本地通达信"
                ),
            }
        )
    return rows


OVERNIGHT_INDUSTRY_TAXONOMY = {
    "食品饮料": ("白酒", "饮料制造", "食品加工制造", "农产品加工"), "医药生物": ("医疗器械", "医疗服务", "医药商业", "中药", "化学制药"),
    "家用电器": ("白色家电", "黑色家电", "小家电", "厨卫电器"), "汽车": ("汽车整车", "汽车服务及其他", "汽车零部件"),
    "商贸零售": ("贸易", "互联网电商"), "社会服务": ("教育", "旅游及酒店", "其他社会服务", "美容护理"),
    "传媒": ("游戏", "文化传媒", "影视院线"), "银行": ("银行",), "非银金融": ("证券", "保险", "多元金融"), "房地产": ("房地产",),
    "有色金属": ("贵金属", "工业金属", "能源金属", "小金属", "金属新材料"), "钢铁": ("钢铁",), "煤炭": ("煤炭开采加工",),
    "石油石化": ("油气开采及服务", "石油加工贸易"), "基础化工": ("农化制品", "化学原料", "化学制品", "化学纤维", "塑料制品", "橡胶制品", "非金属材料"),
    "建筑材料": ("建筑材料",), "建筑装饰": ("建筑装饰",), "机械设备": ("工程机械", "专用设备", "通用设备", "轨交设备", "自动化设备"),
    "电力设备": ("电网设备", "风电设备", "光伏设备", "电池", "其他电源设备", "电机"), "国防军工": ("军工装备", "军工电子"),
    "电子": ("半导体", "元件", "光学光电子", "电子化学品", "其他电子", "消费电子"), "计算机": ("计算机设备",), "通信": ("通信设备", "通信服务"),
    "公用事业": ("电力", "燃气", "环保设备", "环境治理"), "交通运输": ("机场航运", "港口航运", "公路铁路运输", "物流"),
    "农林牧渔": ("种植业与林业", "养殖业"), "轻工制造": ("造纸", "包装印刷", "家居用品"), "纺织服饰": ("纺织制造", "服装家纺"),
}
OVERNIGHT_INDUSTRY_ALIASES = {
    "半导体": ("半导体", "集成电路", "芯片", "功率器件"), "元件": ("元件", "PCB", "电子元器件"), "医疗器械": ("医疗器械", "体外诊断"),
    "旅游及酒店": ("旅游", "酒店"), "文化传媒": ("传媒", "文字图片媒体"), "煤炭开采加工": ("煤炭", "煤化工"), "油气开采及服务": ("油气", "石油", "天然气"),
    "电力": ("火力发电", "水力发电", "电力"), "物流": ("物流", "仓储"), "机场航运": ("机场", "航空"), "港口航运": ("港口", "航运"),
    "公路铁路运输": ("公路", "铁路"), "光伏设备": ("光伏",), "风电设备": ("风电",), "电网设备": ("电网",), "自动化设备": ("自动化",),
    "汽车零部件": ("汽车零部件", "车身", "汽车配件"), "互联网电商": ("电商",),
}


class XunlongService:
    def __init__(
        self,
        provider: MarketDataProvider,
        database: Database,
        *,
        enable_external_enrichment: bool | None = None,
    ) -> None:
        self.provider = provider
        self.db = database
        self._yijiner_scan_lock = Lock()
        # Test/fake providers must never reach public market APIs. Production
        # keeps enrichment enabled and each enrichment module owns its cache.
        self.enable_external_enrichment = (
            isinstance(provider, MarketDataProvider)
            if enable_external_enrichment is None
            else bool(enable_external_enrichment)
        )

    def _load_dragon_external_context(self) -> dict[str, Any]:
        if not self.enable_external_enrichment:
            return {
                "overseas": {},
                "retail_sentiment": {},
                "fund_consensus": {},
                "external_context": {"status": "disabled"},
            }

        context: dict[str, Any] = {
            "overseas": {},
            "retail_sentiment": {},
            "fund_consensus": {},
        }
        errors: list[str] = []
        try:
            from .overseas import fetch_overseas_snapshot

            context["overseas"] = fetch_overseas_snapshot()
        except Exception as exc:
            errors.append(f"overseas: {exc}")
        try:
            from .fund_flow import (
                compute_fund_consensus,
                compute_industry_retail_sentiment,
                fetch_etf_flow,
            )

            etf_data = fetch_etf_flow()
            context["retail_sentiment"] = compute_industry_retail_sentiment(etf_data)
            context["fund_consensus"] = compute_fund_consensus()
        except Exception as exc:
            errors.append(f"fund_flow: {exc}")
        context["external_context"] = {
            "status": "degraded" if errors else "ok",
            "errors": errors,
        }
        return context

    def settings(self) -> dict[str, Any]:
        return self.db.get_settings()

    def oversold_rebound(
        self,
        *,
        limit: int = 80,
        drawdown_min: float = 12.0,
        volume_multiple: float = 1.5,
        min_amount: float = 100_000_000.0,
        amount_multiple: float = 1.2,
        profile: str = "steady",
        wave: str = "all",
        triggered_only: bool = True,
        market_filter: bool = True,
        min_reward_risk: float = 1.0,
        require_ths_hot: bool = True,
        require_positive_dde: bool = True,
    ) -> dict[str, Any]:
        """Screen the local post-close universe for main-board rebound setups."""

        cache_key = (
            limit, drawdown_min, volume_multiple, min_amount, amount_multiple,
            profile, wave, triggered_only, market_filter, min_reward_risk,
            require_ths_hot, require_positive_dde,
        )
        cached = _OVERSOLD_CACHE.get(cache_key)
        if cached and (datetime.now() - cached[0]).total_seconds() < _OVERSOLD_CACHE_TTL:
            hit = dict(cached[1])
            hit["cache"] = {"hit": True, "age_seconds": int((datetime.now() - cached[0]).total_seconds())}
            return hit

        snapshots = self.provider.get_market_universe(limit=None)
        # The post-close K-line is intentionally a stable historical series, so
        # it can lag the live/quote overlay by one session.  Prefer the
        # snapshot's repaired quote date first and retain the K-line date as a
        # fallback for rows without a live overlay.
        trade_dates = []
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            candidates = [
                item.get("trade_date"),
                item.get("data_as_of"),
                item.get("quote_time"),
                item.get("as_of"),
            ]
            postclose = item.get("postclose_kline")
            if isinstance(postclose, list) and postclose:
                candidates.append(postclose[-1].get("date"))
            for value in candidates:
                normalized = str(value or "").strip()
                if len(normalized) >= 10 and normalized[4] == "-":
                    normalized = normalized[:10].replace("-", "")
                else:
                    normalized = normalized[:8]
                if len(normalized) == 8 and normalized.isdigit():
                    trade_dates.append(normalized)
                    break
        trade_date = max(trade_dates, default="")
        hot_getter = getattr(self.provider, "get_ths_hot_stocks", None)
        hot_payload = hot_getter(trade_date) if callable(hot_getter) else {
            "trade_date": trade_date,
            "rows": [],
            "count": 0,
            "available": False,
            "source": "unavailable",
            "_meta": {"errors": ["provider has no Tonghuashun hot-pool capability"]},
        }
        result = oversold.screen_universe(
            snapshots,
            hot_stocks=hot_payload.get("rows") or [],
            limit=limit,
            drawdown_min=drawdown_min,
            volume_multiple=volume_multiple,
            min_amount=min_amount,
            amount_multiple=amount_multiple,
            profile=profile,
            wave=wave,
            triggered_only=triggered_only,
            market_filter=market_filter,
            min_reward_risk=min_reward_risk,
            require_ths_hot=require_ths_hot,
            require_positive_dde=require_positive_dde,
        )
        result["provider_state"] = {
            "source": str(snapshots[0].get("source") or "local_tdx_postclose") if snapshots else "unavailable",
            "stale": any(bool(item.get("stale")) for item in snapshots[:100]),
        }
        result["popularity_state"] = {
            "available": bool(hot_payload.get("available")),
            "trade_date": hot_payload.get("trade_date") or trade_date,
            "count": int(hot_payload.get("count") or 0),
            "source": hot_payload.get("source") or "ths_hot_reason",
            "stale": bool((hot_payload.get("_meta") or {}).get("stale")),
            "errors": list((hot_payload.get("_meta") or {}).get("errors") or []),
        }
        result["cache"] = {"hit": False, "age_seconds": 0}
        _OVERSOLD_CACHE[cache_key] = (datetime.now(), result)
        return result

    def yijiner_scan(
        self,
        *,
        limit: int = 50,
        min_auction_amount: float | None = None,
        manual: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Run one guarded scan and avoid duplicate full-market requests."""

        current_time = now or datetime.now()
        scan_date = current_time.strftime("%Y-%m-%d")
        minute = current_time.hour * 60 + current_time.minute
        if current_time.weekday() >= 5 or minute < 9 * 60 + 15:
            reason = "当前为非交易日" if current_time.weekday() >= 5 else "集合竞价尚未开始"
            return {
                "status": "waiting",
                "persisted": False,
                "scan_date": scan_date,
                "trade_date": scan_date,
                "rows": [],
                "message": f"{reason}，未发起全市场扫描，也不会覆盖最近一次有效记录",
                "auction_validation": {
                    "valid": False,
                    "scan_date": scan_date,
                    "reason": reason,
                },
            }
        if not self._yijiner_scan_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "persisted": False,
                "scan_date": scan_date,
                "trade_date": scan_date,
                "rows": [],
                "message": "已有一进二扫描正在运行，本次请求未重复执行",
                "auction_validation": {"valid": False, "scan_date": scan_date, "reason": "scan_busy"},
            }
        try:
            return self._yijiner_scan_impl(
                limit=limit,
                min_auction_amount=min_auction_amount,
                manual=manual,
                now=current_time,
            )
        finally:
            self._yijiner_scan_lock.release()

    def yijiner_premarket_preview(
        self,
        *,
        limit: int = 50,
        min_auction_amount: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build today's watchlist from the latest completed session.

        This remains separate from ``yijiner_scan`` because genuine same-day
        auction observations do not exist before 09:15.  It refreshes the
        post-close first-board pool, never persists a successful auction run,
        and leaves all auction fields unavailable.
        """

        current_time = now or datetime.now()
        scan_date = current_time.strftime("%Y-%m-%d")
        if current_time.weekday() >= 5:
            return {
                "status": "waiting",
                "persisted": False,
                "scan_date": scan_date,
                "trade_date": scan_date,
                "rows": [],
                "message": "当前为非交易日，未生成盘前首板池",
                "auction_validation": {
                    "valid": False,
                    "scan_date": scan_date,
                    "reason": "当前为非交易日",
                },
            }
        if not self._yijiner_scan_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "persisted": False,
                "scan_date": scan_date,
                "trade_date": scan_date,
                "rows": [],
                "message": "已有一进二扫描正在运行，本次请求未重复执行",
                "auction_validation": {"valid": False, "scan_date": scan_date, "reason": "scan_busy"},
            }
        try:
            result = self._yijiner_scan_impl(
                limit=limit,
                min_auction_amount=min_auction_amount,
                manual=True,
                now=current_time,
            )
            result["preview"] = True
            result["message"] = (
                f"盘前预览：已用 {result.get('base_trade_date') or '最新交易日'} "
                "收盘数据刷新首板池；当日竞价数据尚未产生"
            )
            return result
        finally:
            self._yijiner_scan_lock.release()

    def _yijiner_scan_impl(
        self,
        *,
        limit: int,
        min_auction_amount: float | None,
        manual: bool,
        now: datetime,
    ) -> dict[str, Any]:
        """一进二评分卡扫描：昨日首板 ∩ 今日竞价爆量。"""

        current_time = now
        scan_date = current_time.strftime("%Y-%m-%d")
        snapshots = self.provider.get_market_universe(limit=None)
        # FFD supplies the date-valid full-market seed, while daily history is
        # hydrated only for symbols that can actually be yesterday's limit-up.
        hydrate = getattr(self.provider, "hydrate_postclose_klines", None)
        if callable(hydrate):
            likely_limit_up = [
                str(item.get("code") or "")
                for item in snapshots
                if str(item.get("code") or "").startswith(
                    ("000", "001", "002", "003", "600", "601", "603", "605")
                )
                and _number(item.get("change_pct")) >= 9.8
            ]
            if likely_limit_up:
                snapshots = hydrate(
                    snapshots,
                    codes=likely_limit_up,
                    max_symbols=max(120, len(likely_limit_up)),
                    days=120,
                )
        params: dict[str, Any] = {"min_auction_amount": min_auction_amount} if min_auction_amount else {}
        # 先用昨日收盘数据找出首板名单，再只为这些代码取竞价，控制竞价接口配额。
        first_board_codes = []
        for item in snapshots:
            code = str(item.get("code") or "")
            if not code or not yijiner.is_main_board_stock(code, item.get("name")):
                continue
            bars = item.get("postclose_kline") or []
            if not isinstance(bars, list) or len(bars) < 2:
                continue
            closes = [_number(row.get("close")) for row in bars]
            if len(closes) < 2 or min(closes[-1], closes[-2]) <= 0:
                continue
            prev_pct = (closes[-1] / closes[-2] - 1.0) * 100.0
            if prev_pct < float(params.get("limit_up_pct", yijiner.DEFAULT_PARAMS["limit_up_pct"])):
                continue
            if yijiner.board_count(closes) == 1:
                first_board_codes.append(code)
        # 本地通达信盘后快照不带流通市值：优先一次 FFD 全市场增强调用拿流通市值，
        # 缺口再用 get_stock_info 本地缓存兜底（东财口径）。
        by_code = {
            str(item.get("code") or ""): item
            for item in snapshots
            if isinstance(item, dict)
        }
        mcap_map: dict[str, float] = {}
        # 首板池为空时下面的涨停池分支仍会读取 iso_date，必须先给默认值。
        iso_date = ""
        ffd_getter = getattr(self.provider, "get_ffd_float_mcap", None)
        if callable(ffd_getter) and first_board_codes:
            trade_dates = [
                str((item.get("postclose_kline") or [{}])[-1].get("date") or "")[:10]
                for item in snapshots
                if isinstance(item.get("postclose_kline"), list) and item.get("postclose_kline")
            ]
            latest = max(trade_dates, default="")
            iso_date = (
                f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
                if len(latest) == 8 and latest.isdigit()
                else latest
            )
            if iso_date:
                mcap_map = ffd_getter(iso_date)
        # FFD 涨停池：炸板次数、首板时间、连板数、官方题材。
        pool_payload: dict[str, Any] = {}
        pool_getter = getattr(self.provider, "get_ffd_limit_pool", None)
        if callable(pool_getter) and iso_date:
            pool_payload = pool_getter(iso_date) or {}
        info_getter = getattr(self.provider, "get_stock_info", None)
        for code in first_board_codes:
            item = by_code.get(code)
            if not item:
                continue
            if _number(item.get("float_market_cap")) > 0:
                if (
                    str(item.get("reference_source") or "") == "ffd_market_daily"
                    or str(item.get("source") or "").startswith("ffd_market_daily")
                ):
                    item["float_mcap_source"] = "ffd_market_daily_enhanced"
                continue
            if mcap_map.get(code):
                item["float_market_cap"] = mcap_map[code]
                item["float_mcap_source"] = "ffd_market_daily_enhanced"
                continue
            if callable(info_getter):
                try:
                    info = info_getter(code)
                except Exception:
                    continue
                if isinstance(info, dict) and _number(info.get("float_mcap")) > 0:
                    item["float_market_cap"] = _number(info.get("float_mcap"))
                    item["float_mcap_source"] = "stock_info_fallback"
        auction_getter = getattr(self.provider, "get_auction_quotes", None)
        quotes: dict[str, dict[str, Any]] = {}
        auction_validation: dict[str, Any] = {
            "valid": False,
            "scan_date": scan_date,
            "live_window": False,
            "accepted_count": 0,
            "rejected_count": 0,
            "rejected_reasons": {},
        }
        minute = current_time.hour * 60 + current_time.minute
        can_request_auction = current_time.weekday() < 5 and minute >= 9 * 60 + 15
        if callable(auction_getter) and first_board_codes and can_request_auction:
            try:
                try:
                    raw = auction_getter(first_board_codes)
                except TypeError:
                    raw = auction_getter(first_board_codes)
                if isinstance(raw, dict):
                    quotes = {
                        str(key): dict(value)
                        for key, value in raw.items()
                        if isinstance(value, dict)
                    }
                    quotes, auction_validation = _filter_same_day_auction_quotes(quotes, current_time)
            except Exception:
                quotes = {}
        elif not can_request_auction:
            auction_validation["reason"] = "非交易日或集合竞价尚未开始"
        traces: dict[str, dict[str, Any]] = {}
        try:
            traces = self.db.get_auction_traces(scan_date)
        except Exception:
            traces = {}
        result = yijiner.screen_yijiner(
            snapshots, quotes, limit=limit, params=params, limit_pool=pool_payload,
            trace_by_code=traces,
        )
        base_trade_date = str(result.get("trade_date") or "")
        result["base_trade_date"] = base_trade_date
        result["scan_date"] = scan_date
        result["trade_date"] = scan_date
        result["auction_validation"] = auction_validation
        # 双源交叉校验：FFD 竞价终态 vs 腾讯实时价。09:25-09:27 两者应一致；
        # 偏差超阈值说明某一源异常，逐票打数据质量旗标（只提示，不改决策）。
        cross_checked = 0
        cross_warned = 0
        cross_getter = getattr(self.provider, "get_quotes", None)
        if callable(cross_getter) and first_board_codes:
            try:
                try:
                    ref_rows = cross_getter(first_board_codes, force=True)
                except TypeError:
                    ref_rows = cross_getter(first_board_codes)
            except Exception:
                ref_rows = {}
            for row in result.get("rows") or []:
                code = str(row.get("code") or "")
                auction_price = _number(row.get("auction_price"))
                ref = ref_rows.get(code) or {} if isinstance(ref_rows, dict) else {}
                ref_price = _number(ref.get("price")) or _number(ref.get("open"))
                if not auction_price or not ref_price:
                    continue
                cross_checked += 1
                dev = abs(auction_price - ref_price) / max(auction_price, ref_price) * 100.0
                if dev > 0.5:
                    cross_warned += 1
                    row["cross_dev_pct"] = round(dev, 2)
                    row.setdefault("risk_flags", []).append(
                        f"双源校验偏差{dev:.2f}%（竞价{auction_price} vs 腾讯{ref_price}），数据源异常需人工核对"
                    )
        funnel = result.get("funnel") or {}
        ffd_sourced = sum(
            1
            for code in first_board_codes
            if (by_code.get(code) or {}).get("float_mcap_source") == "ffd_market_daily_enhanced"
        )
        result["provider_state"] = {
            "source": str(snapshots[0].get("source") or "local_tdx_postclose") if snapshots else "unavailable",
            "stale": any(bool(item.get("stale")) for item in snapshots[:100]),
            "cross_check": {"checked": cross_checked, "warned": cross_warned},
            "float_mcap_sources": {
                "ffd": ffd_sourced,
                "fallback": max(len(first_board_codes) - ffd_sourced, 0),
            },
        }
        result["source"] = (
            f"{result['provider_state']['source']} + FFD limit_pool + auction_quotes"
        )
        if not manual:
            result["message"] = (
                f"一进二扫描：昨日首板 {funnel.get('prev_first_boards', 0)} 只，"
                f"候选 {len(result.get('rows') or [])} 只"
            )
        for row in result.get("rows") or []:
            row["snapshot"] = {
                "float_mcap": row.get("float_mcap"),
                "auction_source": row.get("auction_source"),
                "cross_dev_pct": row.get("cross_dev_pct"),
                "perf20": row.get("perf20"),
                "volume_multiple": row.get("volume_multiple"),
                "near_high": row.get("near_high"),
                "ma_ok": row.get("ma_ok"),
                "ma_bull": row.get("ma_bull"),
                "tier_label": row.get("tier_label"),
                "auction_available": row.get("auction_available"),
                "auction_amount_to_mcap_pct": row.get("auction_amount_to_mcap_pct"),
                "open_count": row.get("open_count"),
                "first_limit_time": row.get("first_limit_time"),
                "limit_reason": row.get("limit_reason"),
                "related_concepts": row.get("related_concepts"),
            }
        if not bool(auction_validation.get("valid")):
            result["status"] = "waiting"
            result["persisted"] = False
            rejected = auction_validation.get("rejected_reasons") or {}
            detail = "、".join(f"{reason} {count} 只" for reason, count in rejected.items())
            result["message"] = (
                f"未取得当日有效竞价成交额（{detail or '数据源无有效返回'}），本次仅完成昨日首板预览，"
                "不会覆盖最近一次有效扫描记录"
            )
            return result
        self.db.create_yijiner_run(
            {
                "trade_date": result.get("trade_date"),
                "universe_count": funnel.get("main_board", 0),
                "prev_limit_up_count": result.get("market_state", {}).get("prev_limit_up_count", 0),
                "first_board_count": funnel.get("prev_first_boards", 0),
                "auction_available_count": result.get("market_state", {}).get("auction_quotes_count", 0),
                "status": "success",
                "source": result.get("source", "live"),
                "message": result.get("message", ""),
                "strategy_version": result.get("strategy_version", yijiner.STRATEGY_VERSION),
                "metadata": {
                    "scan_date": scan_date,
                    "base_trade_date": base_trade_date,
                    "auction_validation": auction_validation,
                    "tier_counts": result.get("tier_counts", {}),
                    "thresholds": result.get("thresholds", {}),
                    "market_state": result.get("market_state", {}),
                    "provider_state": result.get("provider_state", {}),
                    "manual": manual,
                },
            },
            result.get("rows") or [],
        )
        result["status"] = "success"
        result["persisted"] = True
        return result

    def yijiner_latest(self) -> dict[str, Any]:
        run = _present_yijiner_run(self.db.get_latest_yijiner_run())
        return {"run": run, "available": bool(run)}

    def yijiner_history(self, limit: int = 20) -> dict[str, Any]:
        return {"runs": [_present_yijiner_run(run) for run in self.db.list_yijiner_runs(limit)]}

    def sample_auction_trace(self) -> dict[str, Any]:
        """交易日 09:22 对昨日首板池采样竞价快照（免费腾讯通道）。

        样本供一进二评分卡的“竞价过程弱转强”路径因子：早期明显水下而
        09:25 终态健康高开，是视频战法里的低位弱转强形态；反之早期高开
        后终态回落则是“冲高回落”。采样失败不影响任何下游任务。
        """

        now = datetime.now()
        trade_date = now.strftime("%Y-%m-%d")
        if now.weekday() >= 5:
            return {"trade_date": trade_date, "sampled": 0, "message": "非交易日，跳过竞价采样"}
        minute = now.hour * 60 + now.minute
        if not (9 * 60 + 15 <= minute <= 9 * 60 + 25):
            return {"trade_date": trade_date, "sampled": 0, "message": "不在 09:15-09:25 竞价窗口，跳过采样"}

        universe = self.provider.get_market_universe(limit=None)
        rows, _, _ = _normalize_rows(universe)
        snapshots = [
            row for row in rows
            if re.fullmatch(r"\d{6}", str(row.get("code", "")))
            and not scoring.is_star_security(row.get("code"), row.get("name"))
        ]
        hydrate = getattr(self.provider, "hydrate_postclose_klines", None)
        if callable(hydrate):
            likely_limit_up = [
                str(item.get("code") or "")
                for item in snapshots
                if str(item.get("code") or "").startswith(
                    ("000", "001", "002", "003", "600", "601", "603", "605")
                )
                and _number(item.get("change_pct")) >= yijiner.DEFAULT_PARAMS["limit_up_pct"]
            ]
            if likely_limit_up:
                snapshots = hydrate(
                    snapshots,
                    codes=likely_limit_up,
                    max_symbols=max(120, len(likely_limit_up)),
                    days=120,
                )
        first_board_codes: list[str] = []
        for item in snapshots:
            code = str(item.get("code") or "")
            if not code or not yijiner.is_main_board_stock(code, item.get("name")):
                continue
            bars = item.get("postclose_kline") or []
            if not isinstance(bars, list) or len(bars) < 2:
                continue
            closes = [_number(row.get("close")) for row in bars]
            if len(closes) < 2 or min(closes[-1], closes[-2]) <= 0:
                continue
            if (closes[-1] / closes[-2] - 1.0) * 100.0 < yijiner.DEFAULT_PARAMS["limit_up_pct"]:
                continue
            if yijiner.board_count(closes) == 1:
                first_board_codes.append(code)
        if not first_board_codes:
            return {"trade_date": trade_date, "sampled": 0, "first_boards": 0, "message": "昨日首板池为空，未采样"}

        quotes = self._quote_batch(first_board_codes, auction=False, force=True)
        payload = []
        for code in first_board_codes:
            quote = quotes.get(code) or {}
            price = _number(quote.get("price")) or _number(quote.get("open"))
            if not price or price <= 0:
                continue
            change = _number(quote.get("change_pct"))
            last_close = _number(quote.get("last_close")) or _number(quote.get("previous_close"))
            if change is None and price and last_close:
                change = round((price / last_close - 1.0) * 100.0, 2)
            payload.append(
                {
                    "code": code,
                    "sampled_at": now.strftime("%H:%M"),
                    "price": round(price, 3),
                    "change_pct": change,
                    "source": str(quote.get("source") or "unknown"),
                }
            )
        saved = self.db.save_auction_traces(trade_date, payload)
        return {
            "trade_date": trade_date,
            "first_boards": len(first_board_codes),
            "sampled": saved,
            "message": f"竞价采样完成：首板池 {len(first_board_codes)} 只，采到 {saved} 只（09:2x 路径因子用）",
        }

    def stock_perf20(self, code: str) -> dict[str, Any]:
        """单只主板股票近 20 个交易日表现（已收盘K线，无估算）。"""

        code = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码格式不正确（6位数字）")
        if not scoring.is_main_board_security(code, ""):
            raise ValueError("仅支持沪深主板代码（000/001/002/003/600/601/603/605）")
        hydrate = getattr(self.provider, "hydrate_postclose_klines", None)
        if not callable(hydrate):
            raise RuntimeError("K线数据通道不可用")
        hydrated = hydrate([{"code": code}], codes=[code], max_symbols=1, days=60)
        item = (hydrated or [{}])[0]
        bars = item.get("postclose_kline") or []
        perf = yijiner.compute_perf20(bars if isinstance(bars, list) else [])
        if not perf:
            raise LookupError("K线数据不足，无法计算20日表现（请确认通达信盘后数据已同步）")
        return {
            "code": code,
            "name": str(item.get("name") or ""),
            "industry": str(item.get("industry") or ""),
            "as_of": str((bars[-1] if bars else {}).get("date") or "")[:10],
            "perf": perf,
        }

    def yijiner_outcomes(self, run_id: int | None = None) -> dict[str, Any]:
        """候选真实表现复盘：决策日是否封住二板、次日是否续板、平均最高涨幅。

        结果按 run_id 缓存进 yijiner_outcomes；重复请求零成本。该闭环用于
        把评分卡里"待回测"的经验概率逐步替换成自积累的实测胜率。
        """

        run = self.db.get_yijiner_run(run_id) if run_id else self.db.get_latest_yijiner_run()
        if not run:
            raise LookupError("暂无一进二扫描记录，无法复盘")
        run_id = int(run["id"])
        cached = self.db.get_yijiner_outcome(run_id)
        if cached:
            return cached
        trade_date = str(run.get("trade_date") or "")[:10]
        rows = run.get("rows") or []
        codes = [str(item.get("code")) for item in rows if re.fullmatch(r"\d{6}", str(item.get("code") or ""))][:100]
        outcome_rows: dict[str, dict[str, Any]] = {}
        if codes and trade_date:
            hydrate = getattr(self.provider, "hydrate_postclose_klines", None)
            if callable(hydrate):
                try:
                    hydrated = hydrate(
                        [{"code": code} for code in codes],
                        codes=codes,
                        max_symbols=max(60, len(codes)),
                        days=20,
                    )
                except Exception:
                    hydrated = None
                for item in hydrated or []:
                    code = str(item.get("code") or "")
                    bars = item.get("postclose_kline") or []
                    if not isinstance(bars, list):
                        continue
                    index = next((i for i, bar in enumerate(bars) if str(bar.get("date") or "")[:10] == trade_date), None)
                    if index is None or index < 1:
                        continue
                    prev_close = _number(bars[index - 1].get("close"))
                    day0 = bars[index]
                    day1 = bars[index + 1] if index + 1 < len(bars) else None
                    if not prev_close or prev_close <= 0:
                        continue
                    entry: dict[str, Any] = {
                        "gap_pct": round((_number(day0.get("open")) or prev_close) / prev_close * 100 - 100, 2),
                        "max_gain_pct": round(((_number(day0.get("high")) or prev_close) / prev_close - 1) * 100, 2),
                        "close_pct": round(((_number(day0.get("close")) or prev_close) / prev_close - 1) * 100, 2),
                        "limit_up_day0": bool((_number(day0.get("close")) or 0) / prev_close >= 1.098),
                    }
                    if day1 is not None:
                        day1_prev = _number(day0.get("close")) or prev_close
                        entry["next_close_pct"] = round(((_number(day1.get("close")) or day1_prev) / day1_prev - 1) * 100, 2)
                        entry["next_max_gain_pct"] = round(((_number(day1.get("high")) or day1_prev) / day1_prev - 1) * 100, 2)
                        entry["next_limit_up"] = bool((_number(day1.get("close")) or 0) / day1_prev >= 1.098)
                    outcome_rows[code] = entry
        evaluated = [item for item in rows if str(item.get("code")) in outcome_rows]
        candidates = [item for item in evaluated if item.get("decision") == "candidate"]
        limit_day0 = sum(1 for item in candidates if outcome_rows[str(item["code"])]["limit_up_day0"])
        next_pairs = [item for item in candidates if "next_limit_up" in outcome_rows[str(item["code"])]]
        next_limit = sum(1 for item in next_pairs if outcome_rows[str(item["code"])]["next_limit_up"])
        max_gains = [outcome_rows[str(item["code"])]["max_gain_pct"] for item in evaluated]
        payload = {
            "run_id": run_id,
            "trade_date": trade_date,
            "available": bool(outcome_rows),
            "note": "" if outcome_rows else "决策日K线尚未生成（盘中或数据未同步），稍后再试",
            "stats": {
                "evaluated": len(evaluated),
                "candidates": len(candidates),
                "candidates_limit_up_day0": limit_day0,
                "candidates_hit_rate": round(limit_day0 / len(candidates) * 100, 1) if candidates else None,
                "next_day_samples": len(next_pairs),
                "next_day_limit_up": next_limit,
                "next_day_hit_rate": round(next_limit / len(next_pairs) * 100, 1) if next_pairs else None,
                "avg_max_gain": round(sum(max_gains) / len(max_gains), 2) if max_gains else None,
            },
            "rows": outcome_rows,
        }
        if outcome_rows:
            self.db.save_yijiner_outcome(run_id, payload)
        return payload

    def yijiner_winrates(self, lookback: int = 20) -> dict[str, Any]:
        """分档实测胜率：聚合最近 N 次扫描的候选真实表现，对照经验概率。

        S/A/B/C/D 各档的"封住二板率"与"次日续板率"逐档统计，样本随每天
        实盘自动积累。outcomes 有缓存，本聚合本身开销可忽略。
        """

        runs = self.db.list_yijiner_runs(max(1, min(lookback, 100)))
        tiers: dict[str, dict[str, int]] = {}
        total_c = total_c_hit = 0
        runs_used = 0
        earliest = ""
        for run in runs:
            try:
                payload = self.yijiner_outcomes(int(run["id"]))
            except Exception:
                continue
            if not payload.get("available"):
                continue
            runs_used += 1
            earliest = str(run.get("trade_date") or earliest) if not earliest else min(earliest, str(run.get("trade_date") or earliest))
            outcome_rows = payload.get("rows") or {}
            for item in run.get("rows") or []:
                code = str(item.get("code") or "")
                oc = outcome_rows.get(code)
                if not oc:
                    continue
                bucket = tiers.setdefault(str(item.get("tier") or ""), {"n": 0, "day0_hit": 0, "next_n": 0, "next_hit": 0})
                bucket["n"] += 1
                if oc.get("limit_up_day0"):
                    bucket["day0_hit"] += 1
                if "next_limit_up" in oc:
                    bucket["next_n"] += 1
                    if oc.get("next_limit_up"):
                        bucket["next_hit"] += 1
                if item.get("decision") == "candidate":
                    total_c += 1
                    if oc.get("limit_up_day0"):
                        total_c_hit += 1
        tier_stats: dict[str, dict[str, Any]] = {}
        for tier, bucket in tiers.items():
            tier_stats[tier] = {
                "n": bucket["n"],
                "day0_hit": bucket["day0_hit"],
                "day0_hit_rate": round(bucket["day0_hit"] / bucket["n"] * 100.0, 1) if bucket["n"] else None,
                "next_n": bucket["next_n"],
                "next_hit": bucket["next_hit"],
                "next_hit_rate": round(bucket["next_hit"] / bucket["next_n"] * 100.0, 1) if bucket["next_n"] else None,
            }
        return {
            "lookback": len(runs),
            "runs_used": runs_used,
            "since": earliest,
            "tiers": tier_stats,
            "candidates": {
                "n": total_c,
                "day0_hit": total_c_hit,
                "hit_rate": round(total_c_hit / total_c * 100.0, 1) if total_c else None,
            },
        }

    def liveboard(self, codes: list[str]) -> dict[str, Any]:
        """实时行情轻量榜（腾讯免费源）：竞价实时排行与开盘盯盘共用。

        返回字段刻意保持最小：名称/现价/涨跌幅/成交额/量比/昨收/来源。
        """

        clean: list[str] = []
        seen: set[str] = set()
        for item in codes or []:
            code = str(item).strip()
            if re.fullmatch(r"\d{6}", code) and code not in seen:
                seen.add(code)
                clean.append(code)
        clean = clean[:200]
        if not clean:
            return {"rows": [], "count": 0}
        getter = getattr(self.provider, "get_quotes", None)
        if not callable(getter):
            return {"rows": [], "count": 0}
        try:
            try:
                raw = getter(clean, force=True)
            except TypeError:
                raw = getter(clean)
        except Exception:
            raw = {}
        rows: list[dict[str, Any]] = []
        for code in clean:
            quote = (raw or {}).get(code) if isinstance(raw, dict) else None
            if not isinstance(quote, dict):
                continue
            price = _number(quote.get("price")) or _number(quote.get("open"))
            if not price or price <= 0:
                continue
            rows.append(
                {
                    "code": code,
                    "name": str(quote.get("name") or code),
                    "price": round(price, 2),
                    "change_pct": _number(quote.get("change_pct")),
                    "amount": _number(quote.get("amount")),
                    "volume_ratio": _number(quote.get("volume_ratio")),
                    "last_close": _number(quote.get("last_close")) or _number(quote.get("previous_close")),
                    "source": str(quote.get("source") or ""),
                }
            )
        return {"rows": rows, "count": len(rows), "as_of": datetime.now().strftime("%H:%M:%S")}

    def auction_trace_status(self) -> dict[str, Any]:
        """今日竞价采样进度（09:22 采样任务的页面反馈）。"""

        today = datetime.now().strftime("%Y-%m-%d")
        traces = self.db.get_auction_traces(today)
        if not traces:
            return {"trade_date": today, "count": 0, "at": "", "available": False, "codes": []}
        at = min((str(v.get("first_at") or "") for v in traces.values() if v.get("first_at")), default="")
        return {"trade_date": today, "count": len(traces), "at": at, "available": True, "codes": sorted(traces.keys())}

    def dixi_scan(self, *, limit: int = 50, manual: bool = True) -> dict[str, Any]:
        """素衣低吸模式：趋势+人气+回调反包，盘后生成次日低吸计划。"""

        snapshots = self.provider.get_market_universe(limit=None)
        hydrate = getattr(self.provider, "hydrate_postclose_klines", None)
        if callable(hydrate):
            snapshots = hydrate(snapshots, max_symbols=400, days=120)
        hot_getter = getattr(self.provider, "get_ths_hot_stocks", None)
        hot_payload = hot_getter() if callable(hot_getter) else {"rows": [], "available": False}
        history_getter = getattr(self.provider, "get_market_amount_history", None)
        amount_history: list[float] = []
        market_total = 0.0
        if callable(history_getter):
            try:
                payload = history_getter()
                if isinstance(payload, dict):
                    amount_history = [_number(v) for v in (payload.get("rows") or payload.get("amounts") or [])]
                    market_total = _number(payload.get("latest"))
            except Exception:
                amount_history = []
        result = dixi.screen_universe(
            snapshots,
            hot_stocks=hot_payload.get("rows") or [],
            limit=limit,
            market_total_amount=market_total,
            market_amount_history=amount_history,
        )
        funnel = result.get("funnel") or {}
        result["provider_state"] = {
            "source": str(snapshots[0].get("source") or "local_tdx_postclose") if snapshots else "unavailable",
            "stale": any(bool(item.get("stale")) for item in snapshots[:100]),
            "hot_available": bool(hot_payload.get("available")),
        }
        result["source"] = result["provider_state"]["source"]
        if not manual:
            result["message"] = (
                f"低吸计划：候选 {len(result.get('rows') or [])} 只，"
                f"模式触发 {funnel.get('triggered', 0)} 只"
            )
        for row in result.get("rows") or []:
            row["snapshot"] = {
                "volume_ratio": row.get("volume_ratio"),
                "channel_gain_pct": row.get("channel_gain_pct"),
                "pullback_shrink_ratio": row.get("pullback_shrink_ratio"),
                "ma10": row.get("ma10"),
                "ma20": row.get("ma20"),
                "buy_reference": row.get("buy_reference"),
                "stop_reference": row.get("stop_reference"),
                "hot_rank": row.get("hot_rank"),
            }
        self.db.create_dixi_run(
            {
                "trade_date": result.get("trade_date"),
                "universe_count": funnel.get("main_board", 0),
                "amount_qualified_count": funnel.get("amount_qualified", 0),
                "triggered_count": funnel.get("triggered", 0),
                "market_shrink": result.get("market_state", {}).get("market_shrink", False),
                "status": "success",
                "source": result.get("source", "live"),
                "message": result.get("message", ""),
                "strategy_version": result.get("strategy_version", dixi.STRATEGY_VERSION),
                "metadata": {
                    "thresholds": result.get("thresholds", {}),
                    "market_state": result.get("market_state", {}),
                    "provider_state": result.get("provider_state", {}),
                    "manual": manual,
                },
            },
            result.get("rows") or [],
        )
        return result

    def dixi_latest(self) -> dict[str, Any]:
        run = self.db.get_latest_dixi_run()
        return {"run": run, "available": bool(run)}

    def dixi_auction_check(self) -> dict[str, Any]:
        """昨日低吸计划池的今日竞价验证（符合昨晚思路才执行）。"""

        run = self.db.get_latest_dixi_run()
        rows = (run or {}).get("rows") or []
        codes = [str(row.get("code") or "") for row in rows if row.get("code")]
        quotes: dict[str, dict[str, Any]] = {}
        if codes:
            auction_getter = getattr(self.provider, "get_auction_quotes", None)
            if callable(auction_getter):
                try:
                    try:
                        raw = auction_getter(codes)
                    except TypeError:
                        raw = auction_getter(codes)
                    if isinstance(raw, dict):
                        quotes = {
                            str(key): dict(value)
                            for key, value in raw.items()
                            if isinstance(value, dict)
                        }
                except Exception:
                    quotes = {}
        # 仅交易日 09:25-09:30 的终态竞价才做硬性执行判定，其余时段标为参考
        now = datetime.now()
        enforce = now.weekday() < 5 and now.hour == 9 and 25 <= now.minute <= 30
        checked = dixi.auction_verify(rows, quotes, enforce=enforce)
        return {
            "run_id": (run or {}).get("id"),
            "plan_trade_date": (run or {}).get("trade_date"),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "quotes_available": bool(quotes),
            "rows": checked,
            "disclaimer": DISCLAIMER,
        }

    def dixi_history(self, limit: int = 20) -> dict[str, Any]:
        return {"runs": self.db.list_dixi_runs(limit)}

    def maifu_overview(self) -> dict[str, Any]:
        """埋伏挖掘总览：事件日历 + 大热股映射 + 热议快讯。"""

        snapshots = self.provider.get_market_universe(limit=None)
        # The post-close K-line is a stable historical series and can lag the
        # repaired live/quote overlay by one session. Prefer the snapshot's
        # latest quote date, with the K-line date as a fallback.
        trade_dates = []
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            candidates = [
                item.get("trade_date"),
                item.get("data_as_of"),
                item.get("quote_time"),
                item.get("as_of"),
            ]
            postclose = item.get("postclose_kline")
            if isinstance(postclose, list) and postclose:
                candidates.append(postclose[-1].get("date"))
            for value in candidates:
                normalized = str(value or "").strip()
                if len(normalized) >= 10 and normalized[4] == "-":
                    normalized = normalized[:10].replace("-", "")
                else:
                    normalized = normalized[:8]
                if len(normalized) == 8 and normalized.isdigit():
                    trade_dates.append(normalized)
                    break
        latest = max(trade_dates, default="")
        iso_date = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}" if len(latest) == 8 and latest.isdigit() else latest
        pool: dict[str, Any] = {}
        pool_getter = getattr(self.provider, "get_ffd_limit_pool", None)
        if callable(pool_getter) and iso_date:
            try:
                pool = pool_getter(iso_date) or {}
            except Exception:
                pool = {}
        news: list[dict[str, Any]] = []
        # 埋伏日历需要覆盖更广的题材线索，优先使用便携版同款多源快讯；
        # 旧 Provider 没有该方法时仍兼容原有 FFD/东财/腾讯链路。
        news_getter = getattr(self.provider, "get_maifu_news", None)
        if not callable(news_getter):
            news_getter = getattr(self.provider, "get_market_news", None)
        if callable(news_getter):
            try:
                # Keep the full half-day multi-source feed. The UI virtualizes
                # the visible area with a scroll container and provides search
                # and filters, so the API must not collapse it to the old
                # headline-only sample of 15 items.
                news = news_getter(limit=3000) or []
            except Exception:
                news = []
        result = maifu.build_overview(
            snapshots,
            limit_pool=pool,
            trade_date=latest,
            news=news,
        )
        status_getter = getattr(self.provider, "get_maifu_news_status", None)
        if callable(status_getter):
            try:
                result["news_sources"] = status_getter() or {}
            except Exception:
                result["news_sources"] = {}
        return result

    def research_pool(self) -> dict[str, Any]:
        items = self.db.list_research_watchlist()
        return {
            "items": items,
            "count": len(items),
            "analyzed_count": sum(1 for item in items if item.get("analysis")),
            "mode": "manual_research_pool",
            "disclaimer": DISCLAIMER,
        }

    def add_research_stock(self, code: str, note: str = "") -> dict[str, Any]:
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码必须是6位数字")
        if not scoring.is_main_board_security(code):
            raise ValueError("研究池当前仅支持沪深主板股票")
        quote = self.provider.get_quote(code)
        name = str(quote.get("name") or code)
        return self.db.upsert_research_stock(code, name, str(note or "").strip())

    def import_research_stocks(self, text: str, note: str = "") -> dict[str, Any]:
        """Extract, validate and upsert up to 200 pasted main-board codes."""

        raw = str(text or "")
        codes = list(dict.fromkeys(re.findall(r"(?<!\d)(\d{6})(?!\d)", raw)))
        if not codes:
            raise ValueError("粘贴内容中没有识别到6位股票代码")
        if len(codes) > 200:
            raise ValueError("单次最多导入200只股票，请分批粘贴")

        existing = {str(item.get("code") or "") for item in self.db.list_research_watchlist()}
        universe = self.provider.get_market_universe(limit=None)
        by_code = {
            str(item.get("code") or ""): item
            for item in universe
            if isinstance(item, dict)
        }
        added: list[str] = []
        updated: list[str] = []
        rejected: list[dict[str, str]] = []
        shared_note = str(note or "").strip()
        for code in codes:
            if not scoring.is_main_board_security(code):
                rejected.append({"code": code, "reason": "非沪深主板"})
                continue
            quote = by_code.get(code)
            if not quote:
                rejected.append({"code": code, "reason": "本地股票池未找到该代码"})
                continue
            name = str(quote.get("name") or code)
            self.db.upsert_research_stock(code, name, shared_note)
            if code in existing:
                updated.append(code)
            else:
                added.append(code)
                existing.add(code)
        accepted = [code for code in codes if code in set(added + updated)]
        return {
            "detected_count": len(codes),
            "accepted_count": len(accepted),
            "added_count": len(added),
            "updated_count": len(updated),
            "rejected_count": len(rejected),
            "accepted_codes": accepted,
            "added_codes": added,
            "updated_codes": updated,
            "rejected": rejected,
        }

    def remove_research_stock(self, code: str) -> bool:
        return self.db.delete_research_stock(str(code or "").strip())

    @staticmethod
    def _news_research_view(news: list[dict[str, Any]]) -> dict[str, Any]:
        positive_terms = ("利好", "增长", "中标", "增持", "突破", "回购", "上调", "盈利")
        negative_terms = ("风险", "下滑", "亏损", "减持", "处罚", "诉讼", "问询", "终止")
        positive = negative = 0
        rows = []
        for item in news[:8]:
            text = f"{item.get('title', '')} {item.get('content', '')}"
            sentiment = str(item.get("sentiment_label") or item.get("sentiment") or "").lower()
            if any(token in sentiment for token in ("positive", "bullish", "正面", "积极")) or any(term in text for term in positive_terms):
                tone = "正面"
                positive += 1
            elif any(token in sentiment for token in ("negative", "bearish", "负面", "消极")) or any(term in text for term in negative_terms):
                tone = "负面"
                negative += 1
            else:
                tone = "中性"
            rows.append(
                {
                    "title": item.get("title", ""),
                    "time": item.get("time", ""),
                    "summary": item.get("content", ""),
                    "tone": tone,
                }
            )
        if not rows:
            conclusion = "近期未取得有效新闻，新闻面暂不评分。"
        elif negative > positive:
            conclusion = f"近期负面线索多于正面线索（{negative}比{positive}），需优先核验风险事件。"
        elif positive > negative:
            conclusion = f"近期正面线索多于负面线索（{positive}比{negative}），仍需确认是否已被价格反映。"
        else:
            conclusion = "近期新闻情绪大致均衡，暂未形成单边催化判断。"
        return {"items": rows, "positive": positive, "negative": negative, "conclusion": conclusion}

    @staticmethod
    def _financial_research_view(financial: dict[str, Any]) -> dict[str, Any]:
        metrics = list(financial.get("metrics") or [])
        by_key = {str(item.get("key")): _number(item.get("value")) for item in metrics}
        positives: list[str] = []
        risks: list[str] = []
        if "roe" in by_key:
            (positives if by_key["roe"] >= 12 else risks).append(f"ROE {by_key['roe']:.1f}%")
        if "revenue_yoy" in by_key:
            (positives if by_key["revenue_yoy"] > 0 else risks).append(f"营收同比 {by_key['revenue_yoy']:+.1f}%")
        if "net_profit_yoy" in by_key:
            (positives if by_key["net_profit_yoy"] > 0 else risks).append(f"净利润同比 {by_key['net_profit_yoy']:+.1f}%")
        if "debt_ratio" in by_key and by_key["debt_ratio"] >= 65:
            risks.append(f"资产负债率 {by_key['debt_ratio']:.1f}%")
        if not metrics:
            conclusion = "财务指标暂不可用，不能据此判断基本面。"
        elif risks and not positives:
            conclusion = f"财报面偏谨慎：{'；'.join(risks)}。"
        elif positives:
            suffix = f"；需关注 {'；'.join(risks)}" if risks else ""
            conclusion = f"财报面可观察：{'；'.join(positives)}{suffix}。"
        else:
            conclusion = "已取得部分财务指标，但有效字段不足，暂不做方向判断。"
        return {
            **financial,
            "metrics": metrics,
            "positives": positives,
            "risks": risks,
            "conclusion": conclusion,
        }

    def analyze_research_stock(self, code: str) -> dict[str, Any]:
        code = str(code or "").strip()
        item = self.db.get_research_stock(code)
        if not item:
            raise LookupError("该股票尚未加入自选研究池")
        analysis = self.stock_analysis(code)
        try:
            news_rows = self.provider.get_stock_news(code, limit=8)
        except Exception:
            news_rows = []
        financial_getter = getattr(self.provider, "get_financial_metrics", None)
        try:
            financial = financial_getter(code) if callable(financial_getter) else {"code": code, "metrics": []}
        except Exception:
            financial = {"code": code, "metrics": [], "warning": "财务指标暂不可用"}
        technical = analysis.get("technical") or {}
        indicators = technical.get("indicators") or {}
        result = {
            "code": code,
            "name": analysis.get("name") or item.get("name") or code,
            "analyzed_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": analysis.get("as_of"),
            "news": self._news_research_view(news_rows),
            "technical": {
                "score": analysis.get("score", 0),
                "label": (analysis.get("signal") or {}).get("label", "中性"),
                "summary": (analysis.get("signal") or {}).get("summary", ""),
                "indicators": {
                    key: indicators.get(key)
                    for key in ("ma5", "ma10", "ma20", "rsi14", "macd_dif", "macd_dea", "macd_hist", "volume_ratio")
                    if indicators.get(key) is not None
                },
                "evidence": (technical.get("evidence") or [])[:6],
                "trigger": analysis.get("trigger") or {},
            },
            "financial": self._financial_research_view(financial),
            "disclaimer": DISCLAIMER,
        }
        return self.db.save_research_analysis(code, result)

    def market_context(self) -> dict[str, Any]:
        overview = self.provider.get_market_overview()
        indices = overview.get("indices", []) if isinstance(overview, dict) else []
        score_input: list[dict[str, Any]] = []
        try:
            score_input = self.provider.get_kline("000001", days=30, market="sh")
        except TypeError:
            score_input = self.provider.get_kline("000001", days=30)
        except Exception:
            score_input = []
        market = scoring.market_score(score_input)
        if not market.get("note"):
            evidence = market.get("evidence") or []
            market["note"] = "；".join(evidence) if evidence else "市场指标按中性处理"
        market.update(
            {
                "indices": [
                    {**item, "name": item.get("name") or item.get("display_name") or item.get("code")}
                    for item in indices
                ],
                "indexes": [
                    {**item, "name": item.get("name") or item.get("display_name") or item.get("code")}
                    for item in indices
                ],
                "rise_count": overview.get("advance_count", 0) if isinstance(overview, dict) else 0,
                "fall_count": overview.get("decline_count", 0) if isinstance(overview, dict) else 0,
                "flat_count": overview.get("flat_count", 0) if isinstance(overview, dict) else 0,
                "breadth": overview.get("breadth", {}) if isinstance(overview, dict) else {},
                "as_of": overview.get("as_of") if isinstance(overview, dict) else None,
                "data_as_of": overview.get("data_as_of") if isinstance(overview, dict) else None,
                "server_time": overview.get("server_time") if isinstance(overview, dict) else None,
                "trade_date": overview.get("trade_date") if isinstance(overview, dict) else None,
                "session": overview.get("session") if isinstance(overview, dict) else None,
                "realtime": bool(overview.get("realtime")) if isinstance(overview, dict) else False,
                "data_delayed": bool(overview.get("data_delayed")) if isinstance(overview, dict) else True,
                "source": overview.get("source", "tencent") if isinstance(overview, dict) else "unavailable",
                "stale": bool(overview.get("stale")) if isinstance(overview, dict) else True,
                "index_kline": score_input,  # raw kline for crowding/liquidity factors
            }
        )
        return market

    def board_ranking(self, board_type: str = "industry", top_n: int = 20) -> dict[str, Any]:
        """Return top-N industry or concept board ranking with full metrics.

        board_type: 'industry' (行业板块) or 'concept' (概念板块)
        Returns per-board: 涨跌幅/涨速/成交额/涨停家数/主力资金.
        """
        if board_type == "concept":
            return self.provider.get_concept_ranking(top_n=top_n)
        return self.provider.get_industry_ranking(top_n=top_n)

    @staticmethod
    def _latest_limit_up(rows: list[dict[str, Any]], code: str, name: str) -> bool:
        if len(rows) < 3:
            return False
        latest, previous, before = rows[-1], rows[-2], rows[-3]
        prev_close = _number(previous.get("close"))
        close = _number(latest.get("close"))
        before_close = _number(before.get("close"))
        prev_day_close = _number(previous.get("close"))
        if not prev_close or not before_close:
            return False
        threshold = 4.8 if "ST" in name.upper() else (19.5 if code.startswith(("300", "301")) else 9.5)
        latest_pct = (close / prev_close - 1) * 100
        previous_pct = (prev_day_close / before_close - 1) * 100
        return latest_pct >= threshold and previous_pct < threshold

    @staticmethod
    def _consecutive_limit_up_count(rows: list[dict[str, Any]], code: str, name: str) -> int:
        """Count the latest consecutive limit-up closes for display/audit."""
        if len(rows) < 2:
            return 0
        threshold = 4.8 if "ST" in name.upper() else (19.5 if code.startswith(("300", "301")) else 9.5)
        count = 0
        for index in range(len(rows) - 1, 0, -1):
            close = _number(rows[index].get("close"))
            previous = _number(rows[index - 1].get("close"))
            if not close or not previous or (close / previous - 1) * 100 < threshold:
                break
            count += 1
        return count

    @staticmethod
    def _is_one_word_limit_up(snapshot: dict[str, Any], code: str, name: str) -> bool:
        """True only when 09:25 data proves a sealed one-word limit-up."""
        if str(snapshot.get("auction_stage") or "").lower() not in {
            "opening_call_auction_final", "final", "auction_final", "集合竞价终态"
        }:
            return False
        price = _number(snapshot.get("auction_price")) or _number(snapshot.get("price"))
        previous = _number(snapshot.get("last_close")) or _number(snapshot.get("previous_close"))
        if price <= 0 or previous <= 0:
            return False
        threshold = 4.8 if "ST" in name.upper() else (19.5 if code.startswith(("300", "301")) else 9.5)
        if (price / previous - 1) * 100 < threshold:
            return False
        # A zero unmatched sell queue is the distinguishing 09:25 signal.
        sell = snapshot.get("auction_unmatched_sell_lots")
        if sell is None:
            sell = snapshot.get("unmatched_sell_lots")
        return sell is not None and _number(sell) <= 0

    @staticmethod
    def _history_covers_previous_session(rows: list[dict[str, Any]]) -> bool:
        if not rows:
            return False
        if not rows[-1].get("source"):
            return True
        now = datetime.now()
        expected = now.date() if now.hour * 60 + now.minute >= 15 * 60 + 5 else now.date() - timedelta(days=1)
        while expected.weekday() >= 5:
            expected -= timedelta(days=1)
        try:
            latest = datetime.strptime(str(rows[-1].get("date", "")).replace("-", "")[:8], "%Y%m%d").date()
        except ValueError:
            return False
        return latest >= expected

    def _load_kline_batch(
        self, snapshots: list[dict[str, Any]], days: int = 120
    ) -> dict[str, list[dict[str, Any]]]:
        results: dict[str, list[dict[str, Any]]] = {}
        codes = list(
            dict.fromkeys(
                str(item.get("code", ""))
                for item in snapshots
                if re.fullmatch(r"\d{6}", str(item.get("code", "")))
            )
        )
        batch_getter = getattr(self.provider, "get_klines", None)
        if callable(batch_getter) and codes:
            try:
                batch = batch_getter(codes, days=days)
                if isinstance(batch, dict):
                    results.update(
                        {
                            str(code): [dict(row) for row in rows if isinstance(row, dict)]
                            for code, rows in batch.items()
                            if isinstance(rows, list) and rows
                        }
                    )
            except Exception:
                pass

        missing = [code for code in codes if code not in results]
        if not missing:
            return results

        def fetch_legacy(code: str) -> list[dict[str, Any]]:
            try:
                return self.provider.get_kline(code, days=days, prefer_ffd=False)
            except TypeError:
                return self.provider.get_kline(code, days=days)

        workers = min(10, max(1, len(missing)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kline") as pool:
            futures = {
                pool.submit(fetch_legacy, code): code for code in missing
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    rows = future.result()
                    if rows:
                        results[code] = rows
                except Exception:
                    continue
        return results

    def _quote_batch(
        self,
        codes: list[str],
        *,
        auction: bool = False,
        force: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Fetch only the requested candidate codes for confirmation/review."""

        unique = list(dict.fromkeys(str(code) for code in codes if re.fullmatch(r"\d{6}", str(code))))
        if not unique:
            return {}
        auction_getter = getattr(self.provider, "get_auction_quotes", None)
        if auction and callable(auction_getter):
            try:
                try:
                    result = auction_getter(unique, force=force)
                except TypeError:
                    result = auction_getter(unique)
                if isinstance(result, dict):
                    return {str(key): dict(value) for key, value in result.items() if isinstance(value, dict)}
            except Exception:
                # Test doubles and older providers can still expose only
                # ordinary quotes; keep that compatibility path below.
                pass
        getter = getattr(self.provider, "get_quotes", None)
        if callable(getter):
            try:
                try:
                    result = getter(unique, force=force)
                except TypeError:
                    result = getter(unique)
                if isinstance(result, dict):
                    return {str(key): dict(value) for key, value in result.items() if isinstance(value, dict)}
            except Exception:
                pass
        result: dict[str, dict[str, Any]] = {}
        for code in unique:
            try:
                quote = self.provider.get_quote(code)
                if isinstance(quote, dict):
                    result[code] = dict(quote)
            except Exception:
                continue
        return result

    @staticmethod
    def _same_day_live_window(now: datetime | None = None) -> bool:
        """Whether a same-day pre-open selection is allowed to create pushes.

        The post-close TDX universe is useful for history and ranking, but it
        must never be presented as an intraday recommendation.  The only
        automatic dragon scan is therefore the short opening-auction window.
        """

        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        minute = now.hour * 60 + now.minute
        return 9 * 60 + 20 <= minute <= 9 * 60 + 29

    def _refresh_live_snapshots(
        self,
        snapshots: list[dict[str, Any]],
        *,
        auction: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Overlay current quotes on the full snapshot pool.

        This is deliberately used only by the short morning scan.  It keeps
        the historical TDX/K-line fields intact while replacing the fields
        that drive today's ranking with a fresh quote/auction value.
        """

        codes = [str(item.get("code", "")) for item in snapshots]
        # FFD's call-auction contract accepts at most 200 symbols per call.
        # Ordinary quotes use the FFD full-market/live batch; terminal auction
        # verification remains bounded to the shortlist below.
        request_codes = codes[:200] if auction else codes
        quotes = self._quote_batch(request_codes, auction=auction, force=True)
        live_fields = {
            "price", "last_close", "previous_close", "open", "high", "low",
            "change", "change_pct", "volume", "volume_lots", "amount",
            "amount_wan", "turnover", "turnover_pct", "volume_ratio",
            "amplitude_pct", "auction_price", "auction_volume_lots",
            "auction_amount", "auction_unmatched_buy_lots",
            "auction_unmatched_sell_lots", "auction_data_status",
            "auction_stage", "auction_source", "quote_time", "data_as_of",
            "trade_date", "source", "available", "stale", "bids", "asks",
        }
        today = datetime.now().strftime("%Y-%m-%d")
        updated = 0
        for item in snapshots:
            quote = quotes.get(str(item.get("code", ""))) or {}
            if not quote or quote.get("available") is False or quote.get("stale"):
                continue
            # During the auction an explicit date is mandatory.  A cached
            # prior-session row is not a valid same-day recommendation.
            if auction and str(quote.get("trade_date") or "")[:10] != today:
                continue
            for key in live_fields:
                if key in quote and quote.get(key) not in (None, ""):
                    item[key] = quote[key]
            item["live_selection"] = True
            item["auction_verified"] = bool(auction)
            updated += 1
        return snapshots, updated

    @staticmethod
    def _auction_session(now: datetime) -> dict[str, Any]:
        """Describe the Shanghai opening-auction window for live UI polling."""

        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5:
            return {"key": "closed", "label": "非交易日", "active": False, "frozen": True}
        if minute < 9 * 60 + 15:
            return {"key": "waiting", "label": "等待竞价", "active": False, "frozen": False}
        if minute < 9 * 60 + 25:
            return {"key": "auction", "label": "竞价同步中", "active": True, "frozen": False}
        if minute < 9 * 60 + 30:
            return {"key": "final", "label": "竞价结果确认", "active": True, "frozen": True}
        if minute <= 15 * 60:
            return {"key": "continuous", "label": "竞价已结束", "active": False, "frozen": True}
        return {"key": "closed", "label": "已收盘", "active": False, "frozen": True}

    def live_auction_candidates(
        self,
        run_id: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Overlay near-real-time auction quotes on one persisted candidate run.

        The candidate pool and its scores stay immutable. Only the small list of
        candidate symbols is fetched, which keeps the three-second refresh cheap
        and avoids repeatedly scanning the full market.
        """

        run = self.db.get_screen_run(run_id)
        if not run:
            raise LookupError("筛选记录不存在")
        current_time = now or datetime.now()
        session = self._auction_session(current_time)
        candidates = [
            item
            for item in run.get("candidates", [])
            if re.fullmatch(r"\d{6}", str(item.get("code", "")))
        ][:80]
        quotes = self._quote_batch([str(item.get("code")) for item in candidates], auction=True)
        overlays: list[dict[str, Any]] = []
        timestamps: list[str] = []
        sources: list[str] = []

        for candidate in candidates:
            code = str(candidate.get("code"))
            quote = dict(quotes.get(code) or {})
            snapshot = candidate.get("snapshot") if isinstance(candidate.get("snapshot"), dict) else {}
            bids = quote.get("bids") if isinstance(quote.get("bids"), list) else []
            asks = quote.get("asks") if isinstance(quote.get("asks"), list) else []
            bid1 = bids[0] if bids and isinstance(bids[0], dict) else {}
            ask1 = asks[0] if asks and isinstance(asks[0], dict) else {}
            bid2 = bids[1] if len(bids) > 1 and isinstance(bids[1], dict) else {}
            ask2 = asks[1] if len(asks) > 1 and isinstance(asks[1], dict) else {}

            previous_close = (
                _number(quote.get("last_close"))
                or _number(snapshot.get("last_close"))
                or _number(snapshot.get("previous_close"))
                or _number(candidate.get("previous_close"))
            )
            bid_price = _number(bid1.get("price"))
            ask_price = _number(ask1.get("price"))
            virtual_price = bid_price if bid_price > 0 and abs(bid_price - ask_price) < 0.0001 else 0.0
            in_auction = session["key"] in {"auction", "final"}
            if session["key"] in {"waiting", "closed"}:
                auction_price = 0.0
            elif session["key"] == "auction":
                auction_price = _number(quote.get("auction_price")) or virtual_price or _number(quote.get("price"))
            else:
                auction_price = _number(quote.get("open")) or virtual_price or _number(quote.get("price"))

            bid1_volume = _number(bid1.get("volume"))
            ask1_volume = _number(ask1.get("volume"))
            auction_source = str(quote.get("auction_source") or quote.get("source") or "")
            matched_volume_lots = _number(quote.get("auction_volume_lots"))
            if matched_volume_lots <= 0 and (in_auction or "auction_volume_lots" not in quote):
                matched_volume_lots = min(bid1_volume, ask1_volume) if bid1_volume > 0 and ask1_volume > 0 else 0.0
            bid_unmatched_lots = _number(quote.get("auction_unmatched_buy_lots"))
            ask_unmatched_lots = _number(quote.get("auction_unmatched_sell_lots"))
            if in_auction or "auction_unmatched_buy_lots" not in quote:
                bid_unmatched_lots = bid_unmatched_lots or _number(bid2.get("volume"))
            if in_auction or "auction_unmatched_sell_lots" not in quote:
                ask_unmatched_lots = ask_unmatched_lots or _number(ask2.get("volume"))
            if auction_price <= 0:
                matched_volume_lots = 0.0
                bid_unmatched_lots = 0.0
                ask_unmatched_lots = 0.0
            unmatched_lots = bid_unmatched_lots - ask_unmatched_lots
            if unmatched_lots > 0:
                unmatched_direction = "买方"
            elif unmatched_lots < 0:
                unmatched_direction = "卖方"
            else:
                unmatched_direction = "平衡"

            feed_amount = _number(quote.get("amount_wan")) * 10_000
            has_verified_auction_volume = "auction_volume_lots" in quote
            estimated_matched_amount = (
                auction_price * matched_volume_lots * 100
                if has_verified_auction_volume and auction_price > 0
                else 0.0
            )
            direct_auction_amount = (
                _number(quote.get("auction_amount"))
                if auction_source == "ffd_market_microstructure"
                else 0.0
            )
            if auction_source == "eastmoney_clist":
                # A public list snapshot cannot prove opening-auction match
                # volume; keep price visible but leave match metrics empty.
                matched_volume_lots = 0.0
                estimated_matched_amount = 0.0
            auction_amount = direct_auction_amount or estimated_matched_amount
            if auction_amount <= 0 and not in_auction and auction_price > 0:
                auction_amount = feed_amount
            elif auction_amount <= 0 and "auction_volume_lots" not in quote and auction_price > 0:
                auction_amount = feed_amount
            gap_pct = (
                (auction_price / previous_close - 1) * 100
                if auction_price > 0 and previous_close > 0
                else None
            )
            data_as_of = str(quote.get("data_as_of") or quote.get("quote_time") or "")
            source = str(quote.get("source") or "tencent_quote")
            if data_as_of:
                timestamps.append(data_as_of)
            if source:
                sources.append(source)
            quote_trade_date = str(quote.get("trade_date") or "")[:10]
            current_trade_date = current_time.strftime("%Y-%m-%d")
            # During the active auction, a quote without an explicit trading
            # date is not safe to display: it may be a cached prior session.
            fresh_for_session = not in_auction or quote_trade_date == current_trade_date
            available = bool(quote.get("available", bool(quote))) and auction_price > 0 and fresh_for_session and not bool(quote.get("stale"))
            overlays.append(
                {
                    "code": code,
                    "name": quote.get("name") or candidate.get("name"),
                    "auction_price": auction_price or None,
                    "previous_close": previous_close or None,
                    "gap_pct": gap_pct,
                    "change_pct": quote.get("change_pct"),
                    "auction_amount": auction_amount or None,
                    "matched_volume_lots": matched_volume_lots or None,
                    "matched_amount": direct_auction_amount or estimated_matched_amount or None,
                    "unmatched_direction": unmatched_direction,
                    "unmatched_volume_lots": abs(unmatched_lots) or None,
                    "current_price": _number(quote.get("price")) or None,
                    "quote_time": quote.get("quote_time") or data_as_of,
                    "data_as_of": data_as_of,
                    "source": source,
                    "available": available,
                    "stale": bool(quote.get("stale")),
                    "live_status": session["label"] if available else ("竞价数据过期或不可用" if in_auction else "等待有效报价"),
                }
            )

        return {
            "run_id": run_id,
            "run_type": run.get("run_type"),
            "session": session["key"],
            "session_label": session["label"],
            "active": session["active"],
            "frozen": session["frozen"],
            "interval_ms": 3_000,
            "updated_at": max(timestamps) if timestamps else current_time.isoformat(timespec="seconds"),
            "source": sources[0] if len(set(sources)) == 1 and sources else ("mixed" if sources else "tencent_quote"),
            "sources": sorted(set(sources)),
            "candidate_count": len(overlays),
            "available_count": sum(1 for item in overlays if item.get("available")),
            "fresh_count": sum(1 for item in overlays if item.get("available") and not item.get("stale")),
            "candidates": overlays,
            "note": "竞价数据优先 FFD 09:25 终态，其次腾讯虚拟撮合价，最后东方财富公开快照；失败不会回退到旧竞价快照。交易决策请以持牌行情终端为准。",
        }

    @staticmethod
    def _board_name(snapshot: dict[str, Any]) -> str:
        return str(
            snapshot.get("concept")
            or snapshot.get("theme")
            or snapshot.get("industry")
            or snapshot.get("sector")
            or snapshot.get("board")
            or ""
        ).strip()

    def _board_rotation_from_snapshots(
        self,
        snapshots: list[dict[str, Any]],
        kline_map: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Aggregate candidate K-lines into a deterministic ten-day board view."""

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for snapshot in snapshots:
            name = self._board_name(snapshot)
            if name:
                grouped[name].append(snapshot)
        board_rows: list[dict[str, Any]] = []
        for name, members in grouped.items():
            changes = [_number(item.get("change_pct")) for item in members if item.get("change_pct") not in (None, "")]
            up_count = sum(1 for value in changes if value > 0)
            down_count = sum(1 for value in changes if value < 0)
            returns: list[float] = []
            for member in members:
                rows = kline_map.get(str(member.get("code", "")), [])
                closes = [_number(row.get("close")) for row in rows if _number(row.get("close")) > 0]
                if len(closes) >= 11 and closes[-11]:
                    returns.append((closes[-1] / closes[-11] - 1.0) * 100.0)
            board_rows.append(
                {
                    "name": name,
                    "change_pct": sum(changes) / len(changes) if changes else None,
                    "returns_10d": sum(returns) / len(returns) if returns else None,
                    "up_count": up_count,
                    "down_count": down_count,
                    "member_count": len(members),
                }
            )
        rotation = scoring.board_rotation_score(board_rows, lookback_days=10)
        rotation["coverage"] = {"boards": len(board_rows), "stocks": sum(len(items) for items in grouped.values()), "kline_stocks": sum(1 for item in snapshots if str(item.get("code", "")) in kline_map)}
        return rotation

    def board_rotation(self, board_type: str = "industry", top_n: int = 20) -> dict[str, Any]:
        """Return the current board ranking with the ten-day state contract."""

        ranking = self.board_ranking(board_type=board_type, top_n=max(20, top_n))
        rows = ranking.get("rows") or ranking.get("top") or []
        rotation = scoring.board_rotation_score(rows, lookback_days=10)
        rotation["source"] = ranking.get("source", "eastmoney")
        rotation["stale"] = bool(ranking.get("stale"))
        return {**rotation, "top": rotation.get("top", [])[: max(1, min(int(top_n), 100))]}

    @staticmethod
    def _news_topic_tags(text: str) -> list[str]:
        rules = {
            "\u4eba\u5de5\u667a\u80fd": ("\u4eba\u5de5\u667a\u80fd", "AI", "\u5927\u6a21\u578b", "\u7b97\u529b"),
            "\u82af\u7247\u534a\u5bfc\u4f53": ("\u82af\u7247", "\u534a\u5bfc\u4f53", "\u96c6\u6210\u7535\u8def"),
            "\u673a\u5668\u4eba": ("\u673a\u5668\u4eba", "\u4eba\u5f62\u673a\u5668\u4eba"),
            "\u65b0\u80fd\u6e90\u8f66": ("\u65b0\u80fd\u6e90\u8f66", "\u7535\u52a8\u6c7d\u8f66", "\u6c7d\u8f66"),
            "\u9502\u7535\u6c60": ("\u9502\u7535\u6c60", "\u56fa\u6001\u7535\u6c60", "\u50a8\u80fd"),
            "\u4f4e\u7a7a\u7ecf\u6d4e": ("\u4f4e\u7a7a", "\u98de\u884c\u6c7d\u8f66", "\u65e0\u4eba\u673a"),
            "\u534e\u4e3a\u4ea7\u4e1a\u94fe": ("\u534e\u4e3a", "\u9e3f\u8499", "\u6607\u817e"),
            "\u519b\u5de5": ("\u519b\u5de5", "\u56fd\u9632", "\u536b\u661f"),
            "\u9ec4\u91d1": ("\u9ec4\u91d1", "\u8d35\u91d1\u5c5e"),
            "\u77f3\u6cb9\u5929\u7136\u6c14": ("\u539f\u6cb9", "\u77f3\u6cb9", "\u5929\u7136\u6c14"),
            "\u6d77\u5916\u5730\u7f18\u98ce\u9669": ("\u4f0a\u6717", "\u6218\u4e89", "\u51b2\u7a81", "\u88ad\u51fb"),
            "\u91d1\u878d": ("\u964d\u606f", "\u8d27\u5e01\u653f\u7b56", "\u94f6\u884c", "\u5238\u5546"),
        }
        return [tag for tag, terms in rules.items() if any(term.lower() in text for term in terms)]

    def board_rotation_matrix(self, board_type: str = "industry", days: int = 10, top_n: int = 10) -> dict[str, Any]:
        return self.provider.get_board_rotation_matrix(board_type=board_type, days=days, top_n=top_n)

    def market_news(self, limit: int = 30) -> dict[str, Any]:
        """Enrich news with transparent, non-trading topic/board/stock tags."""

        rows = self.provider.get_market_news(limit=limit)
        try:
            industry = self.board_rotation("industry", top_n=100)
            concept = self.board_rotation("concept", top_n=100)
            board_rows = [
                *[dict(item, board_type="industry") for item in industry.get("top", [])],
                *[dict(item, board_type="concept") for item in concept.get("top", [])],
            ]
        except Exception:
            board_rows = []
        try:
            universe = self.provider.get_market_universe(limit=None)
        except Exception:
            universe = []
        for row in rows:
            text = f"{row.get('title', '')} {row.get('content', '')}".lower()
            provider_topics = [
                str(value)
                for value in [row.get("sector"), *(row.get("sector_tags") or [])]
                if str(value or "").strip()
            ]
            topics = list(dict.fromkeys([*provider_topics, *self._news_topic_tags(text)]))
            board_hints = {
                "\u6d77\u5916\u5730\u7f18\u98ce\u9669": ("\u77f3\u6cb9", "\u6cb9\u6c14", "\u9ec4\u91d1", "\u8d35\u91d1\u5c5e", "\u519b\u5de5"),
                "\u77f3\u6cb9\u5929\u7136\u6c14": ("\u77f3\u6cb9", "\u6cb9\u6c14", "\u5929\u7136\u6c14"),
                "\u82af\u7247\u534a\u5bfc\u4f53": ("\u82af\u7247", "\u534a\u5bfc\u4f53", "\u96c6\u6210\u7535\u8def"),
                "\u65b0\u80fd\u6e90\u8f66": ("\u65b0\u80fd\u6e90\u8f66", "\u6c7d\u8f66"),
                "\u9502\u7535\u6c60": ("\u7535\u6c60", "\u50a8\u80fd"),
            }
            hints = tuple(hint for tag in topics for hint in board_hints.get(tag, (tag,)))
            matched_boards = [
                item for item in board_rows
                if item.get("name") and (str(item["name"]).lower() in text or any(hint.lower() in str(item["name"]).lower() for hint in hints))
            ][:3]
            mentioned_stocks = [
                {"code": str(item.get("code", "")), "name": str(item.get("name", "")), "reason": "\u6b63\u6587\u63d0\u53ca"}
                for item in universe
                if len(str(item.get("name", ""))) >= 3 and str(item.get("name", "")).lower() in text
            ][:3]
            row["event_tags"] = topics
            row["board_tags"] = [
                {"name": item["name"], "type": item["board_type"], "stage": item.get("stage", "\u8f6e\u52a8\u89c2\u5bdf"), "reason": "\u65b0\u95fb\u5173\u952e\u8bcd\u4e0e\u677f\u5757\u540d\u79f0\u5339\u914d"}
                for item in matched_boards
            ]
            row["stock_tags"] = mentioned_stocks
            row["mapping_notice"] = "\u7ebf\u7d22\u6620\u5c04\uff0c\u4ec5\u7528\u4e8e\u540e\u7eed\u6838\u9a8c\uff0c\u4e0d\u6784\u6210\u63a8\u8350"
        return {
            "rows": rows,
            "source": next((str(row.get("source")) for row in rows if row.get("source")), "unknown"),
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "count": len(rows),
        }

    def _overnight_news_evidence(self, boards: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Collect explicit, timestamped news evidence for each board.

        Rotation is only the discovery layer.  A board without a matching
        news/event record must not advance into the overnight focus list.
        """
        evidence: dict[str, list[dict[str, Any]]] = {str(item.get("name") or ""): [] for item in boards}
        try:
            news = self.provider.get_market_news(limit=50)
        except Exception:
            return evidence
        for item in news:
            text = f"{item.get('title', '')} {item.get('content', '')}".lower()
            tags = set(self._news_topic_tags(text))
            for board in evidence:
                if not board:
                    continue
                direct = board.lower() in text
                topic_match = (
                    ("黄金" in board and "黄金" in tags)
                    or (any(token in board for token in ("芯片", "半导体", "集成电路")) and "芯片半导体" in tags)
                    or ("人工智能" in board and "人工智能" in tags)
                    or ("机器人" in board and "机器人" in tags)
                    or ("低空" in board and "低空经济" in tags)
                    or (any(token in board for token in ("石油", "油气", "天然气")) and "石油天然气" in tags)
                )
                if direct or topic_match:
                    evidence[board].append({
                        "title": str(item.get("title") or ""),
                        "source": str(item.get("source") or "ffd_market_news"),
                        "published_at": str(item.get("published_at") or item.get("time") or ""),
                        "url": str(item.get("url") or ""),
                        "tags": sorted(tags),
                    })
        return evidence

    @staticmethod
    def _overnight_close_technical(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Post-close technical gate for the first overnight round.

        Uses only completed local TDX daily bars: trend, MACD/KDJ momentum,
        volume/price confirmation, breakout and drawdown risk.  It deliberately
        does not use the next session's quote or auction data.
        """
        if len(rows) < 60:
            return {"eligible": False, "score": 0, "signals": [], "vetoes": ["日线不足60根"]}
        indicators = scoring.standard_indicators(rows)
        series = indicators.get("series") or {}
        latest = indicators.get("latest") or {}
        closes = [_number(row.get("close")) for row in rows]
        volumes = [_number(row.get("volume")) for row in rows]
        highs = [_number(row.get("high")) for row in rows]
        lows = [_number(row.get("low")) for row in rows]
        index = len(closes) - 1
        ma5, ma10, ma20 = (_number(latest.get(key)) for key in ("ma5", "ma10", "ma20"))
        dif, dea = (_number(latest.get(key)) for key in ("macd_dif", "macd_dea"))
        k, d = (_number(latest.get(key)) for key in ("kdj_k", "kdj_d"))
        previous_dif = _number((series.get("macd_dif") or [0])[index - 1])
        previous_dea = _number((series.get("macd_dea") or [0])[index - 1])
        previous_k = _number((series.get("kdj_k") or [0])[index - 1])
        previous_d = _number((series.get("kdj_d") or [0])[index - 1])
        close = closes[-1]
        prev_close = closes[-2]
        volume_ratio = volumes[-1] / max(1, sum(volumes[-6:-1]) / 5)
        close_position = (close - lows[-1]) / max(0.0001, highs[-1] - lows[-1])
        trend = ma5 > ma10 > ma20 and close >= ma10
        macd_turn = dif > dea and (previous_dif <= previous_dea or dif > previous_dif)
        kdj_turn = k > d and (previous_k <= previous_d or k < 80)
        volume_breakout = volume_ratio >= 1.2 and close > prev_close and close_position >= 0.6
        platform_breakout = close >= max(closes[-21:-1]) and volume_ratio >= 1.1
        max_drawdown = min((closes[i] / closes[i - 1] - 1) * 100 for i in range(max(1, index - 9), index + 1))
        upper_shadow = (highs[-1] - max(close, _number(rows[-1].get("open")))) / max(0.0001, highs[-1] - lows[-1])
        vetoes = []
        if close < ma20:
            vetoes.append("收盘跌破MA20")
        if max_drawdown <= -7:
            vetoes.append("近10日出现单日大幅回撤")
        if upper_shadow >= 0.55 and close_position < 0.55:
            vetoes.append("高位长上影，收盘质量差")
        signals = []
        for passed, label in ((trend, "MA5>MA10>MA20多头"), (macd_turn, "MACD转强"), (kdj_turn, "KDJ转强"), (volume_breakout, "放量上攻"), (platform_breakout, "突破近20日平台")):
            if passed:
                signals.append(label)
        score = min(100, 28 * trend + 20 * macd_turn + 16 * kdj_turn + 20 * volume_breakout + 16 * platform_breakout)
        return {
            "eligible": bool(trend and sum((macd_turn, kdj_turn, volume_breakout, platform_breakout)) >= 2 and not vetoes),
            "score": score,
            "signals": signals,
            "vetoes": vetoes,
            "volume_ratio_5": round(volume_ratio, 3),
            "close_position": round(close_position, 3),
            "data_date": rows[-1].get("date"),
        }

    @staticmethod
    def _screen_mode(mode: str | None) -> tuple[str, str]:
        """Return the legacy API name and the rulebook mode used for scoring."""

        requested = str(mode or "technical").strip().lower()
        aliases = {
            "technical": ("technical", "balanced"),
            "rulebook": ("rulebook", "balanced"),
            "balanced": ("balanced", "balanced"),
            "auction": ("auction", "event"),
            "event": ("event", "event"),
            "value": ("value", "value"),
            "growth": ("growth", "growth"),
            "trend": ("trend", "trend"),
            "dragon": ("dragon", "dragon"),
            "overnight": ("overnight", "dragon"),
            "擒龙": ("dragon", "dragon"),
        }
        return aliases.get(requested, ("technical", "balanced"))

    @staticmethod
    def _is_main_board_stock(code: Any) -> bool:
        """沪深主板范围：排除创业板、科创板及北交所。"""
        return scoring.is_main_board_security(code)

    @staticmethod
    def _overnight_broad_board(industry: Any) -> str:
        """Map local TDX fine industries into stable, readable macro boards."""
        name = str(industry or "").strip()
        groups = (
            ("科技", ("软件", "通信", "电子", "半导体", "芯片", "计算机", "互联网", "传媒", "元器件", "IT")),
            ("医药健康", ("医药", "医疗", "生物", "保健", "中药")),
            ("大消费", ("食品", "饮料", "白酒", "乳", "酒店", "旅游", "零售", "服装", "家电", "教育", "广告", "造纸", "家具")),
            ("资源能源", ("煤", "石油", "天然气", "有色", "黄金", "铜", "钢铁", "化工", "玻璃", "水泥", "电力", "新能源", "电池", "光伏")),
            ("高端制造", ("机械", "设备", "自动化", "机器人", "汽车", "零部件", "仪器", "军工", "航空", "船舶", "纺织")),
            ("金融地产", ("银行", "保险", "证券", "多元金融", "房地产")),
            ("交通公用", ("运输", "物流", "港口", "航运", "机场", "公路", "水运", "燃气", "公用")),
            ("基建材料", ("建筑", "工程", "装饰", "建材", "园林")),
        )
        for label, keywords in groups:
            if any(token.lower() in name.lower() for token in keywords):
                return label
        return "其他行业"

    @staticmethod
    def _overnight_taxonomy_industry(industry: Any) -> str | None:
        local = str(industry or "").strip()
        for _, children in OVERNIGHT_INDUSTRY_TAXONOMY.items():
            for child in children:
                terms = OVERNIGHT_INDUSTRY_ALIASES.get(child, (child,))
                if any(term.lower() in local.lower() for term in terms):
                    return child
        return None

    @staticmethod
    def _candidate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Fill quote aliases used by the rulebook without changing provider data."""

        result = dict(snapshot)
        if result.get("price") in (None, "") and result.get("open") not in (None, ""):
            result["price"] = result.get("open")
        if result.get("market_cap") in (None, "") and result.get("mcap") not in (None, ""):
            result["market_cap"] = result.get("mcap")
        if result.get("turnover_pct") in (None, "") and result.get("turnover") not in (None, ""):
            result["turnover_pct"] = result.get("turnover")
        return result

    def run_screener(self, mode: str = "technical", limit: int | None = None) -> dict[str, Any]:
        """Run a full-universe snapshot pass and bounded K-line confirmation.

        ``limit`` is the expensive K-line confirmation budget (the API returns
        at most 40 ranked rows); it is not the stock pool.  Every eligible snapshot is pre-scored,
        so a high-ranked stock is not lost merely because it appears late in
        the provider response.
        """

        settings = self.settings()
        run_mode, rule_mode = self._screen_mode(mode)
        limit = max(12, min(int(limit or settings.get("scan_limit", 80)), 240))
        raw_universe = self.provider.get_market_universe(limit=None)
        raw_rows, raw_total, universe_source = _normalize_rows(raw_universe)
        snapshots, filter_stats = filter_stock_universe(raw_rows)
        # A dragon recommendation is only allowed to use same-day quotes in
        # the opening window.  Outside that window the local universe is a
        # post-close historical baseline and can still support observation,
        # but it must not create a fresh push.
        live_selection_window = self._same_day_live_window()
        live_snapshot_count = 0
        live_auction_count = 0
        if rule_mode == "dragon" and live_selection_window:
            # Use the FFD quote layer to rank the whole market. Its 09:25
            # terminal asset is requested only for the bounded shortlist.
            snapshots, live_snapshot_count = self._refresh_live_snapshots(
                snapshots, auction=False
            )
        # A fake/offline provider may not expose the shared filter helper's
        # counters; derive the authoritative count from the returned rows.
        provider_stats = getattr(self.provider, "get_market_universe_stats", None)
        if callable(provider_stats):
            try:
                provider_stats_value = provider_stats() or {}
                for key in ("input", "star_excluded", "non_stock_excluded"):
                    if int(provider_stats_value.get(key, 0)) > int(filter_stats.get(key, 0)):
                        filter_stats[key] = int(provider_stats_value[key])
            except Exception:
                pass
        snapshots = [
            row
            for row in snapshots
            if re.fullmatch(r"\d{6}", str(row.get("code", "")))
            and not scoring.is_star_security(row.get("code"), row.get("name"))
        ]
        st_excluded = sum(1 for row in snapshots if "ST" in str(row.get("name", "")).upper())
        delisted_excluded = sum(1 for row in snapshots if "退" in str(row.get("name", "")))
        snapshots = [
            row
            for row in snapshots
            if "ST" not in str(row.get("name", "")).upper()
            and "退" not in str(row.get("name", ""))
        ]
        before_main_board_filter = len(snapshots)
        snapshots = [
            row for row in snapshots
            if scoring.is_main_board_security(row.get("code"), row.get("name"))
        ]
        excluded_non_main_board = before_main_board_filter - len(snapshots)
        universe_total = len(snapshots)
        market = self.market_context()
        overnight_board_plan: dict[str, Any] = {}
        overnight_memberships: dict[str, list[str]] = {}
        if run_mode == "overnight":
            try:
                rotation = self.provider.get_board_rotation_matrix("all", days=10, top_n=10)
                leaders = list(rotation.get("leaders") or [])
                def board_state(item: dict[str, Any]) -> str:
                    total = _number(item.get("return_10d"))
                    high = _number(item.get("max_daily_pct"))
                    low = _number(item.get("min_daily_pct"))
                    if total >= 10 and high >= 4:
                        return "确认/扩散"
                    if total >= 4 and low > -3:
                        return "首次启动"
                    if total > 0 and low <= -3:
                        return "分歧/回流观察"
                    return "潜伏观察"

                observation = []
                for item in leaders[:10]:
                    row = dict(item)
                    row["state"] = board_state(row)
                    row["selection_reason"] = "10日轮动强度进入观察范围"
                    observation.append(row)
                for row in observation:
                    row["selection_reason"] = "板块轮动旁路参考，不参与第一轮技术选股门槛"
                review = observation[:7]
                focus = observation[:5]
                memberships_getter = getattr(self.provider, "get_stock_board_memberships", None)
                if callable(memberships_getter):
                    overnight_memberships = memberships_getter() or {}
                overnight_board_plan = {
                    "rotation_source": rotation.get("source"),
                    "observation_boards": observation,
                    "review_boards": review,
                    "focus_boards": focus,
                    "laggard_boards": list(rotation.get("laggards") or [])[:3],
                    "news_gate": "已关闭：第一轮仅按收盘技术面选股；新闻只用于第二轮人工复核",
                    "rotation_available": bool(rotation.get("available")),
                }
            except Exception:
                overnight_board_plan = {"rotation_available": False}
        changes = [
            _number(row.get("change_pct"))
            for row in snapshots
            if row.get("change_pct") not in (None, "")
        ]
        if changes:
            advance = sum(1 for value in changes if value > 0)
            decline = sum(1 for value in changes if value < 0)
            flat = len(changes) - advance - decline
            breadth_ratio = advance / max(1, advance + decline)
            provider_breadth = market.get("breadth") or {}
            market["breadth"] = {
                "advance": advance,
                "decline": decline,
                "flat": flat,
                "ratio": round(breadth_ratio, 4),
                "source": "live_universe_filtered",
                "limit_up_count": provider_breadth.get("limit_up_count"),
                "limit_down_count": provider_breadth.get("limit_down_count"),
                "median_change_pct": provider_breadth.get("median_change_pct"),
                "ffd_reference": (
                    provider_breadth
                    if provider_breadth.get("source") == "ffd_market_breadth"
                    else None
                ),
                "scope": "沪深主板",
            }
            market["emotion_phase"] = (
                "退潮" if breadth_ratio < 0.30 else
                "上升/主升" if breadth_ratio >= 0.65 else
                "复苏" if breadth_ratio >= 0.50 else "迷茫/观察"
            )

        # Cheap snapshot scoring covers the whole pool.  Only the best rows and
        # obvious event movers consume K-line requests, keeping a 5k+ universe
        # practical while retaining full-market selection semantics.
        quick: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_snapshot in snapshots:
            snapshot = self._candidate_snapshot(raw_snapshot)
            if run_mode == "overnight":
                technical = self._overnight_close_technical(snapshot.get("postclose_kline") or [])
                if not technical.get("eligible"):
                    continue
                bars = snapshot.get("postclose_kline") or []
                closes = [_number(item.get("close")) for item in bars]
                technical["return_5d"] = round((closes[-1] / closes[-6] - 1) * 100, 4) if len(closes) >= 6 and closes[-6] else None
                snapshot["overnight_technical_pre"] = technical
            try:
                result = scoring.rulebook_score(snapshot, [], market, rule_mode)
            except Exception as exc:
                result = {
                    "score": 0,
                    "label": "数据异常",
                    "selected_mode": rule_mode,
                    "eligible": False,
                    "push_eligible": False,
                    "evidence": [f"快照评分失败：{type(exc).__name__}"],
                    "risk": {"hard_veto": True, "hard_flags": ["快照评分失败"], "soft_flags": []},
                    "breakdown": {},
                    "data_coverage": {"kline_points": 0},
                }
            if result.get("eligible", False) or run_mode == "overnight":
                quick.append((snapshot, result))
        if run_mode == "overnight":
            quick.sort(key=lambda pair: (_number((pair[0].get("overnight_technical_pre") or {}).get("score")), _number(pair[0].get("amount"))), reverse=True)
        elif rule_mode == "dragon":
            quick.sort(key=lambda pair: (_number(pair[0].get("change_pct")), _number(pair[0].get("amount"))), reverse=True)
        else:
            quick.sort(key=lambda pair: (_number(pair[1].get("score")), _number(pair[0].get("amount"))), reverse=True)
        kline_budget = min(len(quick), max(limit, min(240, limit * 2)))
        selected_pairs = quick[:kline_budget]
        if rule_mode == "dragon" and live_selection_window and selected_pairs:
            selected_snapshots_for_auction = [pair[0] for pair in selected_pairs]
            _, live_auction_count = self._refresh_live_snapshots(
                selected_snapshots_for_auction, auction=True
            )
        if run_mode == "overnight":
            # Build every eligible industry group locally, then retain the top
            # five technical stocks per group.  This is intentionally not a
            # global top-score list: each displayed board must have a usable
            # internal strength ranking.
            by_board: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
            board_market_returns: dict[str, list[float]] = defaultdict(list)
            for item in snapshots:
                bars = item.get("postclose_kline") or []
                closes = [_number(bar.get("close")) for bar in bars]
                if len(closes) >= 6 and closes[-6]:
                    board_market_returns[str(item.get("industry") or "未分类").strip()].append((closes[-1] / closes[-6] - 1) * 100)
            for pair in quick:
                board = str(pair[0].get("industry") or "未分类").strip()
                by_board[board].append(pair)
            # Replace the local TDX fine-industry grouping with the user's
            # fixed taxonomy.  Pre-create every child so empty industries are
            # still represented in the workbench.
            parent_by_child = {child: parent for parent, children in OVERNIGHT_INDUSTRY_TAXONOMY.items() for child in children}
            by_board = {child: [] for child in parent_by_child}
            board_market_returns = defaultdict(list)
            for item in snapshots:
                board = self._overnight_taxonomy_industry(item.get("industry"))
                bars = item.get("postclose_kline") or []
                closes = [_number(bar.get("close")) for bar in bars]
                if board and len(closes) >= 6 and closes[-6]:
                    board_market_returns[board].append((closes[-1] / closes[-6] - 1) * 100)
            for pair in quick:
                board = self._overnight_taxonomy_industry(pair[0].get("industry"))
                if board:
                    by_board[board].append(pair)
            selected_pairs = []
            grouped_summary = []
            for board, members in by_board.items():
                members.sort(key=lambda pair: _number((pair[0].get("overnight_technical_pre") or {}).get("score")), reverse=True)
                returns = board_market_returns.get(board) or []
                board_return_5d = sum(returns) / len(returns) if returns else 0.0
                top_members = members[:5]
                for rank, (item, result) in enumerate(top_members, start=1):
                    item["overnight_board_rank"] = rank
                    item["overnight_board_5d_return"] = round(board_return_5d, 4)
                    item["overnight_board_name"] = board
                    selected_pairs.append((item, result))
                grouped_summary.append({"name": board, "parent": parent_by_child[board], "return_5d": round(board_return_5d, 4), "candidate_count": len(top_members)})
            grouped_summary.sort(key=lambda item: item["return_5d"], reverse=True)
            overnight_board_plan["technical_board_groups"] = grouped_summary
            overnight_board_plan["mapped_stock_count"] = len(selected_pairs)
            overnight_board_plan["focus_membership_source"] = "full_market_postclose_technical_by_industry"
        # Event mode gets a small deterministic allowance for strong snapshot
        # movers even when their other quote factors rank below the top slice.
        if rule_mode == "event":
            movers = [
                pair
                for pair in quick[kline_budget:]
                if _number(pair[0].get("change_pct")) >= 8.0
            ][: max(0, min(40, limit // 2))]
            selected_pairs.extend(movers)
        selected_snapshots = [pair[0] for pair in selected_pairs]
        kline_map = (
            {str(item.get("code")): list(item.get("postclose_kline") or []) for item in selected_snapshots}
            if run_mode == "overnight"
            else self._load_kline_batch(selected_snapshots)
        )
        # --- Six-Dimensional Timing: inject previous breadth for velocity calculation ---
        prev_run = self.db.get_latest_screen_run(run_type="dragon") if rule_mode == "dragon" else None
        if prev_run and isinstance(prev_run.get("metadata"), dict):
            prev_breadth = (prev_run.get("metadata") or {}).get("breadth_ratio")
            # Runs made before breadth was persisted still retain the emotion
            # snapshot in each candidate.  Reuse that point-in-time value so
            # the first upgraded scan does not lose the velocity factor.
            if prev_breadth is None:
                for candidate in prev_run.get("candidates") or []:
                    emotion = (((candidate.get("snapshot") or {}).get("rulebook") or {}).get("emotion") or {})
                    prev_breadth = emotion.get("breadth_ratio")
                    if prev_breadth is not None:
                        break
            if prev_breadth is not None:
                market["prev_breadth_ratio"] = prev_breadth
        # --- Six-Dimensional Timing: optional external context. ---
        if rule_mode == "dragon":
            market.update(self._load_dragon_external_context())
        dragon_phase = scoring.dragon_market_phase(market, snapshots) if rule_mode == "dragon" else None
        dragon_boards = scoring.dragon_board_strength(snapshots) if rule_mode == "dragon" else {}
        dragon_ranks: dict[str, tuple[int, int]] = {}
        if rule_mode == "dragon":
            board_members: dict[str, list[tuple[str, float, float]]] = {}
            for item in selected_snapshots:
                board_name = str(item.get("concept") or item.get("theme") or item.get("industry") or item.get("sector") or item.get("board") or "")
                rows = kline_map.get(str(item.get("code", "")), [])
                closes = [_number(row.get("close")) for row in rows if _number(row.get("close")) > 0]
                interval_gain = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else _number(item.get("change_60d_pct"))
                board_members.setdefault(board_name, []).append((str(item.get("code", "")), interval_gain, _number(item.get("change_pct"))))
            for members in board_members.values():
                members.sort(key=lambda value: (value[1], value[2]), reverse=True)
                for rank, (member_code, _, _) in enumerate(members, start=1):
                    dragon_ranks[member_code] = (rank, len(members))

        candidates: list[dict[str, Any]] = []
        first_board_count = 0
        threshold = _number(settings.get("rulebook_threshold"), 62.0)
        push_threshold = max(threshold, _number(settings.get("rulebook_push_threshold"), 72.0))
        for snapshot, quick_result in selected_pairs:
            code = str(snapshot.get("code", ""))
            rows = kline_map.get(code, [])
            # Keep the raw, latest technical readings with every candidate.
            # Dragon scoring uses a different factor breakdown, but the table
            # still needs inspectable MA/KDJ/MACD values rather than blanks.
            technical_latest = scoring.standard_indicators(rows).get("latest", {})
            overnight_technical = self._overnight_close_technical(rows) if run_mode == "overnight" else None
            if run_mode == "overnight" and not overnight_technical.get("eligible"):
                continue
            if rule_mode == "dragon":
                board_name = str(snapshot.get("concept") or snapshot.get("theme") or snapshot.get("industry") or snapshot.get("sector") or snapshot.get("board") or "")
                peers = [item for item in snapshots if str(item.get("industry") or item.get("sector") or item.get("board") or "") == board_name]
                peers.sort(key=lambda item: (_number(item.get("change_pct")), _number(item.get("amount"))), reverse=True)
                leader_rank, leader_count = dragon_ranks.get(code, (99, len(peers)))
                full_result = scoring.dragon_score(snapshot, rows, dragon_phase, dragon_boards.get(board_name), leader_rank=leader_rank, leader_count=leader_count)
            else:
                full_result = scoring.rulebook_score(snapshot, rows, market, rule_mode)
            if not full_result.get("eligible", False) and rule_mode != "dragon":
                continue
            history_current = self._history_covers_previous_session(rows)
            is_first_board = history_current and self._latest_limit_up(rows, code, str(snapshot.get("name", "")))
            event_snapshot = dict(snapshot)
            consecutive_boards = self._consecutive_limit_up_count(rows, code, str(snapshot.get("name", "")))
            one_word_limit_up = (
                rule_mode == "dragon"
                and live_selection_window
                and self._is_one_word_limit_up(snapshot, code, str(snapshot.get("name", "")))
            )
            if rows:
                previous_close = _number(snapshot.get("last_close")) or _number(snapshot.get("previous_close")) or _number(rows[-1].get("close"))
                # Prefer the verified same-day auction price.  Falling back to
                # the local bar's open here would re-introduce yesterday's
                # post-close value after the FFD/Tencent overlay.
                live_open = _number(snapshot.get("auction_price")) or _number(snapshot.get("open"), previous_close)
                event_auction_amount = _number(snapshot.get("auction_amount")) or _number(snapshot.get("amount"))
                event_snapshot.update(
                    {
                        "gap_pct": ((live_open / previous_close - 1) * 100) if previous_close else 0,
                        "auction_amount": event_auction_amount,
                        "prev_close": previous_close,
                        "auction_price": live_open,
                        "market_cap": _number(snapshot.get("mcap")) or _number(snapshot.get("market_cap")),
                        "is_first_board": is_first_board,
                        "board_count": 1 if is_first_board else None,
                        "history_previous_session_confirmed": history_current,
                        "consecutive_boards": consecutive_boards,
                        "board_label": f"{consecutive_boards}板" if consecutive_boards else "待确认",
                        "one_word_limit_up_0925": one_word_limit_up,
                    }
                )
            float_mcap = _number(snapshot.get("float_market_cap")) or _number(snapshot.get("float_mcap"))
            auction_to_float_mcap_pct = (
                _number(event_snapshot.get("auction_amount")) / float_mcap * 100
                if float_mcap > 0 and _number(event_snapshot.get("auction_amount")) > 0
                else None
            )
            gates = scoring.evaluate_auction_gates(event_snapshot, rows) if rule_mode == "event" else []
            if is_first_board:
                first_board_count += 1
            # Preserve the auditable 15-gate view for event runs, while the new
            # rulebook risk layer remains the actual hard veto.
            if rule_mode == "event" and is_first_board:
                passed = sum(1 for gate in gates if gate.get("pass"))
                if passed < max(10, len(gates) - 6):
                    full_result["push_eligible"] = False

            score = _number(full_result.get("score"))
            if run_mode == "overnight":
                score = round(0.55 * _number(overnight_technical.get("score")) + 0.45 * score, 2)
            push_count = sum(1 for item in candidates if item.get("decision") == "push")
            can_push = bool(
                full_result.get("push_eligible")
                and score >= push_threshold
                and push_count < int(scoring.RULEBOOK_CONFIG.get("max_push", 3))
                and (rule_mode != "event" or is_first_board)
                and (
                    rule_mode != "dragon"
                    or (
                        live_selection_window
                        and live_snapshot_count > 0
                        and live_auction_count > 0
                        and bool(snapshot.get("auction_verified"))
                    )
                )
            )
            if one_word_limit_up:
                can_push = False
            if rule_mode == "dragon" and not full_result.get("eligible", False):
                decision = "watch"
                emotion = full_result.get("emotion", {})
                reason = f"擒龙否决：情绪{emotion.get('phase', '未知')}；{emotion.get('action', '等待确认')}"
            elif can_push:
                decision = "push"
                reason = "规则库风险层、模式评分及触发条件通过"
            elif score >= threshold:
                decision = "watch"
                if rule_mode == "dragon" and not live_selection_window:
                    reason = "仅保留观察：当前数据为收盘快照，等待交易日09:20-09:29当日竞价确认"
                elif rule_mode == "dragon" and (live_snapshot_count <= 0 or live_auction_count <= 0):
                    reason = "当日竞价数据未到位，宁可错过，不使用旧日期快照推送"
                elif full_result.get("market", {}).get("short_term_blocked"):
                    reason = "分数达到候选线，但市场处于退潮/弱势，停止短线推送"
                elif full_result.get("trigger", {}).get("confirmed") is False:
                    reason = "分数达到候选线，等待量价与竞价触发确认"
                elif one_word_limit_up:
                    reason = "09:25 已封一字板，按潜在涨停规则不推送"
                elif rule_mode == "dragon":
                    reason = f"擒龙模式等待有效买点：{full_result.get('triggers', {}).get('selected', '无')}"
                elif rule_mode == "event" and not is_first_board:
                    reason = "事件模式等待首板/弱转强触发，当前仅观察"
                else:
                    reason = "达到候选线，等待计划内触发"
            else:
                decision = "watch"
                reason = "未达到规则库候选阈值，保留作复盘样本"
            mode_label = full_result.get("selected_mode_label") or full_result.get("label", "观察")
            zone = 1 if decision == "push" else 2 if score >= threshold else 3
            breakdown = dict(full_result.get("breakdown") or {})
            # Compatibility aliases keep existing table consumers functional.
            breakdown["selected_mode"] = full_result.get("selected_mode")
            breakdown["coverage"] = full_result.get("data_coverage", {}).get("selected_mode", 0)
            candidates.append(
                {
                    "code": code,
                    "name": snapshot.get("name") or code,
                    "industry": snapshot.get("industry") or "待归类",
                    "zone": zone,
                    "score": score,
                    "gap_pct": _number(event_snapshot.get("gap_pct", snapshot.get("gap_pct"))),
                    "auction_amount": _number(event_snapshot.get("auction_amount", snapshot.get("amount"))),
                    "auction_to_float_mcap_pct": round(auction_to_float_mcap_pct, 2) if auction_to_float_mcap_pct is not None else None,
                    "signal": mode_label,
                    "decision": decision,
                    "decision_reason": reason,
                    "consecutive_boards": consecutive_boards,
                    "board_label": f"{consecutive_boards}板" if consecutive_boards else "待确认",
                    "one_word_limit_up_0925": one_word_limit_up,
                    "breakdown": breakdown,
                    "gates": gates,
                    "snapshot": {
                        **event_snapshot,
                        "rulebook": full_result,
                        "quick_score": quick_result.get("score"),
                        "technical": technical_latest,
                        "overnight_technical": overnight_technical,
                        "data_date": rows[-1].get("date") if rows else None,
                        "rows_used": len(rows),
                        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605）",
                    },
                }
            )

        candidates.sort(key=lambda item: (int(item.get("zone", 3)), -_number(item.get("score"))))
        candidates = candidates if run_mode == "overnight" else candidates[:40]
        if run_mode == "overnight":
            focus_names = {str(item.get("name") or "") for item in overnight_board_plan.get("focus_boards", [])}
            for item in candidates:
                item["decision"] = "watch"
                item["zone"] = 2
                signals = ((item.get("snapshot") or {}).get("overnight_technical") or {}).get("signals") or []
                item["decision_reason"] = f"隔夜收盘技术初筛：{'、'.join(signals)}；仅供次日09:20-09:25竞价验证，不构成直接买入指令"
                memberships = set(overnight_memberships.get(str(item.get("code")), []))
                memberships.add(str(item.get("industry") or ""))
                board = next((name for name in focus_names if name in memberships), str(item.get("industry") or "未分类"))
                amount = _number((item.get("snapshot") or {}).get("amount"))
                item["overnight"] = {
                    "board": str((item.get("snapshot") or {}).get("overnight_board_name") or board),
                    "role": "容量/趋势核心" if amount >= 500000000 else "先锋/观察",
                    "board_rank": (item.get("snapshot") or {}).get("overnight_board_rank"),
                    "board_5d_return": (item.get("snapshot") or {}).get("overnight_board_5d_return"),
                    "round": "first",
                    "auction_window": "09:20-09:25",
                }
                item.setdefault("snapshot", {})["overnight"] = item["overnight"]
        push_count = sum(1 for item in candidates if item["decision"] == "push")
        strong_count = sum(
            1 for item in candidates
            if _number(item.get("score")) >= threshold
            and item.get("snapshot", {}).get("rulebook", {}).get("eligible", True)
        )
        funnel = [
            {"key": "universe", "label": "沪深主板池", "count": universe_total},
            {"key": "risk_pass", "label": "风险否决后", "count": len(quick)},
            {"key": "snapshot_scored", "label": "主板快照评分", "count": len(quick)},
            {"key": "kline", "label": "K线确认预算", "count": len(kline_map)},
            {"key": "candidate", "label": "规则库候选", "count": strong_count},
            {"key": "push", "label": "最终推送", "count": push_count},
        ]
        if not candidates:
            message = "当前沪深主板池没有形成可验证候选，宁错过，不做错。"
        elif push_count == 0:
            if rule_mode == "dragon" and not live_selection_window:
                message = f"沪深主板 {universe_total} 只已完成收盘快照预筛；当前仅作观察，待交易日09:20-09:29当日竞价确认。"
            elif rule_mode == "dragon" and (live_snapshot_count <= 0 or live_auction_count <= 0):
                message = f"沪深主板 {universe_total} 只已完成预筛，但当日竞价数据未到位，本次不推送。"
            else:
                message = f"沪深主板 {universe_total} 只已完成快照预筛，本次无推送，等待触发或环境确认。"
        else:
            message = f"沪深主板 {universe_total} 只中，规则库选出 {push_count} 只推送候选。"
        stale = any(bool(row.get("stale")) for row in raw_rows)
        run = {
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "run_type": run_mode,
            "market_score": market.get("score", 0),
            "market_label": market.get("label", "中性"),
            "market_note": market.get("note", ""),
            "universe_count": universe_total,
            "first_board_count": first_board_count,
            "status": "degraded" if (not snapshots or "starter" in universe_source or stale) else "success",
            "source": universe_source,
            "message": f"{message} {DISCLAIMER}",
            "strategy_version": settings.get("strategy_version") or scoring.STRATEGY_VERSION,
            "metadata": {
                "pipeline_version": scoring.PIPELINE_VERSION,
                "pipeline_stage": "overnight_first_round" if run_mode == "overnight" else "candidate_pool",
                "live_selection_window": live_selection_window if rule_mode == "dragon" else None,
                "live_snapshot_count": live_snapshot_count if rule_mode == "dragon" else 0,
                "live_auction_count": live_auction_count if rule_mode == "dragon" else 0,
                "push_policy": (
                    "仅当日09:20-09:29实时竞价数据可用才允许擒龙推送"
                    if rule_mode == "dragon"
                    else "按对应模式规则执行"
                ),
                "overnight_board_plan": overnight_board_plan if run_mode == "overnight" else None,
                "candidate_pool_scope": "首轮沪深主板筛选；用户补充信息后进行增量复核",
                "next_day_scope": "仅验证本次候选，不重新扩张全市场",
                "funnel": funnel,
                "scan_limit": limit,
                "scan_limit_semantics": "沪深主板快照评分；limit仅用于K线确认预算，返回候选最多40条",
                "rulebook_version": scoring.RULEBOOK_VERSION,
                "rulebook_mode": rule_mode,
                "threshold": threshold,
                "push_threshold": push_threshold,
                "data_rows": len(kline_map),
                # Persist this run's breadth so the next dragon scan can measure
                # breadth velocity instead of silently treating it as unavailable.
                "breadth_ratio": _number((market.get("breadth") or {}).get("ratio")),
                # Preserve the 12-factor decision at scan time even if no stock
                # survives the final gates.  This makes later 09:26 replays
                # auditable without substituting post-open or after-close data.
                "dragon_emotion": dragon_phase if rule_mode == "dragon" else None,
                "board_rotation": self._board_rotation_from_snapshots(snapshots, kline_map),
                "raw_universe_count": raw_total,
                "eligible_count": universe_total,
                "excluded_non_main_board_count": excluded_non_main_board,
                "excluded_star_count": int(filter_stats.get("star_excluded", 0)),
                "excluded_non_stock_count": int(filter_stats.get("non_stock_excluded", 0)),
                "excluded_st_count": st_excluded,
                "excluded_delisted_count": delisted_excluded,
                "snapshot_scored_count": len(quick),
                "kline_budget": kline_budget,
                "universe_scope": "沪深主板（000/001/002/003/600/601/603/605）",
                "threshold_status": "候选阈值，待样本外回测；不构成收益保证",
                "disclaimer": DISCLAIMER,
            },
        }
        return self.db.create_screen_run(run, candidates)

    def incremental_review(
        self,
        run_id: int | None = None,
        updates: list[dict[str, Any]] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Apply user/news/industry-chain information to the existing pool only."""

        base = self.db.get_screen_run(run_id) if run_id else self.db.get_latest_candidate_pool_run()
        if not base:
            raise LookupError("暂无首轮候选池，不能进行增量复核")
        base_stage = (base.get("metadata") or {}).get("pipeline_stage")
        if base_stage not in {"candidate_pool", "overnight_first_round"}:
            raise LookupError("最新记录不是候选池阶段，不能进行增量复核")
        raw_updates = [dict(item) for item in (updates or []) if isinstance(item, dict)]
        update_map = {str(item.get("code")): item for item in raw_updates if re.fullmatch(r"\d{6}", str(item.get("code", "")))}
        board_updates: dict[str, list[dict[str, Any]]] = {}
        for item in raw_updates:
            board = str(item.get("board") or "").strip()
            if board:
                board_updates.setdefault(board, []).append(item)
        market = self.market_context()
        mode = str((base.get("metadata") or {}).get("rulebook_mode") or "balanced")
        codes = [str(item.get("code")) for item in base.get("candidates", []) if re.fullmatch(r"\d{6}", str(item.get("code", "")))]
        kline_map = self._load_kline_batch([{"code": code} for code in codes], days=120)
        candidates: list[dict[str, Any]] = []
        for old in base.get("candidates", []):
            code = str(old.get("code", ""))
            snapshot = dict(old.get("snapshot") or {})
            overnight_info = dict(snapshot.get("overnight") or old.get("overnight") or {})
            board = str(overnight_info.get("board") or "")
            applied_updates = [*board_updates.get(board, []), *([update_map[code]] if code in update_map else [])]
            snapshot.update(update_map.get(code, {}))
            rows = kline_map.get(code, [])
            result = scoring.rulebook_score(snapshot, rows, market, mode)
            risk = result.get("risk", {})
            is_overnight = base_stage == "overnight_first_round"
            support = sum(1 for item in applied_updates if str(item.get("stance") or "support") == "support")
            risks = sum(1 for item in applied_updates if str(item.get("stance") or "") in {"risk", "reject"})
            adjustment = min(10, support * 4) - min(25, risks * 12)
            if is_overnight:
                result["score"] = max(0, _number(result.get("score")) + adjustment)
            decision = "watch" if is_overnight else ("push" if result.get("push_eligible") else "watch")
            reason = "增量复核通过" if decision == "push" else ("用户/新闻风险硬性否决" if risk.get("hard_veto") else "增量复核未达到推送条件")
            candidates.append({
                **old,
                "score": _number(result.get("score")),
                "decision": decision,
                "decision_reason": ("第二轮剔除：用户补充风险/失效条件" if is_overnight and risks else "第二轮升级：用户补充经复核后增强" if is_overnight and support else reason),
                "signal": result.get("selected_mode_label") or result.get("label", "观察"),
                "breakdown": result.get("breakdown", {}),
                "gates": [],
                "snapshot": {**snapshot, "overnight": {**overnight_info, "round": "second"}, "rulebook": result, "incremental_review": {"updated": bool(applied_updates), "updates": applied_updates, "notes": notes, "score_adjustment": adjustment}},
            })
        candidates.sort(key=lambda item: (item.get("decision") != "push", -_number(item.get("score"))))
        candidates = candidates[:12] if base_stage == "overnight_first_round" else candidates[:40]
        second_round_changes = []
        if base_stage == "overnight_first_round":
            for item in candidates:
                review = (item.get("snapshot") or {}).get("incremental_review") or {}
                delta = _number(review.get("score_adjustment"))
                if delta:
                    second_round_changes.append({
                        "code": item.get("code"), "name": item.get("name"),
                        "change": "升级" if delta > 0 else "降级/剔除观察",
                        "reason": item.get("decision_reason"),
                    })
        push_count = sum(1 for item in candidates if item.get("decision") == "push")
        run = {
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "run_type": "incremental-review",
            "market_score": market.get("score", 0),
            "market_label": market.get("label", "中性"),
            "universe_count": len(codes),
            "first_board_count": 0,
            "status": "success",
            "source": "candidate_pool_review",
            "message": f"仅对首轮候选池 {len(codes)} 只做增量复核，未扩张全市场。{DISCLAIMER}",
            "strategy_version": scoring.PIPELINE_VERSION,
            "metadata": {
                "pipeline_version": scoring.PIPELINE_VERSION,
                "pipeline_stage": "overnight_second_round" if base_stage == "overnight_first_round" else "incremental_review",
                "source_run_id": base.get("id"),
                "candidate_pool_scope": "仅复核已有候选",
                "updated_codes": sorted(update_map),
                "board_updates": {key: len(value) for key, value in board_updates.items()},
                "second_round_changes": second_round_changes,
                "notes": notes,
                "funnel": [{"key": "existing_pool", "label": "已有候选池", "count": len(codes)}, {"key": "reviewed", "label": "增量复核", "count": len(candidates)}, {"key": "push", "label": "最终推送", "count": push_count}],
                "disclaimer": DISCLAIMER,
            },
        }
        return self.db.create_screen_run(run, candidates)

    def morning_confirmation(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create the day's recommendation from the day's opening data.

        The old implementation preferred an overnight pool and otherwise
        replayed yesterday's candidate run.  That made a 16:14 post-close
        snapshot look like a current recommendation.  Automatic morning runs
        now perform one fresh dragon scan in the 09:20-09:29 window; a caller
        may still pass an explicit historical ``base`` for audit/replay only.
        """

        if base is None:
            if self._same_day_live_window():
                # This scan refreshes the full universe with same-day auction
                # quotes before ranking, so no overnight or prior-day pool is
                # allowed to leak into today's push list.
                return self.run_screener(
                    "dragon", limit=int(self.settings().get("scan_limit", 80))
                )
            # Outside the short live window, fail closed instead of replaying
            # an old candidate pool.  Historical runs remain available via
            # their explicit run id for review.
            base = None
        if not base:
            run = {
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "run_type": "morning-confirmation",
                "market_score": 0,
                "market_label": "无候选池",
                "universe_count": 0,
                "first_board_count": 0,
                "status": "no_candidate_pool",
                "source": "existing_candidate_pool",
                "message": f"09:26 未找到已有候选池，本次不扫描全市场、不推送。{DISCLAIMER}",
                "strategy_version": scoring.PIPELINE_VERSION,
                "metadata": {
                    "pipeline_version": scoring.PIPELINE_VERSION,
                    "pipeline_stage": "morning_confirmation",
                    "source_run_id": None,
                    "pool_only": True,
                    "new_symbols_added": 0,
                    "skip_reason": "candidate_pool_missing",
                    "funnel": [{"key": "existing_pool", "label": "已有候选池", "count": 0}],
                    "disclaimer": DISCLAIMER,
                },
            }
            return self.db.create_screen_run(run, [])
        pool = [item for item in base.get("candidates", []) if re.fullmatch(r"\d{6}", str(item.get("code", "")))]
        if not pool:
            run = {
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "run_type": "morning-confirmation",
                "market_score": 0,
                "market_label": "候选池为空",
                "universe_count": 0,
                "first_board_count": 0,
                "status": "empty_candidate_pool",
                "source": "existing_candidate_pool",
                "message": f"09:26 已有候选池为空，本次不扫描全市场、不推送。{DISCLAIMER}",
                "strategy_version": scoring.PIPELINE_VERSION,
                "metadata": {
                    "pipeline_version": scoring.PIPELINE_VERSION,
                    "pipeline_stage": "morning_confirmation",
                    "source_run_id": base.get("id"),
                    "pool_only": True,
                    "new_symbols_added": 0,
                    "skip_reason": "candidate_pool_empty",
                    "funnel": [{"key": "existing_pool", "label": "已有候选池", "count": 0}],
                    "disclaimer": DISCLAIMER,
                },
            }
            return self.db.create_screen_run(run, [])
        market = self.market_context()
        quotes = self._quote_batch(
            [str(item.get("code")) for item in pool], auction=True, force=True
        )
        kline_map = self._load_kline_batch(pool, days=120)
        confirmed: list[dict[str, Any]] = []
        for old in pool:
            code = str(old.get("code"))
            current = dict(quotes.get(code) or {})
            rows = kline_map.get(code, [])
            result = scoring.confirm_existing_candidate(old, current, rows, market)
            snapshot = dict(old.get("snapshot") or {})
            snapshot.update(current)
            snapshot["confirmation"] = result
            confirmed.append({
                **old,
                "score": _number(result.get("score")),
                "decision": result.get("decision", "watch"),
                "decision_reason": result.get("reason", ""),
                "signal": "竞价确认" if result.get("confirmed") else "候选复核",
                "breakdown": {"prior_score": result.get("prior_score"), "technical": result.get("technical_score"), "auction": result.get("auction_score")},
                "gates": result.get("gates", []),
                "snapshot": snapshot,
            })
        confirmed.sort(key=lambda item: (item.get("decision") != "push", -_number(item.get("score"))))
        confirmed = confirmed[:40]
        pushes = [item for item in confirmed if item.get("decision") == "push"][:3]
        for item in confirmed:
            item["decision"] = "push" if item in pushes else "watch"
        run = {
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "run_type": "morning-confirmation",
            "market_score": market.get("score", 0),
            "market_label": market.get("label", "中性"),
            "universe_count": len(pool),
            "first_board_count": 0,
            "status": "success",
            "source": "existing_candidate_pool",
            "message": f"09:26仅复核首轮候选 {len(pool)} 只，未重新扫描全市场。{DISCLAIMER}",
            "strategy_version": scoring.PIPELINE_VERSION,
            "metadata": {
                "pipeline_version": scoring.PIPELINE_VERSION,
                "pipeline_stage": "morning_confirmation",
                "source_run_id": base.get("id"),
                "pool_only": True,
                "new_symbols_added": 0,
                "funnel": [{"key": "existing_pool", "label": "前一轮候选", "count": len(pool)}, {"key": "confirmed", "label": "竞价/技术复核", "count": len(confirmed)}, {"key": "push", "label": "最终推送", "count": len(pushes)}],
                "disclaimer": DISCLAIMER,
            },
        }
        return self.db.create_screen_run(run, confirmed)

    def stock_analysis(self, code: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码必须是 6 位数字")
        if scoring.is_star_security(code):
            raise ValueError("科创板股票已按系统规则排除")
        quote = self.provider.get_quote(code)
        rows = self.provider.get_kline(code, days=120)
        if not rows:
            raise LookupError("未获取到该股票的 K 线数据")
        technical = scoring.technical_score(rows)
        trend = scoring.trend_score_series(rows, n=10)
        # Base analysis must stay responsive when Eastmoney is rate-limited.
        # Reuse the locally persisted screener metadata for industry context and
        # leave EM-only board/fund enrichment for an explicit deep-analysis pass.
        cached_candidate = next(
            (
                item for item in (self.db.get_latest_screen_run() or {}).get("candidates", [])
                if str(item.get("code", "")) == code
            ),
            {},
        )
        safe_calls: dict[str, Any] = {
            "stock_info": {
                "code": code,
                "name": quote.get("name", ""),
                "industry": cached_candidate.get("industry", ""),
                "source": "local_screener_cache",
            },
            "boards": {"code": code, "total": 0, "boards": [], "concept_tags": [], "deferred": True},
            "fund_flow": {"code": code, "rows": [], "deferred": True},
            "local_prediction": {},
        }
        try:
            safe_calls["local_prediction"] = self.provider.get_local_prediction(code)
        except Exception as exc:
            safe_calls["local_prediction"] = {"error": str(exc)}
        indicators = technical.get("indicators", {})
        close = _number(rows[-1].get("close"))
        ma5 = _number(indicators.get("ma5"), close)
        atr = _number(indicators.get("atr14"), max(close * 0.025, 0.01))
        trigger = round(max(ma5, close - atr * 0.4), 2)
        invalidation = round(min(ma5, close - atr * 1.2), 2)
        quote = {
            **quote,
            "previous_close": quote.get("previous_close") or quote.get("last_close"),
            "turnover": quote.get("turnover") or quote.get("turnover_pct"),
            "market_cap": quote.get("market_cap")
            or (_number(quote.get("mcap_yi")) * 100_000_000 if quote.get("mcap_yi") is not None else None),
            "industry": safe_calls["stock_info"].get("industry", ""),
        }
        try:
            market = self.market_context()
        except Exception:
            market = {"score": 0, "label": "中性", "coefficient": 1.0}

        # --- NEW: K-line pattern detection ---
        kline_patterns: dict[str, Any] = {}
        try:
            kline_patterns = detect_kline_patterns(rows)
        except Exception:
            kline_patterns = {"signal": 0.0, "bullish": [], "bearish": [], "neutral": [], "details": []}

        # --- NEW: Strategy template matching ---
        strategy_matches: dict[str, Any] = {}
        try:
            snapshot_for_strategy = {
                "price": _number(quote.get("price")) or _number(rows[-1].get("close")),
                "open": _number(quote.get("open")) or _number(rows[-1].get("open")),
                "amount": _number(quote.get("amount")) or _number(rows[-1].get("amount")),
                "volume": _number(quote.get("volume")) or _number(rows[-1].get("volume")),
            }
            strategy_matches = run_strategies(snapshot_for_strategy, rows)
        except Exception:
            strategy_matches = {"strategies": {}, "triggered_count": 0, "triggered": []}

        strategy = scoring.rulebook_score(
            {**quote, "code": code, "name": quote.get("name") or safe_calls["stock_info"].get("name") or code},
            rows,
            market,
            "balanced",
        )
        components = technical.get("components", {})
        signal = {
            "score": strategy.get("score", 0),
            "label": strategy.get("label", "中性"),
            "summary": "；".join((strategy.get("evidence") or technical.get("evidence") or [])[:2]),
            "ma5": components.get("ma5", 0),
            "kdj": components.get("kdj", 0),
            "macd": components.get("macd", 0),
            "ma10": components.get("ma10", 0),
        }
        score_breakdown = {
            key: {
                "score": value,
                "label": key.upper() if key.startswith("ma") else key.upper(),
                "reason": next(
                    (evidence for evidence in technical.get("evidence", []) if key.upper() in evidence.upper()),
                    "标准技术指标评分",
                ),
                "state": technical.get("label", "中性"),
            }
            for key, value in components.items()
        }
        quote_as_of = quote.get("data_as_of") or quote.get("quote_time")
        kline_as_of = rows[-1].get("date")
        data_delayed = bool(quote.get("stale") or rows[-1].get("stale"))
        return {
            "code": code,
            "name": quote.get("name") or safe_calls["stock_info"].get("name") or code,
            "quote": quote,
            "signal": signal,
            "score": strategy.get("score", 0),
            "total_score": strategy.get("score", 0),
            "score_breakdown": score_breakdown,
            "strategy_version": strategy.get("strategy_version"),
            "rulebook_version": strategy.get("rulebook_version"),
            "strategy": strategy,
            "risk": strategy.get("risk", {}),
            "technical": technical,
            "trend_scores": trend,
            "stock_info": safe_calls["stock_info"],
            "boards": safe_calls["boards"],
            "fund_flow": safe_calls["fund_flow"],
            "local_prediction": safe_calls["local_prediction"],
            "kline_patterns": kline_patterns,
            "strategy_matches": strategy_matches,
            "trigger": {
                "observe": f"{round(trigger - atr * 0.3, 2)}–{round(trigger + atr * 0.3, 2)}",
                "buy_condition": (
                    f"{strategy.get('trigger', {}).get('note', '等待规则库触发')}；"
                    f"放量站稳 {trigger:.2f} 且 MACD 柱体继续改善"
                ),
                "invalidation": f"收盘跌破 {invalidation:.2f} 或量价背离；{(strategy.get('exit_plan') or ['逻辑失效退出'])[0]}",
            },
            "kline": rows[-80:],
            "as_of": quote_as_of or kline_as_of,
            "data_as_of": quote_as_of or kline_as_of,
            "kline_as_of": kline_as_of,
            "trade_date": quote.get("trade_date") or str(kline_as_of or "")[:10],
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_delayed": data_delayed,
            "stale": data_delayed,
            "disclaimer": DISCLAIMER,
        }

    def deep_analysis(self, code: str) -> dict[str, Any]:
        analysis = self.stock_analysis(code)
        result = scoring.deep_analysis(analysis["kline"], max_days=30)
        news: list[dict[str, Any]] = []
        try:
            news = self.provider.get_stock_news(code)[:5]
        except Exception:
            pass
        conclusion = result.get("conclusion", {})
        conditions = conclusion.get("conditions", []) if isinstance(conclusion, dict) else []
        conclusion_view = {
            **conclusion,
            "direction": conclusion.get("bias", "待确认") if isinstance(conclusion, dict) else "待确认",
            "key_level": (
                f"支撑 {conclusion.get('support')} / 压力 {conclusion.get('resistance')}"
                if isinstance(conclusion, dict)
                else "--"
            ),
            "risk": conditions[-1] if conditions else "",
            "advice": conditions[0] if conditions else "保持观察",
        }
        phases = result.get("phases", [])
        return {
            "code": code,
            "name": analysis["name"],
            "quote": analysis["quote"],
            "phases": phases,
            "timeline": phases,
            "conclusion": conclusion_view,
            "risk": conditions[-1] if conditions else "",
            "evidence": [evidence for phase in phases for evidence in phase.get("evidence", [])],
            "news": news,
            "kline": analysis["kline"][-30:],
            "disclaimer": DISCLAIMER,
        }

    def overview(self) -> dict[str, Any]:
        market = self.market_context()
        latest = self.db.get_latest_screen_run()
        # 首页只展示候选摘要。完整 breakdown/gates/snapshot 仅在点击详情
        # 时由 /api/screener/runs/{id} 返回，避免每次首页刷新传输数百 KB
        # 的重复 JSON，并降低浏览器解析和 DOM 渲染开销。
        if latest:
            compact = []
            for item in latest.get("candidates", []):
                compact.append(
                    {
                        key: item.get(key)
                        for key in (
                            "rank", "code", "name", "industry", "zone", "score",
                            "gap_pct", "auction_amount", "decision", "decision_reason",
                            "signal", "pushed", "reason", "change_pct",
                        )
                    }
                )
            latest = {**latest, "candidates": compact, "candidate_count": len(compact)}
        backtest = self.db.list_backtests(limit=100)["stats"]
        jobs = self.jobs_payload()["jobs"]
        health = self.provider.health()
        source_rows = _health_source_rows(health)
        return {
            "market": market,
            "data_as_of": market.get("data_as_of") or market.get("as_of"),
            "trade_date": market.get("trade_date"),
            "latest_run": latest,
            "backtest": backtest,
            "jobs": jobs,
            "data_sources": source_rows,
            "system": {
                "strategy_version": self.settings().get("strategy_version"),
                "status": health.get("status", "error"),
                "degraded": bool(health.get("degraded")),
                "provider_status": health,
                "as_of": datetime.now().isoformat(timespec="seconds"),
            },
            "disclaimer": DISCLAIMER,
        }

    def build_review(self) -> dict[str, Any]:
        from .review import analyze_backtest, generate_markdown, write_report

        latest = self.db.get_latest_screen_run() or self.db.get_latest_screen_run("auction")
        market = self.market_context()
        backtest_data = self.db.list_backtests(limit=500)
        stats = backtest_data["stats"]
        push_count = int((latest or {}).get("push_count") or 0)
        candidate_count = int((latest or {}).get("candidate_count") or 0)
        if push_count:
            diagnosis = f"本次 {candidate_count} 只候选中推送 {push_count} 只，继续跟踪量价与板块共振。"
        else:
            diagnosis = f"候选 {candidate_count} 只但未推送，过滤器保持在线，未为凑数放宽门槛。"

        # Generate 7-step short-term review with new engine
        from .review import run_review

        # Collect data from latest screen run
        candidates = (latest or {}).get("candidates", []) or []
        pushed = [c for c in candidates if c.get("pushed")]
        # Build board summary from candidates
        board_map = {}
        for c in candidates:
            snap = c.get("snapshot", {}) or {}
            board = snap.get("industry") or c.get("industry") or "其他"
            if board not in board_map:
                board_map[board] = {"name": board, "stocks": [], "changes": []}
            board_map[board]["stocks"].append(c.get("name") or c.get("code"))
            chg = snap.get("change_pct") or c.get("change_pct") or 0
            board_map[board]["changes"].append(float(chg) if chg else 0)
        board_data = []
        for bname, bd in board_map.items():
            changes = bd["changes"]
            up = sum(1 for c in changes if c > 0)
            board_data.append({
                "name": bname, "avg_change_pct": sum(changes)/max(1,len(changes)),
                "up_ratio": up/max(1,len(changes)),
                "diffusion": up/max(1,len(changes)),
                "count": len(changes),
                "leader_concentration": None,
            })

        enhanced_stats = {}
        obsidian_path = None
        trade_date = datetime.now().strftime("%Y-%m-%d")
        emotion_phase = str((market or {}).get("phase", "低迷"))

        review_result = run_review(
            market or {},
            board_data,
            candidates,
            pushed,
            backtest_data.get("records", []),
            trade_date,
            emotion_phase,
        )
        obsidian_path = review_result.get("path")
        enhanced_stats = {"length": review_result.get("length", 0)}
        report_markdown = str(review_result.get("markdown") or "")

        review = {
            "trade_date": trade_date,
            "run_id": (latest or {}).get("id"),
            "market_summary": f"市场信号 {market.get('score', 0):+g}，{market.get('label', '中性')}。{market.get('note', '')}",
            "strategy_diagnosis": diagnosis,
            "advice": "关注强势行业中的低位补涨，回避高位追涨；所有信号需结合个人风险承受能力。",
            "stats": {
                **stats,
                "universe_count": (latest or {}).get("universe_count", 0),
                "first_board_count": (latest or {}).get("first_board_count", 0),
                "candidate_count": candidate_count,
                "push_count": push_count,
            },
            "enhanced_stats": enhanced_stats,
            "obsidian_report": obsidian_path,
        }
        stored = self.db.upsert_review(review)
        # The database keeps summary fields; expose the full seven-step report
        # to the immediate API/MCP response used by Hermes and personal WeChat.
        stored["report_markdown"] = report_markdown
        stored["report_format"] = "seven-step-review-v1"
        return stored

    def latest_review(self) -> dict[str, Any]:
        # Rebuild current-day aggregate so a newer screen run cannot leave the
        # review page with stale candidate or push counts.
        review = self.build_review()
        run = self.db.get_screen_run(int(review.get("run_id"))) if review.get("run_id") else None
        records = []
        for item in (run or {}).get("candidates", []):
            records.append(
                {
                    "type": "推送" if item.get("pushed") else "候选",
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "score": item.get("score"),
                    "pushed": item.get("pushed"),
                    "hit": None,
                    "reason": item.get("decision_reason"),
                }
            )
        return {
            **review,
            "summary": review.get("stats", {}),
            "records": records,
            "diagnosis": [
                {"title": "策略诊断", "detail": review.get("strategy_diagnosis", "")},
                {"title": "市场概况", "detail": review.get("market_summary", "")},
                {"title": "观察建议", "detail": review.get("advice", "")},
            ],
            "status": "completed",
        }

    def run_backtest(self) -> dict[str, Any]:
        runs = self.db.list_screen_runs(limit=10)
        new_records: list[dict[str, Any]] = []
        for run in runs:
            if run.get("source") == "calibration_sample":
                continue
            trade_date = str(run.get("trade_date", ""))
            # TDX 盘后 K 线的 date 形如 "20260912"，而推送记录是 "2026-09-12"。
            # 直接做字典序比较时 '0' > '-' 恒成立，会永远命中最老一根 K 线，
            # 因此两侧都统一成 8 位数字后再比较。
            target_date = trade_date.replace("-", "")
            for item in run.get("candidates", []):
                if item.get("decision") != "push":
                    continue
                if scoring.is_star_security(item.get("code"), item.get("name")):
                    # Historical/sample runs may contain STAR records; the
                    # replacement strategy must never create new ones.
                    continue
                rows = self.provider.get_kline(str(item.get("code", "")), days=180)
                idx = next(
                    (
                        i
                        for i, row in enumerate(rows)
                        if str(row.get("date") or "").replace("-", "") >= target_date
                    ),
                    None,
                )
                if idx is None or idx >= len(rows):
                    continue
                entry = rows[idx]
                entry_open = _number(entry.get("open"))
                if not entry_open:
                    continue
                pnl = (_number(entry.get("close")) / entry_open - 1) * 100
                future = rows[idx : min(len(rows), idx + 7)]
                best = (max(_number(row.get("high")) for row in future) / entry_open - 1) * 100
                prev_close = _number(rows[idx - 1].get("close")) if idx > 0 else entry_open
                hit = (_number(entry.get("close")) / prev_close - 1) * 100 >= (
                    19.5 if str(item.get("code", "")).startswith(("300", "301")) else 9.5
                )
                new_records.append(
                    {
                        "trade_date": trade_date,
                        "category": f"推送{item.get('rank', '')}",
                        "code": item.get("code"),
                        "name": item.get("name"),
                        "pushed": True,
                        "hit": hit,
                        "pnl_pct": round(pnl, 2),
                        "best_7d_pct": round(best, 2),
                        "score": item.get("score"),
                        "strategy_version": run.get("strategy_version"),
                        "source": "computed",
                        "note": "按真实日 K 开盘价计算，总涨跌=(收盘-开盘)/开盘",
                    }
                )
        inserted = self.db.add_backtest_records(new_records)
        payload = self.db.list_backtests()
        payload["inserted"] = inserted
        payload["message"] = "回测完成" if inserted else "没有新的可计算推送记录"
        return payload

    def jobs_payload(self) -> dict[str, Any]:
        jobs = self.db.list_jobs()
        for job in jobs:
            job["next_run_at"] = self.db.next_scheduled_time(job["id"])
        return {"jobs": jobs, "recent_runs": self.db.recent_job_runs()}

    def run_job(self, job_id: str, manual: bool = True) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError("任务不存在")
        run = self.db.start_job_run(job_id, manual=manual)
        try:
            if job_id == "overnight-pool":
                result = self.run_screener("overnight")
                summary = result.get("message", "\u9694\u591c\u5019\u9009\u6c60\u5df2\u5efa\u7acb")
            elif job_id == "morning-auction":
                result = self.morning_confirmation()
                summary = result.get("message", "候选池早盘复核完成")
            elif job_id == "yijiner-scan":
                result = self.yijiner_scan(manual=manual)
                summary = result.get("message", "一进二扫描完成")
            elif job_id == "auction-trace":
                result = self.sample_auction_trace()
                summary = result.get("message", "竞价采样完成")
            elif job_id == "dixi-scan":
                result = self.dixi_scan(manual=manual)
                summary = result.get("message", "低吸计划已生成")
            elif job_id == "afternoon-review":
                result = self.build_review()
                summary = result.get("strategy_diagnosis", "复盘完成")
            else:
                universe = self.provider.get_market_universe(limit=None)
                rows, _, source = _normalize_rows(universe)
                rows = [
                    row for row in rows
                    if scoring.is_main_board_security(row.get("code"), row.get("name"))
                ]
                total = len(rows)
                result = {
                    "total": total,
                    "sampled": len(rows),
                    "source": source,
                    "universe_scope": "沪深主板（000/001/002/003/600/601/603/605）",
                }
                stats_getter = getattr(self.provider, "get_market_universe_stats", None)
                if callable(stats_getter):
                    result["universe_stats"] = stats_getter()
                summary = f"股票基础信息检查完成，规则库股票池覆盖 {total} 只"
            delivery = self._deliver_job_result(job_id, job, result)
            result["delivery"] = delivery
            result_status = str(result.get("status") or "success")
            status = (
                "warning"
                if result_status in {"waiting", "busy", "warning", "degraded"}
                else "success" if delivery.get("sent") or delivery.get("skipped") else "warning"
            )
            if status == "warning":
                summary = f"{summary}；企业微信未发送：{delivery.get('reason', '未知错误')}"
            self.db.finish_job_run(run["id"], job_id, status, summary, result)
            return {"run_id": run["id"], "status": status, "summary": summary, "result": result}
        except Exception as exc:
            self.db.finish_job_run(run["id"], job_id, "failed", str(exc), {"error": str(exc)})
            raise

    def _deliver_job_result(
        self, job_id: str, job: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        channel = str(job.get("channel") or "local")
        if channel != "wecom":
            return {
                "sent": False,
                "skipped": True,
                "channel": channel,
                "reason": "任务渠道仅记录在本地",
            }
        if job_id == "morning-auction":
            content = self.format_run_message(result)
        elif job_id == "auction-trace":
            content = (
                f"寻龙工作台 · 竞价采样\n{result.get('message', '竞价采样完成')}"
                f"\n\n{DISCLAIMER}"
            )
        elif job_id == "afternoon-review":
            content = self.format_review_message(result)
        else:
            content = (
                "寻龙工作台 · 股票基础信息刷新\n"
                f"规则库沪深主板池覆盖 {result.get('total', 0)} 只，"
                f"本次读取 {result.get('sampled', 0)} 只。"
                f"\n\n{DISCLAIMER}"
            )
        return self.send_wecom_message(content)

    def format_review_message(self, review: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"寻龙工作台 · 收盘复盘 {review.get('trade_date', '')}",
                str(review.get("market_summary") or "暂无市场概况"),
                str(review.get("strategy_diagnosis") or "暂无策略诊断"),
                str(review.get("advice") or "暂无观察建议"),
                "",
                DISCLAIMER,
            ]
        )

    def format_run_message(self, run: dict[str, Any] | None = None) -> str:
        run = run or self.db.get_latest_screen_run()
        if not run:
            return f"暂无筛选记录。\n\n⚠️ {DISCLAIMER}"
        picks = [item for item in run.get("candidates", []) if item.get("decision") == "push"]
        mode = str((run.get("metadata") or {}).get("rulebook_mode") or "balanced")
        funnel = {
            str(item.get("key")): int(item.get("count") or 0)
            for item in (run.get("funnel") or (run.get("metadata") or {}).get("funnel") or [])
        }
        title = "09:28擒龙推送" if mode == "dragon" else f"规则库选股（{mode}）"
        lines = [
            f"寻龙工作台 · {title} {str(run.get('trade_date', ''))[-5:].replace('-', '/')}",
            f"市场信号：{_number(run.get('market_score')):+g} {run.get('market_label', '中性')}",
            f"沪深主板 {run.get('universe_count', 0)} → 风险过滤后 {funnel.get('risk_pass', 0)} → "
            f"K线确认 {funnel.get('kline', 0)} → 观察 {run.get('candidate_count', 0)} → 推送 {len(picks)}",
        ]
        if picks:
            lines.append("")
            for index, item in enumerate(picks, 1):
                gap = _number(item.get("gap_pct"))
                rulebook = (item.get("snapshot") or {}).get("rulebook") or {}
                mode_label = rulebook.get("selected_mode_label") or rulebook.get("selected_mode") or "综合"
                coverage = _number((rulebook.get("data_coverage") or {}).get("selected_mode"))
                lines.append(
                    f"{index}. {item.get('name')} {item.get('code')} | {item.get('industry') or '待归类'} | "
                    f"{item.get('score', 0):g}分 | {mode_label} | 覆盖{coverage:.0%} | 触发参考 {gap:+.2f}%"
                )
                trigger = rulebook.get("trigger") or {}
                if trigger.get("confirmed") is not True:
                    lines.append(f"   等待触发确认：{trigger.get('passed', 0)}/{trigger.get('total', 0)} 条条件")
        else:
            candidates = list(run.get("candidates") or [])
            first = candidates[0] if candidates else {}
            rulebook = (first.get("snapshot") or {}).get("rulebook") or {}
            emotion = (rulebook.get("emotion") or {}).get("phase") or run.get("market_label") or "未知"
            reason = first.get("reason") or first.get("decision_reason") or run.get("message") or "未形成有效买点"
            lines.extend(
                [
                    "",
                    f"结论：本次不推票，情绪阶段为{emotion}。",
                    f"原因：{reason}",
                    "操作：保持观察，不为凑数降低门槛。",
                ]
            )
        lines.extend(["", f"⚠️ {DISCLAIMER}"])
        return "\n".join(lines)

    @staticmethod
    def _valid_wecom_webhook(webhook: str) -> bool:
        parsed = urlsplit(webhook)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "qyapi.weixin.qq.com"
            and parsed.path.rstrip("/") == "/cgi-bin/webhook/send"
            and bool(parse_qs(parsed.query).get("key", [""])[0])
        )

    def send_wecom_message(self, content: str) -> dict[str, Any]:
        webhook = str(self.settings().get("wecom_webhook") or "").strip()
        if not webhook:
            return {
                "sent": False,
                "reason": "尚未配置企业微信 Webhook，已生成消息预览",
                "preview": content,
            }
        if not self._valid_wecom_webhook(webhook):
            return {
                "sent": False,
                "reason": "企业微信 Webhook 地址无效，请在设置中重新配置",
                "preview": content,
            }
        try:
            response = requests.post(
                webhook,
                json={"msgtype": "text", "text": {"content": content}},
                timeout=12,
            )
        except requests.Timeout:
            return {"sent": False, "reason": "企业微信请求超时", "preview": content}
        except requests.RequestException as exc:
            return {
                "sent": False,
                "reason": f"企业微信请求失败：{type(exc).__name__}",
                "preview": content,
            }
        if not response.ok:
            return {
                "sent": False,
                "reason": f"企业微信 HTTP {response.status_code}",
                "preview": content,
            }
        try:
            payload = response.json()
        except ValueError:
            return {
                "sent": False,
                "reason": "企业微信返回内容无法解析",
                "preview": content,
            }
        sent = payload.get("errcode") == 0
        return {
            "sent": sent,
            "reason": "企业微信已接收消息" if sent else str(payload.get("errmsg") or "企业微信拒绝消息"),
            "provider_response": payload,
            "preview": content,
        }

    def send_test_message(
        self, content: str | None = None, channel: str = "wecom"
    ) -> dict[str, Any]:
        if channel != "wecom":
            raise ValueError("仅支持企业微信群机器人通道")
        return self.send_wecom_message(content or self.format_run_message())

    def _deliver_command_response(self, response: dict[str, Any]) -> dict[str, Any]:
        text = str(response.get("text") or "").strip()
        response["delivery"] = (
            self.send_wecom_message(text)
            if text
            else {"sent": False, "reason": "命令没有生成可投递内容"}
        )
        return response

    def bot_command(self, command: str) -> dict[str, Any]:
        command = command.strip()
        code_match = re.search(r"\b(\d{6})\b", command)
        if command == "/sethome":
            self.db.update_settings({"home_channel": "local-command-console"})
            response = {"type": "system", "text": "当前会话已设为本地任务主频道。"}
        elif command == "选好票":
            run = self.run_screener("dragon", limit=24)
            response = {"type": "screen", "text": self.format_run_message(run), "data": run}
        elif code_match and ("深度" in command or "主力" in command):
            data = self.deep_analysis(code_match.group(1))
            phases = "；".join(
                f"{item.get('start_date')}–{item.get('end_date')} {item.get('stage')}"
                for item in data["phases"]
            )
            conclusion = data.get("conclusion") or {}
            response = {
                "type": "deep",
                "text": f"{data['name']}（{data['code']}）30日轨迹：{phases}\n{conclusion.get('summary', '')}\n\n⚠️ {DISCLAIMER}",
                "data": data,
            }
        elif code_match:
            data = self.stock_analysis(code_match.group(1))
            tech = data["technical"]
            strategy = data.get("strategy") or {}
            components = tech.get("components", {})
            component_text = " ".join(
                f"{key}{_number(value.get('score') if isinstance(value, dict) else value):+g}"
                for key, value in components.items()
            )
            response = {
                "type": "stock",
                "text": (
                    f"{data['name']}（{data['code']}）\n"
                    f"近10日趋势：{' '.join(str(item.get('score')) if isinstance(item, dict) else str(item) for item in data['trend_scores'])}\n"
                    f"规则库模式：{strategy.get('selected_mode_label', strategy.get('selected_mode', '综合'))}\n"
                    f"规则库评分：{strategy.get('score', 0)}（数据覆盖 {strategy.get('data_coverage', {}).get('selected_mode', 0):.0%}）\n"
                    f"技术确认：{component_text}\n"
                    f"信号：{strategy.get('label', tech.get('label'))}\n"
                    f"触发：{data['trigger']['buy_condition']}\n\n⚠️ {DISCLAIMER}"
                ),
                "data": data,
            }
        elif command.startswith("复盘"):
            review = self.latest_review()
            response = {
                "type": "review",
                "text": f"{review['trade_date']} 复盘\n{review['market_summary']}\n{review['strategy_diagnosis']}\n{review['advice']}\n\n⚠️ {DISCLAIMER}",
                "data": review,
            }
        elif command in {"状态", "status"}:
            health = self.provider.health()
            response = {
                "type": "status",
                "text": f"系统运行中。策略 {self.settings().get('strategy_version')}，数据源状态：{health.get('status', 'unknown')}。",
                "data": health,
            }
        else:
            response = {
                "type": "help",
                "text": "可用指令：选好票、六位股票代码、深度分析 代码、复盘、状态、/sethome",
            }
        return self._deliver_command_response(response)
