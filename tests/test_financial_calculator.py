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
        data = {"net_profit_current": 80000, "revenue_current": 1200000,
                "gross_margin_pct": 20.83, "industry": "能源"}
        out = scale_amount_fields(data, 1e6)
        assert out["net_profit_current"] == 80000 * 1e6
        assert out["revenue_current"] == 1200000 * 1e6
        assert out["gross_margin_pct"] == 20.83  # 比率字段不动
        assert out["industry"] == "能源"


class TestNormalizeFinancialUnits:
    """单位口径（P8 修订）：只按明确声明的单位换算，不按财务结构猜测量级。"""

    def test_no_structural_rescale_without_declared_unit(self):
        """营收 1.2 万亿元 + 净利 80000 这类量级不一致不再自动换算，原值保留。"""
        data = {
            "revenue_current": 1.2e12,
            "net_profit_current": 80000,
            "net_profit_previous": 85000,
            "dividends": 36000,
            "retained_earnings_begin": 900000,
            "retained_earnings_end": 935800,
        }
        out = normalize_financial_units(data)
        assert out["net_profit_current"] == 80000
        assert out["net_profit_previous"] == 85000
        assert out["dividends"] == 36000
        assert out["retained_earnings_begin"] == 900000
        assert out["retained_earnings_end"] == 935800
        assert out["unit_normalization"]["status"] == "not_applied"
        assert "不按典型财务结构猜测换算因子" in out["unit_normalization"]["reason"]

    def test_already_correct_scale_untouched(self):
        """比率本就在合理区间时原样保留（防误改）。"""
        data = {"revenue_current": 1.2e12, "net_profit_current": 9.4e10,
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
        data = {"revenue_current": 1.2e12, "net_profit_current": 3e12}  # 净利率 250% 超上限
        out = normalize_financial_units(data)
        assert out["net_profit_current"] == 3e12

    def test_validation_style_fields_untouched(self):
        """校验字段体系（裸字段名）同样不做结构化换算，避免静默改错。"""
        data = {"revenue": 1.2e12, "net_profit": 80000, "total_assets": 2400000,
                "operating_cashflow": 180000}
        out = normalize_financial_units(data)
        assert out["net_profit"] == 80000
        assert out["total_assets"] == 2400000
        assert out["operating_cashflow"] == 180000

    def test_declared_unit_is_honored_by_scale_helper(self):
        """明确声明单位时仍按声明换算（唯一允许的换算路径）。"""
        data = {"net_profit": 80000, "revenue": 1200000}
        out = scale_amount_fields(data, 1e6)
        assert out["net_profit"] == 80000 * 1e6
        assert out["revenue"] == 1200000 * 1e6


class TestFinancialCalculator:
    """财务指标计算工具测试集"""

    def _invoke(self, data_dict):
        """辅助方法：调用工具并解析返回 JSON"""
        data = json.dumps(data_dict)
        result = calculate_financial_indicators.invoke({"financial_data_json": data})
        return json.loads(result)

    def test_new_audit_metrics_are_omitted_when_all_inputs_are_absent(self):
        result = self._invoke({"revenue_current": 1000, "revenue_previous": 900})
        metric_ids = {item["metric_id"] for item in result["metric_results"]}
        assert "cash_to_short_term_debt_ratio" not in metric_ids
        assert "operating_cashflow_to_total_liabilities_ratio" not in metric_ids
        assert "bad_debt_provision_to_gross_receivables_ratio_pct" not in metric_ids

    def test_new_audit_metrics_keep_partial_missing_input_visible(self):
        result = self._invoke({
            "cash_and_equivalents_current": 120,
            "operating_cashflow_current": 30,
            "accounts_receivable_gross_current": 500,
        })
        metrics = {item["metric_id"]: item for item in result["metric_results"]}
        assert metrics["cash_to_short_term_debt_ratio"]["value"] is None
        assert "short_term_debt_current" in metrics["cash_to_short_term_debt_ratio"]["reason"]
        assert metrics["operating_cashflow_to_total_liabilities_ratio"]["value"] is None
        assert "total_liabilities_current" in metrics["operating_cashflow_to_total_liabilities_ratio"]["reason"]
        assert metrics["bad_debt_provision_to_gross_receivables_ratio_pct"]["value"] is None
        assert "bad_debt_provision_current" in metrics["bad_debt_provision_to_gross_receivables_ratio_pct"]["reason"]

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

    def test_gross_receivable_divergence_is_a_stable_review_alert(self):
        result = self._invoke({
            "period": "2025年半年度", "scope": "中国准则合并", "amount_unit": "人民币百万元",
            "revenue_current": 1200000, "revenue_previous": 1280000,
            "accounts_receivable_gross_current": 100000,
            "accounts_receivable_gross_same_period_previous": 62500,
            "bad_debt_provision_current": 2400,
            "bad_debt_provision_same_period_previous": 2500,
            "operating_cashflow_current": 180000,
            "net_profit_current": 80000,
            "_field_metadata": {
                "accounts_receivable_gross_same_period_previous": {"period": "2024年半年度（追溯后）"},
                "revenue_previous": {"period": "2024年半年度（追溯后）"},
            },
        })
        metric = next(m for m in result["metric_results"]
                       if m["metric_id"] == "accounts_receivable_gross_yoy_change_pct")
        assert round(metric["value"], 2) == 60.00
        assert "100,000.00人民币百万元" in metric["substitution"]
        assert "62,500.00人民币百万元" in metric["substitution"]
        assert metric["substitution"].endswith("= 60.00%")
        assert any("应收账款账面余额较上年末增长" in alert for alert in result["alerts"])
        assert any("经营现金流" in alert for alert in result["alerts"])

    def test_gross_receivable_and_revenue_mismatched_periods_do_not_claim_divergence(self):
        """Year-end receivable growth must not be subtracted from H1 revenue YoY."""
        result = self._invoke({
            "period": "2025年半年度", "previous_period": "2024年半年度（追溯后）",
            "revenue_current": 1200000, "revenue_previous": 1280000,
            "accounts_receivable_gross_current": 100000,
            "accounts_receivable_gross_same_period_previous": 62500,
            "_field_metadata": {
                "accounts_receivable_gross_same_period_previous": {"period": "2024-12-31"},
                "revenue_previous": {"period": "2024年半年度（追溯后）"},
            },
        })
        alerts = result["alerts"]
        assert any("方向相反" in alert for alert in alerts)
        mismatch = next(alert for alert in alerts if "方向相反" in alert)
        assert "期间不一致" in mismatch
        assert "显著背离" not in mismatch
        assert "不能仅据此" not in mismatch
        assert "60.00%" in mismatch
        assert "相减" not in mismatch

    def test_gross_receivable_unknown_period_does_not_claim_numeric_divergence(self):
        result = self._invoke({
            "period": "2025年半年度", "revenue_current": 1200000, "revenue_previous": 1280000,
            "accounts_receivable_gross_current": 97600,
            "accounts_receivable_gross_same_period_previous": 60000,
        })
        assert any("暂不作背离判断" in alert for alert in result["alerts"])
        assert not any("显著背离" in alert for alert in result["alerts"])

    def test_synthetic_interim_fact_names_and_provenance(self):
        '"""Synthetic CAS-style consolidated facts with declared page provenance."""'
        data = {
            "period": "2025年半年度", "scope": "合并", "amount_unit": "人民币百万元",
            "revenue_current": 1200000, "revenue_previous": 1280000,
            "operating_cost_current": 950000, "operating_cost_previous": 1020000,
            "net_profit_current": 80000, "net_profit_previous": 85000,
            "net_profit_parent_current": 72000, "net_profit_parent_previous": 76000,
            "net_profit_parent_deducted_current": 72500, "net_profit_parent_deducted_previous": 77000,
            "total_assets": 2400000, "total_liabilities": 960000,
            "current_assets": 600000, "current_liabilities": 560000,
            "accounts_receivable": 97600, "accounts_receivable_previous": 60000,
            "inventory": 130000, "inventory_previous": 140000,
            "goodwill": 6000, "equity_total": 1440000, "equity_parent": 1300000,
            "other_receivables": 30000,
            "operating_cashflow_current": 180000,
            "_field_metadata": {
                "operating_cost_current": {"page": "11", "period": "2025年1-6月"},
                "accounts_receivable_previous": {"page": "12", "period": "2024-12-31"},
                "revenue_previous": {"page": "11", "period": "2024年1-6月（追溯后）"},
            },
        }
        result = self._invoke(data)
        metrics = {m["metric_id"]: m for m in result["metric_results"]}
        assert round(metrics["gross_margin_pct"]["value"], 2) == 20.83
        assert round(metrics["debt_to_asset_ratio_pct"]["value"], 2) == 40.00
        assert round(metrics["current_ratio"]["value"], 4) == 1.0714
        assert round(metrics["quick_ratio"]["value"], 4) == 0.8393
        assert round(metrics["accounts_receivable_to_revenue_ratio"]["value"], 2) == 8.13
        assert round(metrics["accounts_receivable_turnover_ratio"]["value"], 2) == 15.23
        assert round(metrics["accounts_receivable_turnover_days"]["value"], 2) == 11.89
        assert round(metrics["inventory_turnover_ratio"]["value"], 2) == 7.04
        assert round(metrics["goodwill_to_net_assets_ratio_pct"]["value"], 2) == 0.42
        assert round(metrics["other_receivables_to_total_assets_ratio_pct"]["value"], 2) == 1.25
        assert metrics["accounts_receivable_to_revenue_ratio_change_pp"]["value"] is None
        assert "期初" in metrics["accounts_receivable_to_revenue_ratio_change_pp"]["reason"]
        assert metrics["net_profit_yoy_change_pct"]["name"] == "净利润（合并）同比变动率"
        assert round(metrics["net_profit_parent_yoy_change_pct"]["value"], 2) == -5.26
        assert round(metrics["net_profit_parent_deducted_yoy_change_pct"]["value"], 2) == -5.84
        cost = next(i for i in metrics["gross_margin_pct"]["inputs"] if i["field"] == "cost_of_goods_current")
        assert cost["fact_id"] == "F-operating_cost_current"
        assert cost["source_field"] == "operating_cost_current"
        assert cost["unit"] == "人民币百万元"
        assert cost["period"] == "2025年1-6月"
        assert "950,000.00人民币百万元" in metrics["gross_margin_pct"]["substitution"]
        assert next(e for e in result["evidence"] if e["evidence_id"] == "E-gross_margin_pct")["page"] == "11"

    def test_alias_conflict_is_not_silently_selected(self):
        result = self._invoke({"total_assets": 1000, "total_assets_current": 2000,
                               "total_liabilities": 500})
        metric = next(m for m in result["metric_results"] if m["metric_id"] == "debt_to_asset_ratio_pct")
        assert metric["value"] is None
        assert "total_assets_current" in metric["reason"]
        assert result["input_warnings"]

    def test_interim_same_period_receivable_is_separate_from_opening_balance(self):
        result = self._invoke({
            "report_period": "2025年半年度", "revenue_current": 1000, "revenue_previous": 800,
            "accounts_receivable": 150, "accounts_receivable_previous": 50,
            "accounts_receivable_same_period_previous": 100,
        })
        metrics = {m["metric_id"]: m for m in result["metric_results"]}
        assert metrics["accounts_receivable_to_revenue_ratio_change_pp"]["value"] == 2.5
        assert metrics["accounts_receivable_turnover_ratio"]["value"] == 10
        assert metrics["accounts_receivable_turnover_days"]["value"] == 18.1

    def test_interim_unknown_day_count_does_not_default_to_year(self):
        result = self._invoke({"period": "中期", "revenue_current": 1000,
                               "accounts_receivable": 150, "accounts_receivable_previous": 50})
        metrics = {m["metric_id"]: m for m in result["metric_results"]}
        assert metrics["accounts_receivable_turnover_ratio"]["value"] == 10
        assert metrics["accounts_receivable_turnover_days"]["value"] is None

    def test_missing_numerator_is_not_described_as_missing_denominator(self):
        result = self._invoke({"revenue_current": 1000})
        metric = next(m for m in result["metric_results"] if m["metric_id"] == "gross_margin_pct")
        assert "cost_of_goods_current" in metric["reason"]
        assert "营业收入缺失" not in metric["reason"]

    def test_interim_turnover_rejects_prior_june_as_opening_balance(self):
        result = self._invoke({
            "period": "2025年半年度", "revenue_current": 1000,
            "accounts_receivable": 150, "accounts_receivable_previous": 50,
            "_field_metadata": {"accounts_receivable_previous": {"period": "2024-06-30"}},
        })
        metric = next(m for m in result["metric_results"] if m["metric_id"] == "accounts_receivable_turnover_ratio")
        assert metric["value"] is None
        assert "期初不匹配" in metric["reason"]

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
