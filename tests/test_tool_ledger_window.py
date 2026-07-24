"""工具链记账台账与消息滑窗解耦的单元测试

背景（回归防护）：
    _windowed_messages 会将 messages 裁剪至 MAX_MESSAGES 条以限制 LLM 上下文长度。
    在 invoke / ainvoke 路径下，_post_process 读取的是裁剪后的 result["messages"]。
    若早期的 validate_financial_data 被裁剪、而后续 calculate_financial_indicators
    仍在窗口内，则：
      ① 顺序门禁会误判「calculate 缺失前置 validate」→ 抛 ToolCallOrderViolation；
      ② 综合评分兜底读到空的 validate 结果 → 评分失真。

    修复：AgentState 增加不受滑窗裁剪的 tool_ledger 状态字段，在裁剪前由
    post_model_hook 累积完整链路（顺序 + 各工具最近结果），供 _enforce_tool_call_order
    与综合评分兜底读取。本测试构造 >40 条、早期含 validate/calculate 的场景加以验证。
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.agent as agent_mod
from agents.agent import (
    _AgentWrapper,
    _windowed_messages,
    _accumulate_tool_ledger,
    _merge_tool_ledger,
    MAX_MESSAGES,
)
from tools.domain_guard import ToolCallOrderViolation


# ── 各工具的真实结果内容（用于验证综合评分读到真实结果而非空值）──
VALIDATE_CONTENT = json.dumps({"failed_checks": 2, "passed": False}, ensure_ascii=False)
CALC_CONTENT = json.dumps({"alerts": ["资产收益率异常"]}, ensure_ascii=False)
DISCLOSURE_CONTENT = json.dumps({"risk_score": 40}, ensure_ascii=False)
SEARCH_CONTENT = json.dumps({"hits": ["信息披露管理办法第X条"]}, ensure_ascii=False)
SCORE_CONTENT = json.dumps({"score": 30}, ensure_ascii=False)

FINAL_RISK_JSON = (
    '分析完成，风险台账如下：\n'
    '{"company_info": {"company_name": "测试公司", "report_year": "2025"}, '
    '"risk_details": [{"dimension": "财务错报风险", "level": "重大"}], '
    '"overall_assessment": "存在一定风险信号"}'
)


def _tool_msg(name, content, idx):
    """构造带稳定 id / tool_call_id 的 ToolMessage。"""
    return ToolMessage(content=content, name=name, tool_call_id=f"call_{name}_{idx}", id=f"tm_{name}_{idx}")


def _build_full_messages():
    """构造 >40 条消息：validate 处于最前（将被滑窗裁剪），calculate 及后续工具在窗口内。

    真实调用顺序完全合法（validate → calculate → disclosure → search → score → 导出对），
    仅因滑窗裁剪使窗口内“看起来”缺失 validate，用以复现误判场景。
    """
    msgs = []
    msgs.append(HumanMessage(content="请分析测试公司 2025 年报"))                  # 0
    msgs.append(AIMessage(content="先校验财务数据", additional_kwargs={}))         # 1
    msgs.append(_tool_msg("validate_financial_data", VALIDATE_CONTENT, 0))         # 2  早期 validate
    # 填充大量中间消息，把 validate 挤出滑窗窗口
    for i in range(20):
        msgs.append(AIMessage(content=f"中间推理 {i}"))
        msgs.append(HumanMessage(content=f"补充说明 {i}"))                         # 3..42（40 条）
    msgs.append(AIMessage(content="计算财务指标"))                                 # 43
    msgs.append(_tool_msg("calculate_financial_indicators", CALC_CONTENT, 0))      # 44  窗口内 calculate
    msgs.append(AIMessage(content="检查披露合规"))                                 # 45
    msgs.append(_tool_msg("check_disclosure_compliance", DISCLOSURE_CONTENT, 0))   # 46
    msgs.append(_tool_msg("search_regulations", SEARCH_CONTENT, 0))                # 47
    msgs.append(_tool_msg("calculate_comprehensive_score", SCORE_CONTENT, 0))      # 48
    msgs.append(_tool_msg("export_pdf_report", "pdf_url", 0))                       # 49
    msgs.append(_tool_msg("export_excel_report", "excel_url", 0))                   # 50
    msgs.append(AIMessage(content=FINAL_RISK_JSON))                                # 51
    return msgs


def _build_ledger(full_messages):
    """按 post_model_hook 的真实语义，在裁剪前从全量消息累积出完整台账。"""
    update = _accumulate_tool_ledger({"messages": full_messages, "tool_ledger": {}})
    return _merge_tool_ledger({}, update["tool_ledger"])


class TestWindowScenarioIsReal:
    """先证明构造的场景确实触发滑窗裁剪（validate 被丢弃、calculate 保留）。"""

    def test_window_drops_early_validate_keeps_calculate(self):
        full = _build_full_messages()
        assert len(full) > MAX_MESSAGES
        windowed = _windowed_messages([], full)
        assert len(windowed) <= MAX_MESSAGES
        windowed_tools = [m.name for m in windowed if isinstance(m, ToolMessage)]
        # 早期 validate 被裁剪，后续 calculate 仍在窗口内 —— 正是误判触发条件
        assert "validate_financial_data" not in windowed_tools
        assert "calculate_financial_indicators" in windowed_tools


class TestLedgerDecoupling:
    """台账与滑窗解耦：顺序门禁与结果读取应基于完整台账而非裁剪后的 messages。"""

    def test_ledger_preserves_full_order_and_results(self):
        full = _build_full_messages()
        ledger = _build_ledger(full)
        # 台账序列完整且以 validate 起始、calculate 紧随其后
        assert ledger["seq"][0] == "validate_financial_data"
        assert ledger["seq"][1] == "calculate_financial_indicators"
        # 台账保留了被裁剪工具的真实结果
        assert ledger["results"]["validate_financial_data"] == VALIDATE_CONTENT

    def test_gather_prefers_ledger_over_windowed_messages(self):
        full = _build_full_messages()
        ledger = _build_ledger(full)
        windowed = _windowed_messages([], full)
        wrapper = _AgentWrapper(object())
        called_seq, results, called = wrapper._gather_tool_bookkeeping(
            {"messages": windowed, "tool_ledger": ledger}, windowed
        )
        # 顺序取自台账：validate 早于 calculate
        assert called_seq.index("validate_financial_data") < called_seq.index("calculate_financial_indicators")
        # 综合评分所需结果均可从台账读到真实值（validate 已被窗口裁剪仍可读）
        assert results["validate_financial_data"] == VALIDATE_CONTENT
        assert results["calculate_financial_indicators"] == CALC_CONTENT
        # 已调用集合并入台账，早期导出工具不被误判为未调用
        assert "export_pdf_report" in called and "export_excel_report" in called

    def test_enforce_order_passes_with_ledger(self):
        full = _build_full_messages()
        ledger = _build_ledger(full)
        windowed = _windowed_messages([], full)
        wrapper = _AgentWrapper(object())
        called_seq, _, _ = wrapper._gather_tool_bookkeeping(
            {"messages": windowed, "tool_ledger": ledger}, windowed
        )
        # 基于完整台账序列，不应误触发顺序违规
        wrapper._enforce_tool_call_order(called_seq)


class TestPostProcessWithLedger:
    """_post_process 端到端：>40 条消息 + 台账场景下不误判且综合评分读到真实结果。"""

    def test_post_process_no_false_violation_and_reads_real_results(self, monkeypatch):
        # 关闭多智能体辩论，避免触发 LLM 网络调用
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", False)

        full = _build_full_messages()
        ledger = _build_ledger(full)
        windowed = _windowed_messages([], full)
        wrapper = _AgentWrapper(object())

        # 导出工具已在台账中 → 不触发兜底导出（无需文件系统 / 网络）
        result = wrapper._post_process({"messages": windowed, "tool_ledger": ledger})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        assert "<!--COMPREHENSIVE_SCORE-->" in final_ai.content

        # 解析综合评分，验证读到了真实的 validate 结果（failed_checks=2 → 校验风险=60）
        marker = "<!--COMPREHENSIVE_SCORE-->"
        score_json = final_ai.content.split(marker, 1)[1].strip()
        score = json.loads(score_json)
        assert score["breakdown"]["validation"] == 60.0

    def test_post_process_without_ledger_would_misfire(self, monkeypatch):
        """反证：缺少台账、仅凭裁剪后的 messages 时会误触发 ToolCallOrderViolation。

        证明本修复的必要性——早期 validate 被裁剪后，窗口内 calculate 无前置 validate。
        """
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", False)
        full = _build_full_messages()
        windowed = _windowed_messages([], full)
        wrapper = _AgentWrapper(object())
        with pytest.raises(ToolCallOrderViolation):
            wrapper._post_process({"messages": windowed})
