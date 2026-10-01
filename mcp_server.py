from __future__ import annotations

import json
import os
import re
import time as time_module
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


BASE_URL = os.environ.get("XUNLONG_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
DISCLAIMER = "仅供研究，不构成投资建议。"
SESSION = requests.Session()
MCP = FastMCP(
    "xunlong",
    instructions=(
        "Use these tools as the sole factual source for Xunlong A-share analysis. "
        "Never invent missing prices, indicators, market phases, candidates, or backtest results."
    ),
    log_level="ERROR",
)


def _request(method: str, path: str, *, timeout: int = 30, **kwargs: Any) -> Any:
    try:
        response = SESSION.request(method, f"{BASE_URL}{path}", timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        detail = ""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                detail = str(response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = response.text[:300]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"寻龙工作台接口不可用{suffix}") from exc


def _code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    if not match:
        raise ValueError("请输入6位A股代码")
    code = match.group(1)
    if code.startswith(("688", "689")):
        raise ValueError("系统强制排除科创板688/689股票")
    return code


def _compact_analysis(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: data.get(key)
        for key in (
            "code",
            "name",
            "quote",
            "signal",
            "score",
            "total_score",
            "score_breakdown",
            "strategy_version",
            "rulebook_version",
            "strategy",
            "risk",
            "technical",
            "trend_scores",
            "stock_info",
            "boards",
            "fund_flow",
            "local_prediction",
            "trigger",
            "as_of",
            "disclaimer",
        )
        if data.get(key) is not None
    }


def _compact_candidate(item: dict[str, Any]) -> dict[str, Any]:
    snapshot = item.get("snapshot") or {}
    rulebook = snapshot.get("rulebook") or {}
    return {
        key: value
        for key, value in {
            "rank": item.get("rank"),
            "code": item.get("code"),
            "name": item.get("name"),
            "industry": item.get("industry"),
            "price": snapshot.get("price"),
            "change_pct": item.get("change_pct"),
            "gap_pct": item.get("gap_pct"),
            "amount": snapshot.get("amount"),
            "turnover_pct": snapshot.get("turnover_pct"),
            "volume_ratio": snapshot.get("volume_ratio"),
            "score": item.get("score"),
            "signal": item.get("signal"),
            "decision": item.get("decision"),
            "pushed": bool(item.get("pushed")),
            "decision_reason": item.get("decision_reason") or item.get("reason"),
            "breakdown": item.get("breakdown") or item.get("scores"),
            "emotion": rulebook.get("emotion"),
            "board": rulebook.get("board"),
            "leader": rulebook.get("leader"),
            "triggers": rulebook.get("triggers"),
            "negative_feedback": rulebook.get("negative_feedback"),
            "risk": rulebook.get("risk"),
            "position": rulebook.get("position"),
            "technical": snapshot.get("technical"),
            "data_source": snapshot.get("source"),
            "data_time": snapshot.get("fetched_at") or snapshot.get("data_date"),
            "stale": snapshot.get("stale"),
        }.items()
        if value is not None
    }


def _compact_run(data: dict[str, Any], max_candidates: int = 10) -> dict[str, Any]:
    size = max(1, min(int(max_candidates), 40))
    candidates = list(data.get("candidates") or [])
    pushed = [item for item in candidates if item.get("pushed")]
    selected = pushed + [item for item in candidates if not item.get("pushed")]
    selected = selected[: max(size, len(pushed))]
    result = {
        key: data.get(key)
        for key in (
            "id",
            "trade_date",
            "created_at",
            "run_type",
            "market_score",
            "market_label",
            "market_note",
            "universe_count",
            "first_board_count",
            "candidate_count",
            "push_count",
            "highest_code",
            "highest_name",
            "highest_score",
            "status",
            "source",
            "message",
            "strategy_version",
            "metadata",
            "funnel",
            "summary",
        )
        if data.get(key) is not None
    }
    result["candidates"] = [_compact_candidate(item) for item in selected]
    result["returned_candidate_count"] = len(result["candidates"])
    result["disclaimer"] = DISCLAIMER
    return result


def _hermes_schedule() -> dict[str, Any] | None:
    hermes_home = Path(
        os.environ.get("HERMES_HOME")
        or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
    )
    jobs_file = hermes_home / "cron" / "jobs.json"
    try:
        payload = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for job in payload.get("jobs", []):
        if job.get("id") != "3670e587dd8f" and job.get("name") not in {
            "寻龙09:26微信选股",
            "寻龙09:28微信选股",
        }:
            continue
        deliver = str(job.get("deliver") or "")
        return {
            "id": job.get("id"),
            "name": job.get("name"),
            "schedule": job.get("schedule"),
            "enabled": bool(job.get("enabled")),
            "next_run_at": job.get("next_run_at"),
            "delivery_platform": deliver.split(":", 1)[0] if deliver else "local",
            "skill": job.get("skills"),
        }
    return None


@MCP.tool()
def system_status() -> dict[str, Any]:
    """Check service health, 09:26 pre-scan, 09:28 delivery and safe settings."""
    health = _request("GET", "/api/health", timeout=10)
    jobs = _request("GET", "/api/jobs", timeout=10)
    settings = _request("GET", "/api/settings", timeout=10)
    return {
        "ok": bool(health.get("ok")),
        "service": health.get("app"),
        "provider": {
            key: (health.get("provider") or {}).get(key)
            for key in ("ok", "status", "time", "optional_failures", "recent_errors")
        },
        "jobs": jobs.get("jobs", []),
        "hermes_personal_wechat_schedule": _hermes_schedule(),
        "settings": {
            key: settings.get(key)
            for key in (
                "auto_scheduler",
                "auction_time",
                "auction_scan_time",
                "auction_push_time",
                "review_time",
                "universe_scope",
                "rulebook_version",
                "rulebook_threshold",
                "rulebook_push_threshold",
                "max_push",
            )
        },
        "disclaimer": DISCLAIMER,
    }


@MCP.tool()
def market_status() -> dict[str, Any]:
    """Get current market score, indices, breadth and the evidence-backed emotion context."""
    data = _request("GET", "/api/market", timeout=30)
    indices = []
    for item in data.get("indices", []):
        indices.append(
            {
                key: item.get(key)
                for key in ("code", "name", "price", "change_pct", "quote_time", "source", "stale")
                if item.get(key) is not None
            }
        )
    return {
        key: value
        for key, value in {
            "score": data.get("score"),
            "label": data.get("label"),
            "note": data.get("note"),
            "coefficient": data.get("coefficient"),
            "data_points": data.get("data_points"),
            "indices": indices,
            "rise_count": data.get("rise_count"),
            "fall_count": data.get("fall_count"),
            "flat_count": data.get("flat_count"),
            "breadth": data.get("breadth"),
            "emotion_phase": data.get("emotion_phase"),
            "as_of": data.get("as_of"),
            "source": data.get("source"),
            "stale": data.get("stale"),
            "disclaimer": DISCLAIMER,
        }.items()
        if value is not None
    }


@MCP.tool()
def latest_selection(max_candidates: int = 10) -> dict[str, Any]:
    """Read the latest completed dragon-mode full-market selection without starting a new scan."""
    data = _request("GET", "/api/screener/runs/latest?run_type=dragon", timeout=15)
    return _compact_run(data, max_candidates)


@MCP.tool()
def await_selection(
    not_before_time: str = "09:26",
    timeout_seconds: int = 180,
    max_candidates: int = 10,
) -> dict[str, Any]:
    """Wait for today's completed pre-scan, then return it without starting a duplicate scan."""
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(not_before_time)):
        raise ValueError("not_before_time 必须是 HH:MM")
    wait_seconds = max(0, min(int(timeout_seconds), 300))
    deadline = time_module.monotonic() + wait_seconds
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.strptime(f"{today} {not_before_time}", "%Y-%m-%d %H:%M")
    latest: dict[str, Any] = {}
    last_error = ""
    while True:
        try:
            latest = _request("GET", "/api/screener/runs/latest?run_type=dragon", timeout=15)
            created_at = str(latest.get("created_at") or "")
            created = datetime.fromisoformat(created_at) if created_at else None
            if created is not None and created.tzinfo is not None:
                created = created.astimezone().replace(tzinfo=None)
            if latest.get("trade_date") == today and created is not None and created >= cutoff:
                result = _compact_run(latest, max_candidates)
                result.update(
                    {
                        "ready": True,
                        "required_trade_date": today,
                        "required_not_before": not_before_time,
                    }
                )
                return result
            last_error = "当天09:26预扫描尚未完成"
        except (RuntimeError, ValueError) as exc:
            last_error = str(exc)
        if time_module.monotonic() >= deadline:
            return {
                "ready": False,
                "required_trade_date": today,
                "required_not_before": not_before_time,
                "reason": last_error or "等待预扫描超时",
                "latest": _compact_run(latest, max_candidates) if latest else None,
                "disclaimer": DISCLAIMER,
            }
        time_module.sleep(min(5.0, max(0.1, deadline - time_module.monotonic())))


@MCP.tool()
def scan_market(kline_budget: int = 160) -> dict[str, Any]:
    """Run a new full-market dragon scan. This can take several minutes and writes one audit run."""
    limit = max(12, min(int(kline_budget), 240))
    data = _request(
        "POST",
        "/api/screener/run",
        timeout=600,
        json={"mode": "dragon", "limit": limit},
    )
    return _compact_run(data, 10)


@MCP.tool()
def analyze_stock(code: str, deep: bool = False) -> dict[str, Any]:
    """Analyze one non-STAR A-share using quotes, local K-line, factors, boards and risk rules."""
    normalized = _code(code)
    path = f"/api/stocks/{normalized}/deep" if deep else f"/api/stocks/{normalized}/analysis"
    data = _request("GET", path, timeout=120)
    if deep:
        data.pop("kline", None)
        return data
    return _compact_analysis(data)


@MCP.tool()
def explain_rejection(code: str) -> dict[str, Any]:
    """Explain why a stock was pushed, observed, vetoed, or absent from the latest dragon shortlist."""
    normalized = _code(code)
    run = _request("GET", "/api/screener/runs/latest?run_type=dragon", timeout=15)
    candidate = next(
        (item for item in (run.get("candidates") or []) if str(item.get("code")) == normalized),
        None,
    )
    analysis = _compact_analysis(
        _request("GET", f"/api/stocks/{normalized}/analysis", timeout=120)
    )
    if candidate:
        status = candidate.get("decision") or ("push" if candidate.get("pushed") else "observe")
        reason = candidate.get("decision_reason") or candidate.get("reason") or candidate.get("label")
    else:
        status = "not_in_latest_shortlist"
        reason = "该股票未进入最近一次全市场扫描返回的前40名候选，不能据此推断具体单一过滤项。"
    return {
        "code": normalized,
        "run_id": run.get("id"),
        "trade_date": run.get("trade_date"),
        "status": status,
        "reason": reason,
        "candidate": candidate,
        "current_analysis": analysis,
        "disclaimer": DISCLAIMER,
    }


@MCP.tool()
def compare_candidates(codes: list[str]) -> dict[str, Any]:
    """Compare 2-5 non-STAR A-share candidates using the same current scoring contract."""
    normalized = list(dict.fromkeys(_code(code) for code in codes))
    if not 2 <= len(normalized) <= 5:
        raise ValueError("请提供2到5只不同的股票代码")
    items = []
    for code in normalized:
        data = _compact_analysis(_request("GET", f"/api/stocks/{code}/analysis", timeout=120))
        items.append(data)
    return {"count": len(items), "items": items, "disclaimer": DISCLAIMER}


@MCP.tool()
def backtest_summary(limit: int = 100) -> dict[str, Any]:
    """Read audited backtest records and aggregate statistics without running a new backtest."""
    size = max(1, min(int(limit), 1000))
    data = _request("GET", f"/api/backtests?limit={size}", timeout=30)
    data["disclaimer"] = DISCLAIMER
    return data


@MCP.tool()
def run_backtest() -> dict[str, Any]:
    """Recompute backtest records from saved selection runs. This writes audit records and may be slow."""
    data = _request("POST", "/api/backtests/run", timeout=600)
    data["disclaimer"] = DISCLAIMER
    return data


@MCP.tool()
def latest_review() -> dict[str, Any]:
    """Get the latest market and strategy review with candidate and push statistics."""
    data = _request("GET", "/api/reviews/latest", timeout=60)
    data["disclaimer"] = DISCLAIMER
    return data


@MCP.tool()
def strategy_rules() -> dict[str, Any]:
    """Read the active rulebook version, thresholds, modes, exclusions and evidence status."""
    return _request("GET", "/api/strategy/rules", timeout=15)


# ── 板块排名 (腾讯 qt.gtimg.cn 直连，不依赖 East Money) ────────

_BOARD_CODES: dict[str, str] = {
    "sh000805": "A股资源",    "sh000811": "细分有色",   "sh000819": "有色金属",
    "sh000823": "800有色",    "sh000820": "煤炭指数",   "sh000813": "细分化工",
    "sh000812": "细分机械",   "sh000854": "500原料",    "sh000856": "500工业",
    "sh000827": "中证环保",   "sh000807": "食品饮料",   "sh000815": "细分食品",
    "sh000806": "消费服务",   "sh000808": "医药生物",   "sh000814": "细分医药",
    "sh000841": "800医药",    "sh000857": "500医药",    "sh000858": "500信息",
    "sh000863": "CS精准医",   "sh000849": "300非银",    "sz399241": "地产指数",
    "sh000828": "300高贝",    "sh000852": "中证1000",   "sh000851": "百发100",
    "sh000861": "央企创新",   "sh000859": "一带一路",   "sh000865": "上海国企",
    "sz399808": "中证赛道",   "sz399993": "CS生科",     "sz399994": "信息安全",
    "sz399997": "中证白酒",   "sz399998": "中证煤炭",   "sz399995": "建筑建材",
    "sz399996": "智能家居",
}


@MCP.tool()
def board_ranking() -> dict[str, Any]:
    """Get real-time sector/theme board rankings — top gainers, top losers, turnover.
    Data from Tencent qt.gtimg.cn (no East Money dependency).
    Covers 30 industry/thematic indices with 涨跌幅 and 成交额.
    """
    import subprocess
    url = "https://qt.gtimg.cn/q=" + ",".join(_BOARD_CODES.keys())
    result = subprocess.run(
        ["curl", "-s", url], capture_output=True, timeout=15
    )
    raw = result.stdout.decode("gbk", errors="replace")
    boards = []
    for line in raw.split("\n"):
        if line.count("~") < 40 or "pv_none_match" in line:
            continue
        f = line.split("~")
        try:
            code = f[0].split("_")[-1].split("=")[0].strip('"')
            name = _BOARD_CODES.get(code, f[1])
            price = float(f[3])
            chg = float(f[32])
            amt = float(f[37]) / 10000 if f[37] else 0  # 万元→亿
            if amt > 0:  # 只保留有成交的
                boards.append({
                    "code": code, "name": name, "price": price,
                    "change_pct": chg, "amount_yi": round(amt, 1),
                })
        except (ValueError, IndexError):
            continue

    boards.sort(key=lambda x: x["change_pct"], reverse=True)
    return {
        "total": len(boards),
        "top_gainers": boards[:5],
        "top_losers": boards[-5:][::-1] if len(boards) >= 5 else [],
        "all": boards,
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "tencent_qt",
        "note": "涨停家数/主力资金需东方财富直达(当前不可达)",
        "disclaimer": DISCLAIMER,
    }


if __name__ == "__main__":
    MCP.run(transport="stdio")
