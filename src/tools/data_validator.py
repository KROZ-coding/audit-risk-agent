"""财务数据一致性校验。

校验分为完整勾稽与有限数据合理性检查。缺失字段保持为缺失；不得用反算
的净资产宣布资产负债表平衡，也不得把缺失调整项当成零。
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from langchain_core.tools import tool

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from tools.financial_calculator import _CURRENT_ALIASES, financial_period

logger = logging.getLogger(__name__)
VALIDATION_VERSION = "2026-09-v4"


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        text = str(value).strip().replace(",", "")
        parsed = Decimal(text) if text else None
        return parsed if parsed is not None and parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _number(value, places: int | None = None):
    if value is None:
        return None
    if places is not None:
        quant = Decimal("1") / (Decimal("10") ** places)
        value = value.quantize(quant, rounding=ROUND_HALF_UP)
    return float(value)


def _metadata(data: dict) -> dict:
    value = data.get("_metadata") or data.get("metadata") or {}
    return value if isinstance(value, dict) else {}


def _normalize_validation_input(data: dict) -> dict:
    aliases = {
        field: tuple(dict.fromkeys((f"{field}_current", *_CURRENT_ALIASES.get(f"{field}_current", ()))))
        for field in ("total_assets", "total_liabilities", "net_assets", "net_profit", "operating_cashflow",
                      "income_tax_expense", "profit_before_tax")
    }
    aliases.update({
        "net_profit_parent": ("net_profit_parent_current",),
        "depreciation": ("depreciation_current",),
        "amortization": ("amortization_current",),
        "working_capital_change": ("working_capital_change_current",),
        "dividends": ("dividends_current",),
        "retained_earnings_begin": ("retained_earnings_previous",),
        "retained_earnings_end": ("retained_earnings_current",),
    })
    normalized = dict(data)
    sources = {}
    warnings = []
    for target, alternatives in aliases.items():
        present = list(dict.fromkeys(key for key in (target, *alternatives) if _decimal(data.get(key)) is not None))
        if not present:
            continue
        if len({_decimal(data[key]) for key in present}) > 1:
            normalized[target] = None
            warnings.append(f"{target} 别名值冲突：{'、'.join(present)}，须核实后校验")
            continue
        source = present[0]
        normalized[target] = data[source]
        sources[target] = source
    normalized["_validation_sources"] = sources
    normalized["_validation_warnings"] = warnings
    return normalized


def _fact(value, field: str, data: dict, fact_id: str) -> dict:
    meta = _metadata(data)
    source_field = data.get("_validation_sources", {}).get(field, field)
    field_meta = data.get("_field_metadata") or meta.get("fields") or {}
    field_meta = field_meta.get(source_field, {}) if isinstance(field_meta, dict) else {}
    field_meta = field_meta if isinstance(field_meta, dict) else {}
    period = financial_period(data)
    if field.endswith(("_begin", "_previous")):
        period = str(data.get("previous_period") or meta.get("previous_period") or "期初（具体日期未提供）")
    return make_fact(
        source_field, data.get(source_field, value), fact_id=f"F-{source_field}",
        unit=str(field_meta.get("unit") or data.get("amount_unit") or meta.get("amount_unit") or ""),
        currency=str(data.get("currency") or meta.get("currency") or "人民币"),
        period=str(field_meta.get("period") or period),
        scope=str(field_meta.get("scope") or data.get("scope") or meta.get("scope") or ""),
        source_document=str(field_meta.get("source_document") or meta.get("source_document") or data.get("source_document") or ""),
        source_hash=str(field_meta.get("source_hash") or meta.get("source_hash") or data.get("source_hash") or ""),
        page=str(field_meta.get("page") or meta.get("page") or meta.get("page_number") or ""),
        locator=str(field_meta.get("locator") or meta.get("locator") or meta.get("table_locator") or ""),
        excerpt=str(field_meta.get("excerpt") or meta.get("excerpt") or ""),
        extraction_method=str(meta.get("extraction_method") or "structured_input"),
    ).to_dict()


def _base_result(check: str, formula: str, status: str, passed, message: str, *, data: dict,
                 inputs: dict, difference=None, threshold=None, reason: str = "") -> dict:
    fact_list = [_fact(value, key, data, f"F-{key}") for key, value in inputs.items() if value is not None]
    metric_slug = {
        "资产负债表平衡": "balance_sheet",
        "现金流勾稽": "cashflow_reconciliation",
        "现金流合理性（有限检查）": "cashflow_limited",
        "未分配利润一致性": "retained_earnings",
        "有效税率合理性": "effective_tax_rate",
    }.get(check, "check")
    metric_id = f"validation_{metric_slug}"
    metric = MetricResult(
        metric_id=metric_id,
        name=check,
        formula=formula,
        inputs=[{"field": f["field"], "fact_id": f["fact_id"], "raw_value": f["raw_value"], "value": f["value"], "unit": f["unit"], "period": f["period"], "scope": f["scope"]} for f in fact_list],
        period=financial_period(data),
        scope=str(data.get("scope") or _metadata(data).get("scope") or ""),
        unit=str(data.get("amount_unit") or _metadata(data).get("amount_unit") or ""),
        value=_number(difference),
        display_value="未获取" if difference is None else str(_number(difference, 2)),
        threshold=threshold,
        threshold_source="内部数据质量校验规则",
        status=status,
        reason=reason,
        evidence_ids=[f"E-{metric_id}"],
    ).to_dict()
    meta = _metadata(data)
    def source_values(key):
        return "；".join(dict.fromkeys(str(item[key]) for item in fact_list if item.get(key)))
    evidence = Evidence(
        evidence_id=f"E-{metric_id}",
        source_type="local_validation",
        source_document=source_values("source_document"),
        source_hash=source_values("source_hash"),
        page=source_values("page"),
        locator=source_values("locator"),
        excerpt=source_values("excerpt"),
        fact_ids=[f["fact_id"] for f in fact_list],
        metric_ids=[metric_id],
        verified=status == "calculated" and passed is not None,
        status="verified" if status == "calculated" and passed is not None else status,
    ).to_dict()
    return {
        "check": check,
        "formula": formula,
        "passed": passed,
        "status": status,
        "difference": _number(difference, 2),
        "threshold": threshold,
        "message": message,
        "reason": reason,
        "metric_id": metric_id,
        "evidence_id": f"E-{metric_id}",
        "facts": fact_list,
        "metric_result": metric,
        "evidence": evidence,
    }


def _validate_balance_sheet(data: dict) -> dict:
    assets = _decimal(data.get("total_assets"))
    liabilities = _decimal(data.get("total_liabilities"))
    equity_raw = data.get("net_assets") if data.get("net_assets") is not None else data.get("owners_equity")
    equity = _decimal(equity_raw)
    inputs = {"total_assets": assets, "total_liabilities": liabilities, "net_assets": equity}
    if assets is None or liabilities is None or equity is None:
        return _base_result(
            "资产负债表平衡", "总资产 = 总负债 + 净资产", "insufficient_data", None,
            "缺少总资产、总负债或净资产，不能宣布资产负债表平衡，也不反算净资产",
            data=data, inputs=inputs, reason="完整勾稽需要三项原始事实",
        )
    total = liabilities + equity
    diff = abs(assets - total)
    ratio = diff / max(abs(assets), abs(total), Decimal("1"))
    # F2 容差分层：会计恒等式容差收紧为千分之一（0.1%，与 knowledge_base/
    # 报表勾稽规则库 的舍入容差一致），超过即勾稽不平衡；0.1%~2% 区间为
    # 「需人工复核」提示层级（可能是口径/舍入尾差），2% 以上为显著不平衡。
    # 旧的 2% 一刀切"通过"会把亿元级报表 200 万差额判为平衡，掩盖必须追查的线索。
    _IDENTITY_TOLERANCE = Decimal("0.001")   # 会计恒等式容差（千分之一）
    _REVIEW_BAND = Decimal("0.02")           # 需复核提示上限（非通过线）
    passed = ratio <= _IDENTITY_TOLERANCE
    exact_match = diff == 0
    if exact_match:
        message = "所提供资产负债表三项金额相等"
    elif passed:
        message = (f"资产负债表差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%），"
                   "在千分之一舍入容差内，视为勾稽平衡（允许舍入尾差）")
    elif ratio <= _REVIEW_BAND:
        message = (f"资产负债表差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%），"
                   "超出千分之一会计恒等式容差，勾稽不平衡；差额量级在 2% 复核提示线内，"
                   "优先排查舍入与口径差异")
    else:
        message = (f"资产负债表差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%），"
                   "超过 2% 复核提示线，勾稽显著不平衡，必须核查数据来源")
    result = _base_result(
        "资产负债表平衡", "总资产 = 总负债 + 净资产",
        "calculated" if exact_match else "limited_check", passed, message,
        data=data, inputs=inputs, difference=diff, threshold="0.1%（千分之一舍入容差）；2% 为需复核提示线",
        reason="会计恒等式容差依据 knowledge_base/报表勾稽规则库；2% 仅是需人工复核的提示层级，不是通过线",
    )
    result.update({
        "total_assets": _number(assets), "total_liabilities": _number(liabilities),
        "net_assets": _number(equity), "liabilities_plus_equity": _number(total),
        "difference_pct": f"{_number(ratio * 100, 2):.2f}%",
        "exact_match": exact_match,
        "needs_review": (not exact_match) and (not passed) and (ratio <= _REVIEW_BAND),
    })
    return result


def _validate_cashflow_reconciliation(data: dict) -> dict:
    net_profit = _decimal(data.get("net_profit"))
    operating_cashflow = _decimal(data.get("operating_cashflow"))
    depreciation = _decimal(data.get("depreciation"))
    amortization = _decimal(data.get("amortization"))
    working_capital = _decimal(data.get("working_capital_change"))
    inputs = {
        "net_profit": net_profit, "operating_cashflow": operating_cashflow,
        "depreciation": depreciation, "amortization": amortization,
        "working_capital_change": working_capital,
    }
    if net_profit is None or operating_cashflow is None:
        return _base_result(
            "现金流勾稽", "经营现金流 ≈ 净利润 + 折旧 + 摊销 - 营运资本变动", "insufficient_data", None,
            "缺少净利润或经营现金流，无法校验", data=data, inputs=inputs,
            reason="核心输入缺失",
        )
    # Three selected adjustments do not cover the complete indirect-method statement.
    if depreciation is not None and amortization is not None and working_capital is not None:
        estimated = net_profit + depreciation + amortization - working_capital
        diff = abs(operating_cashflow - estimated)
        ratio = diff / max(abs(operating_cashflow), abs(estimated), Decimal("1"))
        passed = ratio <= Decimal("0.15")
        result = _base_result(
            "现金流勾稽", "经营现金流 ≈ 净利润 + 折旧 + 摊销 - 营运资本变动", "limited_check", passed,
            f"所列调整项估算现金流差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%），"
            + ("在内部15%筛查阈值内" if passed else "超过内部15%筛查阈值")
            + "；尚未覆盖减值、投资损益、递延税项等其他调整，不能视为完整间接法勾稽",
            data=data, inputs=inputs, difference=diff, threshold="15%",
            reason="仅检查已提供的折旧、摊销和营运资本变动，未假定其他缺失调整项为零；估算差额不直接等同于已确认风险",
        )
        result.update({"estimated_cashflow": _number(estimated), "actual_cashflow": _number(operating_cashflow), "difference_pct": f"{_number(ratio * 100, 2):.2f}%"})
        return result

    # 缺少调整项时不构造估算现金流，只检查利润与现金流是否同向。
    same_direction = None if net_profit == 0 or operating_cashflow == 0 else (net_profit > 0) == (operating_cashflow > 0)
    return _base_result(
        "现金流合理性（有限检查）", "净利润与经营现金流方向一致性", "limited_check", same_direction,
        "净利润或经营现金流为零，方向比较不具判别力，需补充完整勾稽资料" if same_direction is None else
        "净利润与经营现金流方向一致，但缺少折旧/摊销/营运资本调整项，不能替代完整勾稽" if same_direction
        else "净利润与经营现金流方向相反，需补充完整勾稽资料",
        data=data, inputs=inputs, threshold="方向一致性",
        reason="缺少完整间接法调整项，结果仅作有限合理性检查",
    )


def _validate_retained_earnings(data: dict) -> dict:
    parent = _decimal(data.get("net_profit_parent"))
    consolidated = _decimal(data.get("net_profit"))
    begin = _decimal(data.get("retained_earnings_begin"))
    end = _decimal(data.get("retained_earnings_end"))
    dividends = _decimal(data.get("dividends"))
    other_changes = _decimal(data.get("retained_earnings_other_changes"))
    inputs = {"net_profit_parent": parent, "net_profit": consolidated, "retained_earnings_begin": begin, "retained_earnings_end": end, "dividends": dividends}
    if other_changes is not None:
        inputs["retained_earnings_other_changes"] = other_changes
    if parent is None or begin is None or end is None or dividends is None:
        result = _base_result(
            "未分配利润一致性", "期末未分配利润-期初未分配利润 ≈ 归母净利润-分红", "insufficient_data", None,
            "缺少归母净利润、期初/期末未分配利润或分红，无法校验；不能用合并净利润替代归母口径",
            data=data, inputs=inputs, reason="合并未分配利润勾稽需要归属于母公司股东的净利润口径，不能替换为母公司单体净利润",
        )
        result.update({
            "net_profit_parent": _number(parent), "net_profit": _number(consolidated),
            "retained_earnings_begin": _number(begin), "retained_earnings_end": _number(end),
            "dividends": _number(dividends),
            "net_profit_note": "缺少归母净利润，不以合并口径替代",
        })
        return result
    net_profit = parent
    actual = end - begin
    expected = net_profit - dividends
    formula = "期末未分配利润-期初未分配利润 ≈ 归母净利润-分红"
    if other_changes is not None:
        expected += other_changes
        formula += "+其他已披露变动"
    diff = abs(actual - expected)
    ratio = diff / max(abs(expected), abs(actual), Decimal("1"))
    passed = ratio <= Decimal("0.05")
    exact_match = diff == 0
    supplied_changes_match = exact_match and other_changes is not None
    message = (
        "所提供归母净利润、分红及其他已披露变动合计与未分配利润变动相符"
        if supplied_changes_match else
        f"所列项目与未分配利润变动差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%），"
        + ("在内部5%筛查阈值内" if passed else "超过内部5%筛查阈值")
        + "；仍需核实其他权益变动、提取及结转项目，不等同于完整勾稽相符"
    )
    result = _base_result(
        "未分配利润一致性", formula, "calculated" if supplied_changes_match else "limited_check", passed,
        message,
        data=data, inputs=inputs, difference=diff, threshold="5%",
        reason="使用合并报表中归属于母公司股东的净利润，不是母公司单体净利润；内部阈值不证明其他权益变动不存在",
    )
    result.update({"actual_change": _number(actual), "expected_change": _number(expected),
                   "difference_pct": f"{_number(ratio * 100, 2):.2f}%", "net_profit_note": "归母口径（合并报表，不是母公司单体）",
                   "exact_match": exact_match, "other_changes": _number(other_changes)})
    return result


def _validate_effective_tax_rate(data: dict) -> dict:
    """知识库跨表勾稽第 9 条：所得税费用 ↔ 利润总额（有效税率合理性）。

    所得税费用 / 利润总额 应接近适用税率（考虑税收优惠与递延所得税后）；
    有效税率异常偏低且无优惠政策支撑，指向利润虚增（虚增的利润通常不交税）。
    筛查线：有效税率 < 5% 判为异常偏低（低于常见法定优惠税率 10%~15% 的下限）；
    利润总额 ≤ 0 或所得税费用为负（退税/递延调整）时比率无判别力，仅作有限检查。
    """
    tax = _decimal(data.get("income_tax_expense"))
    profit = _decimal(data.get("profit_before_tax"))
    inputs = {"income_tax_expense": tax, "profit_before_tax": profit}
    if tax is None or profit is None:
        return _base_result(
            "有效税率合理性", "所得税费用 / 利润总额", "insufficient_data", None,
            "缺少所得税费用或利润总额，无法执行有效税率勾稽",
            data=data, inputs=inputs, reason="完整勾稽需要两项原始事实",
        )
    if profit <= 0 or tax < 0:
        return _base_result(
            "有效税率合理性", "所得税费用 / 利润总额", "limited_check", None,
            f"利润总额{_number(profit, 2)}、所得税费用{_number(tax, 2)}：亏损或退税情景下"
            "有效税率无判别力，不作异常判定；如利润为负而所得税费用为正，需人工核实递延所得税处理",
            data=data, inputs=inputs, reason="比率在分母非正或分子为负时不具方向性",
        )
    rate = tax / profit
    passed = rate >= Decimal("0.05")
    if passed:
        message = (f"有效税率{_number(rate * 100, 2)}%，处于合理适用税率区间"
                   "（考虑税收优惠与递延所得税后），未触发异常偏低筛查线")
    else:
        message = (f"有效税率仅{_number(rate * 100, 2)}%，低于 5% 异常偏低筛查线"
                   "（常见法定优惠税率为 10%~15%）：若无税收优惠支撑，指向利润虚增"
                   "（虚增的利润通常不交税），须核实税收优惠依据与递延所得税构成")
    result = _base_result(
        "有效税率合理性", "所得税费用 / 利润总额", "calculated", passed, message,
        data=data, inputs=inputs, difference=abs(tax - profit * Decimal("0.25")),
        threshold="有效税率 ≥ 5%（异常偏低筛查线，参考法定税率 25% 与优惠税率 10%~15%）",
        reason="知识库跨表勾稽第 9 条：有效税率异常偏低且无优惠政策支撑，指向利润虚增；筛查线不构成税务结论",
    )
    result.update({
        "income_tax_expense": _number(tax), "profit_before_tax": _number(profit),
        "effective_tax_rate": f"{_number(rate * 100, 2):.2f}%",
        "statutory_reference": "法定税率 25%；高新技术等优惠税率 10%~15%",
    })
    return result


# F1 覆盖声明：对照 knowledge_base/报表勾稽规则库「三、跨表勾稽」10 条规则，
# 逐条登记本工具（及其他工具）的实现状态。未实现 ≠ 无风险，缺字段条目须
# 先扩展提取 schema 才能勾稽，此处如实声明避免"勾稽通过"被误读为全量交叉验证。
_KB_CROSS_TABLE_COVERAGE = [
    {"rule_id": 1, "rule": "净利润 → 现金流量表起点", "status": "implemented",
     "detail": "由「现金流勾稽」校验承担（净利润+间接法调整=经营活动现金流量净额）"},
    {"rule_id": 2, "rule": "未分配利润变动 ↔ 净利润与分红", "status": "implemented",
     "detail": "由「未分配利润一致性」校验承担（归母口径）"},
    {"rule_id": 3, "rule": "营业收入 ↔ 应收账款", "status": "partial",
     "detail": "由财务指标工具的应收/营收偏离告警部分覆盖（阈值筛查，非严格勾稽）"},
    {"rule_id": 4, "rule": "营业收入 ↔ 销售商品提供劳务收到的现金", "status": "not_implemented",
     "detail": "提取 schema 缺「销售商品提供劳务收到的现金」字段，待扩展后实现收现比勾稽"},
    {"rule_id": 5, "rule": "营业成本 ↔ 存货与应付账款", "status": "not_implemented",
     "detail": "缺「购买商品接受劳务支付的现金」及存货/应付账款变动字段"},
    {"rule_id": 6, "rule": "固定资产 ↔ 折旧", "status": "not_implemented",
     "detail": "缺固定资产原值/在建工程字段，现有折旧率筛查在 M-Score DEPI 内且口径受限"},
    {"rule_id": 7, "rule": "货币资金 ↔ 利息收入", "status": "partial",
     "detail": "由财务指标工具的存贷双高告警部分覆盖（含利息收入/货币资金比率）"},
    {"rule_id": 8, "rule": "有息负债 ↔ 财务费用", "status": "not_implemented",
     "detail": "缺有息负债合计口径字段（现有短期借款不构成全口径）"},
    {"rule_id": 9, "rule": "所得税费用 ↔ 利润总额", "status": "implemented",
     "detail": "由「有效税率合理性」校验承担（本次新增）"},
    {"rule_id": 10, "rule": "现金流量表三项净额 ↔ 货币资金变动", "status": "not_implemented",
     "detail": "缺投资/筹资活动现金流量净额与现金及现金等价物净增加额字段"},
]


@tool
def validate_financial_data(financial_data_json: str) -> str:
    """执行资产负债表平衡、现金流勾稽、未分配利润一致性、有效税率合理性四类数据质量校验，
    并声明对知识库跨表勾稽规则的覆盖范围（未覆盖条目不表示无风险）。"""
    try:
        data = json.loads(financial_data_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"JSON解析失败: {exc}", "validation_version": VALIDATION_VERSION}, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps({"error": "输入 JSON 顶层须为对象", "validation_version": VALIDATION_VERSION}, ensure_ascii=False)

    data = _normalize_validation_input(data)
    results = [_validate_balance_sheet(data), _validate_cashflow_reconciliation(data),
               _validate_retained_earnings(data), _validate_effective_tax_rate(data)]
    failed = [item for item in results if item.get("passed") is False]
    skipped = [item for item in results if item.get("passed") is None]
    limited = [item for item in results if item.get("status") == "limited_check"]
    risks = []
    for index, item in enumerate(failed, 1):
        risks.append({
            "risk_id": f"V{index:03d}", "dimension": "数据可靠性风险", "title": item["message"],
            "level": "待定级", "confidence": None, "status": "candidate",
            "verification_status": "待复核", "source": "系统勾稽校验",
            "evidence_ids": [item["evidence_id"]], "metric_ids": [item["metric_id"]],
            "evidence": f"校验项：{item['check']}；差异：{item.get('difference', '未获取')}；阈值：{item.get('threshold', '未设置')}",
            "audit_suggestion": "核实财务数据来源、单位、期间和母子公司口径，补充原始报表定位后再定级",
        })
    all_evidence = [item["evidence"] for item in results if item.get("evidence")]
    pending_checks = [item for item in results if item.get("passed") is None or item.get("status") == "limited_check"]
    validation_result = (
        "未通过" if failed else "部分完成" if pending_checks else "通过"
    )
    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "validation_version": VALIDATION_VERSION,
        "amount_unit": data.get("amount_unit") or _metadata(data).get("amount_unit") or "",
        "period": financial_period(data),
        "scope": data.get("scope") or _metadata(data).get("scope") or "",
        "results": results,
        "data_validation": {
            "all_checks": results, "results": results, "total_checks": len(results),
            "passed_checks": sum(1 for item in results if item.get("passed") is True and item.get("status") == "calculated"),
            "failed_checks": len(failed), "skipped_checks": len(skipped),
            "limited_checks": len(limited),
            "validation_result": validation_result,
            "status": "failed" if failed else "partially_tested" if pending_checks else "verified",
            "risks": risks,
            "pending_checks": pending_checks,
            # F1 覆盖声明：本工具实现了知识库跨表勾稽 10 条中的 3 条（第 1、2、9 条），
            # 另有 2 条由财务指标工具阈值告警部分覆盖；未实现条目逐项列明原因，
            # 「勾稽校验通过」仅指已实现条目通过，不等于三大报表完成全量交叉验证。
            "kb_cross_table_coverage": {
                "implemented": sum(1 for c in _KB_CROSS_TABLE_COVERAGE if c["status"] == "implemented"),
                "partial": sum(1 for c in _KB_CROSS_TABLE_COVERAGE if c["status"] == "partial"),
                "not_implemented": sum(1 for c in _KB_CROSS_TABLE_COVERAGE if c["status"] == "not_implemented"),
                "rules": _KB_CROSS_TABLE_COVERAGE,
                "note": "勾稽校验通过仅指已实现条目通过；未实现条目不表示无风险，缺字段条目待提取 schema 扩展后补齐",
            },
        },
        "facts": [f for item in results for f in item.get("facts", [])],
        "metric_results": [item["metric_result"] for item in results],
        "evidence": all_evidence,
        "input_warnings": data.get("_validation_warnings", []),
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
