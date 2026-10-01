"""多源财经快讯抓取与合并。

参考资产交易工作台便携版的新闻管线：各来源并行抓取，单源失败不影响
其它来源；结果按标题去重、按时间倒序，并只保留最近半天的内容。
"""

from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping


RequestFn = Callable[..., Any]

IMPORTANT_TERMS = (
    "央行", "证监会", "国务院", "国常会", "财政部", "降准", "降息", "加息", "LPR",
    "GDP", "CPI", "PMI", "突发", "紧急", "重大", "重要", "熔断", "暂停", "立案",
    "退市", "监管", "新规", "美联储", "汇率", "IPO", "重组", "并购", "增持", "回购",
    "涨停", "跌停", "暴涨", "暴跌",
)


def _clean(value: Any, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def _parse_time(value: Any, now: datetime) -> datetime:
    """Normalize common JSON/HTML news timestamps to an aware datetime.

    输出统一落在 ``now`` 所在时区——**不跟随进程的系统时区**。系统时区取决于运行
    环境（CI runner 是 UTC，用户机器可能是任意值），一旦依赖它，同样的输入在不同
    机器上会解析出不同的时刻，而且开发机碰巧对得上时根本发现不了。
    """

    reference = now.tzinfo or timezone.utc

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(reference)
        except (OverflowError, OSError, ValueError):
            return now
    text = str(value or "").strip()
    if not text:
        return now
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _parse_time(float(text), now)
    if text in {"刚刚", "刚才"}:
        return now
    match = re.fullmatch(r"(\d+)分钟前", text)
    if match:
        return now - timedelta(minutes=int(match.group(1)))
    match = re.fullmatch(r"(\d+)小时前", text)
    if match:
        return now - timedelta(hours=int(match.group(1)))
    match = re.fullmatch(r"(\d+)天前", text)
    if match:
        return now - timedelta(days=int(match.group(1)))
    for candidate in (text.replace("Z", "+00:00"), text):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                # Sina/THS return local Beijing time without an offset. Treat
                # naive timestamps as the reference timezone instead of UTC,
                # otherwise every item is shifted eight hours forward.
                parsed = parsed.replace(tzinfo=reference)
            return parsed.astimezone(reference)
        except ValueError:
            pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(reference)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=now.year)
                if parsed > now + timedelta(days=1):
                    parsed = parsed.replace(year=now.year - 1)
            return parsed.replace(tzinfo=reference)
        except ValueError:
            pass
    return now


def _item(
    source: str,
    source_name: str,
    title: Any,
    summary: Any,
    published: Any,
    *,
    item_id: Any = "",
    url: Any = "",
    tags: Any = (),
    important: bool = False,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    title_text = _clean(title, 240)
    if not title_text:
        return None
    current = now or datetime.now().astimezone()
    timestamp = _parse_time(published, current)
    summary_text = _clean(summary, 600) or title_text
    tag_values = [
        _clean(tag, 40)
        for tag in (tags if isinstance(tags, (list, tuple, set)) else ())
        if _clean(tag, 40)
    ]
    important = bool(important or any(term in f"{title_text} {summary_text}" for term in IMPORTANT_TERMS))
    stable_id = _clean(item_id, 100) or re.sub(r"\W+", "", title_text)[:40]
    iso = timestamp.isoformat(timespec="seconds")
    return {
        "id": f"{source}-{stable_id}",
        "source": source,
        "source_name": source_name,
        "publisher": source_name,
        "title": title_text,
        "content": summary_text,
        "summary": summary_text,
        "published": iso,
        "time": iso,
        "url": str(url or ""),
        "tags": list(dict.fromkeys(tag_values)),
        "important": important,
        "_timestamp": timestamp.timestamp(),
    }


def _json(request: RequestFn, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, Any] | None = None) -> Any:
    response = request(
        "GET",
        url,
        params=dict(params or {}),
        headers=dict(headers or {}),
        timeout=8,
    )
    return response.json()


def _eastmoney(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
        params={
            "client": "web", "biz": "web_home_channel", "column": "350,35,466,467",
            "order": "1", "needInteractData": "0", "page_index": "1",
            "page_size": str(limit),
        },
        headers={"Referer": "https://finance.eastmoney.com/"},
    )
    rows = ((payload.get("data") or {}).get("list") or []) if isinstance(payload, Mapping) else []
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        item = _item(
            "eastmoney", "东方财富", raw.get("title"), raw.get("summary"), raw.get("showTime"),
            item_id=raw.get("code") or raw.get("uniqueUrl"),
            url=raw.get("url") or raw.get("uniqueUrl"),
            important=bool(raw.get("important")), now=now,
        )
        if item:
            item["publisher"] = _clean(raw.get("mediaName")) or "东方财富"
            result.append(item)
    if not result:
        raise ValueError("东方财富快讯返回为空")
    return result[:limit]


def _eastmoney_7x24(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList",
        params={
            "client": "web", "biz": "web_7x24", "fastColumn": "102", "sortEnd": "",
            "pageSize": str(limit),
        },
        headers={"Referer": "https://finance.eastmoney.com/"},
    )
    rows = ((payload.get("data") or {}).get("fastNewsList") or []) if isinstance(payload, Mapping) else []
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        item = _item(
            "eastmoney7x24", "东财7x24", raw.get("title"), raw.get("summary"), raw.get("showTime"),
            item_id=raw.get("code") or raw.get("uniqueUrl"),
            url=raw.get("url") or raw.get("uniqueUrl"), now=now,
        )
        if item:
            result.append(item)
    if not result:
        raise ValueError("东财7x24返回为空")
    return result[:limit]


def _sina(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://zhibo.sina.com.cn/api/zhibo/feed",
        params={"page": "1", "page_size": str(limit), "zhibo_id": "152", "tag_id": "0", "type": "0"},
        headers={"Referer": "https://finance.sina.com.cn/7x24/"},
    )
    rows = (((payload.get("result") or {}).get("data") or {}).get("feed") or {}).get("list", []) if isinstance(payload, Mapping) else []
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        tags = []
        for tag in raw.get("tag") or []:
            if isinstance(tag, Mapping) and tag.get("name"):
                tags.append(tag.get("name"))
        ext = raw.get("ext")
        if isinstance(ext, str):
            try:
                ext_obj = json.loads(ext)
                tags.extend(
                    stock.get("symbol") or stock.get("key")
                    for stock in (ext_obj.get("stocks") or [])
                    if isinstance(stock, Mapping)
                )
            except (TypeError, ValueError):
                pass
        text = raw.get("rich_text") or raw.get("title")
        item = _item(
            "sina7x24", "新浪7x24", text, text, raw.get("create_time"),
            item_id=raw.get("id"), url=raw.get("docurl"), tags=tags,
            important=raw.get("is_top") == 1, now=now,
        )
        if item:
            result.append(item)
    if not result:
        raise ValueError("新浪7x24返回为空")
    return result[:limit]


def _wallstreet(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://api-one.wallstcn.com/apiv1/content/lives",
        params={"channel": "global-channel", "limit": str(limit)},
        headers={"Referer": "https://wallstreetcn.com/", "Origin": "https://wallstreetcn.com"},
    )
    rows = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, Mapping) else []
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title") or str(raw.get("content_text") or "")[:80]
        item = _item(
            "wallstreetcn", "华尔街见闻", title, raw.get("content_text"), raw.get("display_time"),
            item_id=raw.get("id"), url=raw.get("uri"),
            important=raw.get("is_important") is True or (raw.get("score") or 0) >= 3, now=now,
        )
        if item:
            result.append(item)
    if not result:
        raise ValueError("华尔街见闻返回为空")
    return result[:limit]


def _cls(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://www.cls.cn/api/cache",
        params={"app": "CailianpressWeb", "name": "telegraph", "os": "web", "sv": "8.7.9"},
        headers={"Referer": "https://www.cls.cn/telegraph"},
    )
    data = payload.get("data") if isinstance(payload, Mapping) else {}
    data = data if isinstance(data, Mapping) else {}
    rows = data.get("roll_data") or data.get("telegraph") or data.get("roll") or data.get("depth_list") or []
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title") or raw.get("brief") or str(raw.get("content") or "")[:80]
        item = _item(
            "cls", "财联社", title, raw.get("brief") or raw.get("content"), raw.get("ctime"),
            item_id=raw.get("id"), url=raw.get("url") or (f"https://www.cls.cn/detail/{raw.get('id')}" if raw.get("id") else ""),
            important=raw.get("level") in (0, 1), now=now,
        )
        if item:
            result.append(item)
    if not result:
        raise ValueError("财联社返回为空")
    return result[:limit]


def _ths(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    payload = _json(
        request,
        "https://news.10jqka.com.cn/tapp/news/push/stock/",
        params={"page": "1", "tag": "", "track": "website", "pagesize": str(limit)},
        headers={"Referer": "https://www.10jqka.com.cn/"},
    )
    data = payload.get("data") if isinstance(payload, Mapping) else {}
    rows = data.get("list") if isinstance(data, Mapping) else []
    result = []
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        item = _item(
            "ths", "同花顺", raw.get("title"), raw.get("digest"), raw.get("ctime"),
            item_id=raw.get("id"), url=raw.get("url"),
            tags=[tag.get("name") for tag in (raw.get("tags") or []) if isinstance(tag, Mapping)],
            important=raw.get("import") in (1, "1"), now=now,
        )
        if item:
            result.append(item)
    if not result:
        raise ValueError("同花顺快讯返回为空")
    return result[:limit]


def _jin10(request: RequestFn, limit: int, now: datetime) -> list[dict[str, Any]]:
    response = request(
        "GET",
        "https://xnews.jin10.com/",
        headers={"Referer": "https://www.jin10.com/", "Accept": "text/html"},
        timeout=8,
    )
    text = response.content.decode("utf-8", errors="replace")
    blocks = text.split('<div data-id="')[1:]
    result = []
    for block in blocks:
        item_id = (re.match(r"(\d+)", block) or [""])[0]
        title = (re.search(r'jin10-news-list-item-title">\s*([\s\S]*?)\s*</p>', block) or ["", ""])[1]
        summary = (re.search(r'jin10-news-list-item-introduction">\s*([\s\S]*?)\s*</div>', block) or ["", ""])[1]
        url = (re.search(r'jin10-news-list-item-info"><a href="([^"]+)"', block) or ["", ""])[1]
        time_text = (re.search(r'jin10-news-list-item-display_datetime[\s\S]*?<span>([^<]+)</span>', block) or ["", ""])[1]
        item = _item(
            "jin10", "金十数据", title, summary, time_text, item_id=item_id,
            url=url or (f"https://xnews.jin10.com/details/{item_id}" if item_id else ""),
            important=("精选" in block or "重要" in block), now=now,
        )
        if item:
            result.append(item)
        if len(result) >= limit:
            break
    if not result:
        raise ValueError("金十数据返回为空")
    return result


def fetch_sources(request: RequestFn, limit: int = 30) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """并行抓取便携版同款七路新闻源，返回扁平新闻和每源状态。"""

    now = datetime.now().astimezone()
    per_source = max(20, min(120, int(limit) * 3))
    jobs: dict[str, tuple[str, Callable[[], list[dict[str, Any]]]]] = {
        "eastmoney": ("东方财富", lambda: _eastmoney(request, per_source, now)),
        "eastmoney7x24": ("东财7x24", lambda: _eastmoney_7x24(request, per_source, now)),
        "sina7x24": ("新浪7x24", lambda: _sina(request, per_source, now)),
        "wallstreetcn": ("华尔街见闻", lambda: _wallstreet(request, per_source, now)),
        "cls": ("财联社", lambda: _cls(request, per_source, now)),
        "ths": ("同花顺", lambda: _ths(request, per_source, now)),
        "jin10": ("金十数据", lambda: _jin10(request, per_source, now)),
    }
    rows: list[dict[str, Any]] = []
    status: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="news") as pool:
        future_map = {pool.submit(fn): (source, name) for source, (name, fn) in jobs.items()}
        for future in as_completed(future_map):
            source, name = future_map[future]
            try:
                fetched = future.result()
                latest = max((float(item.get("_timestamp") or 0) for item in fetched), default=0)
                status[source] = {
                    "name": name, "ok": True, "count": len(fetched), "error": None,
                    "latest_minutes": round(max(0, (now.timestamp() - latest) / 60), 1) if latest else None,
                }
                rows.extend(fetched)
            except Exception as exc:
                status[source] = {"name": name, "ok": False, "count": 0, "error": str(exc)[:240], "latest_minutes": None}
    return rows, status


def merge_news(groups: list[list[Mapping[str, Any]]], limit: int = 30, retention_hours: int = 12) -> list[dict[str, Any]]:
    """合并新闻：半天留存、按来源+标题去重、按发布时间倒序。"""

    now = datetime.now().astimezone()
    cutoff = now.timestamp() - retention_hours * 3600
    candidates: list[dict[str, Any]] = []
    for group in groups:
        for raw in group:
            if not isinstance(raw, Mapping) or not raw.get("title"):
                continue
            item = dict(raw)
            try:
                timestamp = float(item.get("_timestamp"))
            except (TypeError, ValueError):
                timestamp = _parse_time(item.get("published") or item.get("time"), now).timestamp()
            if timestamp > now.timestamp() + 5 * 60:
                continue
            if timestamp < cutoff:
                continue
            item["_timestamp"] = timestamp
            item.setdefault("published", item.get("time") or now.isoformat(timespec="seconds"))
            item.setdefault("time", item.get("published"))
            candidates.append(item)
    candidates.sort(key=lambda item: float(item.get("_timestamp") or 0), reverse=True)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in candidates:
        title_key = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(item.get("title") or "")).lower()[:40]
        source = str(item.get("source") or "unknown")
        key = f"{source}|{title_key}"
        if not title_key or key in seen:
            continue
        seen.add(key)
        item.pop("_timestamp", None)
        result.append(item)
        if len(result) >= max(1, int(limit)):
            break
    return result
