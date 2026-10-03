"""财务数据一致性校验工具的单元测试

覆盖 validate_financial_data 的三项勾稽校验：
- 资产负债表平衡校验
- 现金流勾稽校验
- 未分配利润一致性校验
- 综合通过/未通过判定
"""
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.data_validator import validate_financial_data


class TestDataValidator:
    """财务数据校验工具测试集"""

    def _invoke(self, data_dict):
        """辅助方法：调用工具并解析返回 JSON"""
        data = json.dumps(data_dict)
        result = validate_financial_data.invoke({"financial_data_json": data})
        return json.loads(result)

    def test_balanced_sheet_pass(self):
        """资产负债表平衡时应通过校验"""
        result = self._invoke({
            "total_assets": 10000,
            "total_liabilities": 6000,
            "net_assets": 4000,
        })
        checks = result["data_validation"]["all_checks"]
        balance_check = next(c for c in checks if "资产负债表" in c["check"])
        assert balance_check["passed"] is True

    def test_balanced_sheet_fail(self):
        """资产负债表不平衡时应未通过"""
        result = self._invoke({
            "total_assets": 10000,
            "total_liabilities": 6000,
            "net_assets": 5000,
        })
        assert result["data_validation"]["validation_result"] == "未通过"
        assert result["data_validation"]["failed_checks"] >= 1

    def test_cashflow_reconciliation_pass(self):
        """Selected adjustments can pass screening without completing indirect reconciliation."""
        result = self._invoke({
            "net_profit": 1000,
            "operating_cashflow": 1100,
            "depreciation": 200,
            "amortization": 0,
            "working_capital_change": 100,
        })
        checks = result["data_validation"]["all_checks"]
        cf_check = next(c for c in checks if "现金流" in c["check"])
        assert cf_check["passed"] is True
        assert cf_check["status"] == "limited_check"
        assert cf_check["evidence"]["verified"] is False
        assert "不能视为完整间接法勾稽" in cf_check["message"]

    def test_insufficient_data_skip(self):
        """数据不足时应跳过校验而非报错"""
        result = self._invoke({})
        assert result["data_validation"]["skipped_checks"] >= 1

    def test_retained_earnings_consistency(self):
        """未分配利润变动与净利润一致时应通过"""
        result = self._invoke({
            "net_profit_parent": 500,
            "net_profit": 520,
            "retained_earnings_begin": 1000,
            "retained_earnings_end": 1500,
            "dividends": 0,
        })
        checks = result["data_validation"]["all_checks"]
        re_check = next(c for c in checks if "未分配利润" in c["check"])
        assert re_check["passed"] is True

    def test_failed_checks_generate_risks(self):
        """未通过的校验项应自动生成数据可靠性风险条目"""
        result = self._invoke({
            "total_assets": 10000,
            "total_liabilities": 5000,
            "net_assets": 3000,
        })
        if result["data_validation"]["failed_checks"] > 0:
            risks = result["data_validation"].get("risks", [])
            assert len(risks) > 0
            assert all(r["dimension"] == "数据可靠性风险" for r in risks)

    def test_partial_cashflow_and_equity_checks_are_not_full_validation(self):
        """Directional/selected-component checks must remain partially tested overall."""
        result = self._invoke({
            "total_assets": 10000,
            "total_liabilities": 6000,
            "net_assets": 4000,
            "net_profit": 800,
            "net_profit_parent": 800,
            "operating_cashflow": 900,
            "depreciation": 100,
            "amortization": 0,
            "working_capital_change": 0,
            "retained_earnings_begin": 2000,
            "retained_earnings_end": 2800,
            "dividends": 0,
        })
        validation = result["data_validation"]
        assert validation["validation_result"] == "部分完成"
        assert validation["status"] == "partially_tested"
        assert validation["passed_checks"] == 1
        assert validation["limited_checks"] == 2
        assert validation["failed_checks"] == 0
        assert len(validation["pending_checks"]) == 2

    def test_current_aliases_keep_original_source_fields_and_dates(self):
        result = self._invoke({
            "report_period": "2025年半年度", "amount_unit": "百万元", "scope": "中国准则合并",
            "total_assets_current": "2,400,000", "total_liabilities_current": 960000,
            "equity_total": 1440000, "net_profit_current": 80000,
            "operating_cashflow_current": 180000, "net_profit_parent_current": 72000,
            "retained_earnings_begin": 900000, "retained_earnings_end": 935800, "dividends": 36000,
            "_field_metadata": {
                "total_assets_current": {"page": "11", "period": "2025-06-30", "locator": "合并资产总计"},
                "equity_total": {"page": "12", "period": "2025-06-30"},
                "retained_earnings_begin": {"page": "15", "period": "2025-01-01"},
            },
        })
        checks = result["results"]
        balance = checks[0]
        assert balance["difference"] == 0
        assert balance["passed"] is True
        asset = next(f for f in balance["facts"] if f["field"] == "total_assets_current")
        assert asset["fact_id"] == "F-total_assets_current"
        assert asset["raw_value"] == "2,400,000"
        assert asset["period"] == "2025-06-30"
        assert balance["evidence"]["page"] == "11；12"
        assert result["period"] == "2025年半年度"
        assert checks[1]["status"] == "limited_check"
        assert checks[2]["difference"] == 200
        assert checks[2]["status"] == "limited_check"
        assert "内部5%筛查阈值内" in checks[2]["message"]

    def test_conflicting_alias_values_require_resolution(self):
        result = self._invoke({"total_assets": 1000, "total_assets_current": 2000,
                               "total_liabilities_current": 400, "equity_total": 600})
        assert result["results"][0]["passed"] is None
        assert result["input_warnings"]
        assert result["results"][0]["status"] == "insufficient_data"

    def test_nonzero_balance_difference_is_not_announced_as_balanced(self):
        result = self._invoke({"total_assets": 1000, "total_liabilities": 400, "net_assets": 610})
        balance = result["results"][0]
        assert balance["passed"] is True
        assert balance["difference"] == 10
        assert balance["exact_match"] is False
        assert balance["status"] == "limited_check"
        assert "不能据此认定严格平衡" in balance["message"]

    def test_zero_cashflow_has_no_directional_pass(self):
        result = self._invoke({"net_profit_current": 100, "operating_cashflow_current": 0})
        cashflow = result["results"][1]
        assert cashflow["passed"] is None
        assert "方向比较不具判别力" in cashflow["message"]

    def test_disclosed_other_equity_change_reconciles_declared_difference(self):
        result = self._invoke({
            "amount_unit": "百万元", "period": "2025年半年度", "scope": "中国准则合并",
            "net_profit_parent_current": 72000, "retained_earnings_begin": 900000,
            "retained_earnings_end": 935800, "dividends": 36000,
            "retained_earnings_other_changes": -200,
            "_field_metadata": {"retained_earnings_other_changes": {
                "page": "15", "locator": "合并股东权益变动表：其他权益变动-其他，未分配利润栏",
            }},
        })
        equity = result["results"][2]
        assert equity["difference"] == 0
        assert equity["expected_change"] == equity["actual_change"] == 35800
        assert equity["status"] == "calculated"
        assert equity["other_changes"] == -200
        assert equity["evidence"]["page"] == "15"
        assert "F-retained_earnings_other_changes" in equity["evidence"]["fact_ids"]

    def test_missing_equity_changes_are_not_invented_as_zero(self):
        result = self._invoke({"net_profit_parent_current": 500, "retained_earnings_begin": 1000,
                               "retained_earnings_end": 1500, "dividends": 0})
        equity = result["results"][2]
        assert equity["passed"] is True
        assert equity["other_changes"] is None
        assert equity["status"] == "limited_check"
        assert all(f["field"] != "retained_earnings_other_changes" for f in equity["facts"])

class TestParentCaliber:
    """R 补丁：未分配利润勾稽必须用归母口径，缺归母净利润即不判定。"""

    def test_consolidated_only_inconsistent_not_judged(self):
        from tools.data_validator import validate_financial_data
        data = json.dumps({
            "net_profit": 80000, "retained_earnings_begin": 900000,
            "retained_earnings_end": 935800, "dividends": 36000,
        }, ensure_ascii=False)
        r = json.loads(validate_financial_data.invoke({"financial_data_json": data}))
        dv = r["data_validation"]
        re_chk = next(c for c in dv["all_checks"] if "未分配利润" in c["check"])
        assert re_chk["passed"] is None, "缺归母净利润时不判定，也不得用合并口径替代"
        assert re_chk["status"] == "insufficient_data"
        assert "归母净利润" in re_chk["net_profit_note"]
        assert "未分配利润" not in str([x.get("title") for x in dv.get("risks", [])]), \
            "不得生成未分配利润 V 风险"
        assert dv["failed_checks"] == 0, "口径提示不计 failed_checks"

    def test_consolidated_only_consistent_not_judged(self):
        """即使合并口径自洽也不判定：口径不可替代，须补归母净利润后再勾稽。"""
        from tools.data_validator import validate_financial_data
        data = json.dumps({
            "net_profit": 500, "retained_earnings_begin": 1000,
            "retained_earnings_end": 1500, "dividends": 0,
        }, ensure_ascii=False)
        r = json.loads(validate_financial_data.invoke({"financial_data_json": data}))
        re_chk = next(c for c in r["data_validation"]["all_checks"] if "未分配利润" in c["check"])
        assert re_chk["passed"] is None
        assert re_chk["status"] == "insufficient_data"

    def test_parent_caliber_consistent_passes(self):
        from tools.data_validator import validate_financial_data
        data = json.dumps({
            "net_profit_parent": 500, "net_profit": 620,
            "retained_earnings_begin": 1000, "retained_earnings_end": 1500,
            "dividends": 0,
        }, ensure_ascii=False)
        r = json.loads(validate_financial_data.invoke({"financial_data_json": data}))
        re_chk = next(c for c in r["data_validation"]["all_checks"] if "未分配利润" in c["check"])
        assert re_chk["passed"] is True

    def test_parent_scale_used_when_provided(self):
        from tools.data_validator import validate_financial_data
        data = json.dumps({
            "net_profit_parent": 72000, "net_profit": 80000,
            "retained_earnings_begin": 900000, "retained_earnings_end": 935800,
            "dividends": 36000,
        }, ensure_ascii=False)
        r = json.loads(validate_financial_data.invoke({"financial_data_json": data}))
        re_chk = next(c for c in r["data_validation"]["all_checks"] if "未分配利润" in c["check"])
        assert re_chk["passed"] is True and "归母" in re_chk["net_profit_note"]
        assert re_chk["difference"] == 200
        assert re_chk["status"] == "limited_check"
