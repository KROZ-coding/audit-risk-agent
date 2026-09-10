"""财务指标计算工具的单元测试

覆盖 calculate_financial_indicators 的核心场景：
- 正常数据计算（毛利率、资产负债率、流动比率等）
- 存贷双高预警检测
- 连续亏损持续经营预警
- 空数据/零值防御
- 现金流与利润背离检测
- 应收账款异常检测
"""
import json
import sys
import os
import pytest

# 确保 src 目录在 Python 路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.financial_calculator import (calculate_financial_indicators, detect_amount_unit,
                                        extract_parent_net_profit, normalize_financial_units,
                                        scale_amount_fields)


class TestExtractParentNetProfit:
    """归母净利润规则兜底（P11）：未分配利润勾稽必须用归母口径。"""

    def test_with_explicit_unit(self):
        """带单位表述：归属于母公司股东的净利润 840.07亿元 → 8.4007e10 元。"""
        text = "归属于母公司股东的净利润 840.07亿元，同比下降5.4%。"
        assert extract_parent_net_profit(text) == 840.07 * 1e8

    def test_with_table_number_and_declared_unit(self):
        """财务摘要表数字 84,007 + 声明单位百万元 → 8.4007e10 元。"""
        text = ("除特别注明外，金额单位为人民币百万元。"
                "归属于母公司股东的净利润 84,007 88,806 88,611 (5.4)")
        assert extract_parent_net_profit(text) == 84007 * 1e6

    def test_no_match_returns_none(self):
        """无匹配时保守返回 None（不猜测）。"""
        assert extract_parent_net_profit("公司主营业务为油气勘探开发。") is None
        assert extract_parent_net_profit("") is None


class TestAmountUnitDetection:
    """年报金额单位声明识别（P8b）：无绝对锚场景的整体换算依据。"""

    def test_detect_declared_unit(self):
        assert detect_amount_unit("单位：人民币百万元") == "百万元"
        assert detect_amount_unit("除特别注明外，金额单位为人民币百万元。") == "百万元"
        assert detect_amount_unit("本报告金额单位:万元") == "万元"

    def test_no_declaration_returns_none(self):
        assert detect_amount_unit("公司主营业务为油气勘探开发。") is None
        assert detect_amount_unit("") is None

    def test_scale_amount_fields_keeps_ratios(self):
        """整体换算只动金额字段，比率/次数字段保留。"""
        data = {"net_profit_current": 93666, "revenue_current": 1450099,
                "gross_margin_pct": 20.89, "industry": "能源"}
        out = scale_amount_fields(data, 1e6)
        assert out["net_profit_current"] == 93666 * 1e6
        assert out["revenue_current"] == 1450099 * 1e6
        assert out["gross_margin_pct"] == 20.89  # 比率字段不动
        assert out["industry"] == "能源"


class TestNormalizeFinancialUnits:
    """单位口径（P8 修订）：只按明确声明的单位换算，不按财务结构猜测量级。"""

    def test_no_structural_rescale_without_declared_unit(self):
        """营收 1.45 万亿元 + 净利 93666 这类量级不一致不再自动换算，原值保留。"""
        data = {
            "revenue_current": 1.45e12,
            "net_profit_current": 93666,
            "net_profit_previous": 99805,
            "dividends": 45755,
            "retained_earnings_begin": 982234,
            "retained_earnings_end": 1020356,
        }
        out = normalize_financial_units(data)
        assert out["net_profit_current"] == 93666
        assert out["net_profit_previous"] == 99805
        assert out["dividends"] == 45755
        assert out["retained_earnings_begin"] == 982234
        assert out["retained_earnings_end"] == 1020356
        assert out["unit_normalization"]["status"] == "not_applied"
        assert "不按典型财务结构猜测换算因子" in out["unit_normalization"]["reason"]

    def test_already_correct_scale_untouched(self):
        """比率本就在合理区间时原样保留（防误改）。"""
        data = {"revenue_current": 1.45e12, "net_profit_current": 9.4e10,
                "operating_cashflow_current": 2.2e11}
        out = normalize_financial_units(data)
        assert out["net_profit_current"] == 9.4e10
        assert out["operating_cashflow_current"] == 2.2e11

    def test_no_anchor_untouched(self):
        """无明确单位声明时原值保留，仅登记未换算状态。"""
        data = {"net_profit_current": 100, "revenue_current": 200}
        out = normalize_financial_units(dict(data))
        assert {k: v for k, v in out.items() if k != "unit_normalization"} == data
        assert out["unit_normalization"]["status"] == "not_applied"

    def test_unfixable_kept_with_warning(self):
        """修正后仍无法落入合理区间时保留原值（不静默改错）。"""
        data = {"revenue_current": 1.45e12, "net_profit_current": 3e12}  # 净利率 207% 超上限
        out = normalize_financial_units(data)
        assert out["net_profit_current"] == 3e12

    def test_validation_style_fields_untouched(self):
        """校验字段体系（裸字段名）同样不做结构化换算，避免静默改错。"""
        data = {"revenue": 1.45e12, "net_profit": 93666, "total_assets": 2849632,
                "operating_cashflow": 227063}
        out = normalize_financial_units(data)
        assert out["net_profit"] == 93666
        assert out["total_assets"] == 2849632
        assert out["operating_cashflow"] == 227063

    def test_declared_unit_is_honored_by_scale_helper(self):
        """明确声明单位时仍按声明换算（唯一允许的换算路径）。"""
        data = {"net_profit": 93666, "revenue": 1450099}
        out = scale_amount_fields(data, 1e6)
        assert out["net_profit"] == 93666 * 1e6
        assert out["revenue"] == 1450099 * 1e6


class TestFinancialCalculator:
    """财务指标计算工具测试集"""

    def _invoke(self, data_dict):
        """辅助方法：调用工具并解析返回 JSON"""
        data = json.dumps(data_dict)
        result = calculate_financial_indicators.invoke({"financial_data_json": data})
        return json.loads(result)

    def test_basic_indicators(self):
        """正常财务数据应正确计算毛利率和资产负债率"""
        result = self._invoke({
            "revenue_current": 10000,
            "cost_of_goods_current": 7000,
            "total_assets_current": 50000,
            "total_liabilities_current": 30000,
        })
        assert result["indicators"]["gross_margin_pct"] == 30.0
        assert result["indicators"]["debt_to_asset_ratio_pct"] == 60.0
        assert result["alert_count"] == 0

    def test_high_debt_alert(self):
        """资产负债率超过70%应触发预警"""
        result = self._invoke({
            "total_assets_current": 10000,
            "total_liabilities_current": 8000,
        })
        assert result["indicators"]["debt_to_asset_ratio_pct"] == 80.0
        assert any("70%" in alert for alert in result["alerts"])

    def test_deposit_loan_abnormal(self):
        """存贷双高异常模式应触发预警"""
        result = self._invoke({
            "cash_and_equivalents_current": 5000,
            "short_term_debt_current": 3000,
            "interest_income_current": 100,
            "interest_expense_current": 500,
        })
        assert any("存贷双高" in alert for alert in result["alerts"])

    def test_consecutive_loss(self):
        """连续两年净利润亏损应触发持续经营预警"""
        result = self._invoke({
            "net_profit_current": -500,
            "net_profit_previous": -300,
        })
        assert any("持续经营" in alert for alert in result["alerts"])

    def test_empty_data_defense(self):
        """空数据不应导致崩溃，应返回空结果"""
        result = self._invoke({})
        assert result["alert_count"] == 0
        assert isinstance(result["indicators"], dict)

    def test_cashflow_profit_divergence(self):
        """经营现金流与净利润严重背离应触发预警"""
        result = self._invoke({
            "net_profit_current": 1000,
            "operating_cashflow_current": 200,
        })
        ratio = result["indicators"]["operating_cashflow_to_net_profit_ratio"]
        assert ratio == 0.2
        assert any("现金流" in alert for alert in result["alerts"])

    def test_revenue_decline_alert(self):
        """营收同比下滑超过30%应触发预警"""
        result = self._invoke({
            "revenue_current": 6000,
            "revenue_previous": 10000,
        })
        assert result["indicators"]["revenue_yoy_change_pct"] == -40.0
        assert any("营业收入" in alert for alert in result["alerts"])

    def test_goodwill_risk(self):
        """商誉占净资产超过30%应触发减值风险预警"""
        result = self._invoke({
            "goodwill_current": 4000,
            "net_assets_current": 10000,
        })
        assert result["indicators"]["goodwill_to_net_assets_ratio_pct"] == 40.0
        assert any("商誉" in alert for alert in result["alerts"])

    def test_current_ratio_low(self):
        """流动比率低于1应触发短期偿债能力预警"""
        result = self._invoke({
            "current_assets_current": 800,
            "current_liabilities_current": 1000,
        })
        assert result["indicators"]["current_ratio"] == 0.8
        assert any("流动比率" in alert for alert in result["alerts"])

    def test_negative_cashflow_consecutive(self):
        """经营活动现金流连续两年为负应触发持续经营风险"""
        result = self._invoke({
            "operating_cashflow_current": -100,
            "operating_cashflow_previous": -200,
        })
        assert any("连续" in alert and "为负" in alert for alert in result["alerts"])
