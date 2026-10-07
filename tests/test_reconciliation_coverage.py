"""F1 勾稽覆盖声明与有效税率勾稽测试

锁定行为：
- validate_financial_data 输出 kb_cross_table_coverage（对照知识库跨表勾稽 10 条，
  声明已实现/部分覆盖/未实现），未实现条目带原因
- 有效税率勾稽：正常区间通过、<5% 异常偏低不通过、亏损/退税情景不判定
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.data_validator import validate_financial_data


def _invoke(data):
    result = validate_financial_data.invoke({"financial_data_json": json.dumps(data)})
    return json.loads(result)


class TestCoverageDeclaration:
    def test_coverage_block_present_and_honest(self):
        out = _invoke({"total_assets": 10000, "total_liabilities": 6000, "net_assets": 4000})
        cov = out["data_validation"]["kb_cross_table_coverage"]
        assert cov["implemented"] == 3
        assert cov["partial"] == 2
        assert cov["not_implemented"] == 5
        assert len(cov["rules"]) == 10
        missing = [r for r in cov["rules"] if r["status"] == "not_implemented"]
        assert all("缺" in r["detail"] for r in missing), "未实现条目必须说明原因"

    def test_coverage_notes_missing_fields(self):
        out = _invoke({"total_assets": 10000, "total_liabilities": 6000, "net_assets": 4000})
        rules = {r["rule_id"]: r for r in out["data_validation"]["kb_cross_table_coverage"]["rules"]}
        assert "销售商品提供劳务收到的现金" in rules[4]["detail"]
        assert rules[10]["status"] == "not_implemented"


class TestEffectiveTaxRate:
    def test_normal_rate_passes(self):
        out = _invoke({"income_tax_expense": 2500, "profit_before_tax": 10000})
        chk = next(c for c in out["data_validation"]["all_checks"] if c["check"] == "有效税率合理性")
        assert chk["passed"] is True
        assert chk["effective_tax_rate"] == "25.00%"

    def test_abnormally_low_rate_fails(self):
        """有效税率 <5% 判为异常偏低（虚增利润通常不交税）"""
        out = _invoke({"income_tax_expense": 80, "profit_before_tax": 10000})
        chk = next(c for c in out["data_validation"]["all_checks"] if c["check"] == "有效税率合理性")
        assert chk["passed"] is False
        assert "利润虚增" in chk["message"]
        assert chk["effective_tax_rate"] == "0.80%"

    def test_loss_scenario_not_judged(self):
        out = _invoke({"income_tax_expense": -50, "profit_before_tax": -1000})
        chk = next(c for c in out["data_validation"]["all_checks"] if c["check"] == "有效税率合理性")
        assert chk["passed"] is None
        assert chk["status"] == "limited_check"

    def test_missing_fields_insufficient(self):
        out = _invoke({"total_assets": 10000})
        chk = next(c for c in out["data_validation"]["all_checks"] if c["check"] == "有效税率合理性")
        assert chk["passed"] is None
        assert chk["status"] == "insufficient_data"
