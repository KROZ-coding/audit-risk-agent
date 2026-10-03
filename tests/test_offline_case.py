"""Offline acceptance case safeguards.

These tests do not execute PDF parsing, LLMs, export, or network calls. They lock the
synthetic case's consolidated source values and make zero placeholders impossible.
The embedded dataset is constructed (no real company); a local real case can be run
separately via ``scripts/offline_case.py --case-file``.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from offline_case import CALC_JSON, MY_INPUT, VD_JSON
from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators


def _invoke(tool, data):
    return json.loads(tool.invoke({"financial_data_json": json.dumps(data, ensure_ascii=False)}))


def test_offline_fixture_uses_synthetic_consolidated_values_and_period():
    assert CALC_JSON["amount_unit"] == "人民币元"
    assert CALC_JSON["scope"] == "中国准则合并"
    assert CALC_JSON["period"] == "2025年半年度"
    assert CALC_JSON["total_assets_current"] == 2_400_000_000_000
    assert CALC_JSON["total_liabilities_current"] == 960_000_000_000
    assert CALC_JSON["net_assets_current"] == 1_440_000_000_000
    assert CALC_JSON["cost_of_goods_current"] == 950_000_000_000
    assert CALC_JSON["cash_and_equivalents_current"] == 190_000_000_000
    # 货币资金不是现金等价物：不得与总资产同值（占位符检测）
    assert CALC_JSON["cash_and_equivalents_current"] != CALC_JSON["total_assets_current"]
    # 勾稽恒等式：资产 = 负债 + 净资产；应收净额 = 账面余额 − 坏账准备
    assert CALC_JSON["total_assets_current"] == (
        CALC_JSON["total_liabilities_current"] + CALC_JSON["net_assets_current"])
    assert CALC_JSON["accounts_receivable_current"] == (
        CALC_JSON["accounts_receivable_gross_current"] - CALC_JSON["bad_debt_provision_current"])


def test_offline_fixture_calculator_has_expected_gross_margin_and_no_ifrs_mix():
    output = _invoke(calculate_financial_indicators, CALC_JSON)
    metrics = {item["metric_id"]: item for item in output["metric_results"]}
    assert round(metrics["gross_margin_pct"]["value"], 2) == 20.83
    assert round(metrics["debt_to_asset_ratio_pct"]["value"], 2) == 40.0
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


def test_validator_fixture_reconciles_retained_earnings_with_declared_other_change():
    output = _invoke(validate_financial_data, VD_JSON)
    equity = next(item for item in output["results"] if "未分配利润" in item["check"])
    assert equity["difference"] == 0
    assert equity["status"] == "calculated"
    assert equity["exact_match"] is True
    assert equity["other_changes"] == -200_000_000
    assert equity["evidence"]["page"] == "15"
    assert "其他权益变动" in equity["evidence"]["locator"]


def test_multi_year_fixture_has_no_zero_placeholders_for_report_items():
    for year in MY_INPUT["years"]:
        for key in ("revenue", "net_profit", "operating_cashflow", "total_assets",
                    "total_liabilities", "accounts_receivable", "inventory",
                    "cost_of_goods", "current_assets", "current_liabilities"):
            assert year[key] != 0, (year["year"], key)
