"""整改计划「新增关键测试」中缺失的门禁路径回归。

计划第三节与第五节要求验证三条此前无用例覆盖的路径：

1. 两次一致但原文不支持：C2 两次判断一致只是自动采信的附加必要条件，仍须满足
   原文与规则校验；证据不可回指时不得采信，待处理原因须指向证据定位问题。
2. 模型分歧：两次判断未对齐时转人工复核，待处理原因须指向语义分歧。两类原因
   的补救动作不同（前者补证据定位，后者补语义判断），不得写反。
3. 关闭审查：保留有证据的事实与候选风险，但不得冒充「复核通过」。

用例同时锁定 _apply_review_gates 的原因分支方向，以及「已采信条目不写待处理
原因」，防止回归时把两类待复核原因写反、或给正式风险挂上待处理标注。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agents.agent import _apply_review_gates

# 可回指的证据：已核验 + 有原文摘录 + 有页码定位，满足 usable_evidence 全部条件
_TRACEABLE_EVIDENCE = {
    "evidence_id": "E1",
    "source_type": "local_calculation",
    "source_document": "2025年年度报告.pdf",
    "page": "72",
    "excerpt": "应收账款周转天数由 45 天上升至 88 天",
    "verified": True,
    "status": "verified",
}

_C1_COMPLETED = {
    "status": "completed",
    "overall_status": "completed",
    "phases": {
        "advocate": {"status": "completed"},
        "skeptic": {"status": "completed"},
        "arbiter": {"status": "completed"},
    },
}


def _report():
    """单条候选风险台账，证据可回指。"""
    return {
        "risk_details": [{
            "risk_id": "R001",
            "dimension": "financial_misstatement",
            "title": "应收账款周转显著放缓",
            "level": "重要",
            "evidence_ids": ["E1"],
            "evidence": "应收账款周转天数同比上升 43 天",
        }],
        "evidence": [dict(_TRACEABLE_EVIDENCE)],
    }


def _c2(state, evidence_ids_1, evidence_ids_2):
    """构造 C2 比较结果；state 取 consistent / disputed。"""
    return {
        "status": "completed",
        "overall_status": "consistent" if state == "consistent" else "pending_review",
        "checks": [{
            "risk_id": "R001",
            "state": state,
            "decision_1": "supported",
            "decision_2": "supported" if state == "consistent" else "not_supported",
            "evidence_ids_1": evidence_ids_1,
            "evidence_ids_2": evidence_ids_2,
        }],
    }


class TestC2ConsistencyIsNotEnough:
    """两次一致但原文不支持：不得自动采信。"""

    def test_consistent_but_untraceable_evidence_not_accepted(self):
        report = _report()
        _apply_review_gates(report, _c2("consistent", ["E-未登记"], ["E-未登记"]),
                            True, _C1_COMPLETED)
        risk = report["risk_details"][0]
        assert risk["formal_status"] == "unaccepted"
        assert risk["verification_status"] == "待复核"
        assert risk["pending_reason"] == "C2证据引用未能回指已核验证据目录"
        assert report["accepted_risk_details"] == []
        assert risk in report["pending_items"]

    def test_evidence_without_excerpt_is_pending_evidence_not_pending_review(self):
        """缺原文摘录即不可回指，归入待补证据，而不是语义待复核。"""
        report = _report()
        report["evidence"][0]["excerpt"] = ""
        _apply_review_gates(report, _c2("consistent", ["E1"], ["E1"]),
                            True, _C1_COMPLETED)
        risk = report["risk_details"][0]
        assert risk["formal_status"] == "unaccepted"
        assert risk["verification_status"] == "待补证据"
        assert risk["status"] == "pending_evidence"

    def test_consistent_with_traceable_evidence_is_accepted(self):
        """正向对照：一致且证据可回指才进入正式风险，且不携带待处理原因。"""
        report = _report()
        _apply_review_gates(report, _c2("consistent", ["E1"], ["E1"]),
                            True, _C1_COMPLETED)
        risk = report["risk_details"][0]
        assert risk["formal_status"] == "accepted"
        assert risk["verification_status"] == "C2一致"
        assert "pending_reason" not in risk
        assert risk in report["accepted_risk_details"]
        assert risk["level_status"] == "accepted"

    def test_consistent_negative_or_pending_decisions_are_not_accepted(self):
        """一致仅说明复核意见相同，不代表两次都支持风险成立。"""
        for decision in ("not_supported", "pending"):
            report = _report()
            c2 = _c2("consistent", ["E1"], ["E1"])
            c2["checks"][0].update(decision_1=decision, decision_2=decision)
            _apply_review_gates(report, c2, True, _C1_COMPLETED)
            risk = report["risk_details"][0]
            assert report["accepted_risk_details"] == []
            assert risk["pending_reason"] == "C2判断未支持该风险成立"
            assert report["review_gate"]["status"] == "not_passed"

    def test_acceptance_after_supplement_clears_old_pending_reason(self):
        report = _report()
        report["risk_details"][0]["pending_reason"] = "旧证据缺失"
        _apply_review_gates(report, _c2("consistent", ["E1"], ["E1"]), True, _C1_COMPLETED)
        assert "pending_reason" not in report["risk_details"][0]


class TestSemanticDivergenceGoesToHuman:
    """模型分歧：转人工复核，原因指向语义分歧。"""

    def test_disputed_c2_reports_divergence_reason(self):
        report = _report()
        _apply_review_gates(report, _c2("disputed", ["E1"], ["E1"]),
                            True, _C1_COMPLETED)
        risk = report["risk_details"][0]
        assert risk["formal_status"] == "unaccepted"
        assert risk["verification_status"] == "待复核"
        assert risk["pending_reason"] == "C2两次关键语义判断未明确对齐"
        assert report["review_gate"]["human_review_required"] is True
        assert "1项待复核提示" in report["review_gate"]["pending_reason"]

    def test_two_pending_reasons_are_not_interchanged(self):
        """证据问题与语义分歧的待处理原因必须可区分。"""
        evidence_problem, divergence = _report(), _report()
        _apply_review_gates(evidence_problem,
                            _c2("consistent", ["E-未登记"], ["E-未登记"]),
                            True, _C1_COMPLETED)
        _apply_review_gates(divergence, _c2("disputed", ["E1"], ["E1"]),
                            True, _C1_COMPLETED)
        reason_a = evidence_problem["risk_details"][0]["pending_reason"]
        reason_b = divergence["risk_details"][0]["pending_reason"]
        assert reason_a != reason_b
        assert "证据" in reason_a and "语义" in reason_b


class TestReviewDisabledDoesNotClaimPassed:
    """关闭审查不冒充复核通过。"""

    def test_disabled_review_gate_not_passed(self):
        report = _report()
        _apply_review_gates(report, {"status": "not_run"}, False, None)
        gate = report["review_gate"]
        assert gate["status"] == "not_passed"
        assert gate["review_enabled"] is False
        assert gate["c1_status"] == "not_run"
        assert gate["c2_overall_status"] == "not_run"
        assert "未标记为复核通过" in gate["note"]

    def test_disabled_review_keeps_candidates_but_marks_untested(self):
        report = _report()
        _apply_review_gates(report, {"status": "not_run"}, False, None)
        risk = report["risk_details"][0]
        assert risk["formal_status"] == "unaccepted"
        assert risk["status"] == "pending_review"
        assert risk["verification_status"] == "未测试"
        assert risk["level_status"] == "provisional"
        assert risk["suggested_level"] == "重要"
        assert report["accepted_risk_details"] == []
