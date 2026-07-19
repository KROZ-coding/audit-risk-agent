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

from tools.financial_calculator import calculate_financial_indicators


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
