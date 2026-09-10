"""行业风向标工具（C 端轻量工具）

输入行业名（或公司名），在线抓取公开财经新闻并结合本地知识库的行业风险
特征，输出未来 3-6 个月的行业景气度预判：利好/利空事件清单（带来源与
日期）、景气度温度计（1-10）、对目标公司年报风险的传导影响提示。

数据链路（降级可见）：
1. 在线层：并发抓取公开 RSS 源，壁钟预算 8s 硬截断（不拖垮 SSE 流式响应）；
2. 离线层：全部源失败/超时时降级检索 knowledge_base 的
   《行业经营风险特征库》《行业基准库》，输出标注 data_mode=offline_kb；
3. 进程内 TTL 缓存（30 分钟）：同行业短时间重复查询不重复发网络请求。

合规定位：输出为行业风险提示参考，不构成投资建议，强制携带免责声明。
"""
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 新闻源注册表：(源名称, RSS URL, 解析器) ──
# 未来加源只需 append；parse_fn=None 表示用缺省 RSS 解析（接 JSON API 源时可传专用解析器）；
# 单源失败独立自吞，不影响其他源
NEWS_SOURCES = [
    ("东方财富财经要闻", "https://rss.eastmoney.com/rss_partener.xml", None),
    ("新浪财经证券", "https://rss.sina.com.cn/roll/stock/hot_roll.xml", None),
    ("百度股票焦点", "http://news.baidu.com/n?cmd=1&class=stock&tn=rss", None),
]

# 抓取预算：单源 (连接 3s, 读取 5s)；总壁钟 8s 硬截断（可环境变量覆盖）
_SOURCE_TIMEOUT = (3, 5)
_WALL_BUDGET = float(os.getenv("OUTLOOK_FETCH_BUDGET", "8"))
_PER_SOURCE_TOP = 5      # 每源保留条数（控制输出体量，防挤占消息滑窗）
_PARSE_SCAN_CAP = 100    # 单源扫描条目上限：超大 feed 只分类前 100 条，控解析耗时
_CACHE_TTL = 30 * 60     # 正缓存 30 分钟
_NEG_CACHE_TTL = 60      # 全失败负缓存 60s，防降级路径被雪崩重试
_CACHE_MAX = 64          # 缓存键容量上限（FIFO 淘汰）

# ── 情感关键词规则表（可追溯）：命中一次记一票 ──
_POSITIVE_KEYWORDS = [
    "增长", "回暖", "复苏", "扩产", "涨价", "利好", "突破", "创新高",
    "政策支持", "补贴", "订单", "中标", "放量", "景气",
]
_NEGATIVE_KEYWORDS = [
    "下滑", "下跌", "亏损", "过剩", "降价", "利空", "处罚", "违规",
    "退市", "风险", "萎缩", "裁员", "债务", "违约", "低迷",
]

DISCLAIMER = (
    "本风向标由 AI 辅助生成，基于公开新闻与本地知识库的行业风险提示参考，"
    "不构成任何投资建议；新闻时效与完整性受数据源限制，决策需独立判断。"
)

# ── 进程内 TTL 缓存 ──
_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    """读缓存：命中且未过期返回 payload，否则 None。"""
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, ttl, payload = entry
        if time.time() - ts > ttl:
            _cache.pop(key, None)
            return None
        return payload


def _cache_put(key: str, payload, ttl: float):
    """写缓存：超容量时 FIFO 淘汰最老键。"""
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX and key not in _cache:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.time(), ttl, payload)


def _parse_rss(xml_text: str, source_name: str, keyword: str) -> list:
    """解析 RSS XML 为事件列表。

    相关性策略：优先返回标题/摘要命中行业关键词的条目；若整源无命中（泛财经
    要闻源对细分行业词常无命中），退回该源通用要闻的 top 条，避免过度过滤导致
    在线层永远为空、动辄降级离线知识库。来源字段始终标注真实出处，可溯源。

    安全加固：外部 XML 属不可信输入——拒绝含 DTD/实体声明的文档（防实体
    膨胀/外部实体攻击），并限制文档体量（防资源耗尽）。
    宽容解析：item 结构各源略有差异，逐字段容错；解析失败返回空列表。
    """
    if len(xml_text) > 2 * 1024 * 1024:  # 2MB 上限：正常 RSS 远小于此
        logger.warning(f"新闻源 {source_name} 返回体量异常（>2MB），已丢弃")
        return []
    head = xml_text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        logger.warning(f"新闻源 {source_name} 含 DTD/实体声明，已拒绝解析")
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    matched, generic = [], []
    scanned = 0
    for item in root.iter("item"):
        # 扫描上限：超大 feed 只取前若干条参与分类（RSS 通常按时间倒序），控解析耗时
        if scanned >= _PARSE_SCAN_CAP:
            break
        scanned += 1
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        ev = {
            "title": title[:120],
            "date": (item.findtext("pubDate") or "").strip()[:32],
            "source": source_name,
            "url": (item.findtext("link") or "").strip()[:200],
        }
        desc = (item.findtext("description") or "").strip()
        if keyword and (keyword in title or keyword in desc):
            matched.append(ev)
        else:
            generic.append(ev)
        # 两类各攒够 top 数即可停止扫描
        if len(matched) >= _PER_SOURCE_TOP and len(generic) >= _PER_SOURCE_TOP:
            break
    # 关键词命中优先；无命中时退回通用要闻，保证在线层不为空
    return matched[:_PER_SOURCE_TOP] or generic[:_PER_SOURCE_TOP]


def _fetch_one_source(name: str, url: str, keyword: str, parse_fn=None) -> list:
    """抓取单个源：任何异常自吞并记日志，返回空列表。

    parse_fn 缺省用 RSS 解析；源表可按源指定解析器（如未来接 JSON API 源）。
    """
    try:
        import requests
        resp = requests.get(url, timeout=_SOURCE_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (annual-report-risk-agent)"
        })
        resp.raise_for_status()
        # RSS 真实编码写在 XML 声明里，但 HTTP 头常缺 charset，requests 会按 HTTP
        # 默认回退 ISO-8859-1，使中文 feed 变乱码、解析出 0 条（东方财富即此症状）。
        # 探测到这一可疑默认时改用 apparent_encoding（按内容探测）纠正编码。
        if (resp.encoding or "").lower() == "iso-8859-1":
            detected = resp.apparent_encoding
            if detected:
                resp.encoding = detected
        return (parse_fn or _parse_rss)(resp.text, name, keyword)
    except Exception as e:  # noqa: BLE001 - 单源失败不影响其他源
        logger.warning(f"新闻源 {name} 抓取失败: {e}")
        return []


def _fetch_online_news(keyword: str) -> list:
    """并发抓取全部源，总壁钟预算硬截断；超预算的源结果直接弃用。

    注意不能用 with 语义：退出时 shutdown(wait=True) 会等全部线程跑完，
    壁钟预算形同虚设。改为 wait(timeout) 后非阻塞 shutdown；未完成线程由
    单源读超时 5s 保证最终自然退出，不泄漏。
    """
    events = []
    pool = ThreadPoolExecutor(max_workers=len(NEWS_SOURCES))
    try:
        futures = [pool.submit(_fetch_one_source, n, u, keyword, fn)
                   for n, u, fn in NEWS_SOURCES]
        done, _ = wait(futures, timeout=_WALL_BUDGET)
        for f in done:
            try:
                events.extend(f.result())
            except Exception:  # noqa: BLE001
                continue
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return events


def _search_offline_kb(industry: str) -> list:
    """离线降级：检索本地知识库的行业风险特征与基准语料。

    照抄 knowledge_search 范式；按来源过滤两份行业语料，避免混入法规条文。
    """
    try:
        from local_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        results = kb.search(query=f"{industry} 经营风险 景气度 行业特征", top_k=6, min_score=0.05)
        allowed = {"行业经营风险特征库.txt", "行业基准库.txt"}
        picked = [r for r in results if r.get("source") in allowed]
        # 两份行业语料未命中时退而取全库 top 结果：降级路径宁可给出泛化参考
        # 也不输出空卡片（来源字段仍标注真实出处，可溯源不误导）
        if not picked:
            picked = results
        events = []
        for r in picked:
            events.append({
                "title": str(r.get("content", ""))[:160],
                "date": "",
                "source": f"本地知识库·{r.get('source', '')}",
                "url": "",
            })
        return events[:_PER_SOURCE_TOP]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"离线知识库降级检索失败: {e}")
        return []


def _score_sentiment(events: list) -> tuple:
    """按关键词规则表为事件标注利好/利空极性，并合成温度计（1-10）。

    温度计规则（可追溯）：基准 5 分，每条净利好 +0.5、净利空 -0.5，clamp 1-10。

    Returns:
        (温度计分值, 标注极性后的事件列表)
    """
    tagged = []
    net = 0
    for ev in events:
        text = f"{ev.get('title', '')}"
        pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
        neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
        if pos > neg:
            polarity = "利好"
            net += 1
        elif neg > pos:
            polarity = "利空"
            net -= 1
        else:
            polarity = "中性"
        tagged.append({**ev, "polarity": polarity})
    thermometer = max(1.0, min(10.0, 5.0 + net * 0.5))
    return round(thermometer, 1), tagged


def _outlook_text(thermometer: float) -> str:
    """按温度计分值生成 3-6 个月展望文案。"""
    if thermometer >= 7:
        return "未来 3-6 个月行业景气度偏暖：利好事件占优，需同时留意高景气下的产能扩张与估值风险"
    if thermometer >= 4:
        return "未来 3-6 个月行业景气度中性：多空信号交织，建议跟踪政策与头部公司订单变化"
    return "未来 3-6 个月行业景气度偏冷：利空事件占优，关注行业内公司的现金流与减值风险传导"


def _transmission_text(thermometer: float, company_name: str) -> str:
    """行业景气 → 公司年报风险的传导影响提示。"""
    target = company_name or "行业内公司"
    if thermometer >= 7:
        return (f"景气上行期，{target}的年报风险关注点转向：激进扩产带来的在建工程与折旧压力、"
                "高增长下的应收账款质量、商誉并购冲动。")
    if thermometer >= 4:
        return (f"景气平稳期，{target}的年报风险关注点：毛利率与行业基准的偏离度、"
                "存货周转变化、经营现金流与净利润的匹配度。")
    return (f"景气下行期，{target}的年报风险传导路径：收入下滑 → 存货跌价与应收坏账计提压力 → "
            "商誉与固定资产减值风险 → 持续经营能力，需重点核查减值计提是否充分。")


@tool
def industry_outlook(industry: str, company_name: str = "") -> str:
    """生成「行业风向标」：结合近期新闻与知识库，预判行业未来 3-6 个月景气度。

    在线抓取公开财经新闻源（RSS，8 秒预算），失败时自动降级到本地知识库的
    行业风险特征语料（输出 data_mode 标注数据来源，降级可见）。
    输出为行业风险提示参考，不构成投资建议。

    Args:
        industry: 行业名称，如「锂电」「纺织」「白酒」；也可传细分赛道关键词
        company_name: 可选，目标公司名；传入后传导影响分析将针对该公司表述

    Returns:
        JSON 字符串，包含：
        - industry: 行业名
        - events: 事件清单（title/date/source/polarity，每条标注利好/利空/中性）
        - thermometer: 景气度温度计（1-10，5 为中性基准）
        - outlook_3_6m: 3-6 个月展望文案
        - transmission_to_company: 对公司年报风险的传导影响提示
        - data_mode: "online"（在线新闻）/ "offline_kb"（知识库降级）
        - fetched_at: 数据获取时间
        - disclaimer: 免责声明（前端渲染必须展示）
    """
    try:
        industry = str(industry or "").strip()
        if not industry:
            return json.dumps({
                "error": "行业名称为空，请提供行业名（如「锂电」「纺织」）",
                "disclaimer": DISCLAIMER,
            }, ensure_ascii=False, indent=2)

        cache_key = industry
        cached = _cache_get(cache_key)
        if cached is not None:
            events, data_mode = cached
        else:
            events = _fetch_online_news(industry)
            if events:
                data_mode = "online"
                _cache_put(cache_key, (events, data_mode), _CACHE_TTL)
            else:
                # 在线全失败 → 离线知识库降级；写短 TTL 负缓存防雪崩重试
                events = _search_offline_kb(industry)
                data_mode = "offline_kb"
                _cache_put(cache_key, (events, data_mode), _NEG_CACHE_TTL)

        thermometer, tagged = _score_sentiment(events)

        result = {
            "industry": industry,
            "events": tagged,
            "thermometer": thermometer,
            "outlook_3_6m": _outlook_text(thermometer),
            "transmission_to_company": _transmission_text(thermometer, company_name),
            "data_mode": data_mode,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disclaimer": DISCLAIMER,
        }
        logger.info(f"行业风向标生成：{industry}，温度计 {thermometer}，"
                    f"事件 {len(tagged)} 条，模式 {data_mode}")
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001 - 降级可见：保留核心字段不炸流
        logger.warning(f"行业风向标生成异常，降级输出: {e}")
        return json.dumps({
            "industry": str(industry or ""),
            "events": [],
            "thermometer": 5.0,
            "error": f"行业风向标生成发生异常：{e}",
            "data_mode": "error",
            "disclaimer": DISCLAIMER,
        }, ensure_ascii=False, indent=2)
