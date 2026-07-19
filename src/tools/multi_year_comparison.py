"""多年财务数据对比分析工具

支持对多个年度的财务数据进行跨年对比分析，识别趋势性风险。

核心功能：
1. 从多年数据中计算 11 项关键财务指标（毛利率、资产负债率、流动比率等）
2. 计算相邻年度间的同比变动百分比
3. 基于多年趋势判定每项指标的方向（持续上升/持续下降/波动）
4. 针对 6 类高风险趋势自动生成预警（毛利率连降、应收连升、现金流连负等）

输出包含各年度指标明细、同比变动、趋势判定和趋势风险预警。
"""
import json
import logging
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _compare_multi_year_impl(financial_data_json: str) -> str:
    """对多个年度的财务数据进行跨年对比分析的核心实现。

    处理流程：
    1. 解析输入 JSON，兼容两种输入格式（years 数组 / 年度字典）
    2. 按年度排序后，逐一计算每年度 11 项财务指标
    3. 计算相邻年度间的同比变动百分比
    4. 对每项指标进行多年趋势判定（3年+使用持续上升/下降/波动，2年使用同比上升/下降/持平）
    5. 针对 6 类高风险趋势自动生成预警文本

    Args:
        financial_data_json: 多年财务数据 JSON 字符串或字典，支持两种格式：
            格式1（推荐）: {"years": [{"year": "2022", "revenue": 50000, ...}, ...]}
            格式2: {"2022": {"revenue": 50000, ...}, "2023": {...}, ...}

    Returns:
        JSON 字符串，包含：
        - years_analyzed: 分析的年度列表
        - indicators_by_year: 各年度 11 项指标明细
        - yoy_changes: 相邻年度同比变动百分比
        - trends: 各项指标的趋势判定
        - trend_alerts: 趋势性风险预警列表
        - alert_count: 预警总数
    """
    # 第一步：解析输入 JSON，兼容字符串和已解析字典
    try:
        data = json.loads(financial_data_json) if isinstance(financial_data_json, str) else financial_data_json
    except json.JSONDecodeError as e:
        return f"JSON解析失败: {e}"

    # 第二步：统一为年度列表格式，并按年度排序
    # 兼容两种输入格式：{"years": [...]} 和 {"2022": {...}, "2023": {...}}
    years_data = []
    if "years" in data:
        # 格式1：显式 years 数组，按 year 字段排序
        years_data = sorted(data["years"], key=lambda x: x.get("year", ""))
    elif isinstance(data, dict):
        # 格式2：顶层键为年度，值为财务数据字典
        for year, values in sorted(data.items()):
            if isinstance(values, dict):
                year_data = {**values, "year": year}
                years_data.append(year_data)

    # 至少需要 2 年数据才能进行对比分析
    if len(years_data) < 2:
        return "至少需要提供2个年度的财务数据才能进行多年对比分析。"

    # ═══════════════════════════════════════════════════════
    # 第三步：逐年度计算 11 项财务指标
    # ═══════════════════════════════════════════════════════
    indicators_by_year = {}
    for yd in years_data:
        year = yd.get("year", "未知")
        indicators = {}

        # ── 提取 12 项基础财务数据 ──
        revenue = yd.get("revenue", 0)                    # 营业收入
        net_profit = yd.get("net_profit", 0)              # 净利润
        operating_cashflow = yd.get("operating_cashflow", 0) # 经营活动现金流净额
        total_assets = yd.get("total_assets", 0)          # 总资产
        total_liabilities = yd.get("total_liabilities", 0) # 总负债
        accounts_receivable = yd.get("accounts_receivable", 0) # 应收账款
        inventory = yd.get("inventory", 0)                # 存货
        cost_of_goods = yd.get("cost_of_goods", 0)        # 营业成本
        current_assets = yd.get("current_assets", 0)      # 流动资产
        current_liabilities = yd.get("current_liabilities", 0) # 流动负债
        # 净资产：优先使用提供的值，否则通过 总资产-总负债 反算
        net_assets = yd.get("net_assets", total_assets - total_liabilities)
        goodwill = yd.get("goodwill", 0)                  # 商誉

        # ── 直接记录原始值 ──
        indicators["revenue"] = revenue
        indicators["net_profit"] = net_profit
        indicators["operating_cashflow"] = operating_cashflow

        # ── 毛利率 = (营收-营业成本)/营收 × 100 ──
        # ── 应收账款占营收比 = 应收账款/营收 × 100 ──
        if revenue > 0:
            indicators["gross_margin"] = round((revenue - cost_of_goods) / revenue * 100, 2)
            indicators["ar_to_revenue_ratio"] = round(accounts_receivable / revenue * 100, 2)
        else:
            indicators["gross_margin"] = 0
            indicators["ar_to_revenue_ratio"] = 0

        # ── 存货周转率 = 营业成本/存货 ──
        if cost_of_goods > 0 and inventory > 0:
            indicators["inventory_turnover"] = round(cost_of_goods / inventory, 2)
        else:
            indicators["inventory_turnover"] = 0

        # ── 资产负债率 = 总负债/总资产 × 100 ──
        if total_assets > 0:
            indicators["debt_to_asset_ratio"] = round(total_liabilities / total_assets * 100, 2)
        else:
            indicators["debt_to_asset_ratio"] = 0

        # ── 流动比率 = 流动资产/流动负债 ──
        # ── 速动比率 = (流动资产-存货)/流动负债 ──
        if current_liabilities > 0:
            indicators["current_ratio"] = round(current_assets / current_liabilities, 2)
            quick_assets = current_assets - inventory
            indicators["quick_ratio"] = round(quick_assets / current_liabilities, 2)
        else:
            indicators["current_ratio"] = 0
            indicators["quick_ratio"] = 0

        # ── 经营现金流/净利润比（利润含金量） ──
        if net_profit != 0:
            indicators["cashflow_to_profit_ratio"] = round(operating_cashflow / net_profit, 2)
        else:
            indicators["cashflow_to_profit_ratio"] = 0

        # ── 商誉占净资产比 = 商誉/净资产 × 100 ──
        if net_assets > 0:
            indicators["goodwill_to_net_assets"] = round(goodwill / net_assets * 100, 2)
        else:
            indicators["goodwill_to_net_assets"] = 0

        indicators_by_year[year] = indicators

    # ═══════════════════════════════════════════════════════
    # 第四步：计算同比变动和多年趋势
    # ═══════════════════════════════════════════════════════
    years = list(indicators_by_year.keys())
    yoy_changes = {}   # 相邻年度同比变动
    trends = {}         # 各项指标趋势判定
    trend_alerts = []   # 趋势性风险预警列表

    # 指标名称映射：英文键 → 中文名称（用于输出和预警文本）
    metric_names = {
        "revenue": "营业收入",
        "net_profit": "净利润",
        "operating_cashflow": "经营活动现金流量净额",
        "gross_margin": "毛利率(%)",
        "ar_to_revenue_ratio": "应收账款占营收比(%)",
        "inventory_turnover": "存货周转率",
        "debt_to_asset_ratio": "资产负债率(%)",
        "current_ratio": "流动比率",
        "quick_ratio": "速动比率",
        "cashflow_to_profit_ratio": "经营现金流/净利润比",
        "goodwill_to_net_assets": "商誉占净资产比(%)",
    }

    # ── 计算相邻年度间的同比变动百分比 ──
    for i in range(1, len(years)):
        prev_year = years[i - 1]
        curr_year = years[i]
        period_key = f"{prev_year}-{curr_year}"
        yoy_changes[period_key] = {}
        for metric in metric_names:
            prev_val = indicators_by_year[prev_year].get(metric, 0)
            curr_val = indicators_by_year[curr_year].get(metric, 0)
            # 同比变动 = (本期 - 上期) / |上期| × 100
            if prev_val != 0:
                change_pct = round((curr_val - prev_val) / abs(prev_val) * 100, 2)
            else:
                # 上期为零时：本期也为零则变动 0%，否则标记为极大值
                change_pct = 0 if curr_val == 0 else 999.99
            yoy_changes[period_key][f"{metric}_change_pct"] = change_pct

    # ── 对每项指标进行多年趋势判定 ──
    for metric, name in metric_names.items():
        # 收集该指标在各年度的值序列
        values = [indicators_by_year[y].get(metric, 0) for y in years]
        trend_info = {"values": dict(zip(years, values))}

        if len(values) >= 3:
            # 3 年及以上：统计上升和下降次数，判定总体趋势方向
            increases = sum(1 for i in range(1, len(values)) if values[i] > values[i - 1])
            decreases = sum(1 for i in range(1, len(values)) if values[i] < values[i - 1])

            # 几乎所有相邻对都下降 → 持续下降
            if decreases >= len(values) - 1:
                trend_info["trend"] = "持续下降"
            # 几乎所有相邻对都上升 → 持续上升
            elif increases >= len(values) - 1:
                trend_info["trend"] = "持续上升"
            else:
                trend_info["trend"] = "波动"
        elif len(values) == 2:
            # 仅 2 年数据：简单对比方向
            if values[1] > values[0]:
                trend_info["trend"] = "同比上升"
            elif values[1] < values[0]:
                trend_info["trend"] = "同比下降"
            else:
                trend_info["trend"] = "持平"

        trends[metric] = trend_info

        # ═══════════════════════════════════════════════════
        # 趋势性风险预警（6 类高风险模式）
        # ═══════════════════════════════════════════════════

        # 预警1：毛利率连续下滑 → 盈利能力持续恶化
        if metric == "gross_margin" and len(values) >= 2:
            if all(values[i] < values[i - 1] for i in range(1, len(values))):
                trend_alerts.append(f"【趋势风险】毛利率连续{len(values) - 1}年下滑，需关注盈利能力持续恶化")

        # 预警2：应收账款占营收比连续上升 → 回款风险加剧或收入虚增嫌疑
        if metric == "ar_to_revenue_ratio" and len(values) >= 2:
            if all(values[i] > values[i - 1] for i in range(1, len(values))):
                trend_alerts.append(f"【趋势风险】应收账款占营收比连续{len(values) - 1}年上升，回款风险加剧")

        # 预警3：经营现金流连续为负 → 盈利质量严重存疑
        if metric == "operating_cashflow" and len(values) >= 2:
            if all(v < 0 for v in values[-2:]):
                trend_alerts.append(f"【趋势风险】经营活动现金流量净额连续{min(2, len(values))}年为负，盈利质量严重存疑")

        # 预警4：流动比率持续低于 1 且持续恶化 → 短期偿债能力严重不足
        if metric == "current_ratio" and len(values) >= 2:
            if all(v < 1 for v in values) and all(values[i] < values[i - 1] for i in range(1, len(values))):
                trend_alerts.append(f"【趋势风险】流动比率持续低于1且持续恶化，短期偿债能力严重不足")

        # 预警5：资产负债率连续上升且最终超过 70% → 财务杠杆持续加大
        if metric == "debt_to_asset_ratio" and len(values) >= 2:
            if all(values[i] > values[i - 1] for i in range(1, len(values))) and values[-1] > 70:
                trend_alerts.append(f"【趋势风险】资产负债率连续上升且超过70%，财务杠杆持续加大")

        # 预警6：存货周转率连续下降 → 存货积压/跌价风险加大
        if metric == "inventory_turnover" and len(values) >= 2:
            if all(values[i] < values[i - 1] for i in range(1, len(values))):
                trend_alerts.append(f"【趋势风险】存货周转率连续{len(values) - 1}年下降，存货积压风险加大")

    # ═══════════════════════════════════════════════════════
    # 第五步：构建输出 JSON
    # ═══════════════════════════════════════════════════════
    result = {
        "years_analyzed": years,                          # 分析的年度列表
        "indicators_by_year": indicators_by_year,         # 各年度 11 项指标明细
        "yoy_changes": yoy_changes,                       # 相邻年度同比变动
        "trends": {metric_names.get(k, k): v for k, v in trends.items()},  # 趋势（中文键名）
        "trend_alerts": trend_alerts,                     # 趋势性风险预警
        "alert_count": len(trend_alerts),                 # 预警总数
    }

    return json.dumps(result, ensure_ascii=False, indent=2)


@tool
def compare_multi_year(multi_year_data_json: str) -> str:
    """多年财务数据跨年对比分析，识别趋势性风险。

    对多个年度的财务数据计算 11 项关键指标，分析同比变动和多年趋势方向，
    并针对 6 类高风险趋势模式（毛利率连降、应收连升、现金流连负、
    流动比率持续恶化、负债率连升超 70%、存货周转连降）自动生成预警。

    Args:
        multi_year_data_json: 多年财务数据 JSON 字符串，支持两种格式：
            格式1: {"years": [{"year": "2022", "revenue": 50000, ...}, ...]}
            格式2: {"2022": {"revenue": 50000, ...}, "2023": {...}, ...}

    Returns:
        JSON 字符串，包含各年度指标、同比变动、趋势判定和趋势预警
    """
    return _compare_multi_year_impl(multi_year_data_json)
