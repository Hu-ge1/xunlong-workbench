"""本地 QMT(迅投 xtquant)行情桥接。

QMT 客户端登录后会把交易所直连的实时 tick、板块成分与本地日线缓存在本机,
这些数据不受 FFD 每日调用预算和公共接口限流约束。本模块把 xtquant 封装成
一个"要么返回数据、要么抛 QmtUnavailable"的容错接口:任何故障(库缺失、
客户端离线、调用超时)都由上层 provider 自动落回既有 FFD/腾讯/东财链路,
调用方无需编写 QMT 特有的错误处理。

只读行情。交易接口(xttrader)不在此桥接范围内。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# QMT 客户端的安装目录因券商而异(xtquant 库位于 <安装目录>/bin.x64/Lib/site-packages),
# 因此不内置任何券商的默认路径。三种探测来源,按优先级:
#   1. 调用方传入的 explicit 路径(XUNLONG_QMT_PATH)
#   2. 环境变量 XUNLONG_QMT_DIRS,分号/逗号分隔,可填多个
#   3. 按盘符通配 *QMT*(覆盖绝大多数券商客户端的目录命名)
_QMT_DIRS_ENV = "XUNLONG_QMT_DIRS"

# 沪深主板 A 股代码前缀(与 provider 的 universe_scope 口径一致,科创板由
# filter_stock_universe 统一排除)。
_MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")

# 连通性探测用的指数代码(上证指数,任何登录后的客户端都有其 tick)。
_INDEX_PROBE = "000001.SH"

_SOURCE_UNIVERSE = "qmt_universe"
_SOURCE_TICK = "qmt_tick"
_SOURCE_KLINE = "qmt_daily_kline"


class QmtUnavailable(RuntimeError):
    """QMT 桥接无法交付数据(库缺失 / 客户端离线 / 调用超时 / 数据过期)。"""


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def standard_code(code: str | int) -> str:
    """6 位代码 → QMT "600519.SH" 风格代码;无法识别时返回空串。"""

    match = re.search(r"(\d{6})", str(code or ""))
    if not match:
        return ""
    digits = match.group(1)
    head = digits[:1]
    if head in {"5", "6", "9"}:
        return f"{digits}.SH"
    if head in {"4", "8"}:
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _timetag_fields(timetag: Any) -> tuple[str, str]:
    """QMT timetag("20260918 09:26:03")→ (trade_date, iso_time)。"""

    text = str(timetag or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return "", ""
    trade_date = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 14:
        clock = f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        return trade_date, f"{trade_date} {clock}"
    return trade_date, trade_date


def _configured_roots() -> list[Path]:
    """XUNLONG_QMT_DIRS 里配置的安装目录;未配置时返回空列表。"""

    raw = os.environ.get(_QMT_DIRS_ENV, "")
    items = [item.strip() for item in re.split(r"[;,\n]", raw) if item.strip()]
    return [Path(item).expanduser() for item in items]


def find_library(explicit: str | Path | None = None) -> Path | None:
    """定位 xtquant 库目录;explicit 优先,其次环境变量,最后按盘符探测。"""

    roots: list[Path] = []
    if explicit:
        roots.append(Path(str(explicit)).expanduser())
    roots.extend(_configured_roots())
    for drive in ("D", "C"):
        try:
            roots.extend(path for path in Path(f"{drive}:/").glob("*QMT*") if path.is_dir())
        except OSError:
            continue
    for root in roots:
        for site in (root / "bin.x64" / "Lib" / "site-packages", root / "bin.x64" / "lib" / "site-packages", root):
            candidate = site / "xtquant"
            if (candidate / "xtdata.py").is_file():
                return site
    return None


def _import_xtdata(site_dir: Path) -> Any:
    inserted = str(site_dir) not in sys.path
    if inserted:
        sys.path.insert(0, str(site_dir))
    try:
        import xtquant.xtdata as xtdata

        return xtdata
    finally:
        if inserted:
            try:
                sys.path.remove(str(site_dir))
            except ValueError:
                pass


class QmtMarketData:
    """线程安全、惰性加载的 xtquant.xtdata 封装。

    所有取数方法在 QMT 不可用时抛 QmtUnavailable;调用成功/失败会更新
    健康状态,供 provider.health() 的 "qmt" 块展示。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        path: str | None = None,
        kline_enabled: bool = True,
        connect_timeout: float | None = None,
        call_timeout: float | None = None,
        bulk_timeout: float | None = None,
    ) -> None:
        self.enabled = enabled
        self.kline_enabled = kline_enabled
        self.path = path or None
        self.connect_timeout = max(
            2.0, float(connect_timeout or os.environ.get("XUNLONG_QMT_CONNECT_TIMEOUT", "5"))
        )
        self.call_timeout = max(
            2.0, float(call_timeout or os.environ.get("XUNLONG_QMT_TIMEOUT", "20"))
        )
        self.bulk_timeout = max(
            5.0, float(bulk_timeout or os.environ.get("XUNLONG_QMT_BULK_TIMEOUT", "90"))
        )
        self._lock = threading.RLock()
        self._xtdata: Any = None
        self._import_retry_at = 0.0
        self._import_error = ""
        self._library_path = ""
        self._probe_ok = False
        self._probe_checked_at = ""
        self._probe_refresh_at = 0.0
        self._probe_running = False
        self._last_success = ""
        self._last_error = ""
        self._download_lock = threading.Lock()
        self._name_lock = threading.Lock()
        self._names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 底层设施
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """返回已导入的 xtdata 模块;导入失败缓存 60s,避免每次调用重试。"""

        with self._lock:
            if self._xtdata is not None:
                return self._xtdata
            now = time.monotonic()
            if now < self._import_retry_at:
                raise QmtUnavailable(self._import_error or "xtquant 不可用")
            site = find_library(self.path)
            if site is None:
                self._import_error = "未找到 xtquant 库(检查 XUNLONG_QMT_PATH 或 QMT 客户端安装)"
                self._import_retry_at = now + 60
                raise QmtUnavailable(self._import_error)
            try:
                self._xtdata = _import_xtdata(site)
            except Exception as exc:
                self._import_error = f"{type(exc).__name__}: {exc}"
                self._import_retry_at = now + 60
                self._mark_error(f"xtquant 导入失败: {self._import_error}")
                raise QmtUnavailable(self._import_error) from exc
            self._library_path = str(site)
            return self._xtdata

    def _mark_success(self) -> None:
        with self._lock:
            self._last_success = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._last_error = ""
            self._probe_ok = True
            self._probe_checked_at = self._last_success
            self._probe_refresh_at = time.monotonic() + 60

    def _mark_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)[:240]
            self._probe_ok = False
            self._probe_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._probe_refresh_at = time.monotonic() + 60

    def _call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None) -> Any:
        """在守护线程中执行 xtdata 调用并限时;超时/异常统一转 QmtUnavailable。"""

        effective = max(2.0, float(timeout or self.call_timeout))
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_q.put(("ok", fn(*args)))
            except BaseException as exc:  # noqa: BLE001 - 统一转为 QmtUnavailable
                result_q.put(("err", exc))

        threading.Thread(target=run, daemon=True, name="qmt-call").start()
        try:
            status, payload = result_q.get(timeout=effective)
        except queue.Empty:
            self._mark_error(f"QMT 调用超时({effective:.0f}s),客户端可能未登录")
            raise QmtUnavailable(f"QMT 调用超时({effective:.0f}s)") from None
        if status == "err":
            self._mark_error(f"{type(payload).__name__}: {payload}")
            raise QmtUnavailable(f"{type(payload).__name__}: {payload}") from payload
        self._mark_success()
        return payload

    def _require_connected(self) -> Any:
        """先做一次轻量连通性探测(结果缓存 60s),避免批量调用挂着慢超时。

        客户端未登录时探测失败,后续 60s 内的所有取数调用都会瞬时抛出
        QmtUnavailable,请求路径不会被拖住。
        """

        xt = self._client()
        with self._lock:
            if time.monotonic() < self._probe_refresh_at:
                if not self._probe_ok:
                    raise QmtUnavailable(self._last_error or "QMT 客户端未连接")
                return xt
        probe = self._call(xt.get_full_tick, [_INDEX_PROBE], timeout=self.connect_timeout)
        if not isinstance(probe, Mapping) or not probe:
            raise QmtUnavailable("QMT 连通性探测无响应")
        return xt

    def _stock_name(self, xt: Any, std: str) -> str:
        with self._name_lock:
            cached = self._names.get(std)
        if cached:
            return cached
        try:
            detail = xt.get_instrument_detail(std) or {}
        except Exception:
            detail = {}
        name = str(detail.get("InstrumentName") or "").strip() if isinstance(detail, Mapping) else ""
        with self._name_lock:
            self._names[std] = name
        return name

    def _start_background_probe(self) -> None:
        """health() 的非阻塞连通性探测:过期才在后台线程刷新一次。"""

        with self._lock:
            if self._probe_running or time.monotonic() < self._probe_refresh_at:
                return
            self._probe_running = True

        def probe() -> None:
            try:
                xt = self._client()
                self._call(xt.get_full_tick, [_INDEX_PROBE], timeout=self.connect_timeout)
            except Exception:
                pass  # 状态已由 _call/_client 记录
            finally:
                with self._lock:
                    self._probe_running = False

        threading.Thread(target=probe, daemon=True, name="qmt-probe").start()

    # ------------------------------------------------------------------
    # 业务接口
    # ------------------------------------------------------------------

    def universe_rows(self) -> list[dict[str, Any]]:
        """沪深主板成分(仅 code/name;行情由调用方与 tick 合并)。"""

        xt = self._require_connected()
        codes = self._call(xt.get_stock_list_in_sector, "沪深主板", timeout=self.call_timeout)
        if not codes:
            codes = self._call(xt.get_stock_list_in_sector, "沪深A股", timeout=self.call_timeout)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in codes or []:
            match = re.fullmatch(r"(\d{6})\.(SH|SZ)", str(raw).strip().upper())
            if not match:
                continue
            code = match.group(1)
            if not code.startswith(_MAIN_BOARD_PREFIXES) or code in seen:
                continue
            seen.add(code)
            rows.append(
                {"code": code, "name": self._stock_name(xt, match.group(0)) or code}
            )
        if not rows:
            raise QmtUnavailable("QMT 板块成分列表为空")
        return rows

    def full_tick(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """全量 tick 快照,按 6 位代码返回 provider 报价行结构。"""

        xt = self._require_connected()
        wanted: list[str] = []
        for code in codes:
            std = standard_code(code)
            if std:
                wanted.append(std)
        if not wanted:
            return {}
        payload = self._call(xt.get_full_tick, wanted, timeout=self.bulk_timeout)
        if not isinstance(payload, Mapping):
            raise QmtUnavailable("QMT tick 响应格式异常")
        names_needed = [std for std in wanted if std in payload]
        result: dict[str, dict[str, Any]] = {}
        for code in codes:
            std = standard_code(code)
            tick = payload.get(std)
            if not isinstance(tick, Mapping):
                continue
            price = _float(tick.get("lastPrice"))
            if price <= 0:
                continue
            last_close = _float(tick.get("lastClose"))
            trade_date, quote_time = _timetag_fields(tick.get("timetag"))
            volume = _float(tick.get("volume"))
            amount = _float(tick.get("amount"))
            change = price - last_close if last_close > 0 else None
            result[code] = {
                "code": code,
                "standard_code": std,
                "name": self._stock_name(xt, std) or code,
                "price": price,
                "last_close": last_close or None,
                "previous_close": last_close or None,
                "open": _float(tick.get("open")) or None,
                "high": _float(tick.get("high")) or None,
                "low": _float(tick.get("low")) or None,
                "volume": volume,
                "volume_lots": volume / 100.0 if volume > 0 else None,
                "amount": amount,
                "amount_wan": amount / 10_000.0 if amount > 0 else None,
                "change": change,
                "change_pct": round((price / last_close - 1) * 100, 4) if last_close > 0 else None,
                "quote_time": quote_time,
                "data_as_of": quote_time,
                "trade_date": trade_date,
                "available": True,
                "source": _SOURCE_TICK,
                "stale": False,
            }
        return result

    def auction_rows(
        self, codes: Sequence[str], *, now: datetime
    ) -> dict[str, dict[str, Any]]:
        """集合竞价窗口(09:15-09:30)的竞价语义行;窗口外抛 QmtUnavailable。

        09:25 之后 open 即为当日开盘撮合终价,视为终态;09:25 之前 lastPrice
        是虚拟撮合价,标记为 indicative。两种行都带当日 trade_date,满足
        推送门控对"当日实时竞价数据"的要求。
        """

        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (9 * 60 + 15 <= minute <= 9 * 60 + 30):
            raise QmtUnavailable("当前不在集合竞价窗口(09:15-09:30)")
        today = now.strftime("%Y-%m-%d")
        final_phase = minute >= 9 * 60 + 25
        rows: dict[str, dict[str, Any]] = {}
        for code, tick in self.full_tick(codes).items():
            if str(tick.get("trade_date") or "")[:10] != today:
                continue
            row = dict(tick)
            open_price = _float(tick.get("open"))
            if final_phase and open_price > 0:
                row["auction_price"] = open_price
                row["auction_stage"] = "opening_call_auction_final"
                row["auction_data_status"] = "final"
            else:
                row["auction_price"] = _float(tick.get("price"))
                row["auction_stage"] = "call_auction_live"
                row["auction_data_status"] = "indicative"
            row["auction_volume_lots"] = (_float(tick.get("volume")) or 0) / 100.0
            row["auction_amount"] = _float(tick.get("amount"))
            row["auction_source"] = _SOURCE_TICK
            row["source"] = _SOURCE_TICK
            row["ffd_terminal"] = False
            rows[code] = row
        if not rows:
            raise QmtUnavailable("QMT 竞价快照中没有当日 tick")
        return rows

    def daily_kline(
        self, code: str, days: int, expected_date: str | None = None
    ) -> list[dict[str, Any]]:
        """本地日 K(未复权,与 TDX/Sina 链路口径一致);缺数据时补下载一次。

        expected_date 为最新已完结交易日(YYYY-MM-DD);本地数据停在更早
        日期时触发一次 download_history_data 并重读,仍不足则按现状返回。
        """

        if not self.kline_enabled:
            raise QmtUnavailable("QMT 日线桥接未启用(XUNLONG_QMT_KLINE=0)")
        xt = self._require_connected()
        std = standard_code(code)
        if not std:
            return []
        start = (datetime.now() - timedelta(days=max(60, int(days) * 3))).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        rows = self._read_daily(xt, std, start, end)
        expected = re.sub(r"\D", "", str(expected_date or ""))[:8]
        if expected and (not rows or rows[-1]["date"] < expected):
            with self._download_lock:
                rows = self._read_daily(xt, std, start, end)
                if not rows or rows[-1]["date"] < expected:
                    self._call(
                        xt.download_history_data, std, "1d", start, end,
                        timeout=max(self.bulk_timeout, 120.0),
                    )
                    rows = self._read_daily(xt, std, start, end)
        return rows[-max(5, int(days)) :]

    def _read_daily(self, xt: Any, std: str, start: str, end: str) -> list[dict[str, Any]]:
        try:
            data = self._call(
                xt.get_market_data_ex, [], [std], "1d", start, end, -1,
                timeout=self.call_timeout,
            )
        except QmtUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QmtUnavailable(f"get_market_data_ex: {exc}") from exc
        frame = data.get(std) if isinstance(data, Mapping) else None
        if frame is None or len(frame) == 0:
            return []
        records = frame.to_dict("index") if hasattr(frame, "to_dict") else {}
        rows: list[dict[str, Any]] = []
        for index, record in records.items():
            date = re.sub(r"\D", "", str(index))[:8]
            close = _float((record or {}).get("close"))
            if len(date) != 8 or close <= 0:
                continue
            pre = _float(record.get("preClose"))
            rows.append(
                {
                    "date": date,
                    "open": _float(record.get("open")),
                    "close": close,
                    "high": _float(record.get("high")),
                    "low": _float(record.get("low")),
                    "volume": _float(record.get("volume")),
                    "amount": _float(record.get("amount")),
                    "change_pct": round((close / pre - 1) * 100, 4) if pre > 0 else 0.0,
                    "source": _SOURCE_KLINE,
                }
            )
        rows.sort(key=lambda item: item["date"])
        return rows

    # ------------------------------------------------------------------
    # 健康状态
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """非阻塞健康快照;连通性探测在后台线程按需刷新。"""

        library = self._library_path or ""
        if not library and self.enabled:
            site = find_library(self.path)
            library = str(site) if site else ""
        if self.enabled:
            self._start_background_probe()
        with self._lock:
            return {
                "enabled": self.enabled,
                "kline_enabled": self.kline_enabled,
                "library_path": library,
                "client_connected": bool(self._probe_ok),
                "connected_checked_at": self._probe_checked_at,
                "last_success": self._last_success,
                "last_error": self._last_error,
                "import_error": self._import_error,
            }

    def close(self) -> None:
        """xtdata 无常驻资源需要释放;保留接口与 provider.close() 对称。"""

        return None
