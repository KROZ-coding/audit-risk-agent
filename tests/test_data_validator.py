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
        """现金流勾稽关系成立时应通过"""
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

    def test_insufficient_data_skip(self):
        """数据不足时应跳过校验而非报错"""
        result = self._invoke({})
        assert result["data_validation"]["skipped_checks"] >= 1

    def test_retained_earnings_consistency(self):
        """未分配利润变动与净利润一致时应通过"""
        result = self._invoke({
            "net_profit": 500,
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

    def test_all_checks_pass(self):
        """数据完全一致时总体结论应为通过"""
        result = self._invoke({
            "total_assets": 10000,
            "total_liabilities": 6000,
            "net_assets": 4000,
            "net_profit": 800,
            "operating_cashflow": 900,
            "depreciation": 100,
            "amortization": 0,
            "working_capital_change": 0,
            "retained_earnings_begin": 2000,
            "retained_earnings_end": 2800,
            "dividends": 0,
        })
        assert result["data_validation"]["validation_result"] == "通过"
