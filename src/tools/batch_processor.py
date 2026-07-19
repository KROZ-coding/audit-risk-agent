"""批量处理工具 - 多家公司年报批量分析 + 行业对比报告

本模块支持对多家上市公司进行并行的财务风险分析，并在独立分析基础上
生成行业横向对比报告，包括：
- 各公司独立的风险评分和等级划分（重大/重要/一般）
- 按行业分组的风险统计和排名
- 行业平均风险水平对比

使用 ThreadPoolExecutor 实现多线程并行分析，最大 5 个工作线程。
"""
import json
import logging
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────

# 支持的财务字段列表（兼容 _current 后缀和无后缀两种命名格式）
# 用于从公司顶层数据中提取财务字段时使用
_FINANCIAL_FIELDS = [
    "revenue_current", "revenue", "net_profit_current", "net_profit",
    "total_assets_current", "total_assets", "total_liabilities_current", "total_liabilities",
    "accounts_receivable_current", "accounts_receivable", "operating_cashflow_current", "operating_cashflow",
    "inventory_current", "inventory", "current_assets_current", "current_assets",
    "current_liabilities_current", "current_liabilities", "cost_of_goods_current", "cost_of_goods",
    "net_assets_current", "net_assets", "goodwill_current", "goodwill",
    "other_receivables_current", "other_receivables",
    "cash_and_equivalents_current", "cash_and_equivalents",
    "short_term_debt_current", "short_term_debt",
    "interest_income_current", "interest_income", "interest_expense_current", "interest_expense",
    "revenue_previous", "accounts_receivable_previous", "inventory_previous",
]

# 五大风险维度模板，初始值为 0，用于按维度统计告警数量
_DIMENSION_TEMPLATE = {
    "财务错报风险": 0, "关联交易风险": 0, "信息披露合规风险": 0,
    "持续经营风险": 0, "监管处罚类高风险": 0,
}

# 告警文本关键词 → 风险维度的映射规则（按优先级排列）
# 同一条告警可能命中多个维度（如"存贷双高"同时属于财务错报和监管处罚）
_ALERT_DIMENSION_MAP = [
    (["现金流", "毛利率", "应收", "存货", "商誉", "其他应收", "存贷双高"], "财务错报风险"),
    (["关联", "资金占用"], "关联交易风险"),
    (["披露", "会计政策"], "信息披露合规风险"),
    (["流动比率", "资产负债率", "偿债"], "持续经营风险"),
    (["存贷双高", "利息"], "监管处罚类高风险"),
]


# ── 内部函数 ──────────────────────────────────────────────

def _extract_financial(company: dict) -> dict:
    """从公司数据中提取财务字段，支持嵌套和顶层两种格式。

    优先从 company["financial_data"] 嵌套字典中读取；若该字段不存在或为空，
    则回退到 company 顶层，按 _FINANCIAL_FIELDS 定义的字段逐一提取。

    Args:
        company: 单家公司的原始数据字典

    Returns:
        包含财务字段的字典，若无数据则返回空字典
    """
    fin = company.get("financial_data", {})
    # 若嵌套字段为空，则从顶层字段中逐一提取
    return fin or {k: company[k] for k in _FINANCIAL_FIELDS if k in company}


def _classify_alerts(alerts: list) -> dict:
    """将告警列表按五大风险维度分类统计。

    遍历每条告警文本，通过 _ALERT_DIMENSION_MAP 中定义的关键词进行匹配，
    判断该告警所属的风险维度并累加计数。

    Args:
        alerts: calculate_financial_indicators 返回的告警文本列表

    Returns:
        各维度的告警数量字典，键为维度中文名，值为告警数
    """
    dims = dict(_DIMENSION_TEMPLATE)
    for alert in alerts:
        alert_text = str(alert)
        # 逐条关键词规则匹配，命中即累加对应维度计数
        for keywords, dim in _ALERT_DIMENSION_MAP:
            if any(kw in alert_text for kw in keywords):
                dims[dim] += 1
    return dims


def _score_risk(indicators: dict) -> int:
    """基于财务指标计算综合风险得分（0-100 分制）。

    评分规则（各条件独立累加，上限 100）：
    - 经营现金流/净利润比 < 0.5：+20 分（利润缺乏现金流支撑）
    - 应收账款占营收比 > 30%：+15 分（回款风险或收入真实性存疑）
    - 资产负债率 > 70%：+15 分（高杠杆，偿债压力大）
    - 流动比率 < 1：+15 分（短期偿债能力不足）
    - 商誉占净资产比 > 30%：+10 分（减值风险敞口较大）

    Args:
        indicators: calculate_financial_indicators 返回的指标字典

    Returns:
        风险得分（0-100），上限 100
    """
    score = 0
    if indicators.get("operating_cashflow_to_net_profit_ratio", 1) < 0.5: score += 20
    if indicators.get("accounts_receivable_to_revenue_ratio", 0) > 30: score += 15
    if indicators.get("debt_to_asset_ratio_pct", 0) > 70: score += 15
    if indicators.get("current_ratio", 2) < 1: score += 15
    if indicators.get("goodwill_to_net_assets_ratio_pct", 0) > 30: score += 10
    return min(score, 100)


def _analyze_single_company(company_data: Dict[str, Any]) -> Dict[str, Any]:
    """对单个公司执行独立的风险分析。

    处理流程：
    1. 提取公司基本信息（名称、报告年度、所属行业）
    2. 从公司数据中提取财务字段
    3. 调用 calculate_financial_indicators 计算 16 项财务指标和预警
    4. 基于指标结果计算综合风险评分（0-100）
    5. 按评分划分风险等级：≥50 重大、≥30 重要、<30 一般
    6. 将告警按五大风险维度分类统计

    Args:
        company_data: 包含 company_name、report_year、industry、financial_data 的字典

    Returns:
        分析结果字典，包含 risk_score、risk_level、dimension_risks、key_indicators 等
    """
    from tools.financial_calculator import calculate_financial_indicators

    # 第一步：提取公司基本信息（兼容多种字段命名）
    name = company_data.get("company_name") or company_data.get("name") or "未知"
    year = company_data.get("report_year", "")
    industry = company_data.get("industry", "")

    # 第二步：提取财务数据，若为空则直接返回数据不足状态
    financial = _extract_financial(company_data)
    if not financial:
        return {"company_name": name, "report_year": year, "industry": industry,
                "status": "数据不足", "error": "缺少财务数据"}

    # 第三步：调用财务指标计算工具，获取指标结果和告警列表
    try:
        result_str = calculate_financial_indicators.invoke(
            {"financial_data_json": json.dumps(financial, ensure_ascii=False)})
        indicators: Dict[str, Any] = json.loads(result_str)
    except Exception as e:
        indicators = {"error": str(e), "alerts": [], "indicators": {}}

    # 第四步：计算综合风险评分并划分风险等级
    alerts = indicators.get("alerts", [])
    ind_data = indicators.get("indicators", {})
    score = _score_risk(ind_data)
    # 风险等级划分阈值：≥50 → 重大，≥30 → 重要，<30 → 一般
    level = "重大" if score >= 50 else "重要" if score >= 30 else "一般"

    # 第五步：构建完整分析结果
    return {
        "company_name": name, "report_year": year, "industry": industry,
        "status": "已分析", "risk_score": score, "risk_level": level,
        "total_risks": len(alerts), "dimension_risks": _classify_alerts(alerts),
        "key_indicators": {
            "revenue_yoy": ind_data.get("revenue_yoy_change_pct"),
            "gross_margin": ind_data.get("gross_margin_pct"),
            "debt_to_asset": ind_data.get("debt_to_asset_ratio_pct"),
            "current_ratio": ind_data.get("current_ratio"),
            "receivable_to_revenue": ind_data.get("accounts_receivable_to_revenue_ratio"),
        },
        "alerts": alerts,
    }


def _generate_industry_comparison(company_results: List[Dict]) -> Dict[str, Any]:
    """生成行业横向对比报告。

    处理逻辑：
    1. 将所有公司按所属行业分组
    2. 计算每个行业的平均风险得分，筛选出高风险公司名单
    3. 生成全局风险排名（按风险得分降序排列）

    Args:
        company_results: 各公司分析结果列表，每项包含 risk_score、risk_level 等

    Returns:
        行业对比报告字典，含 industries（分组统计）和 ranking（全局排名）
    """
    if not company_results:
        return {}

    # 第一步：按行业分组
    by_industry: Dict[str, list] = {}
    for r in company_results:
        by_industry.setdefault(r.get("industry", "未知行业"), []).append(r)

    comparison = {"total_companies": len(company_results), "industries": {}, "ranking": []}
    # 第二步：计算每个行业的统计指标（平均得分、高风险公司、公司明细）
    for ind, companies in by_industry.items():
        avg = sum(c.get("risk_score", 0) for c in companies) / len(companies)
        # 筛选出该行业中风险等级为"重大"的公司
        high = [c["company_name"] for c in companies if c.get("risk_level") == "重大"]
        comparison["industries"][ind] = {
            "company_count": len(companies), "avg_risk_score": round(avg, 1),
            "high_risk_companies": high,
            "companies": [{"name": c.get("company_name"), "risk_score": c.get("risk_score"),
                           "risk_level": c.get("risk_level"), "total_risks": c.get("total_risks")}
                          for c in companies],
        }

    # 第三步：生成全局风险排名（按风险得分降序）
    ranking = sorted(company_results, key=lambda x: x.get("risk_score", 0), reverse=True)
    comparison["ranking"] = [
        {"rank": i+1, "company_name": r.get("company_name"), "industry": r.get("industry"),
         "risk_score": r.get("risk_score"), "risk_level": r.get("risk_level")}
        for i, r in enumerate(ranking)
    ]
    return comparison


# ── 工具入口 ──────────────────────────────────────────────

@tool
def batch_analyze_companies(companies_data_json: str) -> str:
    """批量分析多家公司年报，独立分析每家公司后生成行业横向对比报告。

    支持的输入格式：
    - 纯数组格式：[{"company_name":"A", "financial_data":{...}}, ...]
    - 嵌套格式：{"companies":[...], "industry":"制造业"}（行业字段会自动填充到各公司）

    每家公司需包含：company_name（或 name）、report_year、industry、financial_data

    返回 JSON 包含：
    - company_list: 各公司独立分析结果
    - industry_comparison: 行业对比报告（分组统计 + 全局排名）
    - summary: 分析概要文本

    Args:
        companies_data_json: 公司数据的 JSON 字符串
    """
    # 解析输入 JSON
    try:
        raw = json.loads(companies_data_json)
    except json.JSONDecodeError:
        return "JSON格式错误，请检查输入"

    # 兼容两种输入格式：嵌套格式（含 companies 字段）和纯数组格式
    if isinstance(raw, dict) and "companies" in raw:
        companies = raw["companies"]
        # 将顶层行业字段填充到未指定行业的公司
        di = raw.get("industry", "")
        for c in companies:
            if "industry" not in c and di:
                c["industry"] = di
    elif isinstance(raw, list):
        companies = raw
    else:
        return "输入必须是非空的公司数组或包含companies字段的对象"

    # 校验输入非空
    if not isinstance(companies, list) or len(companies) == 0:
        return "输入必须是非空的公司数组"

    # 并行线程数：取公司数量和 5 的较小值，避免过多线程竞争
    max_workers = min(len(companies), 5)

    def _safe_analyze(item):
        """安全包装的单公司分析函数，捕获异常避免单个公司失败影响整体"""
        i, company = item
        try:
            return _analyze_single_company(company)
        except Exception as e:
            logger.warning(f"公司 {company.get('company_name', f'公司{i+1}')} 分析失败: {e}")
            return {"company_name": company.get("company_name", f"公司{i+1}"),
                    "status": "分析失败", "error": str(e)}

    # 使用线程池并行分析所有公司
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_safe_analyze, enumerate(companies)))

    # 生成行业横向对比报告
    comparison = _generate_industry_comparison(results)
    n_industries = len(comparison.get("industries", {}))
    # 统计高风险（重大等级）公司数量
    high_risk = sum(1 for r in results if r.get("risk_level") == "重大")

    return json.dumps({
        "company_list": results, "industry_comparison": comparison,
        "summary": f"共分析{len(results)}家公司，覆盖{n_industries}个行业，高风险{high_risk}家"
    }, ensure_ascii=False, indent=2)
