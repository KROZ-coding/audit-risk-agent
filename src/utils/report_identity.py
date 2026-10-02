# -*- coding: utf-8 -*-
"""报告身份识别：从年报/半年报原文确定性提取公司与报告期元数据。

背景（实测缺陷）：综合研判链路完全依赖 LLM 抽取 ``company_info``，模型在
17 万字符原文里经常漏掉封面公司名与报告期，导致：

- 产物文件名退化为 ``未知公司_IX_*``；
- Altman Z-Score / Beneish M-Score 因「缺少本期期间」被判为 ``not_applicable``，
  而不是正确的「中期报告不适用年度模型」；
- 报告元数据表头公司名/股票代码空白。

本模块只做**确定性**识别：封面行、``股票代码：`` 标签、``20XX 年半年度`` /
``20XX 年度`` 期间表述、行业关键词、``未经审计`` 意见标记。绝不按金额量级或
上下文臆测；识别不到就返回空串，由调用方决定缺省文案。
"""
import re

# 行业关键词 -> 归一的行业标签（与提取提示词约定的取值域保持一致）
_INDUSTRY_KEYWORDS = (
    ("能源", ("石油", "天然气", "油气", "煤炭", "能源", "电力")),
    ("金融", ("银行", "证券", "保险", "信托", "金融")),
    ("房地产", ("房地产", "地产", "置业")),
    ("医药", ("医药", "制药", "生物", "医疗", "药业")),
    ("互联网", ("互联网", "软件", "信息技术", "云计算", "电商", "平台")),
    ("制造业", ("制造", "机械", "钢铁", "化工", "汽车", "电子", "半导体")),
    ("零售", ("零售", "商贸", "连锁", "超市", "百货")),
    ("农业", ("农业", "种植", "养殖", "农牧", "食品")),
    ("军工", ("军工", "航空", "航天", "兵器", "船舶")),
    ("传媒", ("传媒", "文化", "出版", "影视", "广告")),
)

# 公司名行：以「股份有限公司/有限公司/集团/公司」结尾的封面标题行
_COMPANY_LINE = re.compile(
    r"^\s*([\u4e00-\u9fa5A-Za-z()（）·]{4,40}"
    r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司|集团|股份有限公司))\s*$"
)
_COMPANY_SUFFIX = re.compile(
    r"([\u4e00-\u9fa5A-Za-z()（）·]{4,40}(?:股份有限公司|有限责任公司|有限公司))"
)
_STOCK_CODE = re.compile(r"(?:股票代码|证券代码|A\s*股股票代码|股票代号)\s*[:：]?\s*(\d{6})")

_HALF_YEAR = re.compile(r"(20\d{2})\s*年\s*(?:半年度?|上半年|中期)")
_FULL_YEAR = re.compile(r"(20\d{2})\s*年度")


def _clean_company(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).strip(" 　")


def extract_company_name(text: str) -> str:
    """优先取封面公司全称；退化为全文首个「XX有限公司」主体。"""
    if not text:
        return ""
    # 1) 封面/正文标题行（取前 40 页以内，封面通常在开头）
    head = text[:8000]
    for raw_line in head.splitlines():
        line = raw_line.strip().strip("＊*·-—_ ")
        m = _COMPANY_LINE.match(line)
        if m:
            return _clean_company(m.group(1))
    # 2) 退化：任意位置的完整公司主体名，取最短的匹配（避免把整句吞进来）
    candidates = {_clean_company(m.group(1)) for m in _COMPANY_SUFFIX.finditer(head)}
    if candidates:
        return min(candidates, key=len)
    return ""


def extract_stock_code(text: str) -> str:
    if not text:
        return ""
    m = _STOCK_CODE.search(text[:8000])
    return m.group(1) if m else ""


def extract_report_period(text: str) -> str:
    """识别报告期：优先半年报/中期，其次年度。"""
    if not text:
        return ""
    head = text[:8000]
    m = _HALF_YEAR.search(head)
    if m:
        return f"{m.group(1)}年半年度"
    m = _FULL_YEAR.search(head)
    if m:
        return f"{m.group(1)}年度"
    return ""


def extract_report_year(text: str) -> str:
    period = extract_report_period(text)
    m = re.search(r"(20\d{2})", period or "")
    return m.group(1) if m else ""


def extract_industry(text: str) -> str:
    """按行业关键词把公司归入提示词约定取值域；命中即返回标签。"""
    if not text:
        return ""
    head = text[:20000]
    # 命中越靠前、出现频次越高者优先：用 (首次出现位置, -次数) 排序
    scored = []
    for label, keywords in _INDUSTRY_KEYWORDS:
        hits = [head.find(kw) for kw in keywords if kw in head]
        if hits:
            scored.append((min(hits), label))
    if not scored:
        return ""
    return min(scored, key=lambda item: item[0])[1]


def extract_audit_opinion(text: str) -> str:
    """半年度报告未经审计；年度报告识别常见审计意见类型。"""
    if not text:
        return ""
    head = text[:8000]
    if "未经审计" in head or "未经审计" in text[:20000]:
        return "未经审计（半年度报告）"
    for opinion in ("标准无保留意见", "带强调事项段的无保留意见", "保留意见",
                    "否定意见", "无法表示意见"):
        if opinion in text:
            return opinion
    if "无保留意见" in text:
        return "无保留意见"
    return ""


def extract_accounting_standard(text: str) -> str:
    """识别报告中用于本次财务取数的会计准则。"""
    if not text:
        return ""
    # 半年度报告的准则说明可能位于目录或财务报告章节之后；只扫前 20k
    # 会把真实报告降级为空。中国准则作为本项目默认取数口径优先返回。
    if "中国企业会计准则" in text:
        return "中国企业会计准则"
    if "国际财务报告会计准则" in text:
        return "国际财务报告会计准则"
    return ""


def extract_report_identity(text: str) -> dict:
    """汇总报告身份字段；识别不到的字段返回空串（不臆测）。"""
    if not text:
        return {}
    company = extract_company_name(text)
    period = extract_report_period(text)
    industry = extract_industry(text)
    opinion = extract_audit_opinion(text)
    identity = {
        "company_name": company,
        "stock_code": extract_stock_code(text),
        "report_year": extract_report_year(text),
        "period": period,
        "report_period": period,
        "industry": industry,
        "audit_opinion": opinion,
        "accounting_standard": extract_accounting_standard(text),
    }
    return {k: v for k, v in identity.items() if v}


def apply_company_info_fallback(company_info, text: str) -> dict:
    """把确定性识别结果补进 ``company_info``（已有值优先，绝不覆盖非空字段）。

    供 ``agent._post_process`` / ``main._build_final_report_from_messages`` 在
    生成文件名与 PDF/Excel 之前调用，保证产物名不再退化为「未知公司」。
    """
    result = dict(company_info) if isinstance(company_info, dict) else {}
    identity = extract_report_identity(text)
    if not identity:
        return result
    aliases = {
        "company_name": ("company_name", "name", "company", "company_full_name"),
        "stock_code": ("stock_code",),
        "report_year": ("report_year", "year", "report_period", "period"),
        "industry": ("industry", "industry_name"),
        "audit_opinion": ("audit_opinion",),
        "accounting_standard": ("accounting_standard",),
        "period": ("period", "report_period"),
    }
    for target, keys in aliases.items():
        value = identity.get(target)
        if not value:
            continue
        if not any(str(result.get(key) or "").strip() for key in keys):
            result[keys[0]] = value
    # period/report_period 使用原文确定性识别结果校正：模型常把半年报简化为
    # "2025" 或写成不规范的 "2025（半年度）"，会掩盖期间类型和比较基础。
    if identity.get("period"):
        for key in ("period", "report_period"):
            existing = re.sub(r"[\s（）()年]", "", str(result.get(key) or ""))
            expected = re.sub(r"[\s（）()年]", "", identity["period"])
            if existing != expected:
                result[key] = identity["period"]
    return result
