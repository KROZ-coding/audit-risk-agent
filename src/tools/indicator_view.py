# -*- coding: utf-8 -*-
"""指标计算过程视图：财务指标与审计关注分析 + 公式→输入→代入→结果→阈值→状态→原文定位。

数据源只有 ``calculate_financial_indicators`` 的结构化结果（metric_results /
evidence）。网页与 PDF 共用本视图，避免两处各写一套渲染口径而出现数字或来源
不一致；本模块不引入任何模型输出，取值、单位与定位全部来自计算层记录。

缺失输入、零分母、负基期等未计算指标不参与能力分组展示，另列
``uncalculated`` 并附状态与原因，不以中性值补齐，也不静默消失。
"""

from __future__ import annotations

import json

# 五项核心能力 + 审计关注附加组的固定分组顺序。指标键与
# financial_calculator 保持一致；现金流质量不再混入盈利能力。
CAPABILITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("盈利能力", ("gross_margin_pct", "sales_net_margin_pct", "operating_margin_pct",
                  "roe_weighted_pct", "roe_deducted_pct", "roa_pct", "eps_basic", "book_value_per_share")),
    ("营运能力", ("accounts_receivable_turnover_ratio", "accounts_receivable_turnover_days",
                 "inventory_turnover_ratio", "inventory_turnover_days",
                 "fixed_asset_turnover_ratio", "current_asset_turnover_ratio", "total_asset_turnover_ratio",
                 "accounts_receivable_to_revenue_ratio")),
    ("偿债能力", ("current_ratio", "quick_ratio", "cash_ratio", "debt_to_asset_ratio_pct",
                  "equity_ratio", "interest_coverage_ratio", "cash_to_short_term_debt_ratio")),
    ("成长能力", ("revenue_yoy_change_pct", "net_profit_yoy_change_pct",
                 "net_profit_parent_yoy_change_pct", "net_profit_parent_deducted_yoy_change_pct",
                 "total_assets_growth_pct", "net_assets_growth_pct", "construction_in_progress_change_pct")),
    ("现金流质量", ("operating_cashflow_to_net_profit_ratio",
                    "operating_cashflow_to_revenue_ratio",
                    "operating_cashflow_to_total_liabilities_ratio",
                    "operating_cashflow_to_total_assets_ratio", "free_cash_flow")),
    ("资产质量与审计关注", ("goodwill_to_net_assets_ratio_pct",
                        "accounts_receivable_to_revenue_ratio_change_pp",
                        "bad_debt_provision_to_gross_receivables_ratio_pct",
                        "other_receivables_to_total_assets_ratio_pct",
                        "other_payables_to_total_assets_ratio_pct")),
)

# 输入字段中文名：与公式术语一致，供「输入值及单位」可读展示；未收录的键原样输出
_INPUT_CN = {
    "revenue_current": "营业收入(本期)", "revenue_previous": "营业收入(上期)",
    "net_profit_current": "净利润(本期)", "net_profit_previous": "净利润(上期)",
    "net_profit_parent_current": "归母净利润(本期)", "net_profit_parent_previous": "归母净利润(上年同期)",
    "net_profit_parent_deducted_current": "扣非归母净利润(本期)",
    "net_profit_parent_deducted_previous": "扣非归母净利润(上年同期)",
    "cost_of_goods_current": "营业成本(本期)",
    "operating_cashflow_current": "经营活动现金流净额(本期)",
    "accounts_receivable_current": "应收账款(期末)",
    "accounts_receivable_previous": "应收账款(期初)",
    "accounts_receivable_same_period_previous": "应收账款(上年同期末)",
    "inventory_current": "存货(期末)", "inventory_previous": "存货(期初)",
    "total_assets_current": "总资产(期末)",
    "total_liabilities_current": "总负债(期末)",
    "net_assets_current": "净资产(期末)",
    "current_assets_current": "流动资产(期末)",
    "current_liabilities_current": "流动负债(期末)",
    "cash_and_equivalents_current": "现金及现金等价物(期末)",
    "short_term_debt_current": "短期借款(期末)",
    "total_assets_previous": "总资产(期初)",
    "current_assets_previous": "流动资产(期初)",
    "net_assets_previous": "净资产(期初)",
    "operating_profit_current": "营业利润(本期)",
    "profit_before_tax_current": "利润总额(本期)",
    "income_tax_expense_current": "所得税费用(本期)",
    "weighted_average_shares_current": "加权平均普通股股数(本期)",
    "common_shares_current": "普通股股数(期末)",
    "capital_expenditure_current": "资本性支出(本期)",
    "accounts_receivable_gross_current": "应收账款账面余额(期末)",
    "bad_debt_provision_current": "坏账准备(期末)",
    "goodwill_current": "商誉(期末)",
    "fixed_assets_current": "固定资产(期末)",
    "fixed_assets_previous": "固定资产(期初)",
    "construction_in_progress_current": "在建工程(本期)",
    "construction_in_progress_previous": "在建工程(上期)",
    "other_receivables_current": "其他应收款(期末)",
    "other_payables_current": "其他应付款(期末)",
    "period_days": "期间天数",
}


def _input_text(inputs: list) -> str:
    """输入值及单位：字段名(中文)=原始值+单位，供人工回查原始事实表。"""
    parts = []
    for item in inputs or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        label = _INPUT_CN.get(field, field)
        raw = item.get("raw_value", item.get("value", ""))
        unit = str(item.get("unit", "") or "")
        suffix = "（规则默认）" if str(item.get("source", "")) == "rule_default" else ""
        if str(item.get("source", "")) == "period_calendar":
            suffix = "（按已声明期间日历计算）"
        parts.append(f"{label}={raw}{unit}{suffix}")
    return "；".join(parts)


def _period_scope(metric: dict) -> str:
    period = str(metric.get("period", "") or "").strip() or "未标注期间"
    scope = str(metric.get("scope", "") or "").strip() or "未标注口径"
    return f"{period} · {scope}"


def metric_source_ref(metric: dict, evidence_index: dict) -> str:
    """原文定位：证据目录中的页码/定位/原文件；无定位时如实标注而非留空。"""
    for evidence_id in metric.get("evidence_ids") or []:
        item = evidence_index.get(str(evidence_id))
        if not isinstance(item, dict):
            continue
        bits = []
        if str(item.get("source_document", "") or "").strip():
            bits.append(f"原文件 {item['source_document']}")
        if str(item.get("page", "") or "").strip():
            bits.append(f"页码 {item['page']}")
        if str(item.get("locator", "") or "").strip():
            bits.append(f"定位 {item['locator']}")
        if str(item.get("excerpt", "") or "").strip():
            bits.append(f"摘录 {str(item['excerpt'])[:60]}")
        if bits:
            return " / ".join(bits)
    return "未提供原文定位（按本系统计算，需人工回查年报原文）"


def build_indicator_view(fin_json) -> dict:
    """构建指标计算过程视图；入参为空或解析失败时返回 available=False 的空视图。"""
    try:
        fin = json.loads(fin_json) if isinstance(fin_json, str) else fin_json
    except (TypeError, ValueError, json.JSONDecodeError):
        fin = None
    if not isinstance(fin, dict) or "error" in fin:
        return {"available": False, "groups": [], "uncalculated": [],
                "metric_count": 0, "note": "未获取财务指标计算结果，本章无法呈现计算过程"}

    metrics = [item for item in (fin.get("metric_results") or []) if isinstance(item, dict)]
    evidence_index = {
        str(item.get("evidence_id")): item
        for item in (fin.get("evidence") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    by_id = {str(item.get("metric_id", "")): item for item in metrics}

    def _view(metric: dict) -> dict:
        return {
            "metric_id": metric.get("metric_id", ""),
            "name": metric.get("name", ""),
            "display_value": metric.get("display_value", ""),
            "value": metric.get("value"),
            "unit": metric.get("unit", ""),
            "formula": metric.get("formula", ""),
            "inputs_text": _input_text(metric.get("inputs") or []),
            "period_scope": _period_scope(metric),
            "substitution": metric.get("substitution", ""),
            "threshold": metric.get("threshold"),
            "threshold_source": metric.get("threshold_source", ""),
            "status": metric.get("status", ""),
            "reason": metric.get("reason", ""),
            "source_ref": metric_source_ref(metric, evidence_index),
            "evidence_ids": list(metric.get("evidence_ids") or []),
        }

    groups = []
    grouped_ids = set()
    for label, metric_ids in CAPABILITY_GROUPS:
        items = []
        for metric_id in metric_ids:
            metric = by_id.get(metric_id)
            if not metric or metric.get("value") is None:
                continue
            grouped_ids.add(metric_id)
            items.append(_view(metric))
        if items:
            groups.append({"label": label, "metrics": items})

    uncalculated = [{
        "metric_id": item.get("metric_id", ""),
        "name": item.get("name", ""),
        "status": item.get("status", ""),
        "reason": item.get("reason", ""),
    } for item in metrics if item.get("value") is None]

    # 未纳入能力分组但已算出的指标（如后续新增指标）必须可见，避免静默遗漏
    others = [_view(item) for item in metrics
              if item.get("value") is not None
              and str(item.get("metric_id", "")) not in grouped_ids]
    if others:
        groups.append({"label": "其他已计算指标", "metrics": others})

    note = ""
    if not metrics:
        note = "计算结果未包含指标明细（metric_results 为空），计算过程无法呈现"
    elif uncalculated:
        note = ("未计算指标不在能力分组中展示，其状态与原因见下方清单；"
                "缺失输入、零分母与不可比期间一律不以中性值补齐")
    return {
        "available": bool(metrics),
        "period": str(fin.get("period", "") or ""),
        "scope": str(fin.get("scope", "") or ""),
        "amount_unit": str(fin.get("amount_unit", "") or ""),
        "currency": str(fin.get("currency", "") or ""),
        "calculation_version": str(fin.get("calculation_version", "") or ""),
        "groups": groups,
        "uncalculated": uncalculated,
        "metric_count": len(metrics),
        "note": note,
    }
