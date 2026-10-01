from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import scoring
from .obsidian_config import load_rules, sync_to_scoring
from .patterns import detect_all as detect_kline_patterns
from .strategies import run_all as run_strategies
from .providers import MarketDataProvider
from .scheduler import LocalScheduler
from .services import DISCLAIMER, XunlongService
from .storage import Database


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
DATA_DIR = Path(os.environ.get("XUNLONG_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

database = Database(DATA_DIR / "xunlong.db")

# ---------------------------------------------------------------------------
# On startup, try to sync strategy rules from the Obsidian vault.
# The vault is the authoritative source; the database and in-memory config
# are caches that get overwritten on reload.
# ---------------------------------------------------------------------------
_obsidian_rules = load_rules()
_startup_sync_updates: dict[str, Any] = {}
if _obsidian_rules.get("_obsidian_synced"):
    _startup_sync_updates = sync_to_scoring(_obsidian_rules)
    # Also persist thresholds to the database so the GET /api/settings endpoint
    # reflects Obsidian values.
    db_updates: dict[str, Any] = {}
    for key in ("rulebook_threshold", "rulebook_push_threshold"):
        if key in _obsidian_rules:
            db_updates[key] = _obsidian_rules[key]
    if db_updates:
        try:
            database.update_settings(db_updates)
        except Exception:
            pass
provider = MarketDataProvider(
    astocklab_root=os.environ.get("ASTOCKLAB_ROOT")
    or str(database.get_settings().get("astocklab_root", ""))
)
service = XunlongService(provider, database)
scheduler = LocalScheduler(database, service.run_job, provider.prewarm_ffd)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    threading.Thread(
        target=_refresh_ffd_baseline_on_startup,
        name="xunlong-ffd-startup-refresh",
        daemon=True,
    ).start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(
    title="寻龙工作台",
    version="1.0.0",
    description="可解释、可回测、可校准的 A 股研究工作台",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_store_core_static(request, call_next):
    """脚本与样式永不缓存：页面 shell 已 no-store，避免浏览器用旧 JS 丢新功能。"""

    response = await call_next(request)
    if request.url.path in ("/static/app.js", "/static/styles.css"):
        response.headers["Cache-Control"] = "no-store"
    return response


class ScreenerRequest(BaseModel):
    mode: str = Field(
        default="technical",
        pattern="^(technical|auction|rulebook|balanced|value|growth|trend|event|dragon|overnight|擒龙)$",
    )
    limit: int | None = Field(default=None, ge=12, le=240)


class SettingsValues(BaseModel):
    technical_threshold: int | None = Field(default=None, ge=-12, le=12)
    auction_threshold: int | None = Field(default=None, ge=0, le=100)
    scan_limit: int | None = Field(default=None, ge=12, le=240)
    rulebook_threshold: int | None = Field(default=None, ge=0, le=100)
    rulebook_push_threshold: int | None = Field(default=None, ge=0, le=100)
    strategy_version: str | None = Field(default=None, min_length=1, max_length=40)
    wecom_webhook: str | None = Field(default=None, max_length=512)
    auto_scheduler: bool | None = None
    clear_wecom_webhook: bool = False

    @field_validator("strategy_version")
    @classmethod
    def validate_strategy_version(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("策略版本不能为空")
        return value

    @field_validator("wecom_webhook")
    @classmethod
    def validate_wecom_webhook(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        key = parse_qs(parsed.query).get("key", [""])[0]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "qyapi.weixin.qq.com"
            or parsed.path.rstrip("/") != "/cgi-bin/webhook/send"
            or not key
        ):
            raise ValueError("必须填写企业微信群机器人的官方 Webhook 地址")
        return value


class SettingsRequest(BaseModel):
    values: SettingsValues


class JobRequest(BaseModel):
    enabled: bool | None = None
    channel: str | None = Field(default=None, pattern="^(local|wecom)$")


class MessageRequest(BaseModel):
    channel: str = Field(default="wecom", pattern="^wecom$")
    content: str | None = None


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=80)


class ResearchStockRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    note: str = Field(default="", max_length=300)


class ResearchBatchRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)
    note: str = Field(default="", max_length=300)


def _refresh_ffd_baseline_on_startup() -> None:
    try:
        result = provider.prewarm_ffd("daily")
        if not result.get("ok"):
            logger.warning("Startup FFD daily refresh was skipped: %s", result.get("reason"))
    except Exception:
        logger.exception("Startup FFD daily refresh failed")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # The static UI evolves alongside the local API; avoid serving an old shell
    # that references a stale client bundle after a service restart.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    provider_health = provider.health()
    provider_status = str(provider_health.get("status") or "error")
    return {
        "ok": bool(provider_health.get("ok")),
        "operational": bool(provider_health.get("operational", provider_health.get("ok"))),
        "degraded": provider_status == "degraded",
        "status": provider_status,
        "app": "寻龙工作台",
        "version": "1.0.0",
        "database": str(database.path),
        "provider": provider_health,
    }


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    try:
        return service.overview()
    except Exception as exc:
        logger.exception("Overview generation failed")
        latest = database.get_latest_screen_run()
        return {
            "market": {"score": 0, "label": "数据延迟", "indices": [], "stale": True},
            "latest_run": latest,
            "backtest": database.list_backtests(limit=100)["stats"],
            "jobs": service.jobs_payload()["jobs"],
            "system": {
                "degraded": True,
                "status": "error",
                "error": str(exc),
                "provider_status": provider.health(),
            },
            "disclaimer": DISCLAIMER,
        }


@app.get("/api/market")
def market() -> dict[str, Any]:
    try:
        return service.market_context()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场数据获取失败：{exc}") from exc


@app.post("/api/screener/run")
def run_screener(request: ScreenerRequest) -> dict[str, Any]:
    try:
        return service.run_screener(request.mode, request.limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"筛选运行失败：{exc}") from exc


@app.get("/api/screener/oversold-rebound")
def oversold_rebound(
    limit: int = Query(default=80, ge=1, le=200),
    drawdown_min: float = Query(default=12.0, ge=5.0, le=50.0),
    volume_multiple: float = Query(default=1.5, ge=1.0, le=5.0),
    min_amount: float = Query(default=100_000_000.0, ge=0.0, le=10_000_000_000.0),
    amount_multiple: float = Query(default=1.2, ge=1.0, le=5.0),
    profile: str = Query(default="steady", pattern="^(basic|steady)$"),
    wave: str = Query(default="all", pattern="^(all|first|second)$"),
    triggered_only: bool = Query(default=True),
    market_filter: bool = Query(default=True),
    min_reward_risk: float = Query(default=1.0, ge=0.5, le=5.0),
    require_ths_hot: bool = Query(default=True),
    require_positive_dde: bool = Query(default=True),
) -> dict[str, Any]:
    try:
        return service.oversold_rebound(
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
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"超跌反弹筛选失败：{exc}") from exc


@app.post("/api/yijiner/scan")
def yijiner_scan(
    limit: int = Query(default=50, ge=1, le=200),
    min_auction_amount: float = Query(default=30_000_000.0, ge=0.0, le=1_000_000_000.0),
) -> dict[str, Any]:
    try:
        return service.yijiner_scan(limit=limit, min_auction_amount=min_auction_amount)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"一进二扫描失败：{exc}") from exc


@app.post("/api/yijiner/preview")
def yijiner_preview(
    limit: int = Query(default=50, ge=1, le=200),
    min_auction_amount: float = Query(default=30_000_000.0, ge=0.0, le=1_000_000_000.0),
) -> dict[str, Any]:
    try:
        return service.yijiner_premarket_preview(
            limit=limit,
            min_auction_amount=min_auction_amount,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"盘前首板池刷新失败：{exc}") from exc


@app.get("/api/yijiner/runs/latest")
def yijiner_latest() -> dict[str, Any]:
    return service.yijiner_latest()


@app.get("/api/yijiner/runs")
def yijiner_history(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return service.yijiner_history(limit)


@app.post("/api/yijiner/trace")
def yijiner_trace() -> dict[str, Any]:
    """手动补采竞价过程快照（09:15-09:25 窗口内有效）。"""
    try:
        return service.sample_auction_trace()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"竞价采样失败：{exc}") from exc


@app.get("/api/yijiner/perf20")
def yijiner_perf20(code: str = Query(..., min_length=6, max_length=6)) -> dict[str, Any]:
    """单只主板股票近 20 个交易日表现。"""
    try:
        return service.stock_perf20(code)
    except (ValueError, LookupError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"20日表现计算失败：{exc}") from exc


@app.get("/api/yijiner/liveboard")
def yijiner_liveboard(codes: str = Query(..., min_length=6)) -> dict[str, Any]:
    """实时行情轻量榜（腾讯免费源）：竞价实时排行与开盘盯盘共用。"""
    try:
        return service.liveboard(codes.split(","))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"实时榜获取失败：{exc}") from exc


@app.get("/api/yijiner/trace-status")
def yijiner_trace_status() -> dict[str, Any]:
    """今日竞价采样进度（09:22 任务的页面反馈）。"""
    try:
        return service.auction_trace_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"采样状态读取失败：{exc}") from exc


@app.get("/api/yijiner/winrates")
def yijiner_winrates(lookback: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """分档实测胜率：最近 N 次扫描按 S/A/B/C/D 档聚合真实命中率。"""
    try:
        return service.yijiner_winrates(lookback)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"胜率聚合失败：{exc}") from exc


@app.get("/api/yijiner/outcomes")
def yijiner_outcomes(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """候选真实表现复盘（晋级二板率/次日续板率/平均最高涨幅），带缓存。"""
    try:
        return service.yijiner_outcomes(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"复盘计算失败：{exc}") from exc


@app.post("/api/dixi/scan")
def dixi_scan(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    try:
        return service.dixi_scan(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"低吸计划生成失败：{exc}") from exc


@app.get("/api/dixi/runs/latest")
def dixi_latest() -> dict[str, Any]:
    return service.dixi_latest()


@app.get("/api/dixi/runs")
def dixi_history(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return service.dixi_history(limit)


@app.get("/api/maifu/overview")
def maifu_overview() -> dict[str, Any]:
    try:
        return service.maifu_overview()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"埋伏日历加载失败：{exc}") from exc


@app.get("/api/dixi/auction-check")
def dixi_auction_check() -> dict[str, Any]:
    try:
        return service.dixi_auction_check()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"竞价验证失败：{exc}") from exc


@app.get("/api/research-pool")
def research_pool() -> dict[str, Any]:
    return service.research_pool()


@app.post("/api/research-pool")
def add_research_stock(request: ResearchStockRequest) -> dict[str, Any]:
    try:
        return service.add_research_stock(request.code, request.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"添加自选股失败：{exc}") from exc


@app.post("/api/research-pool/batch")
def import_research_stocks(request: ResearchBatchRequest) -> dict[str, Any]:
    try:
        return service.import_research_stocks(request.text, request.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"批量导入失败：{exc}") from exc


@app.post("/api/research-pool/{code}/analyze")
def analyze_research_stock(code: str) -> dict[str, Any]:
    try:
        return service.analyze_research_stock(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"个股三面分析失败：{exc}") from exc


@app.delete("/api/research-pool/{code}")
def remove_research_stock(code: str) -> dict[str, Any]:
    if not service.remove_research_stock(code):
        raise HTTPException(status_code=404, detail="该股票不在自选研究池中")
    return {"removed": True, "code": code}


@app.get("/api/screener/runs/latest")
def latest_screener(run_type: str | None = None) -> dict[str, Any]:
    result = database.get_latest_overnight_pool_run() if run_type == "overnight" else database.get_latest_screen_run(run_type)
    if not result:
        raise HTTPException(status_code=404, detail="暂无筛选记录")
    return result


@app.get("/api/screener/runs/{run_id}")
def screener_run(run_id: int) -> dict[str, Any]:
    result = database.get_screen_run(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="筛选记录不存在")
    return result


@app.get("/api/screener/runs/{run_id}/live-auction")
def screener_live_auction(run_id: int) -> dict[str, Any]:
    try:
        return service.live_auction_candidates(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"竞价行情同步失败：{exc}") from exc


@app.get("/api/stocks/{code}/analysis")
def stock_analysis(code: str) -> dict[str, Any]:
    try:
        return service.stock_analysis(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"个股分析失败：{exc}") from exc


@app.get("/api/stocks/{code}/deep")
def deep_analysis(code: str) -> dict[str, Any]:
    try:
        return service.deep_analysis(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"深度分析失败：{exc}") from exc


@app.get("/api/reviews/latest")
def latest_review() -> dict[str, Any]:
    try:
        return service.latest_review()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"复盘生成失败：{exc}") from exc


@app.get("/api/boards/industry")
def board_industry_ranking(top_n: int = 20) -> dict[str, Any]:
    """行业板块排名 — 涨跌幅/涨速/成交额/涨停家数/主力资金."""
    try:
        return service.board_ranking(board_type="industry", top_n=top_n)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"行业板块数据获取失败：{exc}") from exc


@app.get("/api/boards/concept")
def board_concept_ranking(top_n: int = 20) -> dict[str, Any]:
    """概念板块排名 — 涨跌幅/涨速/成交额/涨停家数/主力资金."""
    try:
        return service.board_ranking(board_type="concept", top_n=top_n)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"概念板块数据获取失败：{exc}") from exc


@app.get("/api/boards/{board_type}/rotation")
def board_rotation(board_type: str, top_n: int = 20) -> dict[str, Any]:
    if board_type not in {"industry", "concept"}:
        raise HTTPException(status_code=400, detail="板块类型必须是 industry 或 concept")
    try:
        return service.board_rotation(board_type=board_type, top_n=top_n)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"板块轮动获取失败：{exc}") from exc


@app.get("/api/boards/{board_type}/matrix")
def board_rotation_matrix(board_type: str, days: int = 10, top_n: int = 10) -> dict[str, Any]:
    if board_type not in {"industry", "concept", "all"}:
        raise HTTPException(status_code=400, detail="板块类型必须是 industry、concept 或 all")
    try:
        return service.board_rotation_matrix(board_type=board_type, days=days, top_n=top_n)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"板块轮动矩阵获取失败：{exc}") from exc


@app.get("/api/news/market")
def market_news(limit: int = 30) -> dict[str, Any]:
    try:
        return service.market_news(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场新闻获取失败：{exc}") from exc


@app.post("/api/candidates/review")
def incremental_candidate_review(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return service.incremental_review(
            run_id=payload.get("run_id"),
            updates=payload.get("updates") or [],
            notes=str(payload.get("notes") or ""),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"增量复核失败：{exc}") from exc


@app.get("/api/backtests")
def backtests(limit: int = 200) -> dict[str, Any]:
    return database.list_backtests(limit=max(1, min(limit, 1000)))


@app.post("/api/backtests/run")
def run_backtest() -> dict[str, Any]:
    try:
        return service.run_backtest()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"回测失败：{exc}") from exc


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    return service.jobs_payload()


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: str, request: JobRequest) -> dict[str, Any]:
    values = request.model_dump(exclude_none=True)
    result = database.update_job(job_id, values)
    if not result:
        raise HTTPException(status_code=404, detail="任务不存在")
    result["next_run_at"] = database.next_scheduled_time(job_id)
    return result


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str) -> dict[str, Any]:
    try:
        return service.run_job(job_id, manual=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"任务执行失败：{exc}") from exc


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    values = database.get_settings(mask_secrets=True)
    return {
        **values,
        "min_score": values.get("rulebook_threshold", 62),
        "rulebook_threshold": values.get("rulebook_threshold", 62),
        "rulebook_push_threshold": values.get("rulebook_push_threshold", 72),
        "max_push": 3,
        "auction_time": "09:26复核 / 09:28推送",
        "auction_scan_time": "09:26",
        "auction_push_time": "09:28",
        "review_time": "15:01",
        "auto_push": values.get("auto_scheduler", True),
        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605）",
        "rulebook_version": scoring.RULEBOOK_VERSION,
    }


@app.put("/api/settings")
def put_settings(request: SettingsRequest) -> dict[str, Any]:
    current = database.get_settings()
    values = request.values.model_dump(exclude_none=True)
    clear_webhook = bool(values.pop("clear_wecom_webhook", False))
    masked = str(values.get("wecom_webhook", ""))
    if clear_webhook:
        values["wecom_webhook"] = ""
    elif not masked or "..." in masked:
        values["wecom_webhook"] = current.get("wecom_webhook", "")
    threshold = int(values.get("rulebook_threshold", current.get("rulebook_threshold", 62)))
    push_threshold = int(
        values.get("rulebook_push_threshold", current.get("rulebook_push_threshold", 72))
    )
    if push_threshold < threshold:
        raise HTTPException(
            status_code=400,
            detail="规则库推送门槛不能低于规则库综合门槛",
        )
    updated = database.update_settings(values)
    return {
        **updated,
        "min_score": updated.get("rulebook_threshold", 62),
        "rulebook_threshold": updated.get("rulebook_threshold", 62),
        "rulebook_push_threshold": updated.get("rulebook_push_threshold", 72),
        "max_push": 3,
        "auction_time": "09:26复核 / 09:28推送",
        "auction_scan_time": "09:26",
        "auction_push_time": "09:28",
        "review_time": "15:01",
        "auto_push": updated.get("auto_scheduler", True),
        "universe_scope": "沪深主板（000/001/002/003/600/601/603/605）",
        "rulebook_version": scoring.RULEBOOK_VERSION,
    }


@app.get("/api/strategy/rules")
def strategy_rules() -> dict[str, Any]:
    """Expose the active rulebook contract for audit and UI diagnostics."""

    result: dict[str, Any] = {
        "rulebook_version": scoring.RULEBOOK_VERSION,
        "strategy_version": scoring.STRATEGY_VERSION,
        "modes": list(scoring.RULEBOOK_MODES) + [scoring.DRAGON_MODE],
        "dragon_version": scoring.DRAGON_VERSION,
        "pipeline_version": scoring.PIPELINE_VERSION,
        "pipeline": [
            "近10日板块轮动与状态判断",
            "系统第一轮沪深主板筛选",
            "用户补充新闻、产业链映射与风险",
            "系统第二轮增量复核",
            "交易日09:20-09:29读取当日实时快照与竞价，09:28仅推送通过确认者；不使用隔夜池",
        ],
        "mode_weights": scoring.RULEBOOK_CONFIG["mode_weights"],
        "threshold": scoring.RULEBOOK_CONFIG["threshold"],
        "push_threshold": scoring.RULEBOOK_CONFIG["push_threshold"],
        "universe": {
            "scope": "沪深主板",
            "included_prefixes": list(scoring.MAIN_BOARD_CODE_PREFIXES),
            "excluded": ["创业板300/301", "科创板688/689", "北交所", "ST/*ST/退市整理", "停牌/不可交易", "严重缺失与流动性不足"],
        },
        "threshold_status": "候选阈值（证据等级B/C），需样本外回测；不构成收益保证",
        "obsidian": {
            "synced": bool(_obsidian_rules.get("_obsidian_synced")),
            "file": _obsidian_rules.get("_obsidian_file"),
            "error": _obsidian_rules.get("_obsidian_error"),
        },
        "disclaimer": DISCLAIMER,
    }
    if _startup_sync_updates:
        result["startup_sync"] = _startup_sync_updates
    return result


@app.post("/api/strategy/reload")
def reload_strategy_from_obsidian() -> dict[str, Any]:
    """Re-read strategy rules from the Obsidian vault and apply them."""
    global _obsidian_rules, _startup_sync_updates
    _obsidian_rules = load_rules()
    updates: dict[str, Any] = {}
    if _obsidian_rules.get("_obsidian_synced"):
        updates = sync_to_scoring(_obsidian_rules)
        db_updates: dict[str, Any] = {}
        for key in ("rulebook_threshold", "rulebook_push_threshold"):
            if key in _obsidian_rules:
                db_updates[key] = _obsidian_rules[key]
        if db_updates:
            try:
                database.update_settings(db_updates)
            except Exception:
                pass
    _startup_sync_updates = updates
    return {
        "ok": _obsidian_rules.get("_obsidian_synced", False),
        "file": _obsidian_rules.get("_obsidian_file"),
        "error": _obsidian_rules.get("_obsidian_error"),
        "updates": updates,
        "current_threshold": scoring.RULEBOOK_CONFIG.get("threshold"),
        "current_push_threshold": scoring.RULEBOOK_CONFIG.get("push_threshold"),
    }


@app.post("/api/messages/test")
def test_message(request: MessageRequest) -> dict[str, Any]:
    try:
        return service.send_test_message(request.content, request.channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"消息发送失败：{exc}") from exc


@app.post("/api/bot/command")
def bot_command(request: CommandRequest) -> dict[str, Any]:
    try:
        return service.bot_command(request.command)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"指令执行失败：{exc}") from exc


@app.get("/api/data-sources")
def data_sources() -> dict[str, Any]:
    health = provider.health()
    rows = []
    for name, item in (health.get("sources") or {}).items():
        last_error = item.get("last_error") or {}
        rows.append(
            {
                "name": name,
                "healthy": item.get("ok", True),
                "status": "ok" if item.get("ok", True) else "error",
                "updated_at": item.get("last_success")
                or (last_error.get("time") if isinstance(last_error, dict) else last_error),
                "message": item.get("last_error_message")
                or (last_error.get("message") if isinstance(last_error, dict) else "")
                or "适配器可用",
            }
        )
    ffd = health.get("ffd") if isinstance(health.get("ffd"), dict) else {}
    if ffd.get("enabled"):
        baseline_ready = ffd.get("daily_baseline_status") == "ready"
        rows.append(
            {
                "name": "ffd_daily_baseline",
                "healthy": baseline_ready,
                "degraded": not baseline_ready,
                "status": "ok" if baseline_ready else "degraded",
                "updated_at": ffd.get("daily_baseline_date"),
                "message": (
                    f"FFD 日线基线已就绪（{int(ffd.get('daily_baseline_rows') or 0)} 条）"
                    if baseline_ready
                    else "FFD 日线基线未就绪，已回退本地通达信"
                ),
            }
        )
    return {**health, "sources": rows}
