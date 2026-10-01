from __future__ import annotations

import copy
import contextlib
import csv
import html
import json
import os
import queue
import random
import re
import struct
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter

from . import news_feed
from .qmt import QmtMarketData, QmtUnavailable
from .scoring import A_SHARE_CODE_PREFIXES, STAR_CODE_PREFIXES, is_a_share_security, is_star_security

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - requests normally installs urllib3.
    Retry = None  # type: ignore[assignment]

try:
    import msvcrt  # Windows 跨进程文件锁，用于 FFD 状态文件。
except ImportError:  # pragma: no cover - 非 Windows 开发环境。
    msvcrt = None  # type: ignore[assignment]


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
EASTMONEY_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

INDEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
    ("sh000688", "科创50"),
    ("sh000905", "中证500"),
    ("bj899050", "北证50"),
    ("sh000016", "上证50"),
)

# This is deliberately small. It keeps the application usable when a full-market
# endpoint is unavailable, while making the degraded state explicit in metadata.
STARTER_UNIVERSE: tuple[tuple[str, str, str], ...] = (
    ("000001", "平安银行", "银行"),
    ("000333", "美的集团", "家用电器"),
    ("000651", "格力电器", "家用电器"),
    ("000858", "五粮液", "白酒"),
    ("002230", "科大讯飞", "软件开发"),
    ("002475", "立讯精密", "消费电子"),
    ("002594", "比亚迪", "汽车整车"),
    ("002714", "牧原股份", "养殖业"),
    ("300014", "亿纬锂能", "电池"),
    ("300059", "东方财富", "证券"),
    ("300124", "汇川技术", "自动化设备"),
    ("300308", "中际旭创", "通信设备"),
    ("300750", "宁德时代", "电池"),
    ("600000", "浦发银行", "银行"),
    ("600009", "上海机场", "机场"),
    ("600036", "招商银行", "银行"),
    ("600050", "中国联通", "通信服务"),
    ("600276", "恒瑞医药", "化学制药"),
    ("600309", "万华化学", "化学制品"),
    ("600519", "贵州茅台", "白酒"),
    ("600690", "海尔智家", "家用电器"),
    ("600900", "长江电力", "电力"),
    ("601012", "隆基绿能", "光伏设备"),
    ("601318", "中国平安", "保险"),
    ("601398", "工商银行", "银行"),
    ("601899", "紫金矿业", "贵金属"),
    ("603259", "药明康德", "医疗服务"),
)


class ProviderError(RuntimeError):
    """A provider response was reachable but unusable."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _exchange_datetime(value: Any) -> datetime | None:
    """Parse Tencent/TDX exchange timestamps into local exchange time."""

    digits = re.sub(r"\D", "", str(value or ""))
    try:
        if len(digits) >= 14:
            return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        if len(digits) >= 8:
            # A daily bar represents the completed close, not midnight.
            return datetime.strptime(digits[:8] + "150000", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return None


def _exchange_iso(value: Any) -> str:
    parsed = _exchange_datetime(value)
    return parsed.astimezone().isoformat(timespec="seconds") if parsed else ""


def _exchange_trade_date(value: Any) -> str:
    parsed = _exchange_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def _auction_quote_is_current(row: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    """Reject ordinary or previous-session quotes from the auction endpoint."""

    quote_date = next(
        (
            value
            for value in (
                _exchange_trade_date(row.get("trade_date")),
                _exchange_trade_date(row.get("data_as_of")),
                _exchange_trade_date(row.get("quote_time")),
            )
            if value
        ),
        "",
    )
    if quote_date != now.strftime("%Y-%m-%d"):
        return False, "auction quote is not from the current trading date"
    minute = now.hour * 60 + now.minute
    live_window = now.weekday() < 5 and 9 * 60 + 15 <= minute <= 9 * 60 + 30
    status = str(row.get("auction_data_status") or "").strip().lower()
    stage = str(row.get("auction_stage") or "").strip().lower()
    terminal = (
        bool(row.get("ffd_terminal"))
        or status in {"final", "complete", "completed", "complete_no_event", "no_event", "终态", "终态无成交"}
        or stage in {"opening_call_auction_final", "final", "auction_final", "集合竞价终态"}
    )
    if status == "snapshot_only":
        return False, "ordinary quote snapshot is not an auction match"
    if not live_window and not terminal:
        return False, "auction quote lacks a terminal marker outside the live window"
    if live_window and not terminal:
        quote_time = _exchange_datetime(row.get("quote_time"))
        if quote_time is None or abs((now - quote_time).total_seconds()) > 120:
            return False, "auction match timestamp is missing or older than two minutes"
    if bool(row.get("stale")):
        return False, "auction quote is stale"
    return True, ""


def _normalise_quote_time(value: Any) -> str:
    """Normalise Eastmoney's epoch quote time to the shared exchange format."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
        # Eastmoney f124 is Unix seconds, while other endpoints use YYYYMMDDhhmmss.
        if 900_000_000 <= number <= 4_000_000_000:
            return datetime.fromtimestamp(number).strftime("%Y%m%d%H%M%S")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    digits = re.sub(r"\D", "", text)
    return digits[:14] if len(digits) >= 14 else ""


def _normalise_code(value: str | int) -> str:
    if isinstance(value, int) and 0 <= value <= 999999:
        return f"{value:06d}"
    text = str(value or "").strip().lower()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if not match:
        raise ValueError(f"invalid A-share code: {value!r}")
    return match.group(1)


def _market_prefix(code: str) -> str:
    if code.startswith("920"):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def _ffd_standard_code(code: str | int, market: str | None = None) -> str:
    """Return the exchange-qualified code expected by public FFD tools."""

    normalised = _normalise_code(code)
    prefix = str(market or _market_prefix(normalised)).strip().lower()[:2]
    if prefix not in {"sh", "sz", "bj"}:
        raise ValueError(f"invalid market prefix: {market!r}")
    return f"{normalised}.{prefix.upper()}"


def _eastmoney_secid(code: str) -> str:
    return f"{1 if _market_prefix(code) == 'sh' else 0}.{code}"


def filter_stock_universe(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep the complete supported stock universe and report hard exclusions.

    This is deliberately applied after every provider response, including
    stale/fallback data.  The returned counts make the full-market funnel
    auditable instead of silently treating a truncated list as the universe.
    """

    kept: list[dict[str, Any]] = []
    counts = {"input": 0, "kept": 0, "star_excluded": 0, "non_stock_excluded": 0}
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        counts["input"] += 1
        item = dict(raw)
        raw_code = str(item.get("code") or "")
        try:
            code = _normalise_code(raw_code)
        except ValueError:
            code = ""
        name = str(item.get("name") or "")
        if is_star_security(code, name):
            counts["star_excluded"] += 1
            continue
        if not is_a_share_security(code, name):
            counts["non_stock_excluded"] += 1
            continue
        item["code"] = code
        item["star_excluded"] = False
        item["universe_scope"] = "A股全市场（排除科创板）"
        kept.append(item)
    counts["kept"] = len(kept)
    return kept, counts


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    """Parse an optional market field without turning missing into numeric zero."""

    if value in (None, "", "-", "--"):
        return None
    try:
        result = float(str(value).replace(",", ""))
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(_float(value, float(default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"<[^>]+>", "", html.unescape(str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.values()
    return [item for item in (value or []) if isinstance(item, dict)]


def _coerce_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            result[key] = None
            continue
        value = value.strip() if isinstance(value, str) else value
        if value in ("", "None", "null", "NULL"):
            result[key] = None
        elif isinstance(value, str) and re.fullmatch(r"[-+]?\d+", value):
            try:
                result[key] = int(value)
            except ValueError:
                result[key] = value
        elif isinstance(value, str) and re.fullmatch(
            r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value
        ):
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
        else:
            result[key] = value
    return result


@dataclass
class _CacheEntry:
    value: Any
    source: str
    fetched_at: str
    stored_at: float
    expires_at: float
    fallback: bool = False
    errors: tuple[str, ...] = ()


@dataclass
class _CacheLookup:
    value: Any
    source: str
    fetched_at: str
    age_seconds: float
    stale: bool
    fallback: bool = False
    errors: tuple[str, ...] = ()


class TTLCache:
    """Small thread-safe LRU/TTL cache that can expose bounded stale values."""

    def __init__(self, max_items: int = 512) -> None:
        self.max_items = max(16, max_items)
        self._items: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._stale_hits = 0

    def get(
        self,
        key: str,
        *,
        allow_stale: bool = False,
        max_stale_seconds: float | None = None,
        clone: bool = True,
    ) -> _CacheLookup | None:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self._misses += 1
                return None
            stale = now >= entry.expires_at
            stale_age = max(0.0, now - entry.expires_at)
            if stale and (
                not allow_stale
                or (max_stale_seconds is not None and stale_age > max_stale_seconds)
            ):
                self._misses += 1
                if max_stale_seconds is not None and stale_age > max_stale_seconds:
                    self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            if stale:
                self._stale_hits += 1
            else:
                self._hits += 1
            return _CacheLookup(
                value=copy.deepcopy(entry.value) if clone else entry.value,
                source=entry.source,
                fetched_at=entry.fetched_at,
                age_seconds=max(0.0, now - entry.stored_at),
                stale=stale,
                fallback=entry.fallback,
                errors=entry.errors,
            )

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: float,
        source: str,
        *,
        fallback: bool = False,
        errors: Sequence[str] = (),
        clone: bool = True,
    ) -> _CacheLookup:
        now = time.monotonic()
        fetched_at = _now_iso()
        entry = _CacheEntry(
            value=copy.deepcopy(value) if clone else value,
            source=source,
            fetched_at=fetched_at,
            stored_at=now,
            expires_at=now + max(0.05, ttl_seconds),
            fallback=fallback,
            errors=tuple(errors),
        )
        with self._lock:
            self._items[key] = entry
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return _CacheLookup(
            copy.deepcopy(value) if clone else value,
            source, fetched_at, 0.0, False, fallback, tuple(errors)
        )

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "items": len(self._items),
                "hits": self._hits,
                "misses": self._misses,
                "stale_hits": self._stale_hits,
                "max_items": self.max_items,
            }


class _FFDMCPClient:
    """Small persistent MCP stdio client.

    The FFD key remains in the user's local FFD configuration.  This client
    starts the existing launcher and never copies the credential into the
    workbench database, source tree or logs.
    """

    def __init__(self, launcher: Path, timeout: float = 25.0) -> None:
        self.launcher = launcher
        self.timeout = max(3.0, float(timeout))
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._request_id = 0

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.launcher.is_file():
            raise ProviderError(f"FFD MCP launcher not found: {self.launcher}")
        self._process = subprocess.Popen(
            [sys.executable, str(self.launcher)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self._process

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is not None and process.poll() is None:
                process.terminate()

    def _readline(
        self,
        process: subprocess.Popen[str],
        *,
        timeout: float | None = None,
    ) -> str:
        effective_timeout = self.timeout if timeout is None else max(3.0, float(timeout))
        result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                assert process.stdout is not None
                result.put(process.stdout.readline())
            except BaseException as exc:  # pragma: no cover - OS pipe failure.
                result.put(exc)

        threading.Thread(target=read, daemon=True, name="ffd-mcp-read").start()
        try:
            value = result.get(timeout=effective_timeout)
        except queue.Empty as exc:
            self.close()
            raise TimeoutError(f"FFD MCP timed out after {effective_timeout:.0f}s") from exc
        if isinstance(value, BaseException):
            raise value
        if not value:
            self.close()
            raise ProviderError("FFD MCP closed its output stream")
        return value

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            process = self._start()
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments or {})},
            }
            assert process.stdin is not None
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response = json.loads(self._readline(process, timeout=timeout))
            if response.get("error"):
                raise ProviderError(str((response["error"] or {}).get("message") or "FFD MCP error"))
            blocks = ((response.get("result") or {}).get("content") or [])
            text = next(
                (str(block.get("text") or "") for block in blocks if block.get("type") == "text"),
                "",
            )
            if not text:
                raise ProviderError(f"FFD MCP returned no text for {name}")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ProviderError(f"FFD MCP returned an invalid payload for {name}")
            if int(payload.get("code") or 0) >= 400:
                raise ProviderError(str(payload.get("message") or payload.get("error_code") or name))
            return payload


class MarketDataProvider:
    """Normalised, cached and failure-tolerant public-market data access.

    FFD is the authoritative first source for the full-market snapshot, quotes,
    K-lines, breadth, auction, news and financial metrics. Local TDX and public
    HTTP feeds are compatibility fallbacks only, and every fallback remains
    explicit in result metadata. Public methods never require FastAPI and
    degrade to a stale cache or an explicit empty/starter payload instead of
    hiding failures.
    """

    DEFAULT_TTLS: Mapping[str, float] = {
        "quotes": 4,
        "kline": 300,
        "overview": 10,
        "ffd_breadth": 120,
        # Universe membership is stable, but its quote fields are intraday.
        # Keep this short so a pre-open zero snapshot cannot poison the session.
        "universe": 30,
        "boards": 10 * 60,
        "industries": 5 * 60,
        "fund_minute": 30,
        "fund_day": 15 * 60,
        "stock_info": 24 * 3600,
        "financial_metrics": 6 * 3600,
        "holders": 12 * 3600,
        "news": 5 * 60,
        "announcements": 10 * 60,
        "lhb": 15 * 60,
        "northbound": 60,
        "local_prediction": 60,
        "cninfo_orgs": 24 * 3600,
        "ths_hot": 10 * 60,
    }
    OPTIONAL_SOURCES = {
        "astocklab_csv",
        "cninfo_announcements",
        "cninfo_org_map",
        "eastmoney_boards",
        "eastmoney_fund_flow_daily",
        "eastmoney_fund_flow_minute",
        "eastmoney_holder_disclosure",
        "eastmoney_industry_clist",
        "eastmoney_lhb",
        "eastmoney_lhb_seats",
        "eastmoney_slist",
        "eastmoney_stock_info",
        "eastmoney_stock_news",
        "ffd_market_news",
        "ffd_stock_news",
        "ffd_financial_metrics",
        "qmt_universe",
        "qmt_tick",
        "qmt_daily_kline",
        "ths_northbound",
        "ths_hot_reason",
    }
    # A full A-share snapshot normally contains well over 5,000 securities.
    # Treat a smaller/mostly unusable local response as a broken snapshot and
    # let the single batched FFD market asset take over.
    FFD_DIRECT_MIN_USABLE_ROWS = 2500
    FFD_DIRECT_FIELD_COVERAGE = 0.50

    def __init__(
        self,
        astocklab_root: str | Path | None = None,
        *,
        timeout: float = 12,
        cache_ttls: Mapping[str, float] | None = None,
        session: requests.Session | None = None,
        eastmoney_min_interval: float | None = None,
    ) -> None:
        # 本地 AStockLab 仓库是可选的。默认路径只是猜测,不存在时会被
        # 上层按“未配置”处理,不会静默读到别的地方。
        default_root = Path.home() / "AStockLab"
        self.astocklab_root = Path(
            astocklab_root or os.environ.get("ASTOCKLAB_ROOT") or default_root
        ).expanduser()
        self.data_dir = self.astocklab_root / "data"
        self.tdx_vipdoc = Path(os.environ.get("GUPIAO_TDX_VIPDOC", "C:/new_tdx/vipdoc")).expanduser()
        self.tdx_root = self.tdx_vipdoc.parent
        self.tdx_cache_dir = self.tdx_root / "T0002" / "hq_cache"
        self.tdx_cloud_dir = self.tdx_root / "T0002" / "cloud_cfg"
        self.timeout = max(1.0, float(timeout))
        self.ttls = dict(self.DEFAULT_TTLS)
        if cache_ttls:
            self.ttls.update({key: max(0.05, float(value)) for key, value in cache_ttls.items()})

        self.cache = TTLCache()
        self._session = session or self._new_session()
        self._session_injected = session is not None
        self._thread_local = threading.local()
        self._thread_local.session = self._session
        self._sessions_lock = threading.RLock()
        self._sessions: list[requests.Session] = [self._session]
        self._em_session = self._new_session()
        self._http_lock = threading.RLock()
        self._em_lock = threading.RLock()
        self._em_last_call = 0.0
        self._last_universe_stats: dict[str, int] = {
            "input": 0,
            "kept": 0,
            "star_excluded": 0,
            "non_stock_excluded": 0,
        }
        configured_interval = os.environ.get("EASTMONEY_MIN_INTERVAL", "2.0")
        self._em_min_interval = max(
            0.0,
            float(configured_interval if eastmoney_min_interval is None else eastmoney_min_interval),
        )
        self._key_locks_lock = threading.RLock()
        self._key_locks: dict[str, threading.RLock] = {}
        self._state_lock = threading.RLock()
        self._source_state: dict[str, dict[str, Any]] = {}
        self._errors: deque[dict[str, Any]] = deque(maxlen=100)
        self._cninfo_lock = threading.RLock()
        self._cninfo_orgs: dict[str, str] = {}
        launcher_default = Path(__file__).resolve().parents[3] / "ffd_mcp_launcher.py"
        self.ffd_enabled = os.environ.get("XUNLONG_FFD_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
        }
        self.ffd_launcher = Path(
            os.environ.get("FFD_MCP_LAUNCHER", str(launcher_default))
        ).expanduser()
        ffd_timeout = float(os.environ.get("FFD_MCP_TIMEOUT", "25"))
        self._ffd = _FFDMCPClient(self.ffd_launcher, timeout=ffd_timeout)
        self._ffd_bulk_timeout = max(
            ffd_timeout,
            float(os.environ.get("FFD_MCP_BULK_TIMEOUT", "120")),
        )
        state_default = Path(__file__).resolve().parent.parent / "data" / "ffd_provider_state.json"
        self.ffd_state_path = Path(
            os.environ.get("XUNLONG_FFD_STATE", str(state_default))
        ).expanduser()
        self._ffd_state_lock = threading.RLock()
        self._ffd_daily_call_limit = max(
            1, int(os.environ.get("XUNLONG_FFD_DAILY_CALL_LIMIT", "6"))
        )
        self._auction_ffd_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._tdx_universe_cache: list[dict[str, Any]] | None = None
        self._tdx_universe_cache_at = 0.0
        self._tdx_universe_cache_date = ""
        self._tdx_universe_cache_lock = threading.RLock()
        self._last_maifu_news_status: dict[str, dict[str, Any]] = {}
        # One bounded FFD history batch can hydrate the strategy universe when
        # public Sina/Tencent K-line endpoints are rate-limited.  Keep it in
        # memory by (trade date, window, code) so the two scanners share the
        # same delivery without repeating the FFD request.
        self._ffd_kline_cache: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
        self._ffd_kline_lock = threading.RLock()
        # The stdio client is serial, but callers used to calculate cache misses
        # before waiting for it. A completed request could therefore be followed
        # by an unnecessary duplicate. Serialise the miss check and delivery.
        self._ffd_history_request_lock = threading.RLock()
        # Local QMT bridge: exchange-direct quotes/universe/klines with no call
        # budget. Preferred over FFD whenever the QMT client is logged in; the
        # wrapper degrades to QmtUnavailable so every chain below still works.
        self.qmt_enabled = os.environ.get("XUNLONG_QMT_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
        }
        self.qmt = QmtMarketData(
            enabled=self.qmt_enabled,
            path=os.environ.get("XUNLONG_QMT_PATH") or None,
            kline_enabled=os.environ.get("XUNLONG_QMT_KLINE", "1").strip().lower()
            not in {"0", "false", "off", "no"},
        )

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept": "*/*"})
        if Retry is not None:
            retry = Retry(
                total=2,
                connect=2,
                read=2,
                backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        return session

    def close(self) -> None:
        self._ffd.close()
        with self._sessions_lock:
            sessions = list(dict.fromkeys([*self._sessions, self._em_session]))
        for session in sessions:
            session.close()

    def __enter__(self) -> "MarketDataProvider":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _key_lock(self, key: str) -> threading.RLock:
        with self._key_locks_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    def _record_success(self, source: str) -> None:
        with self._state_lock:
            state = self._source_state.setdefault(source, {})
            state.update(
                {
                    "ok": True,
                    "optional": source in self.OPTIONAL_SOURCES,
                    "last_success": _now_iso(),
                    "consecutive_failures": 0,
                    "requests": int(state.get("requests", 0)) + 1,
                }
            )

    def _record_error(self, source: str, operation: str, exc: BaseException) -> str:
        message = f"{type(exc).__name__}: {str(exc)[:240]}"
        event = {
            "source": source,
            "operation": operation,
            "message": message,
            "time": _now_iso(),
        }
        with self._state_lock:
            self._errors.append(event)
            state = self._source_state.setdefault(source, {})
            state.update(
                {
                    "ok": False,
                    "optional": source in self.OPTIONAL_SOURCES,
                    "last_error": event,
                    "consecutive_failures": int(state.get("consecutive_failures", 0)) + 1,
                    "requests": int(state.get("requests", 0)) + 1,
                }
            )
        return message

    @staticmethod
    def _meta(
        lookup: _CacheLookup,
        *,
        cache_hit: bool,
        errors: Sequence[str] = (),
        fallback: bool = False,
    ) -> dict[str, Any]:
        is_fallback = fallback or lookup.fallback
        return {
            "source": lookup.source,
            "fetched_at": lookup.fetched_at,
            "age_seconds": round(lookup.age_seconds, 3),
            "stale": lookup.stale or is_fallback,
            "cache_hit": cache_hit,
            "fallback": is_fallback,
            "errors": [*lookup.errors, *errors],
        }

    def _cached_fetch(
        self,
        key: str,
        *,
        ttl: float,
        source: str,
        loader: Callable[[], Any],
        empty: Any,
        max_stale: float,
        fallback: Callable[[], Any] | None = None,
        fallback_source: str = "fallback",
        force: bool = False,
        clone: bool = True,
    ) -> tuple[Any, dict[str, Any]]:
        if not force:
            hit = self.cache.get(key, clone=clone)
            if hit is not None:
                return hit.value, self._meta(hit, cache_hit=True)
        with self._key_lock(key):
            if not force:
                hit = self.cache.get(key, clone=clone)
                if hit is not None:
                    return hit.value, self._meta(hit, cache_hit=True)
            try:
                value = loader()
                explicitly_stale = (
                    bool(value.get("stale"))
                    if isinstance(value, Mapping)
                    else any(bool(item.get("stale")) for item in value if isinstance(item, Mapping))
                    if isinstance(value, list)
                    else False
                )
                # A loader may return an intentionally marked local snapshot
                # after a remote sync failure. Retry it soon instead of caching
                # the known delay for the normal data TTL.
                effective_ttl = min(ttl, 15.0) if explicitly_stale else ttl
                lookup = self.cache.set(key, value, effective_ttl, source, clone=clone)
                self._record_success(source)
                return value, self._meta(lookup, cache_hit=False)
            except Exception as exc:  # Every public method owns its degradation path.
                message = self._record_error(source, key, exc)
                stale = self.cache.get(
                    key, allow_stale=True, max_stale_seconds=max_stale, clone=clone
                )
                if stale is not None:
                    return stale.value, self._meta(
                        stale, cache_hit=True, errors=(message,), fallback=True
                    )
                if fallback is not None:
                    try:
                        value = fallback()
                    except Exception as fallback_exc:
                        fallback_message = self._record_error(
                            fallback_source, key, fallback_exc
                        )
                        lookup = _CacheLookup(
                            copy.deepcopy(empty), fallback_source, _now_iso(), 0.0, True
                        )
                        return copy.deepcopy(empty), self._meta(
                            lookup,
                            cache_hit=False,
                            errors=(message, fallback_message),
                            fallback=True,
                        )
                    lookup = self.cache.set(
                        key,
                        value,
                        min(60.0, max(1.0, ttl)),
                        fallback_source,
                        fallback=True,
                        errors=(message,),
                        clone=clone,
                    )
                    return value, self._meta(lookup, cache_hit=False)
                lookup = _CacheLookup(
                    copy.deepcopy(empty), source, _now_iso(), 0.0, True
                )
                return copy.deepcopy(empty), self._meta(
                    lookup, cache_hit=False, errors=(message,), fallback=True
                )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        if self._session_injected:
            with self._http_lock:
                response = self._session.request(method, url, **kwargs)
        else:
            session = getattr(self._thread_local, "session", None)
            if session is None:
                session = self._new_session()
                self._thread_local.session = session
                with self._sessions_lock:
                    self._sessions.append(session)
            response = session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def _em_get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        headers = {"User-Agent": UA, **dict(kwargs.pop("headers", {}) or {})}
        with self._em_lock:
            wait = self._em_min_interval - (time.monotonic() - self._em_last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0.05, 0.18))
            try:
                last_error: Exception | None = None
                for attempt, delay in enumerate((0.0, 2.0, 6.0)):
                    if delay:
                        time.sleep(delay + random.uniform(0.1, 0.4))
                    try:
                        response = self._em_session.get(url, headers=headers, **kwargs)
                        response.raise_for_status()
                        return response
                    except requests.RequestException as exc:
                        last_error = exc
                raise last_error or ProviderError("Eastmoney request failed")
            finally:
                self._em_last_call = time.monotonic()

    def _universe_snapshot_path(self) -> Path:
        return self.data_dir / "market_universe_snapshot.json"

    def _save_universe_snapshot(self, rows: list[dict[str, Any]]) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # K-line history is already persisted in TDX .day files. Keeping
            # it again here inflated this cache to ~44 MB and made restarts
            # spend seconds parsing redundant JSON.
            compact_rows = [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"postclose_kline", "history"}
                }
                for row in rows
                if isinstance(row, Mapping)
            ]
            self._universe_snapshot_path().write_text(
                json.dumps(
                    {"saved_at": _now_iso(), "format": "quotes-v2", "rows": compact_rows},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load_universe_snapshot(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._universe_snapshot_path().read_text(encoding="utf-8"))
            rows = payload.get("rows", [])
            return [row for row in rows if isinstance(row, dict)]
        except (OSError, ValueError, TypeError):
            return []

    @staticmethod
    def _ffd_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        for key in ("raw_rows", "rows", "items"):
            raw_rows = payload.get(key)
            if isinstance(raw_rows, list):
                return [dict(row) for row in raw_rows if isinstance(row, Mapping)]
        data = payload.get("data")
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, Mapping)]
        if not isinstance(data, Mapping):
            return []
        for key in ("raw_rows", "rows", "items"):
            raw_rows = data.get(key)
            if isinstance(raw_rows, list):
                return [dict(row) for row in raw_rows if isinstance(row, Mapping)]
        columns = {str(key): value for key, value in data.items() if isinstance(value, list)}
        length = max((len(value) for value in columns.values()), default=0)
        return [
            {
                key: values[index] if index < len(values) else None
                for key, values in columns.items()
            }
            for index in range(length)
        ]

    def _read_ffd_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.ffd_state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write_ffd_state(self, state: Mapping[str, Any]) -> None:
        self.ffd_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ffd_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(state), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.ffd_state_path)

    @contextlib.contextmanager
    def _ffd_state_file_lock(self, timeout: float = 5.0):
        """跨进程保护 ffd_provider_state.json 的“读-改-写”序列。

        服务器、MCP 工具、手动诊断脚本都可能实例化 provider 并写同一个状态
        文件，仅靠进程内 RLock 无法防止预算被两个进程同时双花。获取不到锁时
        抛 TimeoutError，由调用方决定放弃（预算类）还是降级继续（读取类）。
        """

        if msvcrt is None:
            yield
            return
        lock_path = self.ffd_state_path.with_suffix(".lock")
        handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            if os.fstat(handle).st_size == 0:
                os.write(handle, b"\0")
            os.lseek(handle, 0, os.SEEK_SET)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("ffd state lock busy") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(handle, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(handle)

    def _record_ffd_unbudgeted(self, operation: str) -> None:
        """记录一次有意绕过每日预算闸门的 FFD 调用，让消耗在 health 可见。"""

        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self._ffd_state_file_lock():
                with self._ffd_state_lock:
                    state = self._read_ffd_state()
                    usage = state.get("unbudgeted_operations")
                    if not isinstance(usage, dict) or usage.get("date") != today:
                        usage = {"date": today, "operations": {}}
                    operations = usage.get("operations")
                    if not isinstance(operations, dict):
                        operations = {}
                    operations[operation] = int(operations.get(operation) or 0) + 1
                    usage["operations"] = operations
                    state["unbudgeted_operations"] = usage
                    self._write_ffd_state(state)
        except (TimeoutError, OSError):
            # 统计失败不能影响数据调用本身。
            pass

    def _reserve_ffd_call(self, operation: str, *, operation_limit: int) -> bool:
        if not self.ffd_enabled:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self._ffd_state_file_lock():
                with self._ffd_state_lock:
                    state = self._read_ffd_state()
                    budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
                    if budget.get("date") != today:
                        budget = {"date": today, "total": 0, "operations": {}}
                    operations = budget.get("operations")
                    if not isinstance(operations, dict):
                        operations = {}
                    if int(budget.get("total") or 0) >= self._ffd_daily_call_limit:
                        return False
                    if int(operations.get(operation) or 0) >= operation_limit:
                        return False
                    budget["total"] = int(budget.get("total") or 0) + 1
                    operations[operation] = int(operations.get(operation) or 0) + 1
                    budget["operations"] = operations
                    state["budget"] = budget
                    self._write_ffd_state(state)
                    return True
        except TimeoutError:
            # 拿不到跨进程锁时宁可放弃这次调用，也不能双花 FFD 预算。
            return False

    def _cached_ffd_daily_rows(self) -> list[dict[str, Any]]:
        with self._ffd_state_lock:
            state = self._read_ffd_state()
        daily = state.get("market_daily") if isinstance(state.get("market_daily"), dict) else {}
        rows = daily.get("rows")
        return [dict(row) for row in rows or [] if isinstance(row, Mapping)]

    def _ffd_daily_universe(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        """Build a full-market snapshot from the date-validated FFD baseline.

        The post-close scanners need a complete symbol universe before they can
        ask for per-symbol K-lines.  When the local TDX catalogue is absent,
        the Eastmoney fallback can legitimately return a syntactically valid
        but useless slice (for example only 920xxx symbols).  FFD's daily
        baseline is the only already-batched source in this process, so use it
        as the seed universe when its trade date is exactly the expected date.
        """

        with self._ffd_state_lock:
            state = self._read_ffd_state()
        daily = state.get("market_daily") if isinstance(state.get("market_daily"), dict) else {}
        actual_date = str(daily.get("trade_date") or "")[:10]
        if not actual_date or (trade_date and actual_date != str(trade_date)[:10]):
            return []
        rows = daily.get("rows")
        if not isinstance(rows, list):
            return []
        # FFD daily baseline 不含行业字段：优先本地通达信映射，缺失时用东财全市场接口补充
        industry_map = self._tdx_industry_map()
        if not industry_map:
            industry_map = self._eastmoney_industry_map()
        result: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            try:
                code = _normalise_code(str(raw.get("ts_code") or raw.get("code") or ""))
            except ValueError:
                continue
            close = _optional_float(raw.get("close"))
            if close is None or close <= 0 or not is_a_share_security(code, raw.get("name")):
                continue
            pre_close = _optional_float(raw.get("pre_close"))
            change = _optional_float(raw.get("change"))
            pct_chg = _optional_float(raw.get("pct_chg"))
            if pct_chg is None and pre_close:
                pct_chg = (close / pre_close - 1.0) * 100.0
            volume_ratio = _optional_float(raw.get("volume_ratio"))
            turnover_rate = _optional_float(
                raw.get("turnover_rate")
                if raw.get("turnover_rate") not in (None, "")
                else raw.get("turnover_pct")
            )
            float_market_cap = _optional_float(
                raw.get("float_market_cap")
                if raw.get("float_market_cap") not in (None, "")
                else raw.get("float_mcap")
            )
            result.append(
                {
                    "code": code,
                    "name": str(raw.get("name") or code),
                    "industry": industry_map.get(code, ""),
                    "price": close,
                    "change_pct": pct_chg or 0.0,
                    "change": change if change is not None else 0.0,
                    "volume": _float(raw.get("vol")),
                    "amount": _float(raw.get("amount")),
                    "volume_ratio": volume_ratio,
                    "turnover_rate": turnover_rate,
                    "turnover_pct": turnover_rate,
                    "turnover": turnover_rate,
                    "float_market_cap": float_market_cap,
                    "float_mcap": float_market_cap,
                    "ma60": _optional_float(raw.get("ma60")),
                    "ma120": _optional_float(raw.get("ma120")),
                    "ma250": _optional_float(raw.get("ma250")),
                    "adjust_factor": _optional_float(raw.get("adjust_factor")),
                    "trade_count": _optional_float(raw.get("trade_count")),
                    "open": _optional_float(raw.get("open")),
                    "high": _optional_float(raw.get("high")),
                    "low": _optional_float(raw.get("low")),
                    "last_close": pre_close,
                    "quote_time": actual_date,
                    "as_of": actual_date.replace("-", ""),
                    "available": True,
                    "source": "ffd_market_daily_universe",
                    "reference_source": "ffd_market_daily",
                    "ffd_trade_date": actual_date,
                    "ffd_daily": dict(raw),
                    "postclose_kline": [],
                    "history": [],
                    "stale": False,
                }
            )
        return result

    @classmethod
    def _needs_direct_ffd_universe(cls, rows: Iterable[Mapping[str, Any]]) -> bool:
        """Return whether a full-market response has lost most usable data."""

        items = [row for row in rows if isinstance(row, Mapping)]
        if not items:
            return True
        usable = sum(
            1
            for row in items
            if re.fullmatch(r"\d{6}", str(row.get("code") or row.get("ts_code") or ""))
            and bool(str(row.get("name") or "").strip())
            and (_optional_float(row.get("price") or row.get("close")) or 0.0) > 0
        )
        if usable < cls.FFD_DIRECT_MIN_USABLE_ROWS:
            return True
        for field in ("price", "change_pct", "amount"):
            present = sum(
                1
                for row in items
                if (_optional_float(row.get(field)) or 0.0) > 0
                or (field == "change_pct" and _optional_float(row.get(field)) is not None)
            )
            if present / max(1, len(items)) < cls.FFD_DIRECT_FIELD_COVERAGE:
                return True
        return False

    def _ffd_direct_universe(self, trade_date: str) -> list[dict[str, Any]]:
        """Fetch the requested full-market FFD asset when the cache is stale."""

        if not self.ffd_enabled or not trade_date:
            return []
        rows = self._ffd_daily_universe(trade_date)
        if not rows:
            try:
                synced = self.sync_ffd_market_daily(trade_date=trade_date)
            except Exception as exc:
                self._record_error("ffd_market_daily", "direct_universe", exc)
                return []
            if not isinstance(synced, Mapping) or not synced.get("ok"):
                return []
            rows = self._ffd_daily_universe(trade_date)
        if not rows:
            return []
        for row in rows:
            row["source"] = "ffd_market_daily_direct"
            row["reference_source"] = "ffd_market_daily"
            row["ffd_direct"] = True
            row["live_quote"] = False
            row["reference_quote"] = False
            row["stale"] = False
        return rows

    def _ffd_daily_breadth(self, trade_date: str) -> dict[str, Any]:
        """Build a date-valid breadth snapshot from the full daily baseline."""

        changes: list[float] = []
        for row in self._cached_ffd_daily_rows():
            if str(row.get("trade_date") or "")[:10] != trade_date:
                continue
            change = _optional_float(row.get("pct_chg"))
            if change is None:
                close = _optional_float(row.get("close"))
                pre_close = _optional_float(row.get("pre_close"))
                if close is not None and pre_close:
                    change = (close / pre_close - 1) * 100
            if change is not None:
                changes.append(change)
        if len(changes) < 1000:
            return {}
        ordered = sorted(changes)
        middle = len(ordered) // 2
        median_change = ordered[middle]
        if len(ordered) % 2 == 0:
            median_change = (ordered[middle - 1] + ordered[middle]) / 2
        advance = sum(1 for value in changes if value > 0)
        decline = sum(1 for value in changes if value < 0)
        flat = len(changes) - advance - decline
        return {
            "advance": advance,
            "decline": decline,
            "flat": flat,
            "limit_up_count": None,
            "limit_down_count": None,
            "median_change_pct": round(median_change, 6),
            "average_change_pct": round(sum(changes) / len(changes), 6),
            "total": len(changes),
            "trade_date": trade_date,
            "scope": "FFD A-share market daily baseline",
            "source": "ffd_market_daily",
            "stale": False,
            "ratio": round(advance / max(1, advance + decline), 4),
        }

    def sync_ffd_market_daily(self, trade_date: str | None = None, *, force: bool = False) -> dict[str, Any]:
        """Persist one full-market daily baseline without spending points at 09:26."""

        if not self.ffd_enabled:
            return {"ok": False, "skipped": True, "reason": "ffd_disabled"}
        requested = (
            str(trade_date)[:10]
            if trade_date
            else self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
        )
        # Startup refreshes and a simultaneous screener request can both notice
        # the same stale date. Keep the network call single-flight in-process.
        with self._key_lock(f"ffd_market_daily_sync:{requested}"):
            existing = self._read_ffd_state().get("market_daily") or {}
            if (
                not force
                and str(existing.get("trade_date") or "")[:10] == requested
                and existing.get("rows")
            ):
                return {
                    "ok": True,
                    "cached": True,
                    "trade_date": existing.get("trade_date"),
                    "rows": len(existing.get("rows") or []),
                }
            # A pre-close refresh targets yesterday.  It must not consume the
            # one opportunity to save today's completed baseline after 15:05.
            completed_today = (
                requested == datetime.now().strftime("%Y-%m-%d")
                and datetime.now().hour * 60 + datetime.now().minute >= 15 * 60 + 5
            )
            operation = "market_daily_postclose" if completed_today else "market_daily"
            if not self._reserve_ffd_call(operation, operation_limit=1 if completed_today else 2):
                return {"ok": False, "skipped": True, "reason": "local_ffd_budget"}

            payload = self._ffd.call(
                "ffd_market_daily",
                {
                    "trade_date": requested,
                    "page_size": 6000,
                    "unit_mode": "standard",
                    # Enhanced is still one full-market asset call, but keeps
                    # the fields needed by the screener (量比/换手率/流通市值)
                    # when FFD has to replace a broken local snapshot.
                    "profile": "enhanced",
                    "output_mode": "raw",
                    "format": "json",
                },
                timeout=self._ffd_bulk_timeout,
            )
            rows = self._ffd_rows(payload)
            valid = [
                row for row in rows if _optional_float(row.get("close")) is not None
            ]
            if len(valid) < 1000:
                raise ProviderError(
                    f"FFD market daily was incomplete ({len(valid)}/{len(rows)} rows with close)"
                )
            actual_date = str(valid[0].get("trade_date") or requested)[:10]
            if actual_date != requested:
                raise ProviderError(
                    f"FFD market daily returned {actual_date}, expected {requested}"
                )
            with self._ffd_state_lock:
                state = self._read_ffd_state()
                state["market_daily"] = {
                    "trade_date": actual_date,
                    "fetched_at": _now_iso(),
                    "rows": rows,
                }
                self._write_ffd_state(state)
            self._record_success("ffd_market_daily")
            return {"ok": True, "cached": False, "trade_date": actual_date, "rows": len(rows)}

    def get_ffd_float_mcap(self, trade_date: str) -> dict[str, float]:
        """One enhanced FFD daily call -> {code: float_market_cap} for the whole market.

        Used by the 一进二 scorer because the local TDX post-close universe does
        not carry float market cap. The per-date map is persisted in the FFD
        state file, so repeated scans on the same trade date cost zero budget.
        """

        if not self.ffd_enabled or not trade_date:
            return {}
        with self._ffd_state_lock:
            state = self._read_ffd_state()
            cache = state.get("float_mcap_cache") if isinstance(state.get("float_mcap_cache"), dict) else {}
            if cache.get("trade_date") == trade_date and cache.get("map"):
                return {str(k): float(v) for k, v in cache["map"].items()}

            # The enhanced full-market baseline already contains float market
            # cap. Reuse that persisted batch before spending another FFD call.
            # This is especially important after a service restart: the
            # in-memory stock-info cache is cold, while market_daily remains
            # complete and date-validated on disk.
            daily = state.get("market_daily") if isinstance(state.get("market_daily"), dict) else {}
            if str(daily.get("trade_date") or "")[:10] == str(trade_date)[:10]:
                result: dict[str, float] = {}
                for raw in daily.get("rows") or []:
                    if not isinstance(raw, Mapping):
                        continue
                    code = str(raw.get("ts_code") or raw.get("code") or "").split(".")[0]
                    value = _optional_float(raw.get("float_market_cap") or raw.get("float_mcap"))
                    if len(code) == 6 and code.isdigit() and value and value > 0:
                        result[code] = value
                if result:
                    state["float_mcap_cache"] = {"trade_date": trade_date, "map": result}
                    self._write_ffd_state(state)
                    return result
        if not self._reserve_ffd_call("float_mcap", operation_limit=2):
            return {}
        try:
            payload = self._ffd.call(
                "ffd_market_daily",
                {
                    "trade_date": trade_date,
                    "fields": "ts_code,float_market_cap",
                    "page_size": 6000,
                    "unit_mode": "standard",
                    "profile": "enhanced",
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            data = payload.get("data") if isinstance(payload, Mapping) else {}
            if not isinstance(data, Mapping):
                return {}
            codes = data.get("ts_code") or []
            caps = data.get("float_market_cap") or []
            result: dict[str, float] = {}
            for ts_code, cap in zip(codes, caps):
                code = str(ts_code or "").split(".")[0]
                value = _optional_float(cap)
                if code and value:
                    result[code] = value
            if result:
                with self._ffd_state_lock:
                    state = self._read_ffd_state()
                    state["float_mcap_cache"] = {"trade_date": trade_date, "map": result}
                    self._write_ffd_state(state)
            self._record_success("ffd_float_mcap")
            return result
        except Exception as exc:
            self._record_error("ffd_float_mcap", "float_mcap", exc)
            return {}

    def get_ffd_limit_pool(self, trade_date: str) -> dict[str, Any]:
        """One ffd_limit_pool call -> {by_code, stats} for the trade date.

        炸板次数(open_count)、首次涨停时间(first_limit_time)、连板数与涨停题材
        都来自这个池；结果按交易日缓存进 FFD state，重复扫描零预算消耗。
        """

        if not self.ffd_enabled or not trade_date:
            return {}
        with self._ffd_state_lock:
            state = self._read_ffd_state()
            cache = state.get("limit_pool_cache") if isinstance(state.get("limit_pool_cache"), dict) else {}
            if cache.get("trade_date") == trade_date and cache.get("payload"):
                return dict(cache["payload"])
        if not self._reserve_ffd_call("limit_pool", operation_limit=2):
            return {}
        try:
            payload = self._ffd.call(
                "ffd_limit_pool",
                {"trade_date": trade_date, "requested_pool": "all", "format": "json"},
            )
            # ffd_limit_pool 的行可能挂在顶层或 data 子对象下，两种结构都兼容。
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
            if not isinstance(data, Mapping):
                return {}
            rows = data.get("rows") or payload.get("rows") or []
            by_code: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                code = str(row.get("ts_code") or "").split(".")[0]
                if code and row.get("pool_type") == "limit_up":
                    by_code[code] = dict(row)
            result = {
                "by_code": by_code,
                "stats": {
                    "limit_up_count": data.get("limit_up_count") or payload.get("limit_up_count"),
                    "broken_count": data.get("broken_count") or payload.get("broken_count"),
                    "multi_board_count": data.get("multi_board_count") or payload.get("multi_board_count"),
                    "max_lianban_count": data.get("max_lianban_count") or payload.get("max_lianban_count"),
                    "board_ladder": data.get("board_ladder") or payload.get("board_ladder") or {},
                    "complete": bool(data.get("complete") or payload.get("complete")),
                },
            }
            if by_code:
                with self._ffd_state_lock:
                    state = self._read_ffd_state()
                    state["limit_pool_cache"] = {"trade_date": trade_date, "payload": result}
                    self._write_ffd_state(state)
            self._record_success("ffd_limit_pool")
            return result
        except Exception as exc:
            self._record_error("ffd_limit_pool", "limit_pool", exc)
            return {}

    def get_ffd_market_breadth(
        self, *, force: bool = False, allow_network: bool = True
    ) -> dict[str, Any]:
        # Outside trading hours (especially weekends), "today" may not be a
        # trading date.  Compare against the latest completed session so a
        # Friday breadth snapshot is not incorrectly labelled stale on
        # Saturday/Sunday.
        today = self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
        with self._ffd_state_lock:
            state = self._read_ffd_state()
        saved = state.get("market_breadth") if isinstance(state.get("market_breadth"), dict) else {}
        try:
            age = (
                datetime.now(timezone.utc).astimezone()
                - datetime.fromisoformat(str(saved.get("fetched_at")))
            ).total_seconds()
        except (TypeError, ValueError):
            age = float("inf")
        if (
            not force
            and saved.get("trade_date") == today
            and isinstance(saved.get("data"), dict)
            and age <= self.ttls["ffd_breadth"]
        ):
            return {**saved["data"], "cached": True, "age_seconds": round(age, 1)}
        if not allow_network or not self._reserve_ffd_call("market_breadth", operation_limit=4):
            if isinstance(saved.get("data"), dict):
                return {**saved["data"], "cached": True, "stale": True}
            return {}
        try:
            payload = self._ffd.call(
                "ffd_market_breadth",
                {
                    "trade_date": today,
                    "universe": "A股",
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = self._ffd_rows(payload)
            row = rows[0] if rows else {}
            if not row or _int(row.get("total_count")) < 1000:
                raise ProviderError("FFD market breadth was empty or incomplete")
            data = {
                "advance": _int(row.get("up_count")),
                "decline": _int(row.get("down_count")),
                "flat": _int(row.get("flat_count")),
                "limit_up_count": _int(row.get("limit_up_count")),
                "limit_down_count": _int(row.get("limit_down_count")),
                "median_change_pct": _optional_float(row.get("median_pct_chg")),
                "average_change_pct": _optional_float(row.get("avg_pct_chg")),
                "total": _int(row.get("total_count")),
                "trade_date": str(row.get("trade_date") or today),
                "scope": "FFD A-share market",
                "source": "ffd_market_breadth",
                "stale": False,
            }
            data["ratio"] = round(
                data["advance"] / max(1, data["advance"] + data["decline"]), 4
            )
            with self._ffd_state_lock:
                state = self._read_ffd_state()
                state["market_breadth"] = {
                    "trade_date": today,
                    "fetched_at": _now_iso(),
                    "data": data,
                }
                self._write_ffd_state(state)
            self._record_success("ffd_market_breadth")
            return data
        except Exception as exc:
            self._record_error("ffd_market_breadth", "market_breadth", exc)
            if isinstance(saved.get("data"), dict):
                return {**saved["data"], "cached": True, "stale": True}
            return {}

    def prewarm_ffd(self, task: str = "preopen") -> dict[str, Any]:
        if task == "daily":
            try:
                return self.sync_ffd_market_daily()
            except Exception as exc:
                message = self._record_error("ffd_market_daily", "daily_sync", exc)
                return {
                    "ok": False,
                    "task": task,
                    "fallback": "local_tdx",
                    "reason": message,
                }
        breadth = self.get_ffd_market_breadth(force=True, allow_network=True)
        return {"ok": bool(breadth), "task": task, "breadth": breadth}

    def _merge_ffd_daily(self, live_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        daily_rows = self._cached_ffd_daily_rows()
        expected_date = self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
        available_dates = {
            str(row.get("trade_date") or "")[:10]
            for row in daily_rows
            if isinstance(row, Mapping) and row.get("trade_date")
        }
        # Never attach an old FFD baseline to today's universe.  The local TDX
        # snapshot remains usable, but its source must stay explicitly local.
        local_dates = {
            str(item.get("as_of") or item.get("data_as_of") or item.get("trade_date") or "")[:10]
            for item in live_rows
            if isinstance(item, Mapping)
            and (item.get("as_of") or item.get("data_as_of") or item.get("trade_date"))
        }
        if local_dates and (not available_dates or max(available_dates) < expected_date):
            return live_rows, False
        daily_by_code: dict[str, dict[str, Any]] = {}
        for row in daily_rows:
            code_value = str(row.get("ts_code") or row.get("code") or "")
            match = re.search(r"\d{6}", code_value)
            if match:
                daily_by_code[match.group(0)] = row
        if not daily_by_code:
            return live_rows, False
        merged: list[dict[str, Any]] = []
        for item in live_rows:
            row = dict(item)
            daily = daily_by_code.get(str(row.get("code") or ""))
            if daily:
                row["name"] = row.get("name") or daily.get("name") or ""
                row["ffd_trade_date"] = daily.get("trade_date")
                row["ffd_daily"] = {
                    key: daily.get(key)
                    for key in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "change",
                        "pct_chg",
                        "vol",
                        "amount",
                    )
                }
                row["reference_source"] = "ffd_market_daily"
            row["source"] = "tdx_postclose_day+ffd_daily_reference"
            merged.append(row)
        return merged, True

    def _datacenter(
        self,
        report_name: str,
        *,
        columns: str = "ALL",
        filter_str: str = "",
        page_size: int = 50,
        sort_columns: str = "",
        sort_types: str = "-1",
    ) -> list[dict[str, Any]]:
        params = {
            "reportName": report_name,
            "columns": columns,
            "filter": filter_str,
            "pageNumber": "1",
            "pageSize": str(max(1, min(page_size, 6000))),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        }
        payload = self._em_get(EASTMONEY_DATACENTER, params=params).json()
        result = payload.get("result") or {}
        rows = result.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _decorate_dict(data: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(data))
        result.setdefault("source", meta["source"])
        result.setdefault("fetched_at", meta["fetched_at"])
        result["stale"] = bool(result.get("stale") or meta.get("stale"))
        result["_meta"] = copy.deepcopy(dict(meta))
        return result

    @staticmethod
    def _decorate_rows(
        rows: Iterable[Mapping[str, Any]], meta: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        decorated = []
        for row in rows:
            item = copy.deepcopy(dict(row))
            item.setdefault("source", meta["source"])
            item.setdefault("fetched_at", meta["fetched_at"])
            item["stale"] = bool(item.get("stale") or meta.get("stale"))
            decorated.append(item)
        return decorated

    def _tencent_symbols(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        unique = list(dict.fromkeys(symbols))
        for offset in range(0, len(unique), 80):
            chunk = unique[offset : offset + 80]
            response = self._request(
                "GET",
                "https://qt.gtimg.cn/q=" + ",".join(chunk),
                headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
            )
            raw = response.content.decode("gbk", errors="replace")
            for line in raw.split(";"):
                if "=" not in line or '"' not in line:
                    continue
                symbol_match = re.search(r"v_([a-z]{2}\d{6})", line, re.I)
                quoted = re.search(r'"(.*)"', line, re.S)
                if not symbol_match or not quoted:
                    continue
                symbol = symbol_match.group(1).lower()
                values = quoted.group(1).split("~")
                if len(values) < 53 or not values[1]:
                    continue
                bids = [
                    {"price": _float(values[i]), "volume": _float(values[i + 1])}
                    for i in range(9, 19, 2)
                    if i + 1 < len(values)
                ]
                asks = [
                    {"price": _float(values[i]), "volume": _float(values[i + 1])}
                    for i in range(19, 29, 2)
                    if i + 1 < len(values)
                ]
                bid1_price = _float(bids[0].get("price")) if bids else 0.0
                ask1_price = _float(asks[0].get("price")) if asks else 0.0
                virtual_price = (
                    bid1_price
                    if bid1_price > 0 and abs(bid1_price - ask1_price) < 0.0001
                    else _float(values[3])
                )
                quote_time = values[30] if len(values) > 30 else ""
                # Tencent field 6 is auction-matched volume only during the
                # opening-call window; after 09:30 it becomes full-day volume.
                auction_snapshot = False
                if re.fullmatch(r"\d{14}", quote_time):
                    hhmmss = int(quote_time[-6:])
                    auction_snapshot = 91500 <= hhmmss <= 93000
                result[symbol] = {
                    "code": symbol[2:],
                    "symbol": symbol,
                    "name": values[1],
                    "price": _float(values[3]),
                    "last_close": _float(values[4]),
                    "open": _float(values[5]),
                    "volume_lots": _float(values[6]),
                    "auction_price": virtual_price,
                    "auction_volume_lots": _float(values[6]) if auction_snapshot else 0.0,
                    # Tencent's field 6 is lots in the opening auction.  Its
                    # amount field may be blank until the match is final, so
                    # derive turnover from matched lots and indicative price.
                    # Never reuse the full-day amount outside this window.
                    "auction_amount": (
                        _float(values[6]) * 100.0 * virtual_price
                        if auction_snapshot and virtual_price > 0
                        else 0.0
                    ),
                    "auction_unmatched_buy_lots": _float(values[12]) if auction_snapshot and len(values) > 12 else 0.0,
                    "auction_unmatched_sell_lots": _float(values[22]) if auction_snapshot and len(values) > 22 else 0.0,
                    "external_volume_lots": _float(values[7]),
                    "internal_volume_lots": _float(values[8]),
                    "bids": bids,
                    "asks": asks,
                    "change": _float(values[31]),
                    "change_pct": _float(values[32]),
                    "high": _float(values[33]),
                    "low": _float(values[34]),
                    "amount_wan": _float(values[37]),
                    "turnover_pct": _float(values[38]),
                    "pe_ttm": _float(values[39]),
                    "amplitude_pct": _float(values[43]),
                    "mcap_yi": _float(values[44]),
                    "float_mcap_yi": _float(values[45]),
                    "pb": _float(values[46]),
                    "limit_up": _float(values[47]),
                    "limit_down": _float(values[48]),
                    "volume_ratio": _float(values[49]),
                    "pe_static": _float(values[52]),
                    "quote_time": quote_time,
                    "available": True,
                }
        if not result:
            raise ProviderError("Tencent quote response contained no usable records")
        return result

    def _tencent_symbols_parallel(
        self, symbols: Sequence[str], *, workers: int = 16
    ) -> dict[str, dict[str, Any]]:
        """Fetch a large symbol set concurrently in Tencent's small batches."""

        unique = list(dict.fromkeys(symbols))
        chunks = [unique[index : index + 80] for index in range(0, len(unique), 80)]
        if not chunks:
            return {}
        result: dict[str, dict[str, Any]] = {}
        errors: list[Exception] = []
        with ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(chunks))),
            thread_name_prefix="tencent-quotes",
        ) as pool:
            futures = [pool.submit(self._tencent_symbols, chunk) for chunk in chunks]
            for future in as_completed(futures):
                try:
                    result.update(future.result())
                except Exception as exc:
                    errors.append(exc)
        if not result and errors:
            raise ProviderError(f"Tencent full-market quotes failed: {errors[-1]}")
        return result

    def _qmt_quote_snapshot(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Local QMT tick snapshot keyed by prefixed symbol ("sh600519").

        Partial coverage is intentional: suspended/absent codes simply stay
        absent so get_quotes decorates them as unavailable instead of failing
        the whole batch the way the strict FFD contract does.  During the live
        session a tick from a previous date is dropped so cached prior-session
        prices can never masquerade as live quotes.
        """

        try:
            ticks = self.qmt.full_tick(codes)
        except QmtUnavailable as exc:
            self._record_error("qmt_tick", "quote_batch", exc)
            raise
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        live_window = self._is_live_index_window(now)
        by_symbol: dict[str, dict[str, Any]] = {}
        for code, row in ticks.items():
            if live_window and str(row.get("trade_date") or "")[:10] != today:
                continue
            symbol = _market_prefix(code) + code
            by_symbol[symbol] = {**row, "symbol": symbol}
        self._record_success("qmt_tick")
        return by_symbol

    def _ffd_quote_snapshot(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return FFD quotes, using one live batch or the daily market asset."""

        normalised = list(dict.fromkeys(_normalise_code(code) for code in codes))
        now = datetime.now()
        live = self._is_live_index_window(now)
        by_code: dict[str, dict[str, Any]] = {}

        if live and len(normalised) <= 200:
            standards = [_ffd_standard_code(code) for code in normalised]
            try:
                snapshots = self._ffd_intraday_snapshot(standards)
            except Exception:
                snapshots = {}
            for code, standard in zip(normalised, standards):
                row = snapshots.get(standard)
                if not row:
                    continue
                amount = _float(row.get("amount"))
                volume = _float(row.get("volume"))
                symbol = _market_prefix(code) + code
                by_code[symbol] = {
                    **row,
                    "code": code,
                    "symbol": symbol,
                    "volume_lots": volume / 100.0,
                    "amount_wan": amount / 10_000.0,
                    "data_as_of": row.get("quote_time") or row.get("trade_date") or "",
                    "available": True,
                    "source": "ffd_intraday_snapshot",
                    "ffd_direct": True,
                    "stale": False,
                }

        missing = [
            code for code in normalised if _market_prefix(code) + code not in by_code
        ]
        target_date = (
            now.strftime("%Y-%m-%d")
            if live
            else self._latest_completed_trade_date(now).strftime("%Y-%m-%d")
        )
        rows = self._ffd_direct_universe(target_date) if missing else []
        for row in rows:
            code = str(row.get("code") or "")
            if code not in missing:
                continue
            amount = _float(row.get("amount"))
            volume = _float(row.get("volume"))
            symbol = _market_prefix(code) + code
            by_code[symbol] = {
                "code": code,
                "symbol": symbol,
                "name": row.get("name") or code,
                "price": row.get("price"),
                "last_close": row.get("last_close"),
                "previous_close": row.get("last_close"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "volume": volume,
                "volume_lots": volume / 100.0,
                "amount": amount,
                "amount_wan": amount / 10_000.0,
                "change": row.get("change"),
                "change_pct": row.get("change_pct"),
                "turnover": row.get("turnover"),
                "turnover_pct": row.get("turnover_pct"),
                "volume_ratio": row.get("volume_ratio"),
                "float_market_cap": row.get("float_market_cap"),
                "float_mcap": row.get("float_mcap"),
                "quote_time": row.get("quote_time") or target_date,
                "data_as_of": row.get("quote_time") or target_date,
                "trade_date": row.get("ffd_trade_date") or target_date,
                "available": True,
                "source": "ffd_market_daily_direct",
                "ffd_direct": True,
                "stale": False,
            }
        if not by_code:
            raise ProviderError("FFD quote snapshot contained no requested records")
        if any(_market_prefix(code) + code not in by_code for code in normalised):
            raise ProviderError("FFD quote snapshot did not cover every requested code")
        return by_code

    def get_quotes(
        self, codes: Iterable[str | int], *, force: bool = False
    ) -> dict[str, dict[str, Any]]:
        normalised = list(dict.fromkeys(_normalise_code(code) for code in codes))
        if not normalised:
            return {}
        symbols = [_market_prefix(code) + code for code in normalised]
        key = "quotes:" + ",".join(sorted(symbols))

        def load_quote_snapshot() -> dict[str, dict[str, Any]]:
            # QMT ticks are exchange-direct and budget-free; whenever the local
            # client is logged in they outrank the budgeted FFD snapshot.
            if self.qmt_enabled:
                try:
                    return self._qmt_quote_snapshot(normalised)
                except QmtUnavailable:
                    pass
            return self._ffd_quote_snapshot(normalised)

        data, meta = self._cached_fetch(
            key,
            ttl=self.ttls["quotes"],
            source="ffd_quote_primary",
            loader=load_quote_snapshot,
            empty={},
            max_stale=5 * 60,
            fallback=lambda: self._tencent_symbols(symbols),
            fallback_source="tencent_quote",
            force=force,
        )
        result: dict[str, dict[str, Any]] = {}
        for code, symbol in zip(normalised, symbols):
            quote = data.get(symbol, {}) if isinstance(data, dict) else {}
            if not quote:
                quote = {"code": code, "symbol": symbol, "available": False}
            decorated = self._decorate_dict(quote, meta)
            decorated["data_as_of"] = _exchange_iso(decorated.get("quote_time"))
            decorated["trade_date"] = _exchange_trade_date(decorated.get("quote_time"))
            decorated["server_time"] = meta.get("fetched_at")
            result[code] = decorated
        return result

    def get_auction_quotes(
        self, codes: Iterable[str | int], *, force: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Return auction data with FFD -> Tencent -> Eastmoney fallback.

        FFD publishes the authoritative 09:25 terminal snapshot. During the
        live 09:15-09:25 window that asset is not final yet, so Tencent is used
        for the virtual match quote and Eastmoney is the last public fallback.
        Every result keeps its source and freshness metadata.
        """

        normalised = list(dict.fromkeys(_normalise_code(code) for code in codes))
        if not normalised:
            return {}
        now = datetime.now()
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or minute < 9 * 60 + 15:
            reason = "non-trading day" if now.weekday() >= 5 else "opening auction has not started"
            return {
                code: {
                    "code": code,
                    "available": False,
                    "stale": True,
                    "trade_date": now.strftime("%Y-%m-%d"),
                    "source": "unavailable",
                    "auction_rejected_reason": reason,
                }
                for code in normalised
            }
        ffd_rows: dict[str, dict[str, Any]] = {}
        ffd_cache_key = f"{now:%Y-%m-%d}:" + ",".join(normalised)
        cached_ffd = self._auction_ffd_cache.get(ffd_cache_key)
        if cached_ffd and time.monotonic() - cached_ffd[0] < 300:
            ffd_rows = copy.deepcopy(cached_ffd[1])
        # QMT ticks are exchange-direct and budget-free.  When the client is
        # logged in they verify the auction for the whole request without
        # spending any FFD call, so the daily budget stays available for the
        # assets only FFD can deliver (breadth, limit pool, ...).
        if not ffd_rows and self.qmt_enabled and now.weekday() < 5:
            try:
                qmt_rows = self.qmt.auction_rows(normalised, now=now)
            except QmtUnavailable as exc:
                self._record_error("qmt_tick", "call_auction", exc)
            else:
                self._record_success("qmt_tick")
                ffd_rows.update({
                    code: row for code, row in qmt_rows.items()
                    if _float(row.get("auction_price")) > 0
                    and _float(row.get("auction_amount")) > 0
                })
                if ffd_rows:
                    self._auction_ffd_cache[ffd_cache_key] = (time.monotonic(), copy.deepcopy(ffd_rows))
        # Do not spend an FFD call before its 09:25 final asset can exist.
        # The local budget guard also prevents a three-second UI poll from
        # turning into an unbounded upstream request stream.
        if (
            self.ffd_enabled
            and now.weekday() < 5
            and now.hour * 60 + now.minute >= 9 * 60 + 25
            and not ffd_rows
            and self._reserve_ffd_call("call_auction", operation_limit=4)
        ):
            try:
                symbols = [
                    f"{code}.{'SH' if _market_prefix(code) == 'sh' else 'SZ' if _market_prefix(code) == 'sz' else 'BJ'}"
                    for code in normalised
                ]
                payload = self._ffd.call(
                    "ffd_market_microstructure",
                    {
                        "task": "call_auction",
                        "symbols": symbols,
                        "trade_date": now.strftime("%Y-%m-%d"),
                        "format": "json",
                    },
                )
                data = payload.get("data") if isinstance(payload, Mapping) else {}
                if isinstance(data, list):
                    rows = data
                elif isinstance(data, Mapping):
                    rows = data.get("rows") or data.get("row") or data.get("data") or []
                    if not isinstance(rows, list):
                        rows = [rows]
                else:
                    rows = []
                for row in rows or []:
                    if not isinstance(row, Mapping):
                        continue
                    match = re.search(r"(\d{6})", str(row.get("code") or ""))
                    if not match:
                        continue
                    code = match.group(1)
                    price = _float(row.get("auction_price"))
                    if price <= 0:
                        price = _float(row.get("indicative_price"))
                    volume = _float(row.get("auction_volume"))
                    if volume <= 0:
                        volume = _float(row.get("matched_volume"))
                    amount = _float(row.get("auction_amount"))
                    if amount <= 0:
                        amount = _float(row.get("matched_amount"))
                    data_status = str(row.get("data_status") or "final").strip().lower()
                    terminal_no_event = data_status in {
                        "complete_no_event",
                        "no_event",
                        "终态无成交",
                    }
                    valid_terminal = data_status in {
                        "final",
                        "complete",
                        "completed",
                        "终态",
                    }
                    # A verified no-event row is authoritative too: do not
                    # replace it with a stale live quote. Missing/invalid rows
                    # remain eligible for the Tencent/Eastmoney fallback.
                    if not terminal_no_event and (price <= 0 or amount <= 0 or not valid_terminal):
                        continue
                    ffd_rows[code] = {
                        "code": code,
                        "name": row.get("name") or code,
                        "price": price,
                        "auction_price": price,
                        "last_close": _float(row.get("pre_close") or row.get("prev_close")),
                        "auction_volume_lots": volume / 100.0 if volume > 0 else 0.0,
                        "auction_amount": amount,
                        "change_pct": _float(row.get("auction_gain_pct") or row.get("indicative_change_pct") or row.get("change_pct")),
                        "quote_time": row.get("asset_as_of") or row.get("updated_at") or "",
                        "data_as_of": row.get("asset_as_of") or row.get("updated_at") or "",
                        "trade_date": row.get("trade_date") or now.strftime("%Y-%m-%d"),
                        "source": "ffd_market_microstructure",
                        "auction_source": "ffd_market_microstructure",
                        "auction_stage": row.get("auction_stage") or "opening_call_auction_final",
                        "auction_data_status": data_status,
                        "field_status": row.get("field_status") or {},
                        "field_coverage": row.get("field_coverage") or {},
                        "available": bool(valid_terminal and price > 0),
                        "ffd_terminal": True,
                        "terminal_no_event": terminal_no_event,
                        "stale": False,
                    }
            except Exception as exc:
                self._record_error("ffd_market_microstructure", "call_auction", exc)
            if ffd_rows:
                self._auction_ffd_cache[ffd_cache_key] = (time.monotonic(), copy.deepcopy(ffd_rows))

        missing = [code for code in normalised if code not in ffd_rows]
        tencent_rows: dict[str, dict[str, Any]] = {}
        if missing:
            try:
                # get_quotes() may prefer the last completed FFD daily close.
                # The opening-auction route must request Tencent's live wire
                # format directly, including its auction lots and timestamp.
                live = self._tencent_symbols([_market_prefix(code) + code for code in missing])
                tencent_rows = {
                    code: dict(live.get(_market_prefix(code) + code) or {})
                    for code in missing
                }
            except Exception as exc:
                self._record_error("tencent_auction_quote", "call_auction", exc)
        result = {**ffd_rows}
        for code in missing:
            row = dict(tencent_rows.get(code) or {})
            # A regular FFD daily close is a valid quote fallback, but it is
            # not an opening-auction match. Keep it out of this specialised
            # route so the verified auction semantics remain intact.
            if (
                row
                and row.get("available")
                and _float(row.get("price")) > 0
                and not bool(row.get("stale"))
            ):
                row.setdefault("source", "tencent_quote")
                row["auction_source"] = "tencent_quote"
                row["auction_price"] = _float(row.get("auction_price")) or _float(row.get("price"))
                row["auction_volume_lots"] = _float(row.get("auction_volume_lots"))
                row["auction_amount"] = _float(row.get("auction_amount"))
                if row["auction_amount"] <= 0 and row["auction_volume_lots"] > 0:
                    row["auction_amount"] = row["auction_volume_lots"] * 100.0 * row["auction_price"]
                row["auction_unmatched_buy_lots"] = _float(row.get("auction_unmatched_buy_lots"))
                row["auction_unmatched_sell_lots"] = _float(row.get("auction_unmatched_sell_lots"))
                result[code] = row

        missing = [code for code in normalised if code not in result]
        if missing:
            try:
                result.update(self._eastmoney_auction_quotes(missing))
            except Exception as exc:
                self._record_error("eastmoney_auction_quote", "call_auction", exc)
        validated: dict[str, dict[str, Any]] = {}
        for code in normalised:
            row = dict(result.get(code) or {"code": code, "available": False})
            valid, reason = _auction_quote_is_current(row, now)
            if valid:
                validated[code] = row
            else:
                validated[code] = {
                    "code": code,
                    "available": False,
                    "stale": True,
                    "auction_rejected_reason": reason,
                    "trade_date": _exchange_trade_date(row.get("trade_date"))
                    or _exchange_trade_date(row.get("data_as_of"))
                    or _exchange_trade_date(row.get("quote_time")),
                    "source": row.get("source") or "unavailable",
                }
        return validated

    def _eastmoney_auction_quotes(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Best-effort batch fallback using Eastmoney's public market list."""

        params = {
            "pn": "1",
            "pz": "6000",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f2,f3,f4,f5,f6,f12,f14,f17,f18",
        }
        payload = self._em_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params=params,
            headers={"Referer": "https://quote.eastmoney.com/"},
        ).json()
        wanted = set(codes)
        rows: dict[str, dict[str, Any]] = {}
        for item in _items((payload.get("data") or {}).get("diff")):
            parsed = self._parse_universe_item(item)
            code = str(parsed.get("code") or "")
            if code not in wanted:
                continue
            price = _float(parsed.get("price"))
            rows[code] = {
                **parsed,
                "auction_price": price,
                # Eastmoney's public list is a current snapshot, not a
                # verified opening-auction match asset. Keep its regular
                # volume/amount fields for context but never label them as
                # auction turnover or matched lots.
                "auction_volume_lots": 0.0,
                "auction_amount": 0.0,
                "auction_data_status": "snapshot_only",
                "source": "eastmoney_clist",
                "auction_source": "eastmoney_clist",
                "data_as_of": datetime.now().isoformat(timespec="seconds"),
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "available": price > 0,
                "stale": False,
            }
        return rows

    def get_quote(self, code: str | int) -> dict[str, Any]:
        normalised = _normalise_code(code)
        return self.get_quotes([normalised])[normalised]

    def _ffd_intraday_snapshot(
        self, standard_codes: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch one bounded FFD snapshot batch without per-symbol fan-out."""

        if not self.ffd_enabled:
            raise ProviderError("FFD is disabled")
        requested = list(
            dict.fromkeys(str(code or "").strip().upper() for code in standard_codes)
        )
        requested = [code for code in requested if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code)]
        if not requested:
            return {}
        self._record_ffd_unbudgeted("intraday_snapshot_batch")
        try:
            payload = self._ffd.call(
                "ffd_intraday_snapshot",
                {
                    "codes": ",".join(requested),
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = self._ffd_rows(payload)
            expected = set(requested)
            by_digits = {code[:6]: code for code in requested}
            result: dict[str, dict[str, Any]] = {}
            for raw in rows:
                raw_code = str(
                    raw.get("code")
                    or raw.get("ts_code")
                    or raw.get("symbol")
                    or raw.get("证券代码")
                    or ""
                ).upper()
                match = re.search(r"(\d{6})(?:\.(SH|SZ|BJ))?", raw_code)
                if not match:
                    continue
                standard = (
                    f"{match.group(1)}.{match.group(2)}"
                    if match.group(2)
                    else by_digits.get(match.group(1), "")
                )
                if standard not in expected:
                    continue

                def value(*keys: str) -> Any:
                    return next(
                        (raw.get(key) for key in keys if raw.get(key) not in (None, "")),
                        None,
                    )

                price = _optional_float(value("latest", "price", "close", "最新价"))
                if price is None or price <= 0:
                    continue
                previous_close = _optional_float(
                    value("pre_close", "previous_close", "last_close", "昨收")
                )
                change = _optional_float(value("change", "changeAmount", "涨跌额"))
                change_pct = _optional_float(
                    value("changeRatio", "pct_chg", "change_pct", "涨跌幅")
                )
                if change_pct is None and previous_close:
                    change_pct = (price / previous_close - 1.0) * 100.0
                quote_time = str(
                    value(
                        "time", "datetime", "trade_time", "update_time",
                        "timestamp", "trade_date", "date",
                    )
                    or ""
                )
                result[standard] = {
                    "code": standard[:6],
                    "standard_code": standard,
                    "name": str(value("name", "股票简称", "证券简称") or standard[:6]),
                    "price": price,
                    "last_close": previous_close,
                    "previous_close": previous_close,
                    "open": _optional_float(value("open", "开盘价")),
                    "high": _optional_float(value("high", "最高价")),
                    "low": _optional_float(value("low", "最低价")),
                    "volume": _float(value("volume", "vol", "成交量")),
                    "amount": _float(value("amount", "amt", "成交额")),
                    "change": change,
                    "change_pct": change_pct,
                    "turnover": _optional_float(value("turnover_rate", "turnover", "换手率")),
                    "turnover_pct": _optional_float(value("turnover_rate", "turnover", "换手率")),
                    "volume_ratio": _optional_float(value("volume_ratio", "量比")),
                    "quote_time": quote_time,
                    "trade_date": _exchange_trade_date(quote_time),
                    "available": True,
                    "source": "ffd_intraday_snapshot",
                    "stale": False,
                }
            if not result:
                raise ProviderError("FFD intraday snapshot contained no usable rows")
            self._record_success("ffd_intraday_snapshot")
            return result
        except Exception as exc:
            self._record_error("ffd_intraday_snapshot", "snapshot_batch", exc)
            raise

    def _ffd_history_batch(
        self,
        standard_codes: Sequence[str],
        days: int,
        *,
        force: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Coalesce concurrent FFD history requests before checking the cache."""

        with self._ffd_history_request_lock:
            return self._ffd_history_batch_locked(
                standard_codes,
                days,
                force=force,
            )

    def _ffd_history_batch_locked(
        self,
        standard_codes: Sequence[str],
        days: int,
        *,
        force: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch a multi-code FFD daily-history request exactly once."""

        if not self.ffd_enabled:
            raise ProviderError("FFD is disabled")
        days = max(5, min(int(days), 1000))
        requested = list(
            dict.fromkeys(str(code or "").strip().upper() for code in standard_codes)
        )
        requested = [code for code in requested if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code)]
        if not requested:
            return {}
        end = self._latest_completed_trade_date(datetime.now())
        end_text = end.strftime("%Y-%m-%d")
        result: dict[str, list[dict[str, Any]]] = {}
        missing: list[str] = []
        with self._ffd_kline_lock:
            for standard in requested:
                cached = self._ffd_kline_cache.get((end_text, days, standard))
                if cached is not None and not force:
                    result[standard] = copy.deepcopy(cached)
                else:
                    missing.append(standard)
        if not missing:
            return result

        start = end - timedelta(days=max(30, int(days * 1.8) + 15))
        self._record_ffd_unbudgeted("quote_history_batch")
        try:
            payload = self._ffd.call(
                "ffd_quote_history",
                {
                    "codes": ",".join(missing),
                    "start_date": start.strftime("%Y-%m-%d"),
                    "end_date": end_text,
                    "indicators": "open;high;low;close;volume;amt;pct_chg",
                    "adjust": "不复权",
                    "coverage_policy": "complete_rows",
                    "output_mode": "raw",
                    "format": "json",
                },
                timeout=self._ffd_bulk_timeout,
            )
            raw_rows = self._ffd_rows(payload)
            expected = set(missing)
            by_digits: dict[str, list[str]] = {}
            for standard in missing:
                by_digits.setdefault(standard[:6], []).append(standard)
            grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in missing}
            for raw in raw_rows:
                raw_code = str(
                    raw.get("code")
                    or raw.get("ts_code")
                    or raw.get("symbol")
                    or raw.get("证券代码")
                    or ""
                ).upper()
                code_match = re.search(r"(\d{6})(?:\.(SH|SZ|BJ))?", raw_code)
                if not code_match:
                    continue
                if code_match.group(2):
                    standard = f"{code_match.group(1)}.{code_match.group(2)}"
                else:
                    candidates = by_digits.get(code_match.group(1), [])
                    standard = candidates[0] if len(candidates) == 1 else ""
                if standard not in expected:
                    continue
                raw_date = (
                    raw.get("time") or raw.get("date") or raw.get("day")
                    or raw.get("trade_date") or raw.get("交易日期")
                )
                digits = re.sub(r"\D", "", str(raw_date or ""))
                if len(digits) < 8:
                    continue
                date_text = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
                try:
                    row_date = datetime.strptime(date_text, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if row_date > end:
                    continue
                open_ = _optional_float(raw.get("open"))
                high = _optional_float(raw.get("high"))
                low = _optional_float(raw.get("low"))
                close = _optional_float(raw.get("close"))
                if None in {open_, high, low, close} or not close or close <= 0:
                    continue
                grouped[standard].append(
                    {
                        "date": date_text,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": _float(raw.get("volume") if raw.get("volume") is not None else raw.get("vol")),
                        "amount": _float(raw.get("amount") if raw.get("amount") is not None else raw.get("amt")),
                        "change_pct": _optional_float(
                            raw.get("pct_chg")
                            if raw.get("pct_chg") not in (None, "")
                            else raw.get("change_pct")
                        ),
                        "source": "ffd_quote_history",
                        "stale": False,
                    }
                )
            delivered = 0
            with self._ffd_kline_lock:
                for standard, bars in grouped.items():
                    bars.sort(key=lambda row: str(row.get("date") or ""))
                    previous_close = 0.0
                    for bar in bars:
                        close = _float(bar.get("close"))
                        if bar.get("change_pct") is None:
                            bar["change_pct"] = (
                                round((close / previous_close - 1.0) * 100.0, 4)
                                if previous_close
                                else 0.0
                            )
                        previous_close = close
                    trimmed = bars[-days:]
                    if not trimmed:
                        continue
                    delivered += 1
                    self._ffd_kline_cache[(end_text, days, standard)] = copy.deepcopy(trimmed)
                    result[standard] = copy.deepcopy(trimmed)
            if not delivered:
                raise ProviderError("FFD quote history contained no usable complete rows")
            self._record_success("ffd_quote_history")
            return result
        except Exception as exc:
            self._record_error("ffd_quote_history", "history_batch", exc)
            raise

    def get_klines(
        self,
        codes: Iterable[str | int],
        days: int = 120,
        market: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return FFD K-lines for many stocks with one parent request."""

        normalised = list(dict.fromkeys(_normalise_code(code) for code in codes))
        standards = [_ffd_standard_code(code, market) for code in normalised]
        delivered = self._ffd_history_batch(standards, days, force=force)
        return {
            code: copy.deepcopy(delivered[standard])
            for code, standard in zip(normalised, standards)
            if delivered.get(standard)
        }

    def _tdx_day_rows(self, symbol: str, days: int) -> list[dict[str, Any]]:
        """Read stock or index daily bars from the local TDX vipdoc store."""

        prefix = str(symbol or "")[:2].lower()
        code = str(symbol or "")[2:]
        if prefix not in {"sh", "sz", "bj"} or not re.fullmatch(r"\d{6}", code):
            return []
        path = self.tdx_vipdoc / prefix / "lday" / f"{prefix}{code}.day"
        if not path.is_file():
            return []
        try:
            raw = path.read_bytes()
        except OSError:
            return []
        scale = 1000.0 if code.startswith("5") else 100.0
        rows: list[dict[str, Any]] = []
        previous = 0.0
        for offset in range(0, len(raw), 32):
            chunk = raw[offset : offset + 32]
            if len(chunk) < 32:
                continue
            date, open_, high, low, close, amount, volume, _ = struct.unpack(
                "<IIIIIfII", chunk
            )
            if not date or not close:
                continue
            value = close / scale
            rows.append(
                {
                    "date": str(date),
                    "open": open_ / scale,
                    "close": value,
                    "high": high / scale,
                    "low": low / scale,
                    "volume": float(volume),
                    "amount": float(amount),
                    "change_pct": (
                        round((value / previous - 1) * 100, 4) if previous else 0.0
                    ),
                    "source": "tdx_vipdoc_day",
                }
            )
            previous = value
        return rows[-max(1, int(days)) :]

    def get_kline(
        self,
        code: str | int,
        days: int = 120,
        market: str | None = None,
        *,
        prefer_ffd: bool = True,
    ) -> list[dict[str, Any]]:
        code = _normalise_code(code)
        days = max(5, min(int(days), 1000))
        if market is None:
            prefix = _market_prefix(code)
        else:
            prefix = str(market).strip().lower()[:2]
            if prefix not in {"sh", "sz", "bj"}:
                raise ValueError(f"invalid market prefix: {market!r}")
        symbol = prefix + code

        def local_tdx() -> list[dict[str, Any]]:
            return self._tdx_day_rows(symbol, days)

        def load_legacy() -> list[dict[str, Any]]:
            local_rows = local_tdx()
            now = datetime.now()
            expected = self._latest_completed_trade_date(now)

            def parsed_date(value: Any):
                try:
                    return datetime.strptime(str(value).replace("-", "")[:8], "%Y%m%d").date()
                except (TypeError, ValueError):
                    return None

            local_latest = parsed_date(local_rows[-1].get("date")) if local_rows else None
            # A local file is only authoritative when it has reached the latest
            # completed session.  Merely existing is not evidence of freshness.
            if local_rows and local_latest is not None and local_latest >= expected:
                return local_rows
            remote_rows: list[dict[str, Any]] = []
            remote_errors: list[str] = []

            def append_remote_rows(raw_rows: Iterable[Any], source: str) -> None:
                previous_close = 0.0
                for raw in raw_rows:
                    if isinstance(raw, Mapping):
                        raw_date = raw.get("day") or raw.get("date")
                        open_ = raw.get("open")
                        close_value = raw.get("close")
                        high = raw.get("high")
                        low = raw.get("low")
                        volume = raw.get("volume")
                        amount = raw.get("amount")
                    elif isinstance(raw, list) and len(raw) >= 6:
                        raw_date, open_, close_value, high, low, volume = raw[:6]
                        amount = raw[6] if len(raw) > 6 else 0.0
                    else:
                        continue
                    row_date = parsed_date(raw_date)
                    if row_date is None or row_date > expected:
                        continue
                    close = _float(close_value)
                    remote_rows.append(
                        {
                            "date": str(raw_date),
                            "open": _float(open_),
                            "close": close,
                            "high": _float(high),
                            "low": _float(low),
                            "volume": _float(volume),
                            "amount": _float(amount),
                            "change_pct": (
                                round((close / previous_close - 1) * 100, 4)
                                if previous_close
                                else 0.0
                            ),
                            "source": source,
                        }
                    )
                    previous_close = close

            # Sina's open API is unadjusted like the local TDX files and stays
            # reachable on networks where Tencent's K-line endpoint activates
            # its browser WAF. Tencent remains the secondary remote source.
            try:
                payload = self._request(
                    "GET",
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                    params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(days)},
                    headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
                ).json()
                append_remote_rows(
                    payload if isinstance(payload, list) else ((payload.get("result") or {}).get("data") or []),
                    "sina_daily_kline",
                )
            except Exception as exc:
                remote_errors.append(f"Sina {type(exc).__name__}: {exc}")

            if not remote_rows:
                try:
                    response = self._request(
                        "GET",
                        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                        params={"param": f"{symbol},day,,,{days + 5},qfq"},
                        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
                    )
                    payload = response.json()
                    stock = (payload.get("data") or {}).get(symbol) or {}
                    append_remote_rows(
                        stock.get("qfqday") or stock.get("day") or [],
                        "tencent_qfq_kline",
                    )
                except Exception as exc:
                    remote_errors.append(f"Tencent {type(exc).__name__}: {exc}")

            if not remote_rows:
                if local_rows:
                    return [
                        {
                            **row,
                            "stale": True,
                            "sync_warning": (
                                f"本地日线停在 {local_latest}; 最新应至少到 {expected}; "
                                + " | ".join(remote_errors)
                            ),
                        }
                        for row in local_rows
                    ]
                raise ProviderError("; ".join(remote_errors) or f"no K-line data for {code}")
            remote_latest = parsed_date(remote_rows[-1].get("date")) if remote_rows else None
            if remote_rows and (local_latest is None or (remote_latest is not None and remote_latest > local_latest)):
                return remote_rows[-days:]
            if local_rows:
                return local_rows
            raise ProviderError(f"remote providers returned no newer K-line data for {code}")

        def load_ffd() -> list[dict[str, Any]]:
            standard = _ffd_standard_code(code, prefix)
            delivered = self._ffd_history_batch([standard], days)
            rows = delivered.get(standard) or []
            if not rows:
                raise ProviderError(f"FFD returned no K-line data for {standard}")
            return rows

        def load_qmt() -> list[dict[str, Any]]:
            rows = self.qmt.daily_kline(
                code, days, self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
            )
            if not rows:
                raise QmtUnavailable(f"QMT 本地无 {symbol} 日线")
            return rows

        use_qmt = bool(self.qmt_enabled and self.qmt.kline_enabled)
        use_ffd = bool(prefer_ffd and self.ffd_enabled)
        if use_qmt:

            def load_primary() -> list[dict[str, Any]]:
                try:
                    return load_qmt()
                except QmtUnavailable:
                    return load_ffd() if use_ffd else load_legacy()

            primary_loader: Callable[[], list[dict[str, Any]]] = load_primary
            primary_source = "qmt_daily_kline"
        elif use_ffd:
            primary_loader = load_ffd
            primary_source = "ffd_quote_history"
        else:
            primary_loader = load_legacy
            primary_source = "legacy_daily_kline"
        rows, meta = self._cached_fetch(
            f"kline:{'qmt' if use_qmt else 'ffd' if use_ffd else 'legacy'}:{symbol}:{days}:{self._latest_completed_trade_date(datetime.now()):%Y%m%d}",
            ttl=self.ttls["kline"],
            source=primary_source,
            loader=primary_loader,
            empty=[],
            max_stale=7 * 24 * 3600,
            fallback=load_legacy if (use_qmt or use_ffd) else None,
            fallback_source="legacy_daily_kline",
        )
        return self._decorate_rows(rows, meta)

    def hydrate_postclose_klines(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        codes: Iterable[str] | None = None,
        max_symbols: int = 400,
        days: int = 120,
    ) -> list[dict[str, Any]]:
        """Attach remote daily history to a bounded strategy universe.

        Full-market snapshots are cheap to obtain from FFD, but K-lines are a
        per-symbol resource.  Hydrate only the amount-ranked symbols relevant
        to the strategy (or an explicit code subset), keeping the existing TDX
        bars untouched when present.
        """

        decorated = [dict(item) for item in rows if isinstance(item, Mapping)]
        requested = {str(code) for code in (codes or []) if re.fullmatch(r"\d{6}", str(code))}
        candidates: list[dict[str, Any]] = []
        for item in decorated:
            code = str(item.get("code") or "")
            if requested and code not in requested:
                continue
            bars = item.get("postclose_kline")
            if isinstance(bars, list) and len(bars) >= 2:
                continue
            if not re.fullmatch(r"\d{6}", code):
                continue
            if not is_a_share_security(code, item.get("name")):
                continue
            candidates.append(item)
        candidates.sort(key=lambda item: _float(item.get("amount")), reverse=True)
        selected = candidates[: max(0, int(max_symbols))]
        if not selected:
            return decorated

        by_code = {str(item.get("code")): item for item in selected}

        def backfill_last_bar_from_snapshot(item: dict[str, Any]) -> None:
            """Restore fields omitted by lightweight K-line fallbacks.

            Tencent's adjusted K-line endpoint supplies OHLC and volume but
            reports daily amount as zero.  The full-market snapshot is for the
            same completed trade date and has the authoritative amount needed
            by the 一进二 liquidity gate, so merge it into the final bar.
            """

            bars = item.get("postclose_kline")
            if not isinstance(bars, list) or not bars:
                return
            last = bars[-1]
            if not isinstance(last, dict):
                return
            snapshot_date = str(
                item.get("ffd_trade_date") or item.get("quote_time") or item.get("as_of") or ""
            ).replace("-", "")[:8]
            bar_date = str(last.get("date") or "").replace("-", "")[:8]
            if snapshot_date and bar_date and snapshot_date != bar_date:
                return
            if _float(last.get("amount")) <= 0 and _float(item.get("amount")) > 0:
                last["amount"] = _float(item.get("amount"))
            if _float(last.get("volume")) <= 0 and _float(item.get("volume")) > 0:
                last["volume"] = _float(item.get("volume"))
            if all(last.get(key) in (None, "") for key in ("turnover", "turnover_pct", "turnover_rate")):
                turnover = _optional_float(
                    item.get("turnover_pct")
                    if item.get("turnover_pct") not in (None, "")
                    else item.get("turnover_rate")
                )
                if turnover is not None:
                    last["turnover_pct"] = turnover

        # One FFD parent request hydrates the whole shortlist. Never expand a
        # multi-code history request into paid per-stock calls.
        try:
            ffd_batches = self.get_klines(by_code, days=days)
        except Exception:
            ffd_batches = {}
        for code, bars in ffd_batches.items():
            item = by_code.get(code)
            if item is None or not bars:
                continue
            item["postclose_kline"] = [dict(bar) for bar in bars]
            backfill_last_bar_from_snapshot(item)
            item["history"] = [
                {
                    "date": str(bar.get("date") or ""),
                    "change_pct": _float(bar.get("change_pct")),
                }
                for bar in bars[-10:]
            ]
            item["as_of"] = str(bars[-1].get("date") or item.get("as_of") or "")
            item["kline_source"] = "ffd_quote_history"
            item["source"] = f"{item.get('source') or 'market_universe'}+ffd_quote_history"

        remaining = [item for item in selected if str(item.get("code")) not in ffd_batches]
        if not remaining:
            return decorated

        def fetch(item: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
            code = str(item.get("code") or "")
            try:
                return code, self.get_kline(code, days=days, prefer_ffd=False)
            except TypeError:
                # Compatibility with test doubles and older provider shims.
                try:
                    return code, self.get_kline(code, days=days)
                except Exception:
                    return code, []
            except Exception:
                return code, []

        with ThreadPoolExecutor(max_workers=min(16, len(remaining))) as pool:
            futures = [pool.submit(fetch, item) for item in remaining]
            for future in as_completed(futures):
                code, bars = future.result()
                item = by_code.get(code)
                if item is None or not bars:
                    continue
                item["postclose_kline"] = [dict(bar) for bar in bars]
                backfill_last_bar_from_snapshot(item)
                item["history"] = [
                    {
                        "date": str(bar.get("date") or ""),
                        "change_pct": _float(bar.get("change_pct")),
                    }
                    for bar in bars[-10:]
                ]
                item["as_of"] = str(bars[-1].get("date") or item.get("as_of") or "")
                item["kline_source"] = str(bars[-1].get("source") or "remote_kline")
                item["source"] = f"{item.get('source') or 'market_universe'}+{item['kline_source']}"
        return decorated

    def get_northbound(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            payload = self._request(
                "GET",
                "https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                headers={
                    "User-Agent": UA,
                    "Host": "data.hexin.cn",
                    "Referer": "https://data.hexin.cn/",
                },
            ).json()
            times = payload.get("time") or []
            hgt = payload.get("hgt") or []
            sgt = payload.get("sgt") or []
            latest: dict[str, Any] = {}
            rows = []
            for index, point_time in enumerate(times):
                h_value = hgt[index] if index < len(hgt) else None
                s_value = sgt[index] if index < len(sgt) else None
                row = {
                    "time": point_time,
                    "hgt_yi": None if h_value is None else _float(h_value),
                    "sgt_yi": None if s_value is None else _float(s_value),
                }
                if h_value is not None or s_value is not None:
                    row["total_yi"] = _float(h_value) + _float(s_value)
                    latest = row
                rows.append(row)
            if not latest:
                raise ProviderError("THS northbound response contained no data points")
            return {"latest": latest, "points": rows, "point_count": len(rows)}

        data, meta = self._cached_fetch(
            "northbound",
            ttl=self.ttls["northbound"],
            source="ths_northbound",
            loader=load,
            empty={"latest": {}, "points": [], "point_count": 0},
            max_stale=24 * 3600,
        )
        return self._decorate_dict(data, meta)

    @staticmethod
    def _is_live_index_window(now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        return now.weekday() < 5 and 9 * 60 + 15 <= minute <= 15 * 60 + 5

    @staticmethod
    def _latest_completed_trade_date(now: datetime):
        candidate = now.date()
        if now.hour * 60 + now.minute < 15 * 60 + 5:
            candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate

    @staticmethod
    def _market_session(now: datetime) -> str:
        if now.weekday() >= 5:
            return "weekend"
        minute = now.hour * 60 + now.minute
        if minute < 9 * 60 + 15:
            return "preopen"
        if minute < 9 * 60 + 30:
            return "auction"
        if minute <= 11 * 60 + 30:
            return "morning"
        if minute < 13 * 60:
            return "midday"
        if minute <= 15 * 60:
            return "afternoon"
        return "postclose"

    @staticmethod
    def _is_strategic_network_window(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        minute = now.hour * 60 + now.minute
        return (9 * 60 + 24 <= minute <= 9 * 60 + 29) or (
            14 * 60 + 58 <= minute <= 15 * 60 + 10
        )

    def _tdx_index_quotes(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for symbol, display_name in INDEX_SYMBOLS:
            rows = self._tdx_day_rows(symbol, 2)
            if not rows:
                continue
            latest = rows[-1]
            previous_close = _float(rows[-2].get("close")) if len(rows) > 1 else 0.0
            price = _float(latest.get("close"))
            change = price - previous_close if previous_close else 0.0
            result[symbol] = {
                "code": symbol[2:],
                "symbol": symbol,
                "name": display_name,
                "price": price,
                "last_close": previous_close,
                "open": _float(latest.get("open")),
                "high": _float(latest.get("high")),
                "low": _float(latest.get("low")),
                "volume": _float(latest.get("volume")),
                "amount": _float(latest.get("amount")),
                "change": round(change, 4),
                "change_pct": (
                    round(change / previous_close * 100, 4) if previous_close else 0.0
                ),
                "quote_time": str(latest.get("date") or ""),
                "available": True,
                "source": "tdx_vipdoc_index",
            }
        if not result:
            raise ProviderError("local TDX contained no market index bars")
        return result

    def _ffd_index_quotes(self) -> dict[str, dict[str, Any]]:
        """Return all configured index quotes from one FFD batch."""

        standards = {
            symbol: _ffd_standard_code(symbol[2:], symbol[:2])
            for symbol, _ in INDEX_SYMBOLS
        }
        live_rows: dict[str, dict[str, Any]] = {}
        if self._is_live_index_window(datetime.now()):
            try:
                live_rows = self._ffd_intraday_snapshot(list(standards.values()))
            except Exception:
                live_rows = {}
        missing = [standard for standard in standards.values() if standard not in live_rows]
        history_rows: dict[str, list[dict[str, Any]]] = {}
        if missing:
            try:
                history_rows = self._ffd_history_batch(missing, 2)
            except Exception:
                history_rows = {}
        result: dict[str, dict[str, Any]] = {}
        names = dict(INDEX_SYMBOLS)
        for symbol, standard in standards.items():
            live = live_rows.get(standard)
            if live:
                result[symbol] = {
                    **live,
                    "code": symbol[2:],
                    "symbol": symbol,
                    "name": names[symbol],
                    "source": "ffd_intraday_snapshot",
                }
                continue
            bars = history_rows.get(standard) or []
            if not bars:
                continue
            latest = bars[-1]
            previous_close = _float(bars[-2].get("close")) if len(bars) > 1 else 0.0
            price = _float(latest.get("close"))
            change = price - previous_close if previous_close else 0.0
            result[symbol] = {
                "code": symbol[2:],
                "symbol": symbol,
                "name": names[symbol],
                "price": price,
                "last_close": previous_close,
                "open": latest.get("open"),
                "high": latest.get("high"),
                "low": latest.get("low"),
                "volume": _float(latest.get("volume")),
                "amount": _float(latest.get("amount")),
                "change": round(change, 4),
                "change_pct": latest.get("change_pct"),
                "quote_time": str(latest.get("date") or ""),
                "available": True,
                "source": "ffd_quote_history",
            }
        if not result:
            raise ProviderError("FFD returned no configured index quotes")
        return result

    def get_market_overview(self) -> dict[str, Any]:
        now = datetime.now()
        session = self._market_session(now)
        live_index_window = session in {"auction", "morning", "afternoon"}
        strategic_window = self._is_strategic_network_window(now)

        def build(raw: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
            indices = []
            for symbol, display_name in INDEX_SYMBOLS:
                quote = raw.get(symbol)
                if quote:
                    indices.append(
                        {
                            **quote,
                            "display_name": display_name,
                            "data_as_of": _exchange_iso(quote.get("quote_time")),
                            "trade_date": _exchange_trade_date(quote.get("quote_time")),
                        }
                    )
            if not indices:
                raise ProviderError("no market index quotes were available")
            changes = [float(item.get("change_pct", 0)) for item in indices]
            average = sum(changes) / len(changes)
            score = round(max(-12.0, min(12.0, average * 2.5)), 2)
            if score >= 6:
                label = "强多"
            elif score >= 2:
                label = "偏多"
            elif score <= -6:
                label = "强空"
            elif score <= -2:
                label = "偏空"
            else:
                label = "中性"
            return {
                "indices": indices,
                "market_score": score,
                "market_label": label,
                "advance_count": sum(1 for value in changes if value > 0),
                "decline_count": sum(1 for value in changes if value < 0),
                "flat_count": sum(1 for value in changes if value == 0),
                "average_change_pct": round(average, 3),
                "note": f"{len(indices)}个主要指数平均涨跌 {average:+.2f}%",
            }

        def load() -> dict[str, Any]:
            return build(self._ffd_index_quotes())

        def compatibility_fallback() -> dict[str, Any]:
            try:
                return build(self._tencent_symbols([symbol for symbol, _ in INDEX_SYMBOLS]))
            except Exception:
                return build(self._tdx_index_quotes())

        data, meta = self._cached_fetch(
            f"market_overview:{now:%Y%m%d}:{session}",
            ttl=self.ttls["overview"] if live_index_window else max(60.0, self.ttls["overview"]),
            source="ffd_indices",
            loader=load,
            empty={
                "indices": [],
                "market_score": 0,
                "market_label": "中性",
                "advance_count": 0,
                "decline_count": 0,
                "flat_count": 0,
                "average_change_pct": 0,
                "note": "指数行情暂不可用",
            },
            max_stale=15 * 60,
            fallback=compatibility_fallback,
            fallback_source="public_or_local_index_fallback",
        )
        result = self._decorate_dict(data, meta)
        for item in result.get("indices", []):
            item.setdefault("data_as_of", _exchange_iso(item.get("quote_time")))
            item.setdefault("trade_date", _exchange_trade_date(item.get("quote_time")))
        data_times = [
            str(item.get("data_as_of") or "")
            for item in result.get("indices", [])
            if item.get("data_as_of")
        ]
        trade_dates = [
            str(item.get("trade_date") or "")
            for item in result.get("indices", [])
            if item.get("trade_date")
        ]
        data_as_of = max(data_times, default="")
        trade_date = max(trade_dates, default="")
        expected = self._latest_completed_trade_date(now).strftime("%Y-%m-%d")
        source = str(result.get("source") or "")
        delayed = bool(result.get("stale")) or (
            live_index_window and trade_date != now.strftime("%Y-%m-%d")
        ) or ("tencent" not in source and bool(trade_date) and trade_date < expected)
        result["as_of"] = data_as_of or meta.get("fetched_at")
        result["data_as_of"] = data_as_of
        result["server_time"] = meta.get("fetched_at")
        result["trade_date"] = trade_date
        result["session"] = session
        result["realtime"] = live_index_window and not delayed
        result["data_delayed"] = delayed
        result["stale"] = delayed
        result["breadth"] = {
            "advance": result.get("advance_count", 0),
            "decline": result.get("decline_count", 0),
            "flat": result.get("flat_count", 0),
            "scope": "major_indices",
        }
        ffd_breadth = self.get_ffd_market_breadth(allow_network=strategic_window)
        if ffd_breadth:
            # A cached intraday breadth snapshot can age out over a weekend.
            # If the full-market daily baseline is already on the latest
            # completed session, use it as the date-valid fallback instead of
            # displaying stale counts beside fresh index quotes.
            if ffd_breadth.get("stale"):
                daily_breadth = self._ffd_daily_breadth(expected)
                if daily_breadth:
                    ffd_breadth = daily_breadth
            result["breadth"] = ffd_breadth
            result["advance_count"] = ffd_breadth.get("advance", 0)
            result["decline_count"] = ffd_breadth.get("decline", 0)
            result["flat_count"] = ffd_breadth.get("flat", 0)
            result["data_layers"] = {
                "market_breadth": ffd_breadth.get("source", "ffd_market_breadth"),
                "index_quotes": meta.get("source"),
            }
        if strategic_window:
            northbound = self.get_northbound()
            result["northbound"] = northbound.get("latest", {})
            result["northbound_meta"] = northbound.get("_meta", {})
        else:
            cached_northbound = self.cache.get(
                "northbound", allow_stale=True, max_stale_seconds=24 * 3600
            )
            if cached_northbound is not None:
                result["northbound"] = dict(
                    (cached_northbound.value or {}).get("latest", {})
                )
                result["northbound_meta"] = self._meta(
                    cached_northbound, cache_hit=True
                )
            else:
                result["northbound"] = {}
                result["northbound_meta"] = {
                    "source": "not_requested_outside_strategic_window",
                    "stale": True,
                    "cache_hit": False,
                }
        return result

    @staticmethod
    def _parse_universe_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "code": str(item.get("f12") or "").zfill(6),
            "name": str(item.get("f14") or ""),
            "price": _float(item.get("f2")),
            "change_pct": _float(item.get("f3")),
            "change": _float(item.get("f4")),
            "volume": _float(item.get("f5")),
            "amount": _float(item.get("f6")),
            "amplitude_pct": _float(item.get("f7")),
            "turnover_pct": _float(item.get("f8")),
            "pe_ttm": _optional_float(item.get("f9")),
            "volume_ratio": _optional_float(item.get("f10")),
            "high": _float(item.get("f15")),
            "low": _float(item.get("f16")),
            "open": _float(item.get("f17")),
            "last_close": _float(item.get("f18")),
            "mcap": _optional_float(item.get("f20")),
            "float_mcap": _optional_float(item.get("f21")),
            "pb": _optional_float(item.get("f23")),
            "change_60d_pct": _float(item.get("f24")),
            "change_ytd_pct": _float(item.get("f25")),
            "industry": str(item.get("f100") or ""),
            # Compatibility names used by the scoring/service boundary.
            "market_cap": _optional_float(item.get("f20")),
            "float_market_cap": _optional_float(item.get("f21")),
            "turnover": _optional_float(item.get("f8")),
            "amplitude": _optional_float(item.get("f7")),
            "quote_time": _normalise_quote_time(item.get("f124")),
            "available": True,
        }

    def _starter_universe(self) -> list[dict[str, Any]]:
        base = [
            {"code": code, "name": name, "industry": industry}
            for code, name, industry in STARTER_UNIVERSE
        ]
        quotes = self.get_quotes(item["code"] for item in base)
        for item in base:
            quote = quotes.get(item["code"], {})
            for key in (
                "price",
                "change_pct",
                "change",
                "volume_lots",
                "amount_wan",
                "amplitude_pct",
                "turnover_pct",
                "pe_ttm",
                "volume_ratio",
                "high",
                "low",
                "open",
                "last_close",
                "mcap_yi",
                "float_mcap_yi",
                "pb",
            ):
                if key in quote:
                    item[key] = quote[key]
            item["amount"] = _float(item.get("amount_wan")) * 10000
            item["market_cap"] = _float(item.get("mcap_yi")) * 1e8
            item["float_market_cap"] = _float(item.get("float_mcap_yi")) * 1e8
            item["turnover"] = _float(item.get("turnover_pct"))
            item["amplitude"] = _float(item.get("amplitude_pct"))
        return base

    def _local_html_universe(self) -> list[dict[str, Any]]:
        """Read a user-supplied/static code catalogue for offline coverage.

        The catalogue is only a symbol fallback; prices and fundamentals still
        need a live provider.  It is intentionally marked degraded by the
        cache layer so callers cannot mistake it for a fresh market snapshot.
        """

        configured = os.environ.get("XUNLONG_UNIVERSE_FILE", "").strip()
        if not configured:
            return []
        path = Path(configured).expanduser()
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        # The supplied catalogue uses JSON-like [code, name, ...] entries.
        matches = re.findall(r'\[\s*["\'](\d{6})["\']\s*,\s*["\']([^"\']+)["\']', text)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for code, name in matches:
            if code in seen:
                continue
            seen.add(code)
            rows.append({"code": code, "name": name, "industry": "", "catalogue_source": str(path)})
        return rows

    def _fallback_universe(self) -> list[dict[str, Any]]:
        catalogue = self._local_html_universe()
        if len(catalogue) >= len(STARTER_UNIVERSE):
            return catalogue
        return self._starter_universe()

    def _tdx_quote_universe(self) -> list[dict[str, Any]]:
        """Build the post-close universe from local TDX ``.day`` files.

        A local day file is authoritative for the last completed session.  This
        keeps post-close scans independent of Tencent/Eastmoney and avoids
        treating a pre-open zero quote as a real close.
        """
        now = datetime.now()
        with self._tdx_universe_cache_lock:
            cached = self._tdx_universe_cache
            cache_age = time.monotonic() - self._tdx_universe_cache_at
            # Intraday quote overlays are fetched separately. This cache only
            # holds the expensive historical/K-line base and is invalidated
            # when a new completed trading date becomes available.
            if (
                cached is not None
                and cache_age < 5 * 60
            ):
                return [dict(row) for row in cached]

        rows = self._tdx_day_universe()
        local_dates = [
            _exchange_trade_date(row.get("as_of") or row.get("quote_time"))
            for row in rows
            if isinstance(row, Mapping)
        ]
        local_latest = max((date for date in local_dates if date), default="")
        with self._tdx_universe_cache_lock:
            self._tdx_universe_cache = rows
            self._tdx_universe_cache_at = time.monotonic()
            self._tdx_universe_cache_date = local_latest
        return [dict(row) for row in rows]

    @staticmethod
    def _tdx_text(value: bytes) -> str:
        return value.decode("gbk", errors="ignore").replace("\x00", "").strip()

    def _tdx_name_map(self) -> dict[str, str]:
        """Read stock names from TDX fixed-width ``*.tnf`` files."""
        result: dict[str, str] = {}
        for market in ("sh", "sz", "bj"):
            path = self.tdx_cache_dir / f"{market}s.tnf"
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            # The current TDX format has a 50-byte header and 360-byte rows;
            # tolerate a malformed tail and derive names from the stable row
            # offsets rather than relying on a third-party parser.
            for offset in range(50, len(raw) - 40, 360):
                record = raw[offset : offset + 360]
                code = self._tdx_text(record[:16])
                if not re.fullmatch(r"\d{6}", code):
                    continue
                expected_market = _market_prefix(code)
                if expected_market != market or not is_a_share_security(code):
                    continue
                name = self._tdx_text(record[31:63])
                if name:
                    result[code] = name
        return result

    def _tdx_industry_map(self) -> dict[str, str]:
        """Map local TDX industry codes to captions from ``hy_tree.xml``."""
        captions: dict[str, str] = {}
        path = self.tdx_cloud_dir / "hy_tree.xml"
        if path.is_file():
            try:
                document = path.read_bytes().decode("gbk", errors="ignore")
                document = re.sub(r"^\s*<\?xml[^>]*\?>", "", document, count=1)
                root = ET.fromstring(document)
                for node in root.iter("node"):
                    block_id = str(node.attrib.get("blockid") or "").strip()
                    caption = str(node.attrib.get("caption") or "").strip()
                    if block_id and caption:
                        captions[block_id] = caption
            except (OSError, ET.ParseError, UnicodeError, ValueError):
                pass
        result: dict[str, str] = {}
        path = self.tdx_cache_dir / "tdxhy.cfg"
        if not path.is_file():
            return result
        try:
            lines = path.read_bytes().decode("gbk", errors="ignore").splitlines()
        except OSError:
            return result
        for line in lines:
            parts = line.split("|")
            if len(parts) < 2 or not re.fullmatch(r"\d{6}", parts[1]):
                continue
            category = next((item for item in reversed(parts[2:]) if item.startswith("X")), "")
            if category and category in captions:
                result[parts[1]] = captions[category]
        return result

    def _eastmoney_industry_map(self) -> dict[str, str]:
        """Fetch code->industry mapping from Eastmoney full-market endpoint.

        Used as a fallback when local TDX industry files (tdxhy.cfg / hy_tree.xml)
        are unavailable.  Paginated because the endpoint caps each page at ~100
        rows even when pz is set larger.
        """
        result: dict[str, str] = {}
        try:
            page = 1
            while True:
                response = self._em_get(
                    "https://push2delay.eastmoney.com/api/qt/clist/get",
                    params={
                        "pn": str(page), "pz": "500", "po": "1", "np": "1",
                        "fltt": "2", "invt": "2", "fid": "f3",
                        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                        "fields": "f12,f100",
                    },
                    headers={"Referer": "https://quote.eastmoney.com/"},
                )
                payload = response.json()
                data = payload.get("data") or {}
                diff = data.get("diff") or []
                if not diff:
                    break
                for item in diff:
                    code = str(item.get("f12") or "").zfill(6)
                    industry = str(item.get("f100") or "").strip()
                    if code and industry:
                        result[code] = industry
                total = int(data.get("total") or 0)
                if len(result) >= total or page > 20:
                    break
                page += 1
        except Exception:
            pass
        return result

    def _tdx_day_universe(self) -> list[dict[str, Any]]:
        names = self._tdx_name_map()
        industries = self._tdx_industry_map()
        codes: list[str] = []
        seen: set[str] = set()
        for prefix in ("sh", "sz", "bj"):
            folder = self.tdx_vipdoc / prefix / "lday"
            if not folder.is_dir():
                continue
            for path in folder.glob(f"{prefix}*.day"):
                code = path.stem[2:]
                if len(code) != 6 or code in seen:
                    continue
                if not is_a_share_security(code) or is_star_security(code):
                    continue
                seen.add(code)
                codes.append(code)
        rows: list[dict[str, Any]] = []
        for code in codes:
            prefix = _market_prefix(code)
            path = self.tdx_vipdoc / prefix / "lday" / f"{prefix}{code}.day"
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if len(raw) < 32:
                continue
            records: list[tuple[int, int, int, int, int, float, int]] = []
            for offset in range(max(0, len(raw) - 32 * 65), len(raw), 32):
                chunk = raw[offset : offset + 32]
                if len(chunk) < 32:
                    continue
                date, open_, high, low, close, amount, volume, _ = struct.unpack("<IIIIIfII", chunk)
                if date and close:
                    records.append((date, open_, high, low, close, amount, volume))
            if not records:
                continue
            latest = records[-1]
            previous_close = records[-2][4] if len(records) > 1 else 0
            scale = 1000.0 if code.startswith("5") else 100.0
            close = latest[4] / scale
            last_close = previous_close / scale if previous_close else 0.0
            return_10d = None
            if len(records) >= 11 and records[-11][4]:
                return_10d = round((latest[4] / records[-11][4] - 1.0) * 100.0, 4)
            history = []
            postclose_kline = []
            for index in range(1, len(records)):
                previous_close = records[index - 1][4]
                current_close = records[index][4]
                if previous_close and current_close:
                    history.append({
                        "date": str(records[index][0]),
                        "change_pct": round((current_close / previous_close - 1.0) * 100.0, 4),
                    })
            for date, open_, high, low, close_, amount, volume in records:
                postclose_kline.append({
                    "date": str(date), "open": open_ / scale, "high": high / scale,
                    "low": low / scale, "close": close_ / scale,
                    "amount": float(amount), "volume": float(volume),
                })
            if not names.get(code):
                continue
            rows.append({
                "code": code,
                "name": names[code],
                "price": close,
                "change_pct": round((close / last_close - 1.0) * 100.0, 4) if last_close else 0.0,
                "change": close - last_close if last_close else 0.0,
                "volume": float(latest[6]),
                "amount": float(latest[5]),
                "open": latest[1] / scale,
                "high": latest[2] / scale,
                "low": latest[3] / scale,
                "last_close": last_close,
                "returns_10d": return_10d,
                "history": history[-10:],
                "postclose_kline": postclose_kline,
                "industry": industries.get(code, ""),
                "source": "tdx_postclose_day",
                "as_of": str(latest[0]),
            })
        return rows

    def _qmt_universe_rows(self, expected_date: str) -> list[dict[str, Any]]:
        """Full main-board snapshot from the logged-in QMT client.

        Rows carry live tick fields; valuation/industry fields (float_mcap,
        pe_ttm, industry, ...) are merged from the cached FFD daily baseline
        when one exists so downstream scoring keeps its usual inputs without
        spending any FFD budget.  Raises QmtUnavailable when the local client
        cannot deliver a full-market snapshot.
        """

        members = self.qmt.universe_rows()
        if not members:
            return []
        ticks = self.qmt.full_tick([row["code"] for row in members])
        baseline_by_code: dict[str, Mapping[str, Any]] = {}
        try:
            for baseline in self._cached_ffd_daily_rows():
                code = str(baseline.get("code") or "")
                if code:
                    baseline_by_code[code] = baseline
        except Exception:  # noqa: BLE001 - baseline is best-effort enrichment
            baseline_by_code = {}
        rows: list[dict[str, Any]] = []
        for member in members:
            code = str(member.get("code") or "")
            tick = ticks.get(code) or {}
            baseline = baseline_by_code.get(code) or {}
            # A tick dated on/after the latest completed session is current
            # (pre-open it equals the prior close; intraday it is live).
            tick_current = (
                str(tick.get("trade_date") or "")[:10] >= expected_date
                if tick
                else False
            )
            price = _float(tick.get("price")) if tick_current else 0.0
            last_close = _float(tick.get("last_close")) if tick_current else 0.0
            if price <= 0:
                price = _float(baseline.get("price"))
                last_close = _float(baseline.get("last_close"))
            if price <= 0:
                continue
            float_mcap = _optional_float(baseline.get("float_mcap"))
            rows.append(
                {
                    "code": code,
                    "name": member.get("name") or baseline.get("name") or code,
                    "price": price,
                    "last_close": last_close or None,
                    "previous_close": last_close or None,
                    "open": _optional_float(tick.get("open")) if tick_current else None,
                    "high": _optional_float(tick.get("high")) if tick_current else None,
                    "low": _optional_float(tick.get("low")) if tick_current else None,
                    "volume": _float(tick.get("volume")) if tick_current else 0.0,
                    "volume_lots": _float(tick.get("volume_lots")) if tick_current else None,
                    "amount": _float(tick.get("amount")) if tick_current else 0.0,
                    "amount_wan": _optional_float(tick.get("amount_wan")) if tick_current else None,
                    "change": _optional_float(tick.get("change")) if tick_current else None,
                    "change_pct": (
                        round((price / last_close - 1) * 100, 4)
                        if last_close > 0
                        else _optional_float(baseline.get("change_pct"))
                    ),
                    "float_mcap": float_mcap,
                    "float_market_cap": float_mcap,
                    "mcap": _optional_float(baseline.get("mcap")),
                    "market_cap": _optional_float(baseline.get("mcap")),
                    "pe_ttm": _optional_float(baseline.get("pe_ttm")),
                    "pb": _optional_float(baseline.get("pb")),
                    "industry": baseline.get("industry") or "",
                    "quote_time": tick.get("quote_time") or baseline.get("quote_time") or expected_date,
                    "as_of": tick.get("quote_time") or expected_date,
                    "trade_date": (
                        str(tick.get("trade_date"))[:10]
                        if tick_current
                        else str(baseline.get("trade_date") or expected_date)[:10]
                    ),
                    "turnover_pct": None,
                    "amplitude_pct": None,
                    "volume_ratio": None,
                    "available": True,
                    "source": "qmt_universe",
                    "stale": False,
                }
            )
        if len(rows) < self.FFD_DIRECT_MIN_USABLE_ROWS:
            raise QmtUnavailable(
                f"QMT 全市场快照仅 {len(rows)} 行可用,低于可信下限"
            )
        self._record_success("qmt_universe")
        return rows

    def get_market_universe(
        self, limit: int | None = None, *, force: bool = False
    ) -> list[dict[str, Any]]:
        def load_eastmoney() -> list[dict[str, Any]]:
            params = {
                "pn": "1",
                "pz": "6000",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": (
                    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,"
                    "f20,f21,f23,f24,f25,f100,f124"
                ),
            }
            request_kwargs = {
                "params": params,
                "headers": {"Referer": "https://quote.eastmoney.com/"},
            }
            try:
                # The delay node is more stable for large full-market payloads;
                # keep push2 as a compatibility fallback.
                response = self._em_get(
                    "https://push2delay.eastmoney.com/api/qt/clist/get",
                    **request_kwargs,
                )
            except Exception:
                response = self._em_get(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    **request_kwargs,
                )
            payload = response.json()
            records = [
                self._parse_universe_item(item)
                for item in _items((payload.get("data") or {}).get("diff"))
            ]
            records = [
                item
                for item in records
                if re.fullmatch(r"\d{6}", item["code"]) and item["name"]
            ]
            if not records:
                raise ProviderError("Eastmoney full-market snapshot was empty")
            # A valid HTTP response is not enough: in the morning fallback the
            # endpoint can return only a narrow 920xxx slice.  That slice is
            # not a usable base for the main-board scanners.
            if not any(
                str(item.get("code") or "").startswith(
                    ("000", "001", "002", "003", "600", "601", "603", "605")
                )
                for item in records
            ):
                raise ProviderError("Eastmoney full-market snapshot had no main-board symbols")
            return records

        def load_tencent(local_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            symbols = [
                _market_prefix(str(row.get("code"))) + str(row.get("code"))
                for row in local_rows
                if re.fullmatch(r"\d{6}", str(row.get("code", "")))
            ]
            quotes = self._tencent_symbols_parallel(symbols)
            rows: list[dict[str, Any]] = []
            for local in local_rows:
                code = str(local.get("code"))
                symbol = _market_prefix(code) + code
                quote = quotes.get(symbol)
                if not quote:
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": quote.get("name") or local.get("name") or code,
                        "price": quote.get("price"),
                        "change_pct": quote.get("change_pct"),
                        "change": quote.get("change"),
                        "volume": _float(quote.get("volume_lots")) * 100.0,
                        "amount": _float(quote.get("amount_wan")) * 10_000.0,
                        "amplitude_pct": quote.get("amplitude_pct"),
                        "turnover_pct": quote.get("turnover_pct"),
                        "pe_ttm": quote.get("pe_ttm"),
                        "volume_ratio": quote.get("volume_ratio"),
                        "high": quote.get("high"),
                        "low": quote.get("low"),
                        "open": quote.get("open"),
                        "last_close": quote.get("last_close"),
                        "mcap": _float(quote.get("mcap_yi")) * 100_000_000.0,
                        "float_mcap": _float(quote.get("float_mcap_yi")) * 100_000_000.0,
                        "pb": quote.get("pb"),
                        "market_cap": _float(quote.get("mcap_yi")) * 100_000_000.0,
                        "float_market_cap": _float(quote.get("float_mcap_yi")) * 100_000_000.0,
                        "turnover": quote.get("turnover_pct"),
                        "amplitude": quote.get("amplitude_pct"),
                        "quote_time": quote.get("quote_time") or "",
                        "available": bool(quote.get("available")),
                    }
                )
            if not rows:
                raise ProviderError("Tencent full-market quotes returned no usable records")
            return rows

        def overlay_remote(
            local_rows: list[dict[str, Any]],
            *,
            target_date: str,
            live: bool,
        ) -> list[dict[str, Any]]:
            try:
                live_rows = load_tencent(local_rows)
            except Exception:
                # Eastmoney remains useful as a lower-volume compatibility
                # fallback when Tencent is temporarily unavailable.
                live_rows = load_eastmoney()
            live_by_code = {str(row.get("code")): row for row in live_rows}
            matched = sum(1 for row in local_rows if str(row.get("code")) in live_by_code)
            coverage = matched / max(1, len(local_rows))
            if coverage < 0.80:
                raise ProviderError(
                    f"Remote live universe coverage too low ({matched}/{len(local_rows)})"
                )
            now = datetime.now()
            fresh = sum(
                1
                for row in live_rows
                if _exchange_trade_date(row.get("quote_time")) == target_date
            )
            if fresh < max(500, int(len(live_rows) * 0.60)):
                raise ProviderError(
                    f"Remote live universe timestamp is stale ({fresh}/{len(live_rows)})"
                )
            live_fields = {
                "name", "price", "change_pct", "change", "volume", "amount",
                "amplitude_pct", "turnover_pct", "pe_ttm", "volume_ratio",
                "high", "low", "open", "last_close", "mcap", "float_mcap",
                "pb", "change_60d_pct", "change_ytd_pct", "market_cap",
                "float_market_cap", "turnover", "amplitude", "quote_time",
                "available",
            }
            merged: list[dict[str, Any]] = []
            for local in local_rows:
                code = str(local.get("code"))
                remote = live_by_code.get(code)
                row = dict(local)
                if remote:
                    for key in live_fields:
                        value = remote.get(key)
                        # A suspended/pre-open zero price must not erase a valid
                        # historical price, while zero change remains valid.
                        if key == "price" and _float(value) <= 0:
                            continue
                        if value not in (None, ""):
                            row[key] = value
                    row["source"] = (
                        "tencent_live+tdx_postclose_day"
                        if live
                        else "tencent_daily+tdx_postclose_day"
                    )
                    row["live_quote"] = live
                    row["reference_quote"] = not live
                    row["stale"] = False
                    row["quote_age_seconds"] = max(
                        0.0,
                        (now - (_exchange_datetime(remote.get("quote_time")) or now)).total_seconds(),
                    )
                else:
                    row["source"] = "tdx_postclose_day"
                    row["live_quote"] = False
                    row["stale"] = True
                merged.append(row)
            return merged

        def load() -> list[dict[str, Any]]:
            expected_date = self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
            direct_ffd_rows: list[dict[str, Any]] | None = None

            def load_direct_ffd() -> list[dict[str, Any]]:
                nonlocal direct_ffd_rows
                if direct_ffd_rows is None:
                    direct_ffd_rows = self._ffd_direct_universe(expected_date)
                return direct_ffd_rows

            # Local QMT ticks are exchange-direct and budget-free.  When the
            # QMT client is logged in they outrank the budgeted FFD daily
            # asset; any unavailability falls through to the FFD chain below.
            if self.qmt_enabled:
                try:
                    qmt_rows = self._qmt_universe_rows(expected_date)
                except QmtUnavailable as exc:
                    self._record_error("qmt_universe", "market_universe", exc)
                else:
                    if qmt_rows:
                        self._save_universe_snapshot(qmt_rows)
                        return qmt_rows

            # FFD is the authoritative universe. Local/public feeds below are
            # retained only as explicit compatibility fallbacks when the exact
            # FFD trade-date asset is unavailable.
            ffd_rows = load_direct_ffd()
            if ffd_rows:
                self._save_universe_snapshot(ffd_rows)
                return ffd_rows

            local_rows = self._tdx_quote_universe()
            # Do not let a small or mostly empty compatibility catalogue become
            # the base of a full-market scan.
            if self._needs_direct_ffd_universe(local_rows):
                ffd_rows = load_direct_ffd()
                if ffd_rows:
                    self._save_universe_snapshot(ffd_rows)
                    return ffd_rows
            if local_rows:
                # During the trading session local .day files are the previous
                # close. Overlay one batched live snapshot, while retaining the
                # local history required by technical scoring.
                # Synthetic/local catalogue rows without an exchange timestamp
                # are not safe to overlay with a live quote batch (and make
                # tests or offline repair needlessly depend on Tencent).
                has_exchange_timestamp = any(
                    row.get("as_of") or row.get("quote_time")
                    for row in local_rows
                    if isinstance(row, Mapping)
                )
                if self._is_live_index_window(datetime.now()) and has_exchange_timestamp:
                    live_rows = overlay_remote(
                        local_rows,
                        target_date=datetime.now().strftime("%Y-%m-%d"),
                        live=True,
                    )
                    self._save_universe_snapshot(live_rows)
                    return live_rows
                local_dates = [
                    _exchange_trade_date(row.get("as_of") or row.get("quote_time"))
                    for row in local_rows
                ]
                local_latest = max((date for date in local_dates if date), default="")
                if local_latest and local_latest < expected_date:
                    persisted = self._load_universe_snapshot()
                    persisted_dates = [
                        _exchange_trade_date(row.get("quote_time") or row.get("as_of"))
                        for row in persisted
                        if isinstance(row, Mapping)
                    ]
                    persisted_latest = max(
                        (date for date in persisted_dates if date), default=""
                    )
                    if persisted_latest >= expected_date:
                        persisted_by_code = {
                            str(row.get("code")): row
                            for row in persisted
                            if isinstance(row, Mapping)
                        }
                        if sum(1 for row in local_rows if str(row.get("code")) in persisted_by_code) >= int(len(local_rows) * 0.80):
                            quote_fields = {
                                "name", "price", "change_pct", "change", "volume", "amount",
                                "amplitude_pct", "turnover_pct", "pe_ttm", "volume_ratio",
                                "high", "low", "open", "last_close", "mcap", "float_mcap",
                                "pb", "change_60d_pct", "change_ytd_pct", "market_cap",
                                "float_market_cap", "turnover", "amplitude", "quote_time",
                                "available",
                            }
                            reused: list[dict[str, Any]] = []
                            for local in local_rows:
                                remote = persisted_by_code.get(str(local.get("code")))
                                row = dict(local)
                                if remote:
                                    row.update({
                                        key: remote[key]
                                        for key in quote_fields
                                        if remote.get(key) not in (None, "")
                                    })
                                    row["source"] = "tencent_daily+tdx_postclose_day"
                                    row["live_quote"] = False
                                    row["reference_quote"] = True
                                    row["stale"] = False
                                reused.append(row)
                            return reused
                    # A stale local TDX download is common after a holiday or
                    # interrupted update. Repair it from the daily market
                    # snapshot while retaining local K-line history.
                    try:
                        repaired = overlay_remote(
                            local_rows, target_date=expected_date, live=False
                        )
                        self._save_universe_snapshot(repaired)
                        return repaired
                    except Exception:
                        for row in local_rows:
                            row["stale"] = True
                            row["data_lag_days"] = max(
                                0,
                                (datetime.strptime(expected_date, "%Y-%m-%d").date()
                                 - datetime.strptime(local_latest, "%Y-%m-%d").date()).days,
                            )
                local_rows, ffd_merged = self._merge_ffd_daily(local_rows)
                if not ffd_merged:
                    for row in local_rows:
                        row["source"] = "tdx_postclose_day"
                self._save_universe_snapshot(local_rows)
                return local_rows
            ffd_rows = load_direct_ffd()
            if ffd_rows:
                self._save_universe_snapshot(ffd_rows)
                return ffd_rows
            records = load_eastmoney()
            # Eastmoney can return a valid HTTP payload containing only a
            # narrow slice. If most usable rows are absent, retry the same
            # full-market request through FFD before accepting that slice.
            if self._needs_direct_ffd_universe(records):
                ffd_rows = load_direct_ffd()
                if ffd_rows:
                    self._save_universe_snapshot(ffd_rows)
                    return ffd_rows
            self._save_universe_snapshot(records)
            return records

        rows, meta = self._cached_fetch(
            "market_universe",
            ttl=self.ttls["universe"],
            source="hybrid_market_universe",
            loader=load,
            empty=[],
            max_stale=14 * 24 * 3600,
            fallback=lambda: self._tdx_quote_universe()
            or self._load_universe_snapshot()
            or self._fallback_universe(),
            fallback_source="persisted_universe_or_local_catalogue",
            force=force,
            clone=False,
        )
        filtered, stats = filter_stock_universe(rows)
        self._last_universe_stats = stats
        if limit is not None:
            filtered = filtered[: max(0, int(limit))]
        # The universe contains each stock's historical K-line list. Keep the
        # top-level rows isolated while avoiding a second deep copy of the
        # entire full-market history on every cache hit.
        decorated = []
        for raw in filtered:
            item = dict(raw)
            item.setdefault("source", meta["source"])
            item.setdefault("fetched_at", meta["fetched_at"])
            item["stale"] = bool(item.get("stale") or meta.get("stale"))
            decorated.append(item)
        for item in decorated:
            item["universe_stats"] = dict(stats)
            # Five-level public quotes are retained.  L2 ten-level data is not
            # licensed in the current account, so missing values stay null and
            # can never become a numeric zero in a factor score.
            item["l2_available"] = False
            item["l2_ten_level"] = None
            item["l2_imbalance"] = None
            item["l2_score_eligible"] = False
        return decorated

    def get_ths_hot_stocks(self, trade_date: str | None = None) -> dict[str, Any]:
        """Return the public Tonghuashun strong/popular stock pool.

        The endpoint is the zero-auth ``getharden`` feed documented by the
        local A-share data skill.  It supplies the editor-curated popularity
        reason, turnover and large-order activity for each strong stock.  A
        failure is exposed explicitly so downstream screeners can fail closed
        instead of silently admitting non-popular stocks.
        """

        requested = str(trade_date or datetime.now().strftime("%Y-%m-%d"))[:10]
        if re.fullmatch(r"\d{8}", requested):
            requested = f"{requested[:4]}-{requested[4:6]}-{requested[6:8]}"

        def load() -> list[dict[str, Any]]:
            url = (
                "http://zx.10jqka.com.cn/event/api/getharden/"
                f"date/{requested}/orderby/date/orderway/desc/charset/GBK/"
            )
            payload = self._request(
                "GET",
                url,
                headers={"User-Agent": UA, "Referer": "http://zx.10jqka.com.cn/"},
            ).json()
            if int(payload.get("errocode") or 0) != 0:
                raise ProviderError(str(payload.get("errormsg") or "Tonghuashun hot pool failed"))
            rows: list[dict[str, Any]] = []
            for rank, raw in enumerate(payload.get("data") or [], 1):
                if not isinstance(raw, Mapping):
                    continue
                try:
                    code = _normalise_code(str(raw.get("code") or ""))
                except ValueError:
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": _clean_text(raw.get("name")),
                        "rank": rank,
                        "reason": _clean_text(raw.get("reason"), 240),
                        "trade_date": str(raw.get("date") or requested)[:10],
                        "change_pct": _float(raw.get("zhangfu")),
                        "turnover": _float(raw.get("huanshou")),
                        # getharden reports chengjiaoe in ten-thousand yuan.
                        "amount": _float(raw.get("chengjiaoe")) * 10_000.0,
                        "large_order_net_ratio": _optional_float(raw.get("ddejingliang")),
                        "source": "ths_hot_reason",
                    }
                )
            if not rows:
                raise ProviderError(f"Tonghuashun hot pool returned no rows for {requested}")
            return rows

        rows, meta = self._cached_fetch(
            f"ths_hot:{requested}",
            ttl=self.ttls["ths_hot"],
            source="ths_hot_reason",
            loader=load,
            empty=[],
            max_stale=3 * 24 * 3600,
        )
        return {
            "trade_date": requested,
            "rows": rows,
            "count": len(rows),
            "available": bool(rows),
            "source": "ths_hot_reason",
            "_meta": meta,
        }

    def get_market_universe_stats(self) -> dict[str, int]:
        """Return the most recent full-market exclusion counters."""

        return dict(self._last_universe_stats)

    def _tdx_concept_blocks(self) -> dict[str, dict[str, Any]]:
        """Read TDX concept-board members from ``infoharbor_block.dat``."""
        path = self.tdx_cache_dir / "infoharbor_block.dat"
        if not path.is_file():
            return {}
        try:
            lines = path.read_bytes().decode("gbk", errors="ignore").splitlines()
        except OSError:
            return {}
        result: dict[str, dict[str, Any]] = {}
        current: dict[str, Any] | None = None
        for line in lines:
            if line.startswith("#"):
                current = None
                if not line.startswith("#GN_"):
                    continue
                header = line[4:].split(",")
                name = header[0].strip() if header else ""
                if not name:
                    continue
                current = {
                    "name": name,
                    "code": header[2].strip() if len(header) > 2 else "",
                    "updated_at": header[4].strip() if len(header) > 4 else "",
                    "members": set(),
                }
                result[name] = current
                continue
            if current is None:
                continue
            for token in line.split(","):
                match = re.search(r"(?:^|#)(\d{6})$", token.strip())
                if not match:
                    continue
                code = match.group(1)
                if is_a_share_security(code) and not is_star_security(code):
                    current["members"].add(code)
        return result

    def get_stock_board_memberships(self) -> dict[str, list[str]]:
        """Return local TDX industry/concept memberships keyed by stock code."""
        memberships: dict[str, set[str]] = {}
        for code, industry in self._tdx_industry_map().items():
            if industry:
                memberships.setdefault(code, set()).add(industry)
        for name, block in self._tdx_concept_blocks().items():
            for code in block.get("members", set()):
                memberships.setdefault(str(code), set()).add(name)
        return {code: sorted(names) for code, names in memberships.items()}

    def _tdx_board_rows(self, board_type: str) -> list[dict[str, Any]]:
        universe = self._tdx_day_universe()
        by_code = {str(item.get("code")): item for item in universe}
        if not by_code:
            return []
        groups: dict[str, dict[str, Any]] = {}
        if board_type == "concept":
            groups = self._tdx_concept_blocks()
        else:
            for code, item in by_code.items():
                industry = str(item.get("industry") or "").strip()
                if not industry:
                    continue
                group = groups.setdefault(industry, {"name": industry, "code": "", "members": set()})
                group["members"].add(code)
        rows: list[dict[str, Any]] = []
        for group in groups.values():
            members = [by_code[code] for code in group.get("members", set()) if code in by_code]
            if len(members) < 2:
                continue
            changes = [_float(item.get("change_pct")) for item in members]
            histories = [
                _float(item.get("returns_10d"))
                for item in members
                if item.get("returns_10d") not in (None, "")
            ]
            history_by_date: dict[str, list[float]] = {}
            for item in members:
                for point in item.get("history", []):
                    if not isinstance(point, dict) or not point.get("date"):
                        continue
                    history_by_date.setdefault(str(point["date"]), []).append(_float(point.get("change_pct")))
            history = [
                {"date": date, "change_pct": round(sum(values) / len(values), 4)}
                for date, values in sorted(history_by_date.items())
                if values
            ][-10:]
            leader = max(members, key=lambda item: _float(item.get("change_pct")))
            rows.append(
                {
                    "code": str(group.get("code") or ""),
                    "name": str(group.get("name") or ""),
                    "change_pct": round(sum(changes) / len(changes), 4),
                    "returns_10d": round(sum(histories) / len(histories), 4) if histories else None,
                    "history": history,
                    "up_count": sum(1 for value in changes if value > 0),
                    "down_count": sum(1 for value in changes if value < 0),
                    "member_count": len(members),
                    "turnover": sum(_float(item.get("amount")) for item in members),
                    "limit_up_count": sum(1 for value in changes if value >= 9.5),
                    "leader": leader.get("name", ""),
                    "leader_code": leader.get("code", ""),
                    "as_of": max(str(item.get("as_of") or "") for item in members),
                }
            )
        rows.sort(
            key=lambda item: (_float(item.get("returns_10d")), _float(item.get("change_pct"))),
            reverse=True,
        )
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        return rows

    def _tdx_index_rotation_matrix(self, days: int, top_n: int) -> dict[str, Any]:
        """Use TDX's own 880xxx board indexes, matching the terminal rotation view."""

        config = self.tdx_cache_dir / "tdxzs.cfg"
        if not config.is_file():
            return {}
        try:
            lines = config.read_bytes().decode("gbk", errors="ignore").splitlines()
        except OSError:
            return {}
        by_date: dict[str, list[dict[str, Any]]] = {}
        board_series: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            parts = line.split("|")
            if len(parts) < 3:
                continue
            name, code, category = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not name or not re.fullmatch(r"880\d{3}", code) or category not in {"2", "3", "4"}:
                continue
            history = self._tdx_day_rows(f"sh{code}", days + 1)
            if len(history) < 2:
                continue
            history = history[-days:]
            board_series[name] = history
            for point in history:
                if point.get("change_pct") is not None:
                    by_date.setdefault(str(point.get("date")), []).append(
                        {"name": name, "change_pct": _float(point.get("change_pct"))}
                    )
        if not by_date:
            return {}
        return self._rotation_matrix_payload(
            by_date=by_date, board_series=board_series, days=days, top_n=top_n,
            board_type="all", source="tdx_board_index_day",
        )

    @staticmethod
    def _rotation_matrix_payload(
        *, by_date: dict[str, list[dict[str, Any]]], board_series: dict[str, list[dict[str, Any]]],
        days: int, top_n: int, board_type: str, source: str,
    ) -> dict[str, Any]:
        """Build columns and style summaries from a board-index daily-return series."""

        columns = []
        for date in sorted(by_date)[-days:]:
            values = sorted(by_date[date], key=lambda item: item["change_pct"], reverse=True)
            columns.append({"date": date, "top": values[:top_n], "bottom": list(reversed(values[-top_n:]))})
        defensive_terms = ("\u94f6\u884c", "\u4fdd\u9669", "\u516c\u7528\u4e8b\u4e1a", "\u71c3\u6c14", "\u7535\u529b", "\u9ad8\u80a1\u606f", "\u7164\u70ad")
        offensive_terms = ("\u4eba\u5de5\u667a\u80fd", "AI", "\u82af\u7247", "\u534a\u5bfc\u4f53", "\u96c6\u6210\u7535\u8def", "\u8f6f\u4ef6", "\u4e91", "\u901a\u4fe1", "\u7535\u5b50", "\u673a\u5668\u4eba", "\u81ea\u52a8\u5316", "\u7b97\u529b", "\u4e92\u8054\u7f51", "\u4f4e\u7a7a")
        summary = []
        for name, series in board_series.items():
            values = [_float(point.get("change_pct")) for point in series if point.get("change_pct") is not None]
            if not values:
                continue
            cumulative = 1.0
            for value in values:
                cumulative *= 1.0 + value / 100.0
            style = "defensive" if any(term in name for term in defensive_terms) else "offensive" if any(term.lower() in name.lower() for term in offensive_terms) else "neutral"
            summary.append({"name": name, "style": style, "days_covered": len(values), "return_10d": round((cumulative - 1.0) * 100.0, 4), "max_daily_pct": round(max(values), 4), "min_daily_pct": round(min(values), 4)})
        summary.sort(key=lambda item: item["return_10d"], reverse=True)
        return {"board_type": board_type, "days": days, "top_n": top_n, "columns": columns, "summary": summary, "leaders": summary[:top_n], "laggards": list(reversed(summary[-top_n:])), "offensive": [item for item in summary if item["style"] == "offensive"][:top_n], "defensive": [item for item in summary if item["style"] == "defensive"][:top_n], "source": source, "available": len(columns) >= 2, "note": "TDX board-index daily returns."}

    def get_board_rotation_matrix(
        self, board_type: str = "industry", days: int = 10, top_n: int = 10
    ) -> dict[str, Any]:
        """Build a real daily rotation matrix from local TDX stock .day bars."""

        board_type = board_type if board_type in {"industry", "concept", "all"} else "all"
        days = max(2, min(int(days), 20))
        top_n = max(1, min(int(top_n), 20))
        if board_type == "all":
            direct = self._tdx_index_rotation_matrix(days, top_n)
            if direct:
                return direct
        ranking = self.get_industry_ranking(100) if board_type == "industry" else self.get_concept_ranking(100)
        rows = ranking.get("rows") or []
        by_date: dict[str, list[dict[str, Any]]] = {}
        board_series: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            name = str(row.get("name") or "")
            if name:
                board_series[name] = [dict(point) for point in row.get("history", []) if isinstance(point, dict)]
            for point in row.get("history", []):
                if not isinstance(point, dict) or point.get("change_pct") is None:
                    continue
                by_date.setdefault(str(point.get("date")), []).append(
                    {"name": row.get("name", ""), "change_pct": _float(point.get("change_pct"))}
                )
        columns = []
        for date in sorted(by_date)[-days:]:
            values = sorted(by_date[date], key=lambda item: item["change_pct"], reverse=True)
            columns.append({"date": date, "top": values[:top_n], "bottom": list(reversed(values[-top_n:]))})
        defensive_terms = ("\u94f6\u884c", "\u4fdd\u9669", "\u516c\u7528\u4e8b\u4e1a", "\u71c3\u6c14", "\u7535\u529b", "\u9ad8\u80a1\u606f", "\u7164\u70ad")
        offensive_terms = ("\u4eba\u5de5\u667a\u80fd", "AI", "\u82af\u7247", "\u534a\u5bfc\u4f53", "\u96c6\u6210\u7535\u8def", "\u8f6f\u4ef6", "\u4e91", "\u901a\u4fe1", "\u7535\u5b50", "\u673a\u5668\u4eba", "\u81ea\u52a8\u5316", "\u7b97\u529b", "\u4e92\u8054\u7f51", "\u4f4e\u7a7a")
        summary = []
        for name, series in board_series.items():
            values = [_float(point.get("change_pct")) for point in series if point.get("change_pct") is not None]
            if not values:
                continue
            cumulative = 1.0
            for value in values:
                cumulative *= 1.0 + value / 100.0
            summary.append({
                "name": name,
                "style": "defensive" if any(term in name for term in defensive_terms) else "offensive" if any(term.lower() in name.lower() for term in offensive_terms) else "neutral",
                "days_covered": len(values),
                "return_10d": round((cumulative - 1.0) * 100.0, 4),
                "max_daily_pct": round(max(values), 4),
                "min_daily_pct": round(min(values), 4),
            })
        summary.sort(key=lambda item: item["return_10d"], reverse=True)
        groups = {
            style: [item for item in summary if item["style"] == style]
            for style in ("offensive", "defensive")
        }
        return {
            "board_type": board_type,
            "days": days,
            "top_n": top_n,
            "columns": columns,
            "summary": summary,
            "leaders": summary[:top_n],
            "laggards": list(reversed(summary[-top_n:])),
            "offensive": groups["offensive"][:top_n],
            "defensive": groups["defensive"][:top_n],
            "source": "tdx_postclose_day",
            "available": len(columns) >= 2,
            "note": "Daily board ranks are aggregated from current TDX membership and each member's local .day return.",
        }

    def get_boards(self, code: str | int) -> dict[str, Any]:
        code = _normalise_code(code)

        def load() -> dict[str, Any]:
            params = {
                "fltt": "2",
                "invt": "2",
                "secid": _eastmoney_secid(code),
                "spt": "3",
                "pi": "0",
                "pz": "200",
                "po": "1",
                "fields": "f12,f14,f3,f128,f140",
            }
            payload = self._em_get(
                "https://push2.eastmoney.com/api/qt/slist/get",
                params=params,
                headers={"Referer": "https://quote.eastmoney.com/"},
            ).json()
            boards = [
                {
                    "code": item.get("f12", ""),
                    "name": item.get("f14", ""),
                    "change_pct": _float(item.get("f3")),
                    "lead_stock": item.get("f128", ""),
                    "lead_code": item.get("f140", ""),
                }
                for item in _items((payload.get("data") or {}).get("diff"))
            ]
            if not boards:
                raise ProviderError(f"no board membership returned for {code}")
            return {
                "code": code,
                "total": len(boards),
                "boards": boards,
                "concept_tags": [item["name"] for item in boards if item["name"]],
            }

        data, meta = self._cached_fetch(
            f"boards:{code}",
            ttl=self.ttls["boards"],
            source="eastmoney_slist",
            loader=load,
            empty={"code": code, "total": 0, "boards": [], "concept_tags": []},
            max_stale=7 * 24 * 3600,
        )
        return self._decorate_dict(data, meta)

    def get_industry_ranking(self, top_n: int = 20) -> dict[str, Any]:
        top_n = max(1, min(int(top_n), 100))

        local, local_meta = self._cached_fetch(
            "tdx_industry_ranking",
            ttl=self.ttls["industries"],
            source="tdx_postclose_industry",
            loader=lambda: self._tdx_board_rows("industry"),
            empty=[],
            max_stale=7 * 24 * 3600,
        )
        if local:
            return self._decorate_dict(
                {
                    "total": len(local),
                    "top": local[:top_n],
                    "bottom": local[-top_n:],
                    "rows": local,
                    "data_mode": "post_close",
                },
                local_meta,
            )

        def load() -> dict[str, Any]:
            params = {
                "pn": "1",
                "pz": "100",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:2",
                "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f20,f62,f104,f105,f128,f136,f140,f184,f186",
            }
            payload = self._em_get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params=params,
                headers={"Referer": "https://quote.eastmoney.com/"},
            ).json()
            rows = []
            for index, item in enumerate(_items((payload.get("data") or {}).get("diff"))):
                rows.append(
                    {
                        "rank": index + 1,
                        "code": item.get("f12", ""),
                        "name": item.get("f14", ""),
                        "price": _float(item.get("f2")),
                        "change_pct": _float(item.get("f3")),
                        "change_amt": _float(item.get("f4")),
                        "velocity_pct": _float(item.get("f5")),       # 5分钟涨速
                        "turnover": _float(item.get("f6")),           # 成交额(元)
                        "turnover_display": _float(item.get("f7")),   # 成交额(展示值)
                        "total_mv": _float(item.get("f20")),          # 总市值
                        "main_net_inflow": _float(item.get("f62")),   # 主力净流入(元)
                        "main_inflow": _float(item.get("f184")),      # 主力流入(元)
                        "main_outflow": _float(item.get("f186")),     # 主力流出(元)
                        "up_count": _int(item.get("f104")),
                        "down_count": _int(item.get("f105")),
                        "limit_up_count": _int(item.get("f136")),     # 涨停家数
                        "leader": item.get("f128") or item.get("f140") or "",
                        "leader_code": item.get("f140", ""),
                    }
                )
            if not rows:
                raise ProviderError("Eastmoney industry ranking was empty")
            return {"rows": rows, "total": len(rows)}

        data, meta = self._cached_fetch(
            "industry_ranking",
            ttl=self.ttls["industries"],
            source="eastmoney_industry_clist",
            loader=load,
            empty={"rows": [], "total": 0},
            max_stale=24 * 3600,
        )
        rows = data.get("rows", [])
        result = {
            "total": data.get("total", len(rows)),
            "top": rows[:top_n],
            "bottom": rows[-top_n:] if rows else [],
            "rows": rows,
        }
        return self._decorate_dict(result, meta)

    def get_concept_ranking(self, top_n: int = 20) -> dict[str, Any]:
        """Fetch real-time concept board ranking from East Money (概念板块).

        Returns per-board: 涨跌幅/涨速/成交额/涨停家数/主力资金流入流出.
        """
        top_n = max(1, min(int(top_n), 100))

        local, local_meta = self._cached_fetch(
            "tdx_concept_ranking",
            ttl=self.ttls["industries"],
            source="tdx_postclose_concept",
            loader=lambda: self._tdx_board_rows("concept"),
            empty=[],
            max_stale=7 * 24 * 3600,
        )
        if local:
            return self._decorate_dict(
                {
                    "total": len(local),
                    "top": local[:top_n],
                    "bottom": local[-top_n:],
                    "rows": local,
                    "data_mode": "post_close",
                },
                local_meta,
            )

        def load() -> dict[str, Any]:
            params = {
                "pn": "1",
                "pz": "100",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:3",
                "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f20,f62,f104,f105,f128,f136,f140,f184,f186",
            }
            payload = self._em_get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params=params,
                headers={"Referer": "https://quote.eastmoney.com/"},
            ).json()
            rows = []
            for index, item in enumerate(_items((payload.get("data") or {}).get("diff"))):
                rows.append(
                    {
                        "rank": index + 1,
                        "code": item.get("f12", ""),
                        "name": item.get("f14", ""),
                        "price": _float(item.get("f2")),
                        "change_pct": _float(item.get("f3")),
                        "change_amt": _float(item.get("f4")),
                        "velocity_pct": _float(item.get("f5")),
                        "turnover": _float(item.get("f6")),
                        "turnover_display": _float(item.get("f7")),
                        "total_mv": _float(item.get("f20")),
                        "main_net_inflow": _float(item.get("f62")),
                        "main_inflow": _float(item.get("f184")),
                        "main_outflow": _float(item.get("f186")),
                        "up_count": _int(item.get("f104")),
                        "down_count": _int(item.get("f105")),
                        "limit_up_count": _int(item.get("f136")),
                        "leader": item.get("f128") or item.get("f140") or "",
                        "leader_code": item.get("f140", ""),
                    }
                )
            if not rows:
                raise ProviderError("Eastmoney concept ranking was empty")
            return {"rows": rows, "total": len(rows)}

        data, meta = self._cached_fetch(
            "concept_ranking",
            ttl=self.ttls["industries"],
            source="eastmoney_concept_clist",
            loader=load,
            empty={"rows": [], "total": 0},
            max_stale=24 * 3600,
        )
        rows = data.get("rows", [])
        result = {
            "total": data.get("total", len(rows)),
            "top": rows[:top_n],
            "bottom": rows[-top_n:] if rows else [],
            "rows": rows,
        }
        return self._decorate_dict(result, meta)

    def get_fund_flow(self, code: str | int, period: str = "minute") -> dict[str, Any]:
        code = _normalise_code(code)
        period_key = period.strip().lower()
        daily = period_key in {"day", "daily", "history", "120d"}

        def load() -> dict[str, Any]:
            if daily:
                url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
                params = {
                    "secid": _eastmoney_secid(code),
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "lmt": "120",
                }
            else:
                url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
                params = {
                    "secid": _eastmoney_secid(code),
                    "klt": "1",
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57",
                }
            payload = self._em_get(
                url,
                params=params,
                headers={
                    "Referer": "https://quote.eastmoney.com/",
                    "Origin": "https://quote.eastmoney.com",
                },
            ).json()
            raw_rows = (payload.get("data") or {}).get("klines") or []
            rows = []
            for line in raw_rows:
                parts = str(line).split(",")
                if len(parts) < 6:
                    continue
                rows.append(
                    {
                        "date" if daily else "time": parts[0],
                        "main_net": _float(parts[1]),
                        "small_net": _float(parts[2]),
                        "mid_net": _float(parts[3]),
                        "large_net": _float(parts[4]),
                        "super_net": _float(parts[5]),
                    }
                )
            if not rows:
                raise ProviderError(f"no {period_key} fund flow returned for {code}")
            totals = {
                field: sum(_float(row.get(field)) for row in rows)
                for field in ("main_net", "small_net", "mid_net", "large_net", "super_net")
            }
            return {
                "code": code,
                "period": "day" if daily else "minute",
                "rows": rows,
                "latest": rows[-1],
                "totals": totals,
                "point_count": len(rows),
                "points": len(rows),
                "total_main_net": totals["main_net"],
            }

        ttl_name = "fund_day" if daily else "fund_minute"
        source = "eastmoney_fund_flow_daily" if daily else "eastmoney_fund_flow_minute"
        data, meta = self._cached_fetch(
            f"fund_flow:{code}:{'day' if daily else 'minute'}",
            ttl=self.ttls[ttl_name],
            source=source,
            loader=load,
            empty={
                "code": code,
                "period": "day" if daily else "minute",
                "rows": [],
                "latest": {},
                "totals": {},
                "point_count": 0,
                "points": 0,
                "total_main_net": 0.0,
            },
            max_stale=7 * 24 * 3600 if daily else 6 * 3600,
        )
        return self._decorate_dict(data, meta)

    def get_holder_info(self, code: str | int, limit: int = 10) -> dict[str, Any]:
        code = _normalise_code(code)
        limit = max(1, min(int(limit), 50))

        def load() -> dict[str, Any]:
            holders = self._datacenter(
                "RPT_HOLDERNUMLATEST",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=limit,
                sort_columns="END_DATE",
                sort_types="-1",
            )
            rows = [
                {
                    "date": str(row.get("END_DATE") or "")[:10],
                    "holder_num": _int(row.get("HOLDER_NUM")),
                    "change_num": _int(row.get("HOLDER_NUM_CHANGE")),
                    "change_ratio": _float(row.get("HOLDER_NUM_RATIO")),
                    "avg_shares": _float(row.get("AVG_FREE_SHARES") or row.get("AVG_HOLD_NUM")),
                }
                for row in holders
            ]
            if not rows:
                raise ProviderError(f"no holder disclosure returned for {code}")
            return {"code": code, "latest": rows[0], "rows": rows}

        data, meta = self._cached_fetch(
            f"holders:{code}:{limit}",
            ttl=self.ttls["holders"],
            source="eastmoney_holder_disclosure",
            loader=load,
            empty={"code": code, "latest": {}, "rows": []},
            max_stale=60 * 24 * 3600,
        )
        return self._decorate_dict(data, meta)

    def get_stock_info(
        self, code: str | int, *, include_holders: bool = False
    ) -> dict[str, Any]:
        code = _normalise_code(code)

        def load() -> dict[str, Any]:
            payload = self._em_get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={
                    "fltt": "2",
                    "invt": "2",
                    "fields": "f43,f57,f58,f84,f85,f116,f117,f127,f189",
                    "secid": _eastmoney_secid(code),
                },
                headers={"Referer": "https://quote.eastmoney.com/"},
            ).json()
            item = payload.get("data") or {}
            if not item or not item.get("f58"):
                raise ProviderError(f"no stock information returned for {code}")
            return {
                "code": str(item.get("f57") or code),
                "name": item.get("f58", ""),
                "industry": item.get("f127", ""),
                "total_shares": _float(item.get("f84")),
                "float_shares": _float(item.get("f85")),
                "mcap": _float(item.get("f116")),
                "float_mcap": _float(item.get("f117")),
                "list_date": str(item.get("f189") or ""),
                "price": _float(item.get("f43")),
            }

        def quote_fallback() -> dict[str, Any]:
            quote = self.get_quote(code)
            return {
                "code": code,
                "name": quote.get("name", ""),
                "industry": "",
                "total_shares": 0,
                "float_shares": 0,
                "mcap": _float(quote.get("mcap_yi")) * 1e8,
                "float_mcap": _float(quote.get("float_mcap_yi")) * 1e8,
                "list_date": "",
                "price": _float(quote.get("price")),
            }

        data, meta = self._cached_fetch(
            f"stock_info:{code}",
            ttl=self.ttls["stock_info"],
            source="eastmoney_stock_info",
            loader=load,
            empty={"code": code},
            max_stale=30 * 24 * 3600,
            fallback=quote_fallback,
            fallback_source="tencent_quote",
        )
        result = self._decorate_dict(data, meta)
        if include_holders:
            result["holders"] = self.get_holder_info(code)
        return result

    def get_financial_metrics(self, code: str | int) -> dict[str, Any]:
        """Return a compact latest-period financial view for one A-share."""

        code = _normalise_code(code)
        standard_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        def load_ffd() -> dict[str, Any]:
            if not self.ffd_enabled:
                raise ProviderError("FFD is disabled")
            self._record_ffd_unbudgeted("financial_metrics")
            payload = self._ffd.call(
                "ffd_financial_metrics",
                {
                    "query": f"{standard_code} 最新一期ROE、营收同比、归母净利润同比、销售毛利率、资产负债率和经营现金流",
                    "codes": standard_code,
                    "metrics": "ROE;营收同比;净利润同比;毛利率;资产负债率;经营现金流",
                    "period_policy": "latest_market",
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = self._ffd_rows(payload)
            if not rows:
                raise ProviderError(f"FFD financial metrics returned no rows for {code}")
            row = rows[0]
            aliases = (
                ("roe", "ROE", ("净资产收益率", "roe")),
                ("revenue_yoy", "营收同比", ("营业收入", "营收同比")),
                ("net_profit_yoy", "归母净利润同比", ("归属于母公司", "净利润同比")),
                ("gross_margin", "销售毛利率", ("销售毛利率", "毛利率")),
                ("debt_ratio", "资产负债率", ("资产负债率",)),
                ("operating_cash_flow", "经营现金流", ("经营活动现金流", "经营现金流")),
            )
            metrics: list[dict[str, Any]] = []
            periods: list[str] = []
            used: set[str] = set()
            for metric_key, label, terms in aliases:
                matched_key = next(
                    (
                        str(key) for key in row
                        if str(key) not in used
                        and any(term.lower() in str(key).lower() for term in terms)
                    ),
                    "",
                )
                if not matched_key:
                    continue
                value = row.get(matched_key)
                if value in (None, "", "--", "-"):
                    continue
                used.add(matched_key)
                period_match = re.search(r"\[(\d{8})\]", matched_key)
                period = period_match.group(1) if period_match else ""
                if period:
                    periods.append(period)
                unit = "%" if metric_key != "operating_cash_flow" else ""
                metrics.append(
                    {"key": metric_key, "label": label, "value": _optional_float(value), "unit": unit, "period": period}
                )
            if not metrics:
                raise ProviderError(f"FFD financial fields were not recognized for {code}")
            return {
                "code": code,
                "name": str(row.get("股票简称") or ""),
                "report_date": max(periods, default=""),
                "metrics": metrics,
                "source": "ffd_financial_metrics",
            }

        def quote_fallback() -> dict[str, Any]:
            quote = self.get_quote(code)
            metrics = []
            for key, label, value in (
                ("pe_ttm", "市盈率TTM", quote.get("pe_ttm")),
                ("pb", "市净率", quote.get("pb")),
            ):
                parsed = _optional_float(value)
                if parsed is not None:
                    metrics.append({"key": key, "label": label, "value": parsed, "unit": "倍", "period": ""})
            return {
                "code": code,
                "name": quote.get("name", ""),
                "report_date": "",
                "metrics": metrics,
                "source": "quote_valuation_fallback",
                "warning": "完整财务指标暂不可用，仅展示估值快照",
            }

        data, meta = self._cached_fetch(
            f"financial_metrics:{code}",
            ttl=self.ttls["financial_metrics"],
            source="ffd_financial_metrics",
            loader=load_ffd,
            empty={"code": code, "metrics": []},
            max_stale=7 * 24 * 3600,
            fallback=quote_fallback,
            fallback_source="quote_valuation_fallback",
        )
        return self._decorate_dict(data, meta)

    def get_stock_news(self, code: str | int, limit: int = 20) -> list[dict[str, Any]]:
        code = _normalise_code(code)
        limit = max(1, min(int(limit), 3000))
        try:
            stock_name = str(self.get_quote(code).get("name") or "").strip()
        except Exception:
            stock_name = ""
        search_query = f"{code} {stock_name}".strip()

        def load_ffd() -> list[dict[str, Any]]:
            if not self.ffd_enabled:
                raise ProviderError("FFD is disabled")
            self._record_ffd_unbudgeted("stock_news")
            payload = self._ffd.call(
                "ffd_news_search",
                {
                    "q": search_query,
                    "days": 30,
                    "limit": limit,
                    "sentiment": True,
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = self._normalise_ffd_news(payload, limit)
            if not rows:
                raise ProviderError(f"FFD stock news returned no rows for {code}")
            return rows

        def load_eastmoney() -> list[dict[str, Any]]:
            callback = "jQuery_news"
            inner = json.dumps(
                {
                    "uid": "",
                    "keyword": code,
                    "type": ["cmsArticleWebOld"],
                    "client": "web",
                    "clientType": "web",
                    "clientVersion": "curr",
                    "param": {
                        "cmsArticleWebOld": {
                            "searchScope": "default",
                            "sort": "default",
                            "pageIndex": 1,
                            "pageSize": limit,
                            "preTag": "",
                            "postTag": "",
                        }
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            response = self._em_get(
                "https://search-api-web.eastmoney.com/search/jsonp",
                params={"cb": callback, "param": inner},
                headers={"Referer": "https://so.eastmoney.com/"},
            )
            match = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", response.text, re.S)
            if not match:
                raise ProviderError("Eastmoney news returned invalid JSONP")
            payload = json.loads(match.group(1))
            articles: Any = (payload.get("result") or {}).get("cmsArticleWebOld") or []
            if isinstance(articles, dict):
                articles = articles.get("list") or []
            rows = [
                {
                    "title": _clean_text(item.get("title")),
                    "content": _clean_text(item.get("content"), 300),
                    "time": item.get("date", ""),
                    "publisher": item.get("mediaName", ""),
                    "url": item.get("url", ""),
                }
                for item in articles
                if isinstance(item, dict)
            ]
            for row in rows:
                row["source"] = "eastmoney_stock_news"
            return rows[:limit]

        rows, meta = self._cached_fetch(
            f"stock_news_v2:{code}:{limit}",
            ttl=self.ttls["news"],
            source="ffd_stock_news",
            loader=load_ffd,
            empty=[],
            max_stale=0,
            fallback=load_eastmoney,
            fallback_source="eastmoney_stock_news",
        )
        return self._decorate_rows(rows, meta)

    @staticmethod
    def _normalise_ffd_news(
        payload: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Map FFD's public news contract to the workbench news model."""

        raw_rows = MarketDataProvider._ffd_rows(payload)
        now = datetime.now(timezone.utc)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                continue
            title = _clean_text(
                raw.get("normalized_title") or raw.get("headline") or raw.get("raw_source_title")
            )
            if not title or title in seen:
                continue
            published_at = str(raw.get("pub_dt") or raw.get("created_at") or "")
            try:
                parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed.astimezone(timezone.utc) > now + timedelta(minutes=5):
                    continue
            except ValueError:
                pass
            seen.add(title)
            facts = _clean_text(raw.get("facts"), 500)
            insight = _clean_text(raw.get("insight"), 500)
            content = facts
            if insight and insight != facts:
                content = _clean_text(f"{facts} {insight}".strip(), 700)
            result.append(
                {
                    "id": str(raw.get("news_id") or raw.get("fingerprint") or ""),
                    "title": title,
                    "content": content,
                    "time": published_at,
                    "publisher": _clean_text(raw.get("source_label")) or "FFD 新闻",
                    "url": "",
                    "image": "",
                    "sector": _clean_text(raw.get("sector")),
                    "sector_tags": [
                        str(value)
                        for value in (raw.get("sector_tags") or [])
                        if str(value).strip()
                    ],
                    "market_tags": [
                        str(value)
                        for value in (raw.get("market_tags") or [])
                        if str(value).strip()
                    ],
                    "score": _optional_float(raw.get("score")),
                    "sentiment": str(raw.get("sentiment") or ""),
                    "sentiment_label": str(raw.get("sentiment_label") or ""),
                    "source": "ffd_news",
                }
            )
            if len(result) >= limit:
                break
        return result

    def get_market_news(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return FFD market news, with Eastmoney and Tencent as fallbacks."""

        limit = max(1, min(int(limit), 100))

        def load_ffd() -> list[dict[str, Any]]:
            if not self.ffd_enabled:
                raise ProviderError("FFD is disabled")
            self._record_ffd_unbudgeted("market_news")
            payload = self._ffd.call(
                "ffd_news_latest",
                {
                    "limit": limit,
                    "sentiment": True,
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = self._normalise_ffd_news(payload, limit)
            if not rows:
                raise ProviderError("FFD market news returned no rows")
            return rows

        def load_eastmoney() -> list[dict[str, Any]]:
            response = self._em_get(
                "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                params={
                    "client": "web", "biz": "web_news_col", "column": "344",
                    "order": "0", "page_index": "1", "page_size": str(limit),
                    "needInteract": "0", "req_trace": str(time.time_ns()),
                },
                headers={"Referer": "https://finance.eastmoney.com/"},
            )
            payload = response.json()
            rows = ((payload.get("data") or {}).get("list") or [])
            if not isinstance(rows, list):
                raise ProviderError("Eastmoney market news returned an invalid payload")
            result = [
                {
                    "id": str(item.get("code") or item.get("uniqueUrl") or ""),
                    "title": _clean_text(item.get("title")),
                    "content": _clean_text(item.get("summary"), 400),
                    "time": item.get("showTime", ""),
                    "publisher": _clean_text(item.get("mediaName")),
                    "url": item.get("url") or item.get("uniqueUrl") or "",
                    "image": item.get("image") or "",
                }
                for item in rows
                if isinstance(item, dict) and item.get("title")
            ][:limit]
            if not result:
                raise ProviderError("Eastmoney market news returned no rows")
            for row in result:
                row["source"] = "eastmoney_market_news"
            return result

        def load_tencent() -> list[dict[str, Any]]:
            response = self._request(
                "GET",
                "https://i.news.qq.com/gw/event/pc_hot_ranking_list",
                headers={"Referer": "https://news.qq.com/ch/finance"},
            )
            payload = response.json()
            groups = payload.get("idlist") or []
            raw_rows = [
                item
                for group in groups
                if isinstance(group, dict)
                for item in (group.get("newslist") or [])
                if isinstance(item, dict)
            ]
            finance_terms = (
                "股", "市场", "经济", "金融", "银行", "证券", "基金", "债", "汇率",
                "央行", "政策", "产业", "公司", "科技", "芯片", "汽车", "能源", "黄金",
                "原油", "商品", "消费", "地产", "制造", "贸易", "关税", "指数",
            )
            seen: set[str] = set()
            ranked: list[tuple[int, dict[str, Any]]] = []
            for item in raw_rows:
                title = _clean_text(item.get("title") or item.get("longtitle"))
                if not title or title in seen or title.startswith("腾讯新闻用户最关注"):
                    continue
                seen.add(title)
                score = sum(1 for term in finance_terms if term in title)
                ranked.append((score, item))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            result = []
            for _, item in ranked[:limit]:
                article_id = str(item.get("id") or "")
                result.append({
                    "id": article_id,
                    "title": _clean_text(item.get("title") or item.get("longtitle")),
                    "content": _clean_text(item.get("abstract") or item.get("intro"), 400),
                    "time": item.get("publish_time") or item.get("time") or "",
                    "publisher": _clean_text(item.get("source") or item.get("source_name")) or "腾讯新闻",
                    "url": item.get("surl") or item.get("short_url") or (
                        f"https://view.inews.qq.com/a/{article_id}" if article_id else ""
                    ),
                    "image": item.get("img") or item.get("image") or "",
                    "source": "tencent_market_news",
                })
            if not result:
                raise ProviderError("Tencent market news returned no rows")
            return result

        def load_legacy() -> list[dict[str, Any]]:
            try:
                return load_eastmoney()
            except Exception as exc:
                self._record_error("eastmoney_market_news", "market_news_fallback", exc)
                return load_tencent()

        rows, meta = self._cached_fetch(
            f"market_news:{limit}", ttl=self.ttls["news"],
            source="ffd_market_news", loader=load_ffd, empty=[],
            # Prefer a live legacy response to an expired FFD cache.
            max_stale=0,
            fallback=load_legacy,
            fallback_source="legacy_market_news",
        )
        return self._decorate_rows(rows, meta)

    def get_maifu_news(self, limit: int = 3000) -> list[dict[str, Any]]:
        """Return a long-lived FFD news ledger for the maifu calendar."""

        limit = max(1, min(int(limit), 3000))
        # Keep the global-news ledger for three days: long enough to capture
        # follow-up and settlement signals without letting stale events pile up.
        retention_hours = 3 * 24

        def load() -> list[dict[str, Any]]:
            cached_rows = self._load_maifu_news_cache()
            ffd_rows: list[dict[str, Any]] = []
            try:
                ffd_rows = self.get_market_news(max(15, limit))
            except Exception as exc:
                self._last_maifu_news_status = {"ffd": {
                    "name": "FFD 全球新闻池", "ok": False, "count": 0,
                    "error": str(exc)[:240], "latest_minutes": None,
                }}
            else:
                self._last_maifu_news_status = {"ffd": {
                    "name": "FFD 全球新闻池", "ok": bool(ffd_rows), "count": len(ffd_rows),
                    "error": None, "latest_minutes": None,
                }}
            fresh = news_feed.merge_news(
                [ffd_rows], limit=limit, retention_hours=retention_hours
            )
            if fresh:
                merged = news_feed.merge_news(
                    [fresh, cached_rows], limit=limit, retention_hours=retention_hours
                )
                self._save_maifu_news_cache(merged)
                return merged
            merged = news_feed.merge_news(
                [cached_rows], limit=limit, retention_hours=retention_hours
            )
            if not merged:
                raise ProviderError("FFD 财经快讯未返回有效内容")
            return [{**item, "stale": True, "cache_only": True} for item in merged]

        rows, meta = self._cached_fetch(
            f"maifu_news:{limit}",
            ttl=min(120.0, self.ttls["news"]),
            source="ffd_market_news_ledger",
            loader=load,
            empty=[],
            max_stale=12 * 3600,
        )
        return self._decorate_rows(rows, meta)

    def _maifu_news_cache_path(self) -> Path:
        return self.ffd_state_path.parent / "maifu_news_cache.json"

    def _load_maifu_news_cache(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._maifu_news_cache_path().read_text(encoding="utf-8"))
            rows = payload.get("items") if isinstance(payload, Mapping) else []
            return [dict(row) for row in rows or [] if isinstance(row, Mapping)]
        except (OSError, ValueError, TypeError):
            return []

    def _save_maifu_news_cache(self, rows: list[dict[str, Any]]) -> None:
        try:
            path = self._maifu_news_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"saved_at": _now_iso(), "items": rows}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except (OSError, TypeError, ValueError):
            # News cache is an acceleration/recovery layer; disk errors must
            # never make a live FFD response fail.
            return

    def get_maifu_news_status(self) -> dict[str, dict[str, Any]]:
        """Return the latest per-source status for the maifu news feed."""

        return copy.deepcopy(self._last_maifu_news_status)

    def _cninfo_orgid(self, code: str) -> str:
        with self._cninfo_lock:
            if code in self._cninfo_orgs:
                return self._cninfo_orgs[code]
            if not self._cninfo_orgs:
                try:
                    payload = self._request(
                        "GET", "https://www.cninfo.com.cn/new/data/szse_stock.json"
                    ).json()
                    self._cninfo_orgs = {
                        str(item.get("code")): str(item.get("orgId"))
                        for item in payload.get("stockList", [])
                        if item.get("code") and item.get("orgId")
                    }
                    self._record_success("cninfo_org_map")
                except Exception as exc:
                    self._record_error("cninfo_org_map", "org_map", exc)
            org_id = self._cninfo_orgs.get(code)
            if org_id:
                return org_id
        if code.startswith("6"):
            return f"gssh0{code}"
        if code.startswith(("4", "8")):
            return f"gsbj0{code}"
        return f"gssz0{code}"

    def get_announcements(self, code: str | int, limit: int = 20) -> list[dict[str, Any]]:
        code = _normalise_code(code)
        limit = max(1, min(int(limit), 100))

        def load_ffd() -> list[dict[str, Any]]:
            if not self.ffd_enabled:
                raise ProviderError("FFD is disabled")
            standard = _ffd_standard_code(code)
            self._record_ffd_unbudgeted("announcements")
            payload = self._ffd.call(
                "ffd_announcements",
                {
                    "codes": standard,
                    "output_mode": "raw",
                    "format": "json",
                },
            )
            rows = []
            for raw in self._ffd_rows(payload):
                title = _clean_text(
                    raw.get("reportTitle") or raw.get("title") or raw.get("公告标题")
                )
                if not title:
                    continue
                rows.append(
                    {
                        "title": title,
                        "type": _clean_text(raw.get("reportType") or raw.get("type")),
                        "date": str(
                            raw.get("reportDate") or raw.get("date")
                            or raw.get("publish_time") or ""
                        )[:10],
                        "announcement_id": str(raw.get("seq") or raw.get("id") or ""),
                        "url": str(raw.get("pdf_url") or raw.get("url") or ""),
                        "source": "ffd_announcements",
                    }
                )
            if not rows:
                raise ProviderError(f"FFD returned no announcements for {standard}")
            return rows[:limit]

        def load_cninfo() -> list[dict[str, Any]]:
            org_id = self._cninfo_orgid(code)
            response = self._request(
                "POST",
                "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                data={
                    "stock": f"{code},{org_id}",
                    "tabName": "fulltext",
                    "pageSize": str(limit),
                    "pageNum": "1",
                    "column": "",
                    "category": "",
                    "plate": "",
                    "seDate": "",
                    "searchkey": "",
                    "secid": "",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
                headers={
                    "User-Agent": UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "https://www.cninfo.com.cn/new/disclosure",
                    "Origin": "https://www.cninfo.com.cn",
                },
            )
            rows = []
            for item in response.json().get("announcements", []) or []:
                timestamp = item.get("announcementTime")
                if isinstance(timestamp, (int, float)):
                    published = datetime.fromtimestamp(
                        timestamp / 1000, tz=timezone.utc
                    ).astimezone().strftime("%Y-%m-%d")
                else:
                    published = str(timestamp or "")[:10]
                announcement_id = item.get("announcementId", "")
                rows.append(
                    {
                        "title": _clean_text(item.get("announcementTitle")),
                        "type": item.get("announcementTypeName", ""),
                        "date": published,
                        "announcement_id": announcement_id,
                        "url": (
                            "https://www.cninfo.com.cn/new/disclosure/detail?annoId="
                            f"{announcement_id}"
                        ),
                    }
                )
            return rows[:limit]

        rows, meta = self._cached_fetch(
            f"announcements:{code}:{limit}",
            ttl=self.ttls["announcements"],
            source="ffd_announcements",
            loader=load_ffd,
            empty=[],
            max_stale=30 * 24 * 3600,
            fallback=load_cninfo,
            fallback_source="cninfo_announcements",
        )
        return self._decorate_rows(rows, meta)

    @staticmethod
    def _lhb_record(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "date": str(row.get("TRADE_DATE") or "")[:10],
            "code": str(row.get("SECURITY_CODE") or ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", ""),
            "close": _float(row.get("CLOSE_PRICE")),
            "change_pct": _float(row.get("CHANGE_RATE")),
            "net_buy": _float(row.get("BILLBOARD_NET_AMT")),
            "buy": _float(row.get("BILLBOARD_BUY_AMT")),
            "sell": _float(row.get("BILLBOARD_SELL_AMT")),
            "turnover_pct": _float(row.get("TURNOVERRATE")),
        }

    @staticmethod
    def _lhb_seat(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": row.get("OPERATEDEPT_NAME", ""),
            "code": str(row.get("OPERATEDEPT_CODE") or ""),
            "buy": _float(row.get("BUY")),
            "sell": _float(row.get("SELL")),
            "net": _float(row.get("NET")),
        }

    def get_lhb(
        self,
        code: str | int | None = None,
        trade_date: str | None = None,
        lookback_days: int = 30,
    ) -> dict[str, Any]:
        normalised = _normalise_code(code) if code is not None else None
        end = datetime.strptime(trade_date, "%Y-%m-%d") if trade_date else datetime.now()
        start = end - timedelta(days=max(0, int(lookback_days)))

        def load() -> dict[str, Any]:
            if trade_date:
                filter_str = (
                    f"(TRADE_DATE>='{end:%Y-%m-%d}')(TRADE_DATE<='{end:%Y-%m-%d}')"
                )
            elif normalised:
                filter_str = (
                    f"(TRADE_DATE>='{start:%Y-%m-%d}')(TRADE_DATE<='{end:%Y-%m-%d}')"
                )
            else:
                filter_str = ""
            if normalised:
                filter_str += f'(SECURITY_CODE="{normalised}")'
            raw = self._datacenter(
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                filter_str=filter_str,
                page_size=500,
                sort_columns="TRADE_DATE,BILLBOARD_NET_AMT",
                sort_types="-1,-1",
            )
            records = [self._lhb_record(row) for row in raw]
            if not normalised and not trade_date and records:
                latest_date = records[0]["date"]
                records = [row for row in records if row["date"] == latest_date]
            actual_date = records[0]["date"] if records else (trade_date or "")
            result: dict[str, Any] = {
                "code": normalised,
                "date": actual_date,
                "records": records,
                "total_records": len(records),
                "seats": {"buy": [], "sell": []},
                "institution": {"buy": 0.0, "sell": 0.0, "net": 0.0},
            }
            if normalised and records:
                latest_date = records[0]["date"]
                seat_filter = (
                    f'(TRADE_DATE=\'{latest_date}\')(SECURITY_CODE="{normalised}")'
                )
                try:
                    buy_rows = self._datacenter(
                        "RPT_BILLBOARD_DAILYDETAILSBUY",
                        filter_str=seat_filter,
                        page_size=10,
                        sort_columns="BUY",
                        sort_types="-1",
                    )
                    sell_rows = self._datacenter(
                        "RPT_BILLBOARD_DAILYDETAILSSELL",
                        filter_str=seat_filter,
                        page_size=10,
                        sort_columns="SELL",
                        sort_types="-1",
                    )
                except Exception as exc:
                    self._record_error("eastmoney_lhb_seats", f"lhb_seats:{normalised}", exc)
                    buy_rows, sell_rows = [], []
                result["seats"] = {
                    "buy": [self._lhb_seat(row) for row in buy_rows[:5]],
                    "sell": [self._lhb_seat(row) for row in sell_rows[:5]],
                }
                institution_buy = sum(
                    _float(row.get("BUY"))
                    for row in buy_rows
                    if str(row.get("OPERATEDEPT_CODE") or "") == "0"
                    or "机构专用" in str(row.get("OPERATEDEPT_NAME") or "")
                )
                institution_sell = sum(
                    _float(row.get("SELL"))
                    for row in sell_rows
                    if str(row.get("OPERATEDEPT_CODE") or "") == "0"
                    or "机构专用" in str(row.get("OPERATEDEPT_NAME") or "")
                )
                result["institution"] = {
                    "buy": institution_buy,
                    "sell": institution_sell,
                    "net": institution_buy - institution_sell,
                }
            return result

        key = f"lhb:{normalised or 'market'}:{trade_date or 'latest'}:{lookback_days}"
        data, meta = self._cached_fetch(
            key,
            ttl=self.ttls["lhb"],
            source="eastmoney_lhb",
            loader=load,
            empty={
                "code": normalised,
                "date": trade_date or "",
                "records": [],
                "total_records": 0,
                "seats": {"buy": [], "sell": []},
                "institution": {"buy": 0.0, "sell": 0.0, "net": 0.0},
            },
            max_stale=30 * 24 * 3600,
        )
        return self._decorate_dict(data, meta)

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [_coerce_csv_row(row) for row in csv.DictReader(handle)]

    @staticmethod
    def _latest_code_row(rows: Sequence[Mapping[str, Any]], code: str) -> dict[str, Any]:
        matches = []
        for row in rows:
            raw_code = row.get("code") or row.get("stock_code") or row.get("symbol")
            try:
                row_code = _normalise_code(str(raw_code))
            except ValueError:
                continue
            if row_code == code:
                matches.append(dict(row))
        return matches[-1] if matches else {}

    def get_local_prediction(self, code: str | int) -> dict[str, Any]:
        code = _normalise_code(code)

        def load() -> dict[str, Any]:
            prediction: dict[str, Any] = {}
            prediction_file = ""
            for name in ("latest_pending_forecasts.csv", "stock_predictions.csv"):
                path = self.data_dir / name
                prediction = self._latest_code_row(self._read_csv(path), code)
                if prediction:
                    prediction_file = str(path)
                    break
            macro = self._latest_code_row(
                self._read_csv(self.data_dir / "macro_factors.csv"), code
            )
            source_health = self._read_csv(self.data_dir / "data_source_health.csv")[-8:]
            if not prediction and not macro and not source_health:
                raise ProviderError(f"AStockLab data is unavailable under {self.data_dir}")
            return {
                "code": code,
                "prediction": prediction,
                "macro": macro,
                "data_source_health": source_health,
                "prediction_file": prediction_file,
                "astocklab_root": str(self.astocklab_root),
            }

        data, meta = self._cached_fetch(
            f"local_prediction:{self.astocklab_root}:{code}",
            ttl=self.ttls["local_prediction"],
            source="astocklab_csv",
            loader=load,
            empty={
                "code": code,
                "prediction": {},
                "macro": {},
                "data_source_health": [],
                "prediction_file": "",
                "astocklab_root": str(self.astocklab_root),
            },
            max_stale=30 * 24 * 3600,
        )
        return self._decorate_dict(data, meta)

    def health(self, *, probe: bool = False) -> dict[str, Any]:
        probe_result: dict[str, Any] | None = None
        if probe:
            quote = self.get_quote("000001")
            probe_result = {
                "ok": bool(quote.get("available")),
                "source": quote.get("source"),
                "stale": quote.get("stale", False),
                "errors": (quote.get("_meta") or {}).get("errors", []),
            }
        with self._state_lock:
            source_state = copy.deepcopy(self._source_state)
            errors = list(copy.deepcopy(self._errors))
        with self._ffd_state_lock:
            ffd_state = self._read_ffd_state()
        state_today = datetime.now().strftime("%Y-%m-%d")
        budget = ffd_state.get("budget") if isinstance(ffd_state.get("budget"), dict) else {}
        # 预算只在下一次预留时才滚动日期；报告时必须按日期核对，
        # 否则新的一天会把昨天的调用次数当成“今日已调用”展示。
        budget_is_today = str(budget.get("date") or "") == state_today
        unbudgeted = ffd_state.get("unbudgeted_operations")
        unbudgeted_today = (
            copy.deepcopy(unbudgeted.get("operations") or {})
            if isinstance(unbudgeted, dict) and str(unbudgeted.get("date") or "") == state_today
            else {}
        )
        daily = (
            ffd_state.get("market_daily")
            if isinstance(ffd_state.get("market_daily"), dict)
            else {}
        )
        ffd_daily_ready = bool(daily.get("rows"))
        expected_daily_date = self._latest_completed_trade_date(datetime.now()).strftime("%Y-%m-%d")
        actual_daily_date = str(daily.get("trade_date") or "")[:10]
        # “有数据”不等于“数据新鲜”。旧的 FFD 基线必须明确标记，避免
        # 下游把上周收盘数据误当成当前交易日的全市场快照。
        ffd_daily_stale = bool(
            ffd_daily_ready
            and actual_daily_date
            and actual_daily_date < expected_daily_date
        )
        # A full FFD daily baseline is sufficient for the market-universe
        # scans.  Sina/Tencent are bounded historical-K-line fallbacks; if
        # either endpoint is rate-limited, do not report the whole service as
        # failed while the authoritative FFD snapshot is still available.
        history_fallback_sources = {"sina_daily_kline", "tencent_quote"}
        fallback_failing = [
            name
            for name, state in source_state.items()
            if name in history_fallback_sources and not state.get("ok", True)
        ]
        transient_ffd_history = [
            name
            for name, state in source_state.items()
            if (
                name == "ffd_quote_history"
                and not state.get("ok", True)
                and ffd_daily_ready
                and bool(state.get("last_success"))
                and int(state.get("consecutive_failures") or 0) <= 2
            )
        ]
        tolerated_history_sources = history_fallback_sources | set(transient_ffd_history)
        failing = [
            name
            for name, state in source_state.items()
            if (
                not state.get("ok", True)
                and not state.get("optional", False)
                and not (ffd_daily_ready and name in tolerated_history_sources)
            )
        ]
        optional_failing = [
            name
            for name, state in source_state.items()
            if not state.get("ok", True) and state.get("optional", False)
        ]
        degraded_reasons: list[str] = []
        if optional_failing:
            degraded_reasons.append(
                f"可选数据源异常：{', '.join(sorted(optional_failing))}"
            )
        if fallback_failing and ffd_daily_ready:
            degraded_reasons.append(
                "FFD 全市场日线基线正常，但远程历史K线回退源暂时受限："
                + ", ".join(sorted(fallback_failing))
            )
        if transient_ffd_history:
            degraded_reasons.append(
                "FFD 全市场日线基线正常；历史K线批量请求发生一次瞬时超时，"
                "已有数据仍可用，后续请求会自动恢复"
            )
        if self.ffd_enabled and not ffd_daily_ready:
            degraded_reasons.append("FFD 日线基线不可用，当前回退到本地通达信")
        elif self.ffd_enabled and ffd_daily_stale:
            degraded_reasons.append(
                f"FFD 日线基线滞后（{actual_daily_date}，应为 {expected_daily_date}），当前仅作历史参考"
            )
        status = "error" if failing else ("degraded" if degraded_reasons else "ok")
        return {
            "ok": not failing,
            "operational": not failing,
            "degraded": status == "degraded",
            "status": status,
            "degraded_reasons": degraded_reasons,
            "time": _now_iso(),
            "sources": source_state,
            "optional_failures": optional_failing,
            "recent_errors": errors[-20:],
            "cache": self.cache.stats(),
            "astocklab": {
                "root": str(self.astocklab_root),
                "exists": self.astocklab_root.exists(),
                "data_dir": str(self.data_dir),
                "data_exists": self.data_dir.exists(),
            },
            "configuration": {
                "timeout_seconds": self.timeout,
                "ffd_bulk_timeout_seconds": self._ffd_bulk_timeout,
                "eastmoney_min_interval_seconds": self._em_min_interval,
                "ttls": copy.deepcopy(self.ttls),
            },
            "ffd": {
                "enabled": self.ffd_enabled,
                "launcher_available": self.ffd_launcher.is_file(),
                "credential_storage": "local_ffd_config",
                "daily_call_limit": self._ffd_daily_call_limit,
                "calls_today": int(budget.get("total") or 0) if budget_is_today else 0,
                "operations_today": copy.deepcopy(budget.get("operations") or {}) if budget_is_today else {},
                "budget_date": str(budget.get("date") or ""),
                "unbudgeted_operations_today": unbudgeted_today,
                "daily_baseline_date": daily.get("trade_date"),
                "daily_baseline_rows": len(daily.get("rows") or []),
                "daily_baseline_expected_date": expected_daily_date,
                "daily_baseline_stale": ffd_daily_stale,
                "daily_baseline_status": (
                    "stale_fallback_tdx" if ffd_daily_stale
                    else "ready" if ffd_daily_ready
                    else "ffd_empty_fallback_tdx"
                ),
                "daily_baseline_fallback": "local_tdx",
                "routing": (
                    {
                        "call_auction": "QMT tick primary (client online); FFD 09:25 final; Tencent live indicative; Eastmoney public snapshot fallback",
                        "market_breadth": "FFD primary; cached; Tencent live fallback",
                        "daily_baseline": "QMT universe primary (client online); FFD primary; one post-close sync per trading day",
                        "postclose_full_market": "local QMT universe (client online); FFD direct; local TDX .day files",
                        "board_rotation": "local TDX tdxhy/infoharbor plus .day history",
                        "historical_kline": "local QMT bars (client online); FFD history; Sina/Tencent fallback",
                    }
                    if self.qmt_enabled
                    else {
                        "call_auction": "FFD 09:25 final; Tencent live indicative; Eastmoney public snapshot fallback",
                        "market_breadth": "FFD primary; cached; Tencent live fallback",
                        "daily_baseline": "FFD primary; one post-close sync per trading day",
                        "postclose_full_market": "FFD direct; local TDX .day files",
                        "board_rotation": "local TDX tdxhy/infoharbor plus .day history",
                        "historical_kline": "FFD history; Sina/Tencent fallback",
                    }
                ),
                "capabilities": {
                    "l2_ten_level": False,
                    "l2_depth": None,
                    "l2_imbalance": None,
                    "l2_scoring_enabled": False,
                    "public_five_level_quotes": True,
                },
            },
            "qmt": self.qmt.health(),
            "probe": probe_result,
        }


__all__ = [
    "MarketDataProvider",
    "ProviderError",
    "TTLCache",
    "filter_stock_universe",
    "is_a_share_security",
    "is_star_security",
]
