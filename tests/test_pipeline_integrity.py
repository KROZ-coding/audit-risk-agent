"""架构级数据管道完整性测试（辩论-台账回写 / 勾稽扣披露分 / V001 / 审计意见统一）。

锁定本轮架构修复：
1. 辩论仲裁支持新增条目回写（打通辩论-执行管道）
2. 勾稽差异联动扣披露分（打破 100 分幻觉）
3. V001 勾稽差异条目置信度非 0 + 归入财务报告
4. 审计意见状态统一（半年报"未经审计"）
5. 数据一致性 Debug 日志不印客户可见 PDF
"""
import json

import pytest


class TestArbiterAddNewRisk:
    """辩论仲裁新增条目回写（打通辩论-台账数据管道）。"""

    def test_arbiter_adds_new_risk_item(self):
        from agents.agent import _apply_arbiter_adjustments
        ledger = json.dumps({
            "risk_details": [
                {"risk_id": "R001", "dimension": "财务错报", "title": "已有风险", "level": "重要"},
            ]
        }, ensure_ascii=False)
        # 仲裁裁定新增 R003（应收账款激增），须含完整字段
        adjustments = [{
            "risk_id": "R003", "final_level": "重要",
            "title": "应收账款激增与现金流背离",
            "dimension": "财务错报",
            "evidence": "应收账款+67.2%，经营现金流仅+4.0%",
            "confidence": 0.75,
            "reason": "新增遗漏风险：应收账款激增与现金流背离",
        }]
        new_json, applied = _apply_arbiter_adjustments(ledger, adjustments)
        assert applied == 1
        data = json.loads(new_json)
        ids = [r["risk_id"] for r in data["risk_details"]]
        assert "R003" in ids, "仲裁新增条目须真实进入风险台账"
        new_item = next(r for r in data["risk_details"] if r["risk_id"] == "R003")
        assert new_item["source"] == "仲裁新增"
        assert new_item["level"] == "重要"

    def test_arbiter_rejects_incomplete_new_risk(self):
        """缺 title/dimension 的新增裁定须丢弃（防 LLM 幻觉新增假风险）。"""
        from agents.agent import _apply_arbiter_adjustments
        ledger = json.dumps({"risk_details": []}, ensure_ascii=False)
        adjustments = [{"risk_id": "R009", "final_level": "重要", "reason": "无标题无维度"}]
        new_json, applied = _apply_arbiter_adjustments(ledger, adjustments)
        assert applied == 0, "缺字段的仲裁新增须被丢弃"
        assert json.loads(new_json)["risk_details"] == []

    def test_arbiter_rejects_invalid_level(self):
        from agents.agent import _apply_arbiter_adjustments
        ledger = json.dumps({"risk_details": []}, ensure_ascii=False)
        adjustments = [{"risk_id": "R009", "final_level": "致命", "title": "x", "dimension": "财务错报"}]
        _new, applied = _apply_arbiter_adjustments(ledger, adjustments)
        assert applied == 0, "非白名单等级须被丢弃"


class TestReconciliationPenalty:
    """勾稽差异联动扣披露分（打破 100 分幻觉）。"""

    def test_failed_validation_penalizes_disclosure_score(self):
        from tools.disclosure_checker import check_disclosure_compliance
        text = ("公司基本情况：本公司为制造业企业。" + "主要会计数据指标显示营收稳定。" * 10)
        vd_with_failed = json.dumps({"data_validation": {"failed_checks": 1}}, ensure_ascii=False)
        r_pen = json.loads(check_disclosure_compliance.invoke(
            {"report_text": text, "validation_json": vd_with_failed}))
        r_clean = json.loads(check_disclosure_compliance.invoke({"report_text": text}))
        assert r_pen["compliance_score"] < r_clean["compliance_score"], (
            f"勾稽差异须扣减披露合规分：{r_pen['compliance_score']} vs {r_clean['compliance_score']}")
        assert r_pen["compliance_score"] < 100, "有勾稽差异时不得拿 100 分满分"

    def test_no_penalty_without_failed_checks(self):
        from tools.disclosure_checker import check_disclosure_compliance
        text = ("公司基本情况：本公司为制造业企业。" + "主要会计数据指标显示营收稳定。" * 10)
        vd_clean = json.dumps({"data_validation": {"failed_checks": 0}}, ensure_ascii=False)
        r_with = json.loads(check_disclosure_compliance.invoke(
            {"report_text": text, "validation_json": vd_clean}))
        r_none = json.loads(check_disclosure_compliance.invoke({"report_text": text}))
        # 无勾稽差异时不因勾稽扣分（与不传 validation 结果一致）
        assert r_with["compliance_score"] == r_none["compliance_score"]

    def test_reconciliation_failure_counts_as_check_item(self):
        """勾稽差异计入检查项（P7）：14/14 全绿却扣 15 分的观感矛盾须消失。"""
        from tools.disclosure_checker import check_disclosure_compliance
        text = ("公司基本情况：本公司为制造业企业。" + "主要会计数据指标显示营收稳定。" * 10)
        vd_failed = json.dumps({"data_validation": {"failed_checks": 1}}, ensure_ascii=False)
        r_clean = json.loads(check_disclosure_compliance.invoke({"report_text": text}))
        r_pen = json.loads(check_disclosure_compliance.invoke(
            {"report_text": text, "validation_json": vd_failed}))
        assert r_pen["compliance_score"] < r_clean["compliance_score"]
        assert r_pen["checked_items"] == r_clean["checked_items"] + 1, "勾稽校验须计入检查项"
        assert r_pen["passed_items"] == r_clean["passed_items"]
        assert r_pen["reconciliation_checks"] == 1


class TestFactDeduplication:
    """事实去重：V 系列（系统校验）与 R 系列（LLM 研判）同科目同事实时合并为证据附注。"""

    @staticmethod
    def _v_risk(vid="V001"):
        return {"risk_id": vid, "dimension": "数据可靠性风险",
                "title": "未分配利润变动(26800000000.00)与净利润-分红(35100000000.00)不一致，差额8300000000.00",
                "level": "重要", "source": "系统勾稽校验"}

    def test_same_subject_unique_candidate_merged(self):
        """V001 与唯一同科目 R 条目（未分配利润）→ 合并为证据附注，不新增独立条目。"""
        from agents.agent import _dedupe_v_risks
        details = [{
            "risk_id": "R005", "dimension": "信息披露合规风险", "level": "重要",
            "title": "未分配利润勾稽差异23.65%，存在数据可靠性风险",
            "evidence": "未分配利润变动35,800百万元，净利润减分红36,000百万元",
            "data_analysis": "勾稽差异可能源于中期报告口径差异",
        }]
        kept = _dedupe_v_risks([self._v_risk()], details)
        assert kept == [], "同科目唯一候选须合并，不得作为独立风险并列"
        r5 = details[0]
        assert "审计数据校验附注" in r5["evidence"]
        assert r5["v_evidence_ref"] == "V001"

    def test_no_subject_candidate_kept(self):
        """无同科目 R 条目时 V 保留为独立条目（防吞掉独立风险）。"""
        from agents.agent import _dedupe_v_risks
        details = [{"risk_id": "R001", "dimension": "关联交易风险", "level": "一般",
                    "title": "关联方借款占比高", "evidence": "关联方借款2,600亿元"}]
        kept = _dedupe_v_risks([self._v_risk()], details)
        assert len(kept) == 1 and kept[0]["risk_id"] == "V001"
        assert details[0].get("v_evidence_ref") is None

    def test_same_number_forces_merge_among_multi_candidates(self):
        """数字指纹重合时即使有多个同科目候选也合并（差异金额为强指纹）。"""
        from agents.agent import _dedupe_v_risks
        details = [
            {"risk_id": "R005", "dimension": "信息披露合规风险", "level": "一般",
             "title": "未分配利润勾稽差异", "evidence": "校验显示未分配利润差异8300000000.00，差异率23.65%"},
            {"risk_id": "R099", "dimension": "财务错报风险", "level": "一般",
             "title": "未分配利润波动", "evidence": "未分配利润余额较高"},
        ]
        kept = _dedupe_v_risks([self._v_risk()], details)
        assert kept == []
        merged = next(r for r in details if r["risk_id"] == "R005")
        assert merged["v_evidence_ref"] == "V001"
        assert "审计数据校验附注" in merged["evidence"]


class TestDisclosureReconciliationBackstop:
    """合规勾稽扣分系统层兜底（P9）：LLM 重复调用未传 validation 时强制恢复扣分。"""

    @staticmethod
    def _dc_without_reconciliation():
        """模拟 LLM 调用 check_disclosure_compliance 未传 validation_json 的输出。"""
        return json.dumps({
            "compliance_score": 92.9, "risk_score": 7.1,
            "checked_items": 14, "passed_items": 13, "reconciliation_checks": 0,
            "issues": ["关联交易披露：存在关联交易但未发现详细披露说明"],
            "audit_opinion": "未经审计（半年度报告）",
        }, ensure_ascii=False)

    @staticmethod
    def _validation_with_failure():
        return json.dumps({"data_validation": {"failed_checks": 1, "passed_checks": 2}},
                           ensure_ascii=False)

    def test_backstop_recalculates_when_llm_omitted_validation(self, monkeypatch):
        """validate 有勾稽失败而 dc 无扣分（reconciliation_checks=0）→ 系统强制重算。"""
        from agents.agent import _enforce_disclosure_reconciliation
        from langchain_core.messages import HumanMessage
        from unittest.mock import MagicMock
        import tools.disclosure_checker as dc_mod

        # StructuredTool 为 pydantic 冻结模型，不可 monkeypatch 实例属性，替换整个模块对象
        real_invoke = dc_mod.check_disclosure_compliance.invoke
        fake_tool = MagicMock()
        fake_tool.invoke.side_effect = lambda args: real_invoke(args)
        monkeypatch.setattr(dc_mod, "check_disclosure_compliance", fake_tool)

        tool_results = {
            "validate_financial_data": self._validation_with_failure(),
            "check_disclosure_compliance": self._dc_without_reconciliation(),
        }
        messages = [HumanMessage(content="公司基本情况：本公司为制造业企业。" * 20)]
        _enforce_disclosure_reconciliation(tool_results, messages)
        dc = json.loads(tool_results["check_disclosure_compliance"])
        assert dc["reconciliation_checks"] == 1, "兜底后必须带勾稽扣分标记"
        assert dc["compliance_score"] < 92.9, "勾稽差异须联动扣披露分"
        assert fake_tool.invoke.called, "必须触发系统重算"
        assert fake_tool.invoke.call_args[0][0].get("validation_json"), "重算必须传入 validation_json"

    def test_backstop_skips_when_already_penalized(self):
        """dc 已含勾稽扣分时不做多余重算（幂等）。"""
        from agents.agent import _enforce_disclosure_reconciliation
        from langchain_core.messages import HumanMessage
        dc_ok = json.loads(self._dc_without_reconciliation())
        dc_ok["reconciliation_checks"] = 1
        dc_ok["compliance_score"] = 77.9
        tool_results = {
            "validate_financial_data": self._validation_with_failure(),
            "check_disclosure_compliance": json.dumps(dc_ok, ensure_ascii=False),
        }
        before = tool_results["check_disclosure_compliance"]
        _enforce_disclosure_reconciliation(tool_results, [HumanMessage(content="x" * 500)])
        assert tool_results["check_disclosure_compliance"] == before

    def test_backstop_reuses_p1_injected_version(self):
        """messages 中存在 P1 注入的带扣分版本（tool_call_id=pre_d）时零成本复用。"""
        from agents.agent import _enforce_disclosure_reconciliation
        from langchain_core.messages import HumanMessage, ToolMessage
        dc_ok = json.loads(self._dc_without_reconciliation())
        dc_ok["reconciliation_checks"] = 1
        dc_ok["compliance_score"] = 77.9
        p1_version = json.dumps(dc_ok, ensure_ascii=False)
        tool_results = {
            "validate_financial_data": self._validation_with_failure(),
            "check_disclosure_compliance": self._dc_without_reconciliation(),
        }
        messages = [
            HumanMessage(content="x" * 500),
            ToolMessage(content=p1_version, name="check_disclosure_compliance",
                        tool_call_id="pre_d", id="pre_td"),
        ]
        _enforce_disclosure_reconciliation(tool_results, messages)
        assert tool_results["check_disclosure_compliance"] == p1_version


class TestIndicatorUnitScaleBackstop:
    """指标金额量级补偿（P10）：LLM 自行调用按百万体系抄填时用单位声明换算。"""

    def test_million_scale_indicators_fixed_by_declared_unit(self):
        """net_profit_current=80000（<1e8 可疑）+ 文本声明百万元 → 换算为元。"""
        from agents.agent import _enforce_indicator_unit_scale
        from langchain_core.messages import HumanMessage
        ind = {"revenue_yoy_change_pct": -6.25, "net_profit_current": 80000,
               "net_profit_previous": 85000, "gross_margin_pct": 20.83}
        tool_results = {"calculate_financial_indicators": json.dumps({"indicators": ind})}
        msgs = [HumanMessage(content="（单位：人民币百万元）公司主营业务为油气勘探。" * 5)]
        _enforce_indicator_unit_scale(tool_results, msgs)
        out = json.loads(tool_results["calculate_financial_indicators"])["indicators"]
        assert out["net_profit_current"] == 80000 * 1e6
        assert out["net_profit_previous"] == 85000 * 1e6
        assert out["revenue_yoy_change_pct"] == -6.25  # 比率字段不动
        assert out["gross_margin_pct"] == 20.83

    def test_skips_when_scale_already_plausible(self):
        """金额已为元量级（>=1e8）时不触发补偿（幂等）。"""
        from agents.agent import _enforce_indicator_unit_scale
        from langchain_core.messages import HumanMessage
        ind = {"net_profit_current": 9.4e10}
        tool_results = {"calculate_financial_indicators": json.dumps({"indicators": ind})}
        before = tool_results["calculate_financial_indicators"]
        _enforce_indicator_unit_scale(
            tool_results, [HumanMessage(content="单位：人民币百万元" * 5)])
        assert tool_results["calculate_financial_indicators"] == before

    def test_skips_without_unit_declaration(self):
        """文本无单位声明时不猜测（保守）。"""
        from agents.agent import _enforce_indicator_unit_scale
        from langchain_core.messages import HumanMessage
        ind = {"net_profit_current": 80000}
        tool_results = {"calculate_financial_indicators": json.dumps({"indicators": ind})}
        before = tool_results["calculate_financial_indicators"]
        _enforce_indicator_unit_scale(
            tool_results, [HumanMessage(content="公司主营业务为油气勘探开发。" * 5)])
        assert tool_results["calculate_financial_indicators"] == before


class TestParentNetProfitBackstop:
    """未分配利润勾稽归母口径兜底（P11）：合并净利润假阳性被系统重算消除。"""

    @staticmethod
    def _validate_with_consolidated_net_profit():
        """模拟 LLM 用合并净利润（80000 百万）调用 validate 的输出。"""
        return json.dumps({"data_validation": {
            "all_checks": [
                {"check": "资产负债表平衡", "passed": True},
                {"check": "现金流勾稽", "passed": True},
                {"check": "未分配利润一致性", "passed": False,
                 "net_profit": 80000.0, "net_profit_note": "(合并口径)",
                 "retained_earnings_begin": 900000.0, "retained_earnings_end": 935800.0,
                 "dividends": 36000.0, "expected_change": 44000.0,
                 "difference": 8200.0, "difference_pct": "18.64%",
                 "message": "未分配利润变动(35800.00)与合并口径净利润-分红(44000.00)不一致"},
            ],
            "failed_checks": 1, "passed_checks": 2, "validation_result": "未通过",
            "risks": [{"risk_id": "V001", "title": "未分配利润变动(35800.00)与净利润-分红不一致"}],
        }}, ensure_ascii=False)

    def test_consolidated_net_profit_recomputed_with_parent(self):
        """合并口径勾稽失败 + 年报文本含归母净利润 → 重算为通过并移除 V 风险。"""
        from agents.agent import _enforce_parent_net_profit_validation
        from langchain_core.messages import HumanMessage
        tool_results = {"validate_financial_data": self._validate_with_consolidated_net_profit()}
        msgs = [HumanMessage(content=("除特别注明外，金额单位为人民币百万元。"
                                      "归属于母公司股东的净利润 72,000 76,000 75,000 (5.3)"))]
        _enforce_parent_net_profit_validation(tool_results, msgs)
        vd = json.loads(tool_results["validate_financial_data"])["data_validation"]
        re_chk = next(c for c in vd["all_checks"] if "未分配利润" in c["check"])
        assert re_chk["passed"] is True, "归母口径勾稽应通过"
        assert "归母" in re_chk["net_profit_note"]
        assert re_chk["difference_pct"] == "0.56%"
        assert vd["failed_checks"] == 0
        assert vd["risks"] == [], "被修复的 V 风险应从 risks 移除"

    def test_skips_when_parent_scale_already_used(self):
        """已用归母口径或已通过时不重复重算（幂等）。"""
        from agents.agent import _enforce_parent_net_profit_validation
        from langchain_core.messages import HumanMessage
        vd_ok = json.loads(self._validate_with_consolidated_net_profit())
        re_chk = next(c for c in vd_ok["data_validation"]["all_checks"] if "未分配利润" in c["check"])
        re_chk["net_profit_note"] = "(归母口径)"
        before = json.dumps(vd_ok, ensure_ascii=False)
        tool_results = {"validate_financial_data": before}
        _enforce_parent_net_profit_validation(
            tool_results, [HumanMessage(content="归属于母公司股东的净利润 84,007")])
        assert tool_results["validate_financial_data"] == before

    def test_skips_without_parent_profit_in_text(self):
        """年报文本无归母净利润表述时不猜测（保守）。"""
        from agents.agent import _enforce_parent_net_profit_validation
        from langchain_core.messages import HumanMessage
        tool_results = {"validate_financial_data": self._validate_with_consolidated_net_profit()}
        before = tool_results["validate_financial_data"]
        _enforce_parent_net_profit_validation(
            tool_results, [HumanMessage(content="公司主营业务为油气勘探开发。" * 5)])
        assert tool_results["validate_financial_data"] == before

    def test_false_positive_risk_cleaned_from_ledger(self):
        """归母口径修正后，台账中基于合并口径勾稽失败生成的条目移入备查录。"""
        from agents.agent import _drop_false_positive_reconciliation_risks
        report = {"risk_details": [
            {"risk_id": "R001", "title": "应收账款激增", "evidence": "应收 +67.18%"},
            {"risk_id": "R003", "title": "未分配利润勾稽不一致，数据可靠性存疑",
             "evidence": "未分配利润变动35,800百万元 vs 合并净利润-分红47,911百万元，"
                         "差额9,789百万元，差异率18.64%", "level": "重要", "confidence": 0.9},
        ]}
        _drop_false_positive_reconciliation_risks(report)
        ids = [r["risk_id"] for r in report["risk_details"]]
        assert "R003" not in ids, "合并口径勾稽假阳性条目须移出风险明细"
        assert "R001" in ids, "无关条目不得误删"
        assert any(r["risk_id"] == "R003" for r in report.get("excluded_items", [])), \
            "被清洗条目须进入备查录（可追溯）"

    def test_false_positive_cleaned_without_merge_keyword(self):
        """evidence 未写"合并"字样（仅"净利润-分红"）时同样清洗（P11b 强化）。"""
        from agents.agent import _drop_false_positive_reconciliation_risks
        report = {"risk_details": [
            {"risk_id": "R003", "title": "未分配利润变动与净利润-分红不一致，存在数据可靠性风险",
             "evidence": "未分配利润变动(35800.00)与净利润-分红(44000.00)不一致，差额8200.00",
             "level": "重要", "confidence": 0.9},
            {"risk_id": "R002", "title": "存贷双高", "evidence": "货币资金与利息收支不匹配"},
        ]}
        _drop_false_positive_reconciliation_risks(report)
        ids = [r["risk_id"] for r in report["risk_details"]]
        assert "R003" not in ids and "R002" in ids


class TestV001Integrity:
    """V001 勾稽差异条目：置信度非 0 + 归入财务报告。"""

    def test_v001_has_confidence_and_source(self):
        from tools.data_validator import validate_financial_data
        # 构造勾稽不平的数据（未分配利润变动 与 净利润-分红 不一致）
        data = json.dumps({
            "total_assets": 1000, "total_liabilities": 400, "net_assets": 600,
            "net_profit": 100, "operating_cashflow": 50,
            "retained_earnings_begin": 200, "retained_earnings_end": 500, "dividends": 0,
        }, ensure_ascii=False)
        result = json.loads(validate_financial_data.invoke({"financial_data_json": data}))
        risks = (result.get("data_validation") or {}).get("risks", [])
        if risks:  # 有未通过项时验证置信度与来源
            for r in risks:
                assert r.get("confidence", 0) > 0, "V001 置信度不得为 0（僵尸风险）"
                assert r.get("source") == "系统勾稽校验"
                assert r.get("dimension") == "数据可靠性风险"

    def test_data_reliability_in_financial_dims(self):
        from tools.pdf_export import FINANCIAL_DIMS, _split_risks
        risks = [{"risk_id": "V001", "dimension": "数据可靠性风险", "level": "重要"}]
        assert _split_risks(risks, FINANCIAL_DIMS), (
            "数据可靠性风险（V001）须归入财务健康诊断报告，消除子报告丢包")


class TestAuditOpinionConsistency:
    """审计意见状态统一（半年报"未经审计"前后端一致）。"""

    def test_half_year_report_identified(self):
        from tools.audit_opinion import identify_audit_opinion
        text = "本公司 2025 年半年度报告。" + "主要财务数据如下。" * 20
        r = json.loads(identify_audit_opinion.invoke({"report_text": text}))
        op = r["audit_opinion"]
        assert op["identified"] is True
        assert "未经审计" in op["opinion_type"] and "半年度" in op["opinion_type"]

    def test_disclosure_section_prefers_audit_opinion_tool(self):
        from tools.pdf_export import _disclosure_section_body, _build_styles, _register_chinese_font
        st = _build_styles(_register_chinese_font())
        dc = json.dumps({"compliance_score": 85, "risk_score": 15, "checked_items": 10,
                         "passed_items": 9, "issues": [], "audit_opinion": "未识别"},
                        ensure_ascii=False)
        ao = json.dumps({"audit_opinion": {"identified": True,
                                           "opinion_type": "未经审计（半年度报告）"}},
                        ensure_ascii=False)
        body = _disclosure_section_body(dc, st, _register_chinese_font(), audit_opinion_json=ao)
        text = "".join(str(getattr(el, "text", el)) for el in body)
        assert "未经审计（半年度报告）" in text, (
            "合规报告审计意见须优先用 identify_audit_opinion 结果，而非 disclosure_checker 的'未识别'")

class TestSystemSelfAuditStrip:
    """M 补丁：系统自审条目剥离（自指词+财务锚门禁+issuer 豁免）。"""

    def test_self_ref_without_anchor_stripped(self):
        from agents.agent import _strip_system_self_audit_items
        report = {"risk_details": [
            {"risk_id": "R006", "title": "内部评分矛盾及勾稽差异可能涉及信息披露不准确",
             "evidence": "系统综合评分31.5分（中等风险）与台账内部评分9分（低风险）矛盾",
             "level": "重要", "confidence": 0.55},
            {"risk_id": "R001", "title": "应收账款激增", "evidence": "应收+67.18%"},
        ]}
        _strip_system_self_audit_items(report)
        ids = [r["risk_id"] for r in report["risk_details"]]
        assert "R006" not in ids and "R001" in ids
        notes = report.get("system_quality_notes", [])
        assert any(r["risk_id"] == "R006" for r in notes)

    def test_issuer_domain_not_stripped(self):
        """issuer 域高频短语（内部控制/内部审批）不得被误剥离。"""
        from agents.agent import _strip_system_self_audit_items
        report = {"risk_details": [
            {"risk_id": "R005", "title": "公司内部审批流程缺失，未见授权文件",
             "evidence": "未见授权文件", "level": "一般"},
        ]}
        _strip_system_self_audit_items(report)
        ids = [r["risk_id"] for r in report["risk_details"]]
        assert "R005" in ids, "内部控制类 issuer 短语不得触发剥离"
        assert "system_quality_notes" not in report

    def test_self_ref_with_anchor_kept(self):
        """含财务锚（数字+单位）的条目即使有自指词也不剥离（防误伤真实风险）。"""
        from agents.agent import _strip_system_self_audit_items
        report = {"risk_details": [
            {"risk_id": "R002", "title": "存贷双高异常",
             "evidence": "货币资金284,493百万元 vs 短期借款46,409百万元；系统数据显示利息收支不匹配",
             "level": "重要"},
        ]}
        _strip_system_self_audit_items(report)
        ids = [r["risk_id"] for r in report["risk_details"]]
        assert "R002" in ids
