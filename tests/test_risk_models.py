# -*- coding: utf-8 -*-
"""量化风险模型与审计意见识别测试（P3 新增工具）

背景：知识库文档明确要求引入 Altman Z-Score 与 Beneish M-Score 两个经典模型，
以及完整的审计意见类型库。本测试锁定：
- Z-Score 三个变体的选择规则与判定区间
- M-Score 八变量/五变量降级策略（TATA 缺失必须降级，因其权重最高）
- 审计意见识别的优先级（关键：严重意见不得被「我们认为…公允反映」误判为无保留）
- 两者接入综合评分后的抬升规则，且不传时向后兼容
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.risk_models import calculate_risk_models
from tools.audit_opinion import identify_audit_opinion
from tools.risk_scorer import calculate_comprehensive_score


def _models(data: dict) -> dict:
    return json.loads(calculate_risk_models.invoke({"financial_data_json": json.dumps(data)}))["risk_models"]


def _opinion(text: str) -> dict:
    return json.loads(identify_audit_opinion.invoke({"report_text": text}))


# 财务困境 + 利润与现金流严重背离的样本（制造业、无市值）
_DISTRESS = dict(
    industry="制造业", period="2025", total_assets=1000, total_assets_previous=900,
    current_assets=300, current_liabilities=400, total_liabilities=800,
    current_assets_previous=250,
    total_liabilities_previous=650, net_assets=200, retained_earnings=-50,
    pretax_profit=-60, interest_expense=20, revenue_current=500, revenue_previous=300,
    gross_profit=40, gross_profit_previous=60, accounts_receivable=250,
    accounts_receivable_previous=100, fixed_assets=200, fixed_assets_previous=210,
    depreciation=10, depreciation_previous=21, sga_expense=60, sga_expense_previous=40,
    net_profit_current=-60, operating_cash_flow=-150,
)
# 健康样本（高现金含量、低负债）
_HEALTHY = dict(
    industry="食品制造业", period="2025", total_assets=1000, total_assets_previous=900,
    current_assets=700, current_liabilities=200, total_liabilities=250,
    current_assets_previous=660,
    total_liabilities_previous=240, net_assets=750, retained_earnings=400,
    pretax_profit=300, interest_expense=0, revenue_current=600, revenue_previous=560,
    gross_profit=540, gross_profit_previous=500, accounts_receivable=20,
    accounts_receivable_previous=19, fixed_assets=200, fixed_assets_previous=195,
    depreciation=20, depreciation_previous=19, sga_expense=50, sga_expense_previous=47,
    net_profit_current=250, operating_cash_flow=300,
)


class TestAltmanZScore:

    def test_distress_sample_falls_in_distress_zone(self):
        z = _models(_DISTRESS)["altman_z_score"]
        assert z["available"] is True
        assert z["zone"] == "财务困境区"

    def test_healthy_sample_falls_in_safe_zone(self):
        z = _models(_HEALTHY)["altman_z_score"]
        assert z["zone"] == "财务安全区"

    def test_variant_private_when_no_market_cap(self):
        """制造业但无市值数据时应使用 Z'-Score（而非原始变体）。"""
        assert _models(_DISTRESS)["altman_z_score"]["variant"] == "private"

    def test_variant_original_with_market_cap(self):
        data = dict(_HEALTHY, market_cap=5000)
        assert _models(data)["altman_z_score"]["variant"] == "original"

    def test_variant_emerging_for_non_manufacturing(self):
        """非制造业应使用 Z''-Score，且不含 X5 资产周转率因子。"""
        z = _models(dict(_HEALTHY, industry="互联网"))["altman_z_score"]
        assert z["variant"] == "emerging"
        assert z["factors"]["X5_资产周转率"] == "不适用"

    def test_missing_total_assets_unavailable(self):
        assert _models({"revenue_current": 100, "industry": "制造业",
                        "period": "2025"})["altman_z_score"]["available"] is False

    def test_missing_period_or_industry_is_not_applicable(self):
        """适用性前置门禁：缺行业或本期期间时不计算，并登记缺失上下文。"""
        z = _models({"total_assets": 1000, "revenue_current": 500,
                     "current_assets": 300, "current_liabilities": 200,
                     "retained_earnings": 100, "total_liabilities": 400,
                     "net_assets": 600})["altman_z_score"]
        assert z["available"] is False
        assert z["status"] == "not_applicable"
        assert "行业" in z["missing_context"] and "本期期间" in z["missing_context"]
        assert "score" not in z, "适用条件未确认时不得输出分值"

    def test_single_missing_factor_fails_closed(self):
        """缺任一项必要因子即不输出分值（不以中性值补齐），并登记缺失因子。"""
        z = _models({"total_assets": 1000, "revenue_current": 500,
                     "current_assets": 300, "current_liabilities": 200,
                     "retained_earnings": 100, "total_liabilities": 400,
                     "net_assets": 600, "industry": "制造业",
                     "period": "2025"})["altman_z_score"]
        assert z["available"] is False
        assert "不适用" in z["reason"]
        assert z["missing_factors"], "缺失因子必须被登记，便于报告标注局限"

    def test_too_many_missing_factors_disables_model(self):
        """缺因子 ≥2 项时不输出分值（数据充分性门控）：避免缺字段填 0 导致
        Z 值在不同批次提取间漂移（实测缺陷：0.51/1.62/1.48 随机摇号）。"""
        z = _models({"total_assets": 1000, "revenue_current": 500,
                     "industry": "制造业", "period": "2025"})["altman_z_score"]
        assert z["available"] is False, "缺因子过多时应禁用模型，不输出误导性分值"
        assert "不适用" in z.get("reason", "") or "缺失因子过多" in z.get("reason", "")
        assert z["missing_factors"], "缺失因子须登记，便于报告说明不适用原因"


class TestBeneishMScore:

    def test_distress_sample_flags_manipulation(self):
        m = _models(_DISTRESS)["beneish_m_score"]
        assert m["available"] is True
        assert m["judgement"].startswith("存在")
        assert m["model"] == "八变量完整模型"

    def test_healthy_sample_no_manipulation(self):
        m = _models(_HEALTHY)["beneish_m_score"]
        assert m["judgement"].startswith("未发现")

    def test_degrades_to_five_variable_without_cashflow(self):
        """TATA 权重最高（4.679），缺经营现金流必须降级为五变量模型。"""
        data = {k: v for k, v in _DISTRESS.items() if k != "operating_cash_flow"}
        m = _models(data)["beneish_m_score"]
        assert m.get("model") == "五变量简化模型"

    def test_unavailable_when_missing_revenue(self):
        m = _models({"total_assets": 1000, "industry": "制造业",
                     "period": "2025"})["beneish_m_score"]
        assert m["available"] is False

    def test_too_many_missing_factors_no_conclusion(self):
        """缺失因子过多时不给结论，仅供人工参考（避免伪精确）。"""
        m = _models({"total_assets": 1000, "revenue_current": 500,
                     "revenue_previous": 400, "industry": "制造业",
                     "period": "2025"})["beneish_m_score"]
        assert m["available"] is False and "缺少必要因子" in m["reason"]


class TestCrossInterpretation:

    def test_dual_signal_escalates_to_major(self):
        c = _models(_DISTRESS)["cross_interpretation"]
        assert c["signal"] == "双重信号并存"
        assert c["level_floor"] == "重大"

    def test_no_signal_for_healthy(self):
        assert _models(_HEALTHY)["cross_interpretation"]["signal"] == "未触发模型预警"

    def test_interim_report_still_scores_with_interim_caveat(self):
        """中期报告照常出具分值（演示表格不留空），但必须附年度阈值的口径局限。"""
        models = _models(dict(_HEALTHY, period="2025年半年度"))
        for key in ("altman_z_score", "beneish_m_score"):
            assert models[key]["available"] is True
            assert models[key]["status"] == "calculated"
            assert isinstance(models[key]["score"], float)
            assert models[key]["interim_basis"] is True
            assert "中期" in models[key]["interim_note"]
            assert "年度" in models[key]["interim_note"]
            assert "交叉印证" in models[key]["interim_note"]
            assert "缺少" not in models[key]["interim_note"]
        cross = models["cross_interpretation"]
        assert cross["signal"] == "未触发模型预警"
        assert cross["interim_basis"] is True
        assert "中期" in cross["interpretation"]

    def test_report_year_alias_preserves_interim_context(self):
        data = {k: v for k, v in _HEALTHY.items() if k != "period"}
        data.update(report_year="2025年半年度", year="2025")
        result = json.loads(calculate_risk_models.invoke({"financial_data_json": json.dumps(data)}))
        assert result["period"] == "2025年半年度"
        assert result["risk_models"]["altman_z_score"]["status"] == "calculated"
        assert result["risk_models"]["altman_z_score"]["interim_basis"] is True
        assert result["risk_models"]["beneish_m_score"]["interim_basis"] is True
        assert all(m["period"] == "2025年半年度" for m in result["metric_results"])
        assert all(m["status"] == "calculated" for m in result["metric_results"])
        assert result["risk_findings"] == []


class TestAuditOpinionIdentification:

    def test_standard_unqualified(self):
        r = _opinion("我们认为，上述财务报表在所有重大方面按照企业会计准则的规定编制，公允反映了公司的财务状况。")
        assert r["audit_opinion"]["opinion_type"] == "无保留意见"
        assert r["audit_opinion"]["is_standard_opinion"] is True

    def test_qualified_not_misread_as_unqualified(self):
        """关键用例：保留意见报告中同样含「我们认为…公允反映」，必须判为保留。"""
        r = _opinion("我们认为，除上述事项的影响外，上述财务报表在所有重大方面公允反映了财务状况。"
                     "形成保留意见的基础：存货监盘受限。")
        assert r["audit_opinion"]["opinion_type"] == "保留意见"
        assert r["audit_opinion"]["is_standard_opinion"] is False

    def test_adverse_opinion(self):
        r = _opinion("我们认为，上述财务报表未能公允反映公司财务状况。形成否定意见的基础如下。")
        assert r["audit_opinion"]["opinion_type"] == "否定意见"
        assert r["audit_opinion"]["risk_level"] == "重大"

    def test_disclaimer_opinion(self):
        r = _opinion("我们无法对上述财务报表发表意见。形成无法表示意见的基础：账簿资料缺失。")
        assert r["audit_opinion"]["opinion_type"] == "无法表示意见"

    def test_emphasis_paragraph_and_going_concern(self):
        r = _opinion("审计意见为无保留意见。强调事项段：我们提醒财务报表使用者关注，"
                     "公司存在与持续经营相关的重大不确定性。关键审计事项：收入确认、商誉减值测试。")
        assert r["audit_opinion"]["opinion_type"] == "带强调事项段的无保留意见"
        assert r["going_concern"]["flagged"] is True
        assert r["key_audit_matters"]["found"] is True
        # 强调事项为持续经营时必须提示上调（不适用「不构成否决」宽免）
        assert any("持续经营" in a for a in r["linkage_alerts"])

    def test_auditor_change_linkage(self):
        r = _opinion("形成保留意见的基础：无法核实应收账款。公司本年度变更会计师事务所。")
        assert r["auditor_change"]["flagged"] is True
        assert any("意见购买" in a for a in r["linkage_alerts"])

    def test_short_text_returns_full_schema(self):
        """早退路径也必须返回完整 schema，否则下游取键会 KeyError。"""
        r = _opinion("")
        for key in ("audit_opinion", "going_concern", "key_audit_matters",
                    "auditor_change", "linkage_alerts"):
            assert key in r


class TestScoreIntegration:
    """新工具接入综合评分：抬升规则 + 向后兼容"""

    # N 补丁适配：空 dict 现视为维度未获取（score=None 提前返回），
    # 抬升测试须提供最小可用三维度结构（0 分基础）才能走到抬升分支
    _BASE = {
        "financial_analysis_json": json.dumps({"alerts": []}),
        "disclosure_check_json": json.dumps({"risk_score": 0}),
        "validation_json": json.dumps({"data_validation": {"failed_checks": 0}}),
    }

    def _score(self, **kw):
        return json.loads(calculate_comprehensive_score.invoke({**self._BASE, **kw}))

    def test_backward_compatible_without_new_args(self):
        r = self._score()
        assert r["escalation"] == 0.0 and r["escalation_reasons"] == []

    def test_dual_model_signal_escalates_20(self):
        rm = calculate_risk_models.invoke({"financial_data_json": json.dumps(_DISTRESS)})
        r = self._score(risk_models_json=rm)
        assert r["escalation"] == 20.0
        assert "双重信号" in r["escalation_reasons"][0]

    def test_non_standard_opinion_escalates_25(self):
        op = identify_audit_opinion.invoke({"report_text": "形成保留意见的基础：无法核实应收账款。"})
        r = self._score(audit_opinion_json=op)
        assert r["escalation"] == 25.0

    def test_healthy_models_no_escalation(self):
        rm = calculate_risk_models.invoke({"financial_data_json": json.dumps(_HEALTHY)})
        assert self._score(risk_models_json=rm)["escalation"] == 0.0

    def test_malformed_input_does_not_crash(self):
        r = self._score(risk_models_json="not-json", audit_opinion_json="[]")
        assert r["escalation"] == 0.0

    def test_escalation_reasons_are_traceable(self):
        """抬升必须给出可追溯理由，不能是黑箱调整。"""
        op = identify_audit_opinion.invoke({"report_text": "形成否定意见的基础：未能公允反映。"})
        r = self._score(audit_opinion_json=op)
        assert r["escalation_reasons"] and "否定意见" in r["escalation_reasons"][0]
def test_model_input_facts_keep_balance_dates_and_source_coordinates():
    from tools.risk_models import _model_facts
    facts, _ = _model_facts({'period':'2025年半年度','total_assets_previous':100,
        '_field_metadata':{'total_assets_previous':{'period':'2024-12-31','page':'49',
            'locator':'合并资产负债表','source_hash':'abc','unit':'人民币百万元'}}})
    fact=next(f for f in facts if f['field']=='total_assets_previous')
    assert fact['period']=='2024-12-31' and fact['page']=='49'
    assert fact['source_hash']=='abc' and fact['unit']=='人民币百万元'
