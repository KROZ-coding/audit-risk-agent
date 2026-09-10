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

logger = logging.getLogger(__name__)
VALIDATION_VERSION = "2026-09-v3"


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        text = str(value).strip().replace(",", "")
        return Decimal(text) if text else None
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


def _fact(value, field: str, data: dict, fact_id: str) -> dict:
    meta = _metadata(data)
    return make_fact(
        field, value, fact_id=fact_id,
        unit=str(data.get("amount_unit") or meta.get("amount_unit") or ""),
        currency=str(data.get("currency") or meta.get("currency") or "人民币"),
        period=str(data.get("period") or meta.get("period") or ""),
        scope=str(data.get("scope") or meta.get("scope") or ""),
        source_document=str(meta.get("source_document") or data.get("source_document") or ""),
        source_hash=str(meta.get("source_hash") or data.get("source_hash") or ""),
        page=str(meta.get("page") or meta.get("page_number") or ""),
        locator=str(meta.get("locator") or meta.get("table_locator") or ""),
        excerpt=str(meta.get("excerpt") or ""),
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
    }.get(check, "check")
    metric_id = f"validation_{metric_slug}"
    metric = MetricResult(
        metric_id=metric_id,
        name=check,
        formula=formula,
        inputs=[{"field": f["field"], "fact_id": f["fact_id"], "raw_value": f["raw_value"], "value": f["value"], "unit": f["unit"], "period": f["period"], "scope": f["scope"]} for f in fact_list],
        period=str(data.get("period") or _metadata(data).get("period") or ""),
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
    evidence = Evidence(
        evidence_id=f"E-{metric_id}",
        source_type="local_validation",
        source_document=str(meta.get("source_document") or data.get("source_document") or ""),
        source_hash=str(meta.get("source_hash") or data.get("source_hash") or ""),
        page=str(meta.get("page") or ""),
        locator=str(meta.get("locator") or ""),
        excerpt=str(meta.get("excerpt") or ""),
        fact_ids=[f["fact_id"] for f in fact_list],
        metric_ids=[metric_id],
        verified=passed is not None,
        status="verified" if passed is not None else "insufficient_data",
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
    passed = ratio <= Decimal("0.02")
    result = _base_result(
        "资产负债表平衡", "总资产 = 总负债 + 净资产", "calculated", passed,
        "资产负债表平衡" if passed else f"资产负债表不平，差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%）",
        data=data, inputs=inputs, difference=diff, threshold="2%",
        reason="差额占资产或负债权益合计较大者不超过2%" if passed else "差额超过2%校验阈值",
    )
    result.update({
        "total_assets": _number(assets), "total_liabilities": _number(liabilities),
        "net_assets": _number(equity), "liabilities_plus_equity": _number(total),
        "difference_pct": f"{_number(ratio * 100, 2):.2f}%",
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
    # 调整项全部提供时做完整勾稽；缺少调整项时单独标识为有限合理性检查。
    if depreciation is not None and amortization is not None and working_capital is not None:
        estimated = net_profit + depreciation + amortization - working_capital
        diff = abs(operating_cashflow - estimated)
        ratio = diff / max(abs(operating_cashflow), abs(estimated), Decimal("1"))
        passed = ratio <= Decimal("0.15")
        result = _base_result(
            "现金流勾稽", "经营现金流 ≈ 净利润 + 折旧 + 摊销 - 营运资本变动", "calculated", passed,
            "现金流勾稽关系成立" if passed else f"现金流勾稽不成立，差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%）",
            data=data, inputs=inputs, difference=diff, threshold="15%",
            reason="完整间接法勾稽" if passed else "完整勾稽差异超过15%",
        )
        result.update({"estimated_cashflow": _number(estimated), "actual_cashflow": _number(operating_cashflow), "difference_pct": f"{_number(ratio * 100, 2):.2f}%"})
        return result

    # 缺少调整项时不构造估算现金流，只检查利润与现金流是否同向。
    same_direction = (net_profit == 0 or operating_cashflow == 0 or (net_profit > 0) == (operating_cashflow > 0))
    return _base_result(
        "现金流合理性（有限检查）", "净利润与经营现金流方向一致性", "limited_check", same_direction,
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
    inputs = {"net_profit_parent": parent, "net_profit": consolidated, "retained_earnings_begin": begin, "retained_earnings_end": end, "dividends": dividends}
    if parent is None or begin is None or end is None or dividends is None:
        result = _base_result(
            "未分配利润一致性", "期末未分配利润-期初未分配利润 ≈ 归母净利润-分红", "insufficient_data", None,
            "缺少归母净利润、期初/期末未分配利润或分红，无法校验；不能用合并净利润替代归母口径",
            data=data, inputs=inputs, reason="未分配利润勾稽需要母公司口径的完整输入",
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
    diff = abs(actual - expected)
    ratio = diff / max(abs(expected), abs(actual), Decimal("1"))
    passed = ratio <= Decimal("0.05")
    result = _base_result(
        "未分配利润一致性", "期末未分配利润-期初未分配利润 ≈ 归母净利润-分红", "calculated", passed,
        "未分配利润变动与归母净利润-分红一致" if passed else f"未分配利润勾稽不一致，差额{_number(diff, 2)}（{_number(ratio * 100, 2)}%）",
        data=data, inputs=inputs, difference=diff, threshold="5%",
        reason="使用归属于母公司股东的净利润口径" if parent is not None else "未提供归母净利润，不能用合并口径替代母公司口径",
    )
    result.update({"actual_change": _number(actual), "expected_change": _number(expected), "difference_pct": f"{_number(ratio * 100, 2):.2f}%", "net_profit_note": "归母口径" if parent is not None else "合并口径不可替代归母口径"})
    return result


@tool
def validate_financial_data(financial_data_json: str) -> str:
    """执行资产负债表、现金流和未分配利润三类数据质量校验。"""
    try:
        data = json.loads(financial_data_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"JSON解析失败: {exc}", "validation_version": VALIDATION_VERSION}, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps({"error": "输入 JSON 顶层须为对象", "validation_version": VALIDATION_VERSION}, ensure_ascii=False)

    results = [_validate_balance_sheet(data), _validate_cashflow_reconciliation(data), _validate_retained_earnings(data)]
    failed = [item for item in results if item.get("passed") is False]
    skipped = [item for item in results if item.get("passed") is None]
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
    pending_checks = [item for item in results if item.get("passed") is None]
    validation_result = (
        "未通过" if failed else "部分完成" if skipped else "通过"
    )
    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "validation_version": VALIDATION_VERSION,
        "amount_unit": data.get("amount_unit") or _metadata(data).get("amount_unit") or "",
        "period": data.get("period") or _metadata(data).get("period") or "",
        "scope": data.get("scope") or _metadata(data).get("scope") or "",
        "results": results,
        "data_validation": {
            "all_checks": results, "results": results, "total_checks": len(results),
            "passed_checks": sum(1 for item in results if item.get("passed") is True),
            "failed_checks": len(failed), "skipped_checks": len(skipped),
            "validation_result": validation_result,
            "status": "verified" if not skipped else "partially_tested",
            "risks": risks,
            "pending_checks": pending_checks,
        },
        "facts": [f for item in results for f in item.get("facts", [])],
        "metric_results": [item["metric_result"] for item in results],
        "evidence": all_evidence,
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
