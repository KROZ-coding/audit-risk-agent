"""结果契约（Fact/Evidence/MetricResult）字段与兼容性测试（方案 §6 + §9.2）。

覆盖目标：
- 关键事实与指标输出携带 单位/期间/口径/证据引用 等契约字段；
- 缺省字段（scope/period/unit）如实留空，不得静默填「中国准则合并」；
- gross/net 应收、金额×100 量级等独立反例可被识别；
- Fact/Evidence/MetricResult 的 to_dict() 往返 JSON 兼容（Decimal/None）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.result_contract import _json_value, make_fact
from offline_case import CALC_JSON, VD_JSON
from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators


def _invoke(tool, data):
    return json.loads(tool.invoke({"financial_data_json": json.dumps(data, ensure_ascii=False)}))


# 关键字段事实（金额类，单位应为人民币元）
_CALC_FACTS = (
    "revenue_current", "net_profit_current", "net_profit_parent_current",
    "net_profit_parent_deducted_current", "operating_cashflow_current",
    "total_assets_current", "total_liabilities_current",
    "accounts_receivable_current", "accounts_receivable_previous",
)

# 关键字段 → 使用该字段的指标
_FIELD_METRIC = {
    "revenue_current": "revenue_yoy_change_pct",
    "net_profit_current": "net_profit_yoy_change_pct",
    "net_profit_parent_current": "net_profit_parent_yoy_change_pct",
    "net_profit_parent_deducted_current": "net_profit_parent_deducted_yoy_change_pct",
    "operating_cashflow_current": "operating_cashflow_to_net_profit_ratio",
    "total_assets_current": "debt_to_asset_ratio_pct",
    "total_liabilities_current": "debt_to_asset_ratio_pct",
    "accounts_receivable_current": "accounts_receivable_to_revenue_ratio",
}


def test_key_facts_carry_unit_period_scope():
    """金额类关键事实必须带 单位/期间/口径，防止展示层自行猜测。"""
    output = _invoke(calculate_financial_indicators, CALC_JSON)
    facts = {f["field"]: f for f in output["facts"]}
    for field in _CALC_FACTS:
        fact = facts[field]
        assert fact["unit"] == "人民币元", field
        assert fact["period"] in ("2025年半年度", "2024年半年度（追溯后）"), field
        assert fact["scope"] == "中国准则合并", field
        assert fact["fact_id"], field
        assert fact["raw_value"], field


def test_key_metric_results_carry_contract_fields():
    """使用关键字段的指标必须带 单位/期间/口径/证据引用。"""
    output = _invoke(calculate_financial_indicators, CALC_JSON)
    metrics = {m["metric_id"]: m for m in output["metric_results"]}
    for metric_id in sorted(set(_FIELD_METRIC.values())):
        metric = metrics[metric_id]
        assert metric["period"] == "2025年半年度", metric_id
        assert metric["scope"] == "中国准则合并", metric_id
        assert metric["evidence_ids"] == [f"E-{metric_id}"], metric_id
        assert "unit" in metric and "status" in metric and "formula" in metric


def test_validator_facts_carry_unit_period_scope_page_locator():
    """校验器事实携带页码/定位；指标带单位/期间/口径/证据引用。"""
    output = _invoke(validate_financial_data, VD_JSON)
    facts = {f["field"]: f for f in output["facts"]}
    assert output["amount_unit"] == "人民币元"
    assert output["scope"] == "中国准则合并"
    assert output["period"] == "2025年半年度"
    for metric in output["metric_results"]:
        assert metric["unit"] == "人民币元"
        assert metric["period"] == "2025年半年度"
        assert metric["scope"] == "中国准则合并"
        assert metric["evidence_ids"]
    meta_fields = {
        "total_assets_current": ("2025-06-30", "11", "合并资产负债表：资产总计"),
        "total_liabilities_current": ("2025-06-30", "12", "合并资产负债表：负债合计"),
        "retained_earnings_begin": ("2025-01-01", "15", "合并股东权益变动表：未分配利润期初"),
        "retained_earnings_end": ("2025-06-30", "15", "合并股东权益变动表：未分配利润期末"),
    }
    for field, (period, page, locator) in meta_fields.items():
        fact = facts[field]
        assert fact["period"] == period, field
        assert fact["page"] == page, field
        assert locator in fact["locator"], field


def test_validator_evidence_links_facts_and_metrics():
    """证据目录必须可回指事实/指标（usable_evidence 的基础）。"""
    output = _invoke(validate_financial_data, VD_JSON)
    evidence = {e["evidence_id"]: e for e in output["evidence"]}
    balance = evidence["E-validation_balance_sheet"]
    assert balance["source_type"] == "local_validation"
    assert balance["fact_ids"] == ["F-total_assets_current", "F-total_liabilities_current", "F-equity_total"]
    assert balance["metric_ids"] == ["validation_balance_sheet"]
    assert balance["verified"] is True
    assert balance["status"] == "verified"


def test_missing_scope_is_not_silently_filled():
    """缺省 scope/period/unit 必须留空，禁止静默填「中国准则合并」。"""
    fact = make_fact("revenue_current", 1000, fact_id="F-revenue_current")
    assert fact.scope == "" and fact.period == "" and fact.unit == ""
    assert fact.to_dict()["scope"] == "" and fact.to_dict()["period"] == ""
    output = _invoke(calculate_financial_indicators,
                     {"revenue_current": 1000, "cost_of_goods_current": 700})
    assert output["scope"] == "" and output["period"] == "" and output["amount_unit"] == ""
    assert output["unit_check"]["status"] == "pending"
    fact0 = output["facts"][0]
    assert fact0["scope"] == "" and fact0["period"] == "" and fact0["unit"] == ""


def test_gross_and_net_ar_facts_are_separate_with_source_metadata():
    """gross/net 应收必须分开表达，并保留来源元数据（页码/定位/口径）。"""
    data = dict(CALC_JSON)
    data["accounts_receivable_gross_current"] = 100_000_000_000
    data["accounts_receivable_gross_same_period_previous"] = 62_500_000_000
    data["_field_metadata"] = {
        "accounts_receivable_gross_current": {
            "period": "2025-06-30", "page": "21",
            "locator": "合并资产负债表：应收账款（含坏账准备前）"},
        "accounts_receivable_gross_same_period_previous": {
            "period": "2024-06-30", "page": "21", "locator": "上年同期"},
    }
    output = _invoke(calculate_financial_indicators, data)
    facts = {f["field"]: f for f in output["facts"]}
    gross, net = facts["accounts_receivable_gross_current"], facts["accounts_receivable_current"]
    assert gross["fact_id"] != net["fact_id"]
    assert gross["raw_value"] != net["raw_value"]
    assert gross["unit"] == "人民币元" and gross["scope"] == "中国准则合并"
    assert gross["period"] == "2025-06-30" and gross["page"] == "21"
    assert gross["locator"].startswith("合并资产负债表")
    metrics = {m["metric_id"]: m for m in output["metric_results"]}
    gm = metrics["accounts_receivable_gross_yoy_change_pct"]
    assert gm["value"] == pytest.approx(60.0, abs=0.01)
    assert gm["status"] == "calculated"
    assert gm["evidence_ids"] == ["E-accounts_receivable_gross_yoy_change_pct"]
    # 跨期比较必须标注待核查，不当作同比结论
    assert any("跨期" in alert and "暂不作背离判断" in alert for alert in output["alerts"])


class TestFactContractRoundTrip:
    FACT_FIELDS = ("fact_id", "field", "raw_value", "value", "unit", "currency", "period",
                   "point_in_time", "scope", "source_document", "source_hash", "page",
                   "locator", "excerpt", "extraction_method", "status")
    EVIDENCE_FIELDS = ("evidence_id", "source_type", "source_document", "source_hash",
                       "page", "locator", "excerpt", "fact_ids", "metric_ids",
                       "verified", "status")
    METRIC_FIELDS = ("metric_id", "name", "formula", "inputs", "period", "scope", "unit",
                     "value", "display_value", "substitution", "threshold",
                     "threshold_source", "status", "reason", "evidence_ids")

    def test_fact_to_dict_keeps_all_fields(self):
        fact = make_fact("revenue_current", 1000, fact_id="F-revenue_current",
                         unit="人民币元", period="2025年半年度", scope="中国准则合并",
                         page="47", locator="合并资产负债表：资产总计", excerpt="…")
        d = fact.to_dict()
        assert sorted(d) == sorted(self.FACT_FIELDS)
        assert d["value"] == 1000 and d["raw_value"] == "1000"

    def test_fact_none_value_is_json_null(self):
        d = make_fact("x", None, fact_id="F-x").to_dict()
        assert d["value"] is None and d["raw_value"] == ""
        assert json.dumps(d, ensure_ascii=False) is not None  # JSON 兼容

    def test_evidence_to_dict_round_trip(self):
        from core.result_contract import Evidence
        e = Evidence(evidence_id="E-1", source_type="local_validation",
                     fact_ids=["F-a"], metric_ids=["M-b"], verified=True)
        d = e.to_dict()
        assert sorted(d) == sorted(self.EVIDENCE_FIELDS)
        assert d["verified"] is True

    def test_metric_result_to_dict_round_trip(self):
        from core.result_contract import MetricResult
        m = MetricResult(metric_id="gross_margin_pct", name="毛利率",
                         formula="(营收-成本)/营收", value=None, scope="")
        d = m.to_dict()
        assert sorted(d) == sorted(self.METRIC_FIELDS)
        assert d["value"] is None and d["scope"] == ""

    def test_decimal_json_value(self):
        from decimal import Decimal
        assert _json_value(Decimal("20.89")) == "20.89"
        assert _json_value({"a": Decimal("1.5"), "b": [Decimal("2")], "c": None}) == \
            {"a": "1.5", "b": ["2"], "c": None}

    def test_calculator_output_is_json_serializable(self):
        output = _invoke(calculate_financial_indicators, CALC_JSON)
        rendered = json.dumps(output, ensure_ascii=False)
        assert rendered and json.loads(rendered)["period"] == "2025年半年度"

