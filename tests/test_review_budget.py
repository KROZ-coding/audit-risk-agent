"""审查预算与补证轮次上限回归测试。

计划要求：单次超时 90 秒、阶段总限时 600 秒、最多 20 次调用，重试计入限额，
配置运行前固定并记录；补证循环设上限（默认一轮）；预算耗尽转人工复核，
不得标记为「复核通过」，也不得靠反复追加投票求一致。
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import agents.agent as agent_mod
from agents.agent import (
    REVIEW_CALL_TIMEOUT,
    REVIEW_MAX_CALLS,
    REVIEW_STAGE_TIMEOUT,
    REVIEW_SUPPLEMENT_ROUNDS,
    ReviewBudget,
    ReviewBudgetExceeded,
    _apply_review_gates,
    _invoke_llm_with_retry,
)


class _Clock:
    """可控时钟：按需推进，避免测试依赖真实耗时。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FailingLLM:
    def __init__(self, fail_times=99):
        self.calls = 0
        self.fail_times = fail_times

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"瞬时故障 {self.calls}")
        return f"ok-{self.calls}"


class TestBudgetConfig:
    """限额配置固定并随结果记录。"""

    def test_planned_defaults(self):
        budget = ReviewBudget()
        snapshot = budget.snapshot()
        assert snapshot["max_calls"] == REVIEW_MAX_CALLS == 20
        assert snapshot["call_timeout_seconds"] == REVIEW_CALL_TIMEOUT == 90
        assert snapshot["stage_timeout_seconds"] == REVIEW_STAGE_TIMEOUT == 600
        assert snapshot["supplement_round_limit"] == REVIEW_SUPPLEMENT_ROUNDS == 1

    def test_snapshot_records_usage(self):
        budget = ReviewBudget(clock=_Clock())
        budget.note("C2·隔离判断1", True)
        budget.note("C2·隔离判断2", False, "超时")
        snapshot = budget.snapshot()
        assert snapshot["calls_used"] == 2 and snapshot["calls_remaining"] == 18
        assert snapshot["call_log"][1]["ok"] is False
        assert snapshot["call_log"][1]["error"] == "超时"
        assert snapshot["exhausted"] is False


class TestBudgetEnforcement:
    """重试计入限额；次数或限时耗尽即抛异常转人工。"""

    def test_retries_count_toward_limit(self):
        budget = ReviewBudget(max_calls=2, clock=_Clock())
        with pytest.raises(Exception):
            _invoke_llm_with_retry(_FailingLLM(fail_times=99), [], label="测试", budget=budget)
        # 首次 + 重试共 2 次机会，第 3 次进循环即被限额拦下
        assert len(budget.calls) == 2
        assert budget.snapshot()["exhausted"] is True

    def test_call_refused_when_calls_exhausted(self):
        budget = ReviewBudget(max_calls=1, clock=_Clock())
        budget.note("占位", True)
        llm = MagicMock()
        with pytest.raises(ReviewBudgetExceeded) as exc:
            _invoke_llm_with_retry(llm, [], label="测试", budget=budget)
        assert "上限" in str(exc.value)
        llm.invoke.assert_not_called()

    def test_call_refused_when_stage_timed_out(self):
        clock = _Clock()
        budget = ReviewBudget(stage_timeout=600, clock=clock)
        clock.advance(601)
        assert budget.can_call() is False
        assert "总限时" in budget.exhausted_reason()
        llm = MagicMock()
        with pytest.raises(ReviewBudgetExceeded):
            _invoke_llm_with_retry(llm, [], label="测试", budget=budget)
        llm.invoke.assert_not_called()

    def test_successful_call_recorded_once(self):
        budget = ReviewBudget(clock=_Clock())
        assert _invoke_llm_with_retry(_FailingLLM(fail_times=0), [], label="测试",
                                      budget=budget) == "ok-1"
        assert budget.snapshot()["calls_used"] == 1

    def test_no_budget_keeps_legacy_behaviour(self):
        """未传预算时保持原重试语义（其他调用方不受影响）。"""
        assert _invoke_llm_with_retry(_FailingLLM(fail_times=1), [], label="测试") == "ok-2"


class TestSupplementRounds:
    """补证循环上限：默认一轮，超限返回 False 由调用方转人工。"""

    def test_first_round_allowed_then_capped(self):
        budget = ReviewBudget()
        assert budget.begin_supplement_round() is True
        assert budget.begin_supplement_round() is False
        assert budget.supplement_rounds_used == 1
        assert budget.snapshot()["supplement_round_limit"] == 1

    def test_zero_rounds_disables_supplement(self):
        budget = ReviewBudget(supplement_rounds=0)
        assert budget.begin_supplement_round() is False


class TestReviewGateBudgetSurface:
    """门禁落盘：预算用量可见，耗尽/补证用尽必须转人工且不冒充通过。"""

    def _report(self):
        return {"risk_details": [{"risk_id": "R001", "dimension": "财务错报风险",
                                  "level": "重要", "evidence_ids": ["E1"],
                                  "evidence": "证据"}]}

    def test_budget_recorded_and_gate_not_passed(self):
        budget = ReviewBudget(max_calls=1)
        budget.note("C1·风险关注方", True)
        report = self._report()
        _apply_review_gates(
            report,
            {"status": "completed", "overall_status": "consistent", "checks": []},
            True,
            {"status": "completed", "overall_status": "completed",
             "phases": {"advocate": {"status": "completed"}},
             "budget": budget.snapshot()})
        gate = report["review_gate"]
        assert gate["review_budget"]["calls_used"] == 1
        assert gate["human_review_required"] is True
        assert gate["status"] == "not_passed"
        assert "上限" in gate["pending_reason"]

    def test_supplement_exhausted_blocks_pass(self):
        report = self._report()
        _apply_review_gates(
            report,
            {"status": "completed", "overall_status": "consistent", "checks": []},
            True,
            {"status": "completed", "overall_status": "completed",
             "phases": {"advocate": {"status": "completed"}},
             "supplement_exhausted": True})
        gate = report["review_gate"]
        assert gate["status"] == "not_passed"
        assert "补证轮次" in gate["pending_reason"]

    def test_normal_run_reports_budget_without_human_flag(self):
        report = self._report()
        _apply_review_gates(
            report,
            {"status": "completed", "overall_status": "consistent", "checks": []},
            True,
            {"status": "completed", "overall_status": "completed",
             "phases": {"advocate": {"status": "completed"}},
             "budget": ReviewBudget().snapshot()})
        gate = report["review_gate"]
        assert gate["human_review_required"] is False
        assert gate["review_budget"]["max_calls"] == 20


class TestC2BudgetPath:
    """C2 两次隔离判断共用预算；耗尽时状态为 budget_exhausted 并要求人工复核。"""

    def test_c2_records_actual_call_count(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        wrapper = object.__new__(agent_mod._AgentWrapper)
        wrapper._agent = MagicMock()
        monkeypatch.setattr(agent_mod._AgentWrapper, "_build_review_llm",
                            lambda self: MagicMock(invoke=lambda msgs: MagicMock(
                                content='{"checks": []}')))
        result = wrapper._run_c2('{"risk_details": []}', ReviewBudget())
        assert result["status"] == "completed"
        assert result["calls"] == 2

    def test_c2_budget_exhausted_requires_human(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        wrapper = object.__new__(agent_mod._AgentWrapper)
        wrapper._agent = MagicMock()
        budget = ReviewBudget(max_calls=1)
        budget.note("占位", True)
        monkeypatch.setattr(agent_mod._AgentWrapper, "_build_review_llm",
                            lambda self: MagicMock())
        result = wrapper._run_c2('{"risk_details": []}', budget)
        assert result["status"] == "budget_exhausted"
        assert result["human_review_required"] is True
        assert "预算耗尽" in result["pending_reason"]
