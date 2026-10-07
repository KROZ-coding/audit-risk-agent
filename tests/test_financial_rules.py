"""财务规则与筛查边界回归（方案 §6/§7 的规则层防回归）。

覆盖目标：
- 合成案例（scripts/offline_case.py）的关键指标/事实字面量（独立于被测代码反推）；
- 期间/口径/单位缺失时如实留空，不得用「中国准则合并/人民币元」兜底；
- 同比分母为负/零、总资产为零、净资产缺失等边界必须给 not_comparable /
  insufficient_data，不得硬算或静默反算；
- 金额×100 量级错误必须被数据校验拦截，资产=负债互换同理；
- 关联交易 30% 内部参考值口径滑移的统一归一化（幂等）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agents.agent import _normalize_source_bound_text
from offline_case import CALC_JSON, VD_JSON
from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators


def _invoke(tool, data):
    return json.loads(tool.invoke({"financial_data_json": json.dumps(data, ensure_ascii=False)}))


def _fin(data=None):
    return _invoke(calculate_financial_indicators, data if data is not None else CALC_JSON)


def _metrics(out):
    return {m["metric_id"]: m for m in out["metric_results"]}


class TestSyntheticCaseKeyLiterals:
    '"""合成案例的关键指标值必须与独立字面量一致（防回归漂移）。"""'

    def test_key_metric_values(self):
        out = _fin()
        m = _metrics(out)
        assert m["gross_margin_pct"]["value"] == pytest.approx(20.833333333333332, rel=1e-12)
        assert m["debt_to_asset_ratio_pct"]["value"] == pytest.approx(40.0, rel=1e-12)
        assert m["accounts_receivable_to_revenue_ratio"]["value"] == pytest.approx(8.133333333333333, rel=1e-12)
        assert m["revenue_yoy_change_pct"]["value"] == pytest.approx(-6.25, rel=1e-12)
        assert m["operating_cashflow_to_net_profit_ratio"]["value"] == pytest.approx(2.25, rel=1e-12)
        assert m["net_profit_parent_yoy_change_pct"]["value"] == pytest.approx(-5.2631578947368425, rel=1e-12)
        assert m["net_profit_parent_deducted_yoy_change_pct"]["value"] == pytest.approx(-5.8441558441558445, rel=1e-12)

    def test_ar_turnover_days_shows_source(self):
        """周转天数必须带单位并注明天数来源，不能假装是原始披露值。"""
        out = _fin()
        m = _metrics(out)
        days = m["accounts_receivable_turnover_days"]
        assert days["unit"] == "天"
        assert days["status"] == "calculated"
        assert days["value"] == pytest.approx(11.885666666666667, rel=1e-12)
        assert "日历" in days["reason"] or "期间天数" in days["reason"]

    def test_all_metrics_carry_contract_fields(self):
        """所有指标必须带 单位/期间/口径/证据引用，且证据一一对应。"""
        out = _fin()
        for metric in out["metric_results"]:
            if metric["metric_id"] not in ("operating_cashflow_to_net_profit_ratio",
                                         "current_ratio", "quick_ratio"):
                assert metric["unit"], metric["metric_id"]
            assert metric["period"] == "2025年半年度", metric["metric_id"]
            assert metric["scope"] == "中国准则合并", metric["metric_id"]
            assert metric["evidence_ids"] == [f"E-{metric['metric_id']}"], metric["metric_id"]


class TestFactContractEdges:
    """契约边界：缺字段如实留空 + 声明单位透传。"""

    def test_missing_scope_unit_leaves_empty_and_pending(self):
        data = {k: v for k, v in CALC_JSON.items()
                if k not in ("scope", "amount_unit")}
        out = _fin(data)
        assert out["scope"] == ""
        assert out["amount_unit"] == ""
        assert out["unit_check"]["status"] == "pending"
        fact = next(f for f in out["facts"] if f["field"] == "revenue_current")
        assert fact["unit"] == ""

    def test_declared_unit_passthrough_keeps_ratios(self):
        data = dict(CALC_JSON, amount_unit="百万元")
        out = _fin(data)
        assert out["unit_check"]["status"] == "declared"
        fact = next(f for f in out["facts"] if f["field"] == "revenue_current")
        assert fact["unit"] == "百万元"
        m = _metrics(out)
        assert m["gross_margin_pct"]["value"] == pytest.approx(20.833333333333332, rel=1e-12)


class TestArComparisonPeriods:
    """应收账款比较期间：期初余额不得冒充上年同期。"""

    def test_opening_balance_variant_not_comparable(self):
        """中期报表期初应收余额不能与上年同期营收配比。"""
        data = dict(CALC_JSON)
        data.pop("accounts_receivable_same_period_previous", None)
        out = _fin(data)
        m = _metrics(out)
        pp = m["accounts_receivable_to_revenue_ratio_change_pp"]
        assert pp["value"] is None
        assert pp["status"] == "not_comparable"
        assert "期初应收余额不能与上年同期营收配比" in pp["reason"]

    def test_same_period_variant_calculated(self):
        data = dict(CALC_JSON)
        data["accounts_receivable_same_period_previous"] = 90_000_000_000
        out = _fin(data)
        m = _metrics(out)
        pp = m["accounts_receivable_to_revenue_ratio_change_pp"]
        assert pp["status"] == "calculated"
        assert pp["value"] is not None


class TestDenominatorBoundaries:
    """分母/基期边界必须保守降级，绝不硬算。"""

    def test_negative_previous_yoy_not_comparable(self):
        data = dict(CALC_JSON, net_profit_previous=-5_000_000_000)
        out = _fin(data)
        m = _metrics(out)
        metric = m["net_profit_yoy_change_pct"]
        assert metric["value"] is None
        assert metric["status"] == "not_comparable"
        assert "上期为零或负数时同比百分比不适用" in metric["reason"]
        assert out["indicators"].get("net_profit_yoy_change_desc") == "扭亏为盈"

    def test_zero_previous_yoy_not_comparable(self):
        data = dict(CALC_JSON, net_profit_previous=0)
        out = _fin(data)
        m = _metrics(out)
        metric = m["net_profit_yoy_change_pct"]
        assert metric["value"] is None
        assert metric["status"] == "not_comparable"

    def test_zero_total_assets_not_comparable(self):
        data = dict(CALC_JSON, total_assets_current=0)
        out = _fin(data)
        m = _metrics(out)
        metric = m["debt_to_asset_ratio_pct"]
        assert metric["value"] is None
        assert metric["status"] == "not_comparable"
        assert "总资产缺失或为零" in metric["reason"]

    def test_negative_net_profit_denominator_keeps_honest_ratio(self):
        """净利为负时 ocf/np 照算（负值），不得静默隐藏或掐成转移告警。"""
        data = dict(CALC_JSON, net_profit_current=-100_000_000_000)
        out = _fin(data)
        m = _metrics(out)
        metric = m["operating_cashflow_to_net_profit_ratio"]
        assert metric["value"] == pytest.approx(-1.8, rel=1e-6)
        assert metric["status"] == "calculated"
        assert not any("现金流与利润背离" in a for a in out["alerts"])

    def test_missing_net_assets_not_backfilled(self):
        data = dict(CALC_JSON)
        data.pop("net_assets_current", None)
        out = _fin(data)
        m = _metrics(out)
        metric = m["goodwill_to_net_assets_ratio_pct"]
        assert metric["value"] is None
        assert metric["status"] == "insufficient_data"
        assert "net_assets_current" in metric["reason"]

    def test_net_assets_provided_calculates(self):
        data = dict(CALC_JSON)
        data.pop("net_assets_current", None)
        data["net_assets_current"] = 15_000_000_000
        out = _fin(data)
        m = _metrics(out)
        metric = m["goodwill_to_net_assets_ratio_pct"]
        assert metric["value"] == pytest.approx(40.0, rel=1e-9)
        assert metric["status"] == "calculated"


class TestGrossReceivableSignal:
    """账面余额跨期信号：标注待核查，不当作同比结论。"""

    def test_gross_ar_cross_period_alert(self):
        data = dict(CALC_JSON)
        data["accounts_receivable_gross_current"] = 100_000_000_000
        data["accounts_receivable_gross_same_period_previous"] = 62_500_000_000
        data["_field_metadata"] = {
            "accounts_receivable_gross_current": {"period": "2025-06-30"},
            "accounts_receivable_gross_same_period_previous": {"period": "2024-06-30"},
        }
        out = _fin(data)
        m = _metrics(out)
        gm = m["accounts_receivable_gross_yoy_change_pct"]
        assert gm["value"] == pytest.approx(60.0, rel=1e-9)
        assert gm["status"] == "calculated"
        assert any("跨期" in a and "暂不作背离判断" in a for a in out["alerts"])


class TestValidationRules:
    '"""数据校验：合成案例通过，量级错误必须拦截，缺项不反算。"""'

    def _vd(self, data=None):
        return _invoke(validate_financial_data, data if data is not None else VD_JSON)

    def test_full_fixture_partial_but_no_failure(self):
        out = self._vd()
        dv = out["data_validation"]
        assert dv["failed_checks"] == 0
        assert dv["validation_result"] == "部分完成"
        # F1 后为四类校验：有效税率在 VD_JSON 缺税率字段 → insufficient_data
        # （passed=None），其余三类必须判定通过
        for item in dv["all_checks"]:
            if item["check"] == "有效税率合理性":
                assert item["passed"] is None
            else:
                assert item["passed"] is True

    def test_balance_sheet_facts_keep_page_locator(self):
        out = self._vd()
        bs = next(r for r in out["results"] if r["check"] == "资产负债表平衡")
        facts = {f["field"]: f for f in bs["facts"]}
        assert facts["total_assets_current"]["page"] == "11"
        assert facts["total_assets_current"]["locator"] == "合并资产负债表：资产总计"
        assert facts["total_liabilities_current"]["page"] == "12"
        assert facts["equity_total"]["page"] == "12"
        ev = bs["evidence"]
        assert ev["fact_ids"] == ["F-total_assets_current", "F-total_liabilities_current", "F-equity_total"]
        assert ev["metric_ids"] == ["validation_balance_sheet"]
        assert ev["verified"] is True

    def test_100x_magnitude_error_rejected(self):
        data = dict(VD_JSON)
        data["total_assets_current"] = 240_000_000_000_000
        data["total_liabilities_current"] = 96_000_000_000_000
        out = self._vd(data)
        dv = out["data_validation"]
        assert dv["validation_result"] == "未通过"
        assert dv["failed_checks"] == 1
        bs = next(r for r in out["results"] if r["check"] == "资产负债表平衡")
        assert bs["passed"] is False
        assert "勾稽显著不平衡" in bs["message"]

    def test_missing_equity_not_backcalculated(self):
        data = dict(VD_JSON)
        data.pop("equity_total", None)
        out = self._vd(data)
        dv = out["data_validation"]
        assert dv["validation_result"] == "部分完成"
        assert dv["failed_checks"] == 0
        bs = next(r for r in out["results"] if r["check"] == "资产负债表平衡")
        assert bs["passed"] is None
        assert bs["status"] == "insufficient_data"
        assert "不反算净资产" in bs["message"]


class TestLocalCaseNormalizationMechanism:
    '''本地案例修正机制（规则不入库）：注入合成规则模块验证委托与幂等。'''

    EXPECTED = (
        "关联方提供产品和服务占同类交易9.99%。"
        "该比例与关联采购占营业成本的内部30%筛查参考值分母不同，不作阈值比较。"
        "定价公允性、审批程序及资金流向仍需核查。"
    )

    def test_normalize_related_party_statement(self, synthetic_case_overrides):
        text = "关联采购占同类交易9.99%，未超过30%内部筛查参考值"
        assert _normalize_source_bound_text(text) == self.EXPECTED

    def test_normalize_is_idempotent(self, synthetic_case_overrides):
        once = _normalize_source_bound_text("关联采购占同类交易9.99%，未超过30%内部筛查参考值")
        assert _normalize_source_bound_text(once) == once

    def test_unrelated_text_untouched(self, synthetic_case_overrides):
        text = "公司主营业务为油气勘探开发。"
        assert _normalize_source_bound_text(text) == text
