"""Offline real-data fixture safeguards.

These tests do not execute PDF parsing, LLMs, export, or network calls. They lock the
hand-entered CAS consolidated source values and make zero placeholders impossible.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from offline_real_verify import CALC_JSON, MY_INPUT, VD_JSON
from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators


def _invoke(tool, data):
    return json.loads(tool.invoke({"financial_data_json": json.dumps(data, ensure_ascii=False)}))


def test_offline_fixture_uses_cas_consolidated_values_and_period():
    assert CALC_JSON["amount_unit"] == "人民币元"
    assert CALC_JSON["scope"] == "中国准则合并"
    assert CALC_JSON["period"] == "2025年半年度"
    assert CALC_JSON["total_assets_current"] == 2_849_632_000_000
    assert CALC_JSON["total_liabilities_current"] == 1_096_490_000_000
    assert CALC_JSON["net_assets_current"] == 1_753_142_000_000
    assert CALC_JSON["cost_of_goods_current"] == 1_147_144_000_000
    assert CALC_JSON["cash_and_equivalents_current"] == 224_124_000_000
    assert CALC_JSON["cash_and_equivalents_current"] != 284_493_000_000  # 货币资金不是现金等价物


def test_offline_fixture_calculator_has_real_gross_margin_and_no_ifrs_mix():
    output = _invoke(calculate_financial_indicators, CALC_JSON)
    metrics = {item["metric_id"]: item for item in output["metric_results"]}
    assert round(metrics["gross_margin_pct"]["value"], 2) == 20.89
    assert round(metrics["debt_to_asset_ratio_pct"]["value"], 2) == 38.48
    assert metrics["gross_margin_pct"]["inputs"]
    assert output["period"] == "2025年半年度"
    assert output["scope"] == "中国准则合并"


def test_validator_fixture_does_not_turn_unknown_cashflow_adjustments_into_zero():
    output = _invoke(validate_financial_data, VD_JSON)
    checks = output["data_validation"]["all_checks"]
    cashflow = next(item for item in checks if "现金流" in item["check"])
    assert cashflow["status"] == "limited_check"
    assert cashflow["passed"] is True
    assert "方向一致" in cashflow["message"]
    assert "不能替代完整勾稽" in cashflow["message"]
    assert all(key not in VD_JSON for key in ("depreciation", "amortization", "working_capital_change"))


def test_validator_fixture_reconciles_petrochina_retained_earnings_with_disclosed_other_change():
    output = _invoke(validate_financial_data, VD_JSON)
    equity = next(item for item in output["results"] if "未分配利润" in item["check"])
    assert equity["difference"] == 0
    assert equity["status"] == "calculated"
    assert equity["exact_match"] is True
    assert equity["other_changes"] == -116_000_000
    assert equity["evidence"]["page"] == "51"
    assert "其他权益变动" in equity["evidence"]["locator"]


def test_multi_year_fixture_has_no_zero_placeholders_for_real_report_items():
    for year in MY_INPUT["years"]:
        for key in ("revenue", "net_profit", "operating_cashflow", "total_assets",
                    "total_liabilities", "accounts_receivable", "inventory",
                    "cost_of_goods", "current_assets", "current_liabilities"):
            assert year[key] != 0, (year["year"], key)
