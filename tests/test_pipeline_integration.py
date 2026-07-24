"""Agent 运行时集成测试（mock LLM / mock 工具）

本测试不依赖真实 LLM 与真实导出文件 I/O，而是构造「假底层 Agent」返回预置的
消息序列（模拟 LLM 的工具调用轨迹），并用真实的 `_AgentWrapper._post_process`
后处理逻辑驱动它，验证两条不可回归的领域约束在 Agent 执行链上的机制化门禁：

1. 工具调用顺序门禁（fail-closed）：
   validate_financial_data → calculate_financial_indicators → check_disclosure_compliance
   → search_regulations → calculate_comprehensive_score → export_pdf_report + export_excel_report
   任一后置步骤早于其前置步骤，或先算后校验，均应抛 ToolCallOrderViolation 中断，
   绝不在依据不足 / 次序错乱的前提下继续产出报告。

2. 成对导出门禁（fail-closed）：
   PDF 与 Excel 导出缺一不可。若 LLM 遗漏其中一个，兜底后处理必须自动补调缺失的
   导出工具，保证最终二者都被调用。若成对导出兜底被破坏（删除任一补调分支），
   本测试中「被删除的一方未被调用」的断言即失败。

这两组断言构成 CI 的最小可行功能校验：
- 故意破坏工具顺序 → 顺序门禁抛异常 → 测试捕获，反向验证门禁生效；
- 故意删除一个导出兜底 → 成对导出断言失败 → CI 变红（fail-closed）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.agent as agent_module
from agents.agent import _AgentWrapper
from tools.domain_guard import (
    CALCULATE_TOOL,
    DISCLOSURE_TOOL,
    EXPORT_EXCEL_TOOL,
    EXPORT_PDF_TOOL,
    SCORE_TOOL,
    SEARCH_TOOL,
    VALIDATE_TOOL,
    ToolCallOrderViolation,
)

# 完整声明链路的正序（导出对为并行步骤，彼此无先后约束）
FULL_ORDER = [
    VALIDATE_TOOL,
    CALCULATE_TOOL,
    DISCLOSURE_TOOL,
    SEARCH_TOOL,
    SCORE_TOOL,
    EXPORT_PDF_TOOL,
    EXPORT_EXCEL_TOOL,
]

# 供导出兜底提取的合规风险台账 JSON（含 company_info / risk_details 关键字段）
_RISK_JSON = json.dumps({
    "company_info": {"company_name": "集成测试公司", "report_year": "2025"},
    "risk_summary": {"total_risks": 1, "major_risks": 1, "important_risks": 0, "general_risks": 0},
    "risk_details": [{
        "risk_id": "R001", "dimension": "financial_misstatement", "title": "存在错报风险嫌疑",
        "level": "重大", "confidence": 0.8, "evidence": "示例证据", "audit_suggestion": "建议进一步核查",
    }],
    "overall_assessment": "存在需关注的异常迹象，需结合人工专业判断进行复核确认。",
}, ensure_ascii=False)


class _RecordingExportTool:
    """替代真实导出工具的 mock：仅记录被 .invoke 调用的次数，不做任何文件 I/O。"""

    def __init__(self, url):
        self._url = url
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return self._url


class _FailingExportTool:
    """模拟真实导出失败的 mock：.invoke 始终抛异常，用于验证降级可见行为。"""

    def __init__(self, message="boom"):
        self._message = message
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        raise RuntimeError(self._message)


def _last_ai_text(result):
    """从后处理结果中取最后一条 AIMessage 的文本内容。"""
    messages = result.get("messages", [])
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    return str(last_ai.content) if last_ai is not None else ""


class TestExportFailureSurfacing:
    """降级可见门禁：兜底导出失败时必须在用户可见的报告文本中 surfacing 失败提示，而非静默成功。"""

    def test_pdf_export_failure_is_surfaced(self, monkeypatch):
        """PDF 兜底导出抛异常：回复文本应含可见失败提示，而不是静默吞掉。"""
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report", _FailingExportTool("pdf failed"))
        monkeypatch.setattr(agent_module, "export_excel_report", _RecordingExportTool("http://local/workpaper.xlsx"))
        # 正序但缺 PDF（触发 PDF 兜底），PDF 导出将抛异常
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # fail-closed 降级可见：失败提示必须出现在用户可见文本中
        assert "PDF报告导出失败" in text

    def test_excel_export_failure_is_surfaced(self, monkeypatch):
        """Excel 兜底导出抛异常：回复文本应含可见失败提示。"""
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report", _RecordingExportTool("http://local/report.pdf"))
        monkeypatch.setattr(agent_module, "export_excel_report", _FailingExportTool("excel failed"))
        # 正序但缺 Excel（触发 Excel 兜底），Excel 导出将抛异常
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "Excel底稿导出失败" in text


class _FakeAgent:
    """假底层 Agent：invoke 返回预置消息序列，模拟 LLM 的完整工具调用轨迹。"""

    def __init__(self, messages):
        self._messages = messages

    def invoke(self, payload, config=None, **kw):
        return {"messages": self._messages}


def _tool_msgs(names):
    """按给定工具名顺序构造 ToolMessage 序列（内容为空 JSON，仅用于顺序/成对校验）。"""
    return [
        ToolMessage(content="{}", name=name, tool_call_id=f"call_{i}")
        for i, name in enumerate(names)
    ]


def _build_messages(tool_order, final_text):
    """组装一次完整对话的消息列表：Human 提问 + 工具调用轨迹 + 最终 AI 回复。"""
    msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
    msgs.extend(_tool_msgs(tool_order))
    msgs.append(AIMessage(content=final_text))
    return msgs


@pytest.fixture
def hermetic_env(monkeypatch):
    """隔离外部副作用：关闭多智能体辩论（避免真实 LLM 调用），
    并用 mock 替换 PDF/Excel 导出工具（避免真实文件 I/O），返回两个 recorder。"""
    monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
    pdf_mock = _RecordingExportTool("http://local/report.pdf")
    excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
    monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
    monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
    return pdf_mock, excel_mock


class TestToolOrderGate:
    """工具调用顺序门禁在 Agent 运行时的 fail-closed 行为。"""

    def test_full_correct_order_passes(self, hermetic_env):
        """完整声明链路正序 + 成对导出齐全：后处理应正常完成，不触发兜底导出。"""
        pdf_mock, excel_mock = hermetic_env
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(FULL_ORDER, "分析完成")))
        result = wrapper.invoke({"messages": []})
        assert result is not None
        # 二者均已由「LLM」调用，兜底不应再补调
        assert pdf_mock.calls == []
        assert excel_mock.calls == []

    def test_calculate_before_validate_raises(self, hermetic_env):
        """先算后校验（calculate 早于 validate）应 fail-closed 抛顺序违规。"""
        broken = [CALCULATE_TOOL, VALIDATE_TOOL, DISCLOSURE_TOOL,
                  SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, "分析完成")))
        with pytest.raises(ToolCallOrderViolation):
            wrapper.invoke({"messages": []})

    def test_export_before_score_raises(self, hermetic_env):
        """导出早于综合评分（后置步骤前移）应 fail-closed 抛顺序违规。"""
        broken = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                  SEARCH_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, "分析完成")))
        with pytest.raises(ToolCallOrderViolation):
            wrapper.invoke({"messages": []})

    def test_search_before_disclosure_raises(self, hermetic_env):
        """检索早于披露检查（次序颠倒）应 fail-closed 抛顺序违规。"""
        broken = [VALIDATE_TOOL, CALCULATE_TOOL, SEARCH_TOOL,
                  DISCLOSURE_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, "分析完成")))
        with pytest.raises(ToolCallOrderViolation):
            wrapper.invoke({"messages": []})


class TestPairedExportGate:
    """成对导出门禁：PDF/Excel 缺一不可，遗漏时兜底后处理必须自动补调。"""

    def test_missing_excel_is_backfilled(self, hermetic_env):
        """LLM 仅导出 PDF、遗漏 Excel：兜底应补调 Excel，PDF 不重复调用。"""
        pdf_mock, excel_mock = hermetic_env
        # 正序但缺 export_excel_report
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        wrapper.invoke({"messages": []})
        # fail-closed：缺失的 Excel 必须被兜底补调恰好一次
        assert len(excel_mock.calls) == 1
        # PDF 已由「LLM」调用，兜底不应重复补调
        assert pdf_mock.calls == []

    def test_missing_pdf_is_backfilled(self, hermetic_env):
        """LLM 仅导出 Excel、遗漏 PDF：兜底应补调 PDF，Excel 不重复调用。"""
        pdf_mock, excel_mock = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        assert excel_mock.calls == []

    def test_both_missing_are_backfilled(self, hermetic_env):
        """LLM 完全遗漏导出：兜底应同时补调 PDF 与 Excel，保证成对齐全。"""
        pdf_mock, excel_mock = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        assert len(excel_mock.calls) == 1
