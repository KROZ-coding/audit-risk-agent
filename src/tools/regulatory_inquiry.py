"""监管问询在线查询工具

支持从上交所/深交所公开接口实时查询特定公司的监管问询函记录和最新监管动态。

核心设计原则：网络查询是可选增强，不是必要依赖。
- 接口超时/报错/被拦截时，静默返回空结果 JSON + 手动查询 URL
- 绝不抛异常到 ToolNode，绝不阻断 Agent 主分析流程
- 查询成功时提供结构化的问询函列表供 Agent 引用

数据源：
- 上交所：https://query.sse.com.cn/ 系列公开接口
- 深交所：http://www.szse.cn/api/ 系列公开接口
"""
import json
import logging
import time

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 接口配置 ──
_TIMEOUT = (3, 5)  # (连接超时, 读取超时)
_CACHE_TTL = 600   # 同一 query 10 分钟缓存，避免重复请求
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# 上交所监管问询页面（供降级时给出人工查询入口）
_SSE_MANUAL_URL = "https://www.sse.com.cn/disclosure/credibility/supervision/inquiries/"
# 深交所监管问询页面
_SZSE_MANUAL_URL = "http://www.szse.cn/disclosure/supervision/inquire/index.html"

# 进程内缓存
_cache: dict = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    ts, payload = entry
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload):
    # 容量限制：最多 32 条
    if len(_cache) >= 32 and key not in _cache:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.time(), payload)


def _detect_exchange(query: str) -> str:
    """根据股票代码前缀判断交易所：6开头=sse，0/3开头=szse"""
    code = query.strip()
    if code.startswith("6"):
        return "sse"
    if code.startswith("0") or code.startswith("3"):
        return "szse"
    return "sse"  # 默认上交所


def _fetch_sse(query: str, limit: int):
    """从上交所接口查询监管问询。

    Returns:
        (items, ok)：items 为问询列表；ok 表示接口是否成功响应。
        ok=True 且 items 为空 = 该公司确无问询记录；ok=False = 网络/解析失败。
    """
    try:
        import requests
        # 上交所公告查询接口（监管问询类型）
        url = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
        params = {
            "jsonCallBack": "",
            "isPagination": "true",
            "pageHelp.pageSize": str(limit),
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "productId": query if query else "",
            "bulletinType": "13",  # 13 = 监管问询类
            "_": str(int(time.time() * 1000)),
        }
        headers = {**_HEADERS, "Referer": "https://www.sse.com.cn/"}
        resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        # 上交所接口可能返回 JSONP 或纯 JSON
        text = resp.text.strip()
        if text.startswith("(") or text.startswith("jsonpCallback"):
            # 去掉 JSONP 包装
            start = text.find("{")
            end = text.rfind("}") + 1
            text = text[start:end]
        data = json.loads(text)
        results = data.get("result", [])
        items = []
        for r in results:
            items.append({
                "title": r.get("TITLE", r.get("title", "")),
                "date": r.get("CDATE", r.get("SSEDate", "")),
                "inquiry_type": "监管问询",
                "url": f"https://www.sse.com.cn{r.get('URL', '')}",
            })
        return items, True
    except Exception as e:
        logger.warning(f"上交所监管问询查询失败（静默跳过）: {e}")
        return [], False


def _fetch_szse(query: str, limit: int):
    """从深交所接口查询监管问询。

    Returns:
        (items, ok)：items 为问询列表；ok 表示接口是否成功响应。
        ok=True 且 items 为空 = 该公司确无问询记录；ok=False = 网络/解析失败。
    """
    try:
        import requests
        url = "http://www.szse.cn/api/disc/announcement/annList"
        payload = {
            "seDate": "",
            "stock": [{"code": query}] if query else [],
            "channelCode": ["listedNotice_disc"],
            "bigCategoryId": ["010301"],  # 监管问询
            "pageSize": limit,
            "pageNum": 1,
        }
        headers = {**_HEADERS, "Referer": "http://www.szse.cn/", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("data", [])
        items = []
        for r in records:
            items.append({
                "title": r.get("title", ""),
                "date": r.get("publishTime", "")[:10],
                "inquiry_type": "监管问询",
                "url": f"http://disc.szse.cn/download{r.get('attachPath', '')}",
            })
        return items, True
    except Exception as e:
        logger.warning(f"深交所监管问询查询失败（静默跳过）: {e}")
        return [], False


def _parse_sina_suggest(text: str):
    """解析新浪股票联想接口返回，提取首个匹配的股票代码（6 位数字）；无则 None。

    返回形如：var suggestvalue="贵州茅台,11,600519,sh600519,贵州茅台,...";
    字段以逗号分隔，第 3 个字段为股票代码；多条记录以分号分隔，取第一条。
    """
    start = text.find('"')
    end = text.rfind('"')
    if start < 0 or end <= start:
        return None
    inner = text[start + 1:end]
    if not inner:
        return None
    fields = inner.split(";")[0].split(",")
    if len(fields) >= 3:
        code = fields[2].strip()
        if code.isdigit() and len(code) == 6:
            return code
    return None


def _resolve_stock_code(query: str):
    """把公司名/简称解析为股票代码；已是 6 位代码直接返回；网络失败静默返回 None。

    交易所接口按代码查询命中率远高于按名称，故查询前先尝试解析。
    解析失败不阻断：调用方会回退用原始 query 继续查。
    """
    q = (query or "").strip()
    if not q:
        return None
    if q.isdigit() and len(q) == 6:
        return q
    try:
        import requests
        from urllib.parse import quote
        url = f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={quote(q)}"
        resp = requests.get(url, timeout=_TIMEOUT,
                            headers={**_HEADERS, "Referer": "https://finance.sina.com.cn/"})
        return _parse_sina_suggest(resp.text)
    except Exception as e:
        logger.warning(f"股票代码解析失败（静默跳过）: {e}")
        return None


@tool
def search_regulatory_inquiries(query: str = "", exchange: str = "auto", limit: int = 10) -> str:
    """查询上市公司监管问询函记录（实时在线查询，网络失败自动跳过不影响分析）。

    支持两种模式：
    1. 按公司查询：传入股票代码（如 600519）或公司简称，返回该公司近期监管问询函列表
    2. 查最新动态：不传 query 或传空字符串，返回最近的监管问询公告（用于判断监管热点）

    网络查询失败时返回 degraded=true 的空结果，不抛异常、不阻断分析流程。

    Args:
        query: 股票代码（如 600519、000001）或公司简称（公司名会自动解析为股票代码）；为空时查最新动态
        exchange: 交易所选择，sse=上交所，szse=深交所，auto=按代码前缀自动判断
        limit: 返回条数上限，默认 10

    Returns:
        JSON 字符串，包含：
        - items: 问询函列表 [{title, date, inquiry_type, url}]
        - source: 数据来源（sse/szse）
        - query_mode: 查询模式（by_company/latest）
        - resolved_code: 公司名解析出的股票代码（按名称查询时）
        - degraded: 是否真降级（仅接口异常/超时为 true；查无记录不算）
        - no_records: 接口正常但该公司近期无问询记录
        - manual_url: 人工查询入口 URL
        - disclaimer: 提示说明
    """
    # 顶层 try/except：绝不让任何异常逃逸到 ToolNode
    try:
        query = str(query or "").strip()
        limit = min(max(int(limit), 1), 20)

        # 公司名/简称自动转股票代码：交易所接口按代码查询命中率远高于按名称
        resolved_code = _resolve_stock_code(query) if query else None
        effective_query = resolved_code or query

        # 缓存检查（按解析后的代码缓存，名称与代码查询共享缓存）
        cache_key = f"{effective_query}:{exchange}:{limit}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # 确定交易所
        if exchange == "auto":
            exchange = _detect_exchange(effective_query) if effective_query else "sse"

        query_mode = "by_company" if effective_query else "latest"
        manual_url = _SSE_MANUAL_URL if exchange == "sse" else _SZSE_MANUAL_URL

        # 执行查询（返回 (items, ok)：ok=接口是否成功响应）
        if exchange == "szse":
            items, fetched_ok = _fetch_szse(effective_query, limit)
        else:
            items, fetched_ok = _fetch_sse(effective_query, limit)

        # 区分「接口失败（真降级）」与「接口正常但查无记录」（后者不算降级，
        # 只是该公司确无问询，避免把干净公司误报成网络故障）
        degraded = not fetched_ok
        no_records = fetched_ok and len(items) == 0 and effective_query != ""
        exchange_cn = "上交所" if exchange == "sse" else "深交所"
        if degraded:
            disclaimer = "网络查询未成功，不影响分析结论。如需查看监管问询详情，请手动访问上述 URL。"
        elif no_records:
            disclaimer = f"接口正常，{effective_query} 近期无监管问询记录（来源：{exchange_cn}）。"
        else:
            disclaimer = f"已从{exchange_cn}获取 {len(items)} 条监管问询记录。"

        result = {
            "items": items,
            "source": exchange,
            "query_mode": query_mode,
            "resolved_code": resolved_code,
            "degraded": degraded,
            "no_records": no_records,
            "manual_url": manual_url,
            "disclaimer": disclaimer,
        }

        output = json.dumps(result, ensure_ascii=False, indent=2)
        _cache_put(cache_key, output)
        logger.info(f"监管问询查询完成：query={query}, exchange={exchange}, "
                    f"items={len(items)}, degraded={degraded}")
        return output

    except Exception as e:
        # 终极兜底：任何意外都降级为空结果，绝不炸流
        logger.warning(f"监管问询查询异常（降级输出）: {e}")
        return json.dumps({
            "items": [],
            "source": exchange if 'exchange' in dir() else "unknown",
            "query_mode": "error",
            "degraded": True,
            "manual_url": _SSE_MANUAL_URL,
            "disclaimer": "监管问询查询发生异常，已自动跳过，不影响分析结论。",
        }, ensure_ascii=False, indent=2)
