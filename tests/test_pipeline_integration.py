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
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.agent as agent_module
from agents.agent import (
    ADVOCATE_SYSTEM_PROMPT,
    ARBITER_SYSTEM_PROMPT,
    C2_SEMANTIC_SYSTEM_PROMPT,
    SKEPTIC_SYSTEM_PROMPT,
    _AgentWrapper,
    _drop_stale_validation_risks,
)
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

# 完整声明链路的正序（与 main.TOOL_PIPELINE 锚点声明顺序一致，导出对为并行步骤）
FULL_ORDER = [
    "parse_pdf_report",
    VALIDATE_TOOL,
    CALCULATE_TOOL,
    "calculate_risk_models",
    DISCLOSURE_TOOL,
    "identify_audit_opinion",
    SEARCH_TOOL,
    "search_regulatory_inquiries",
    "industry_outlook",
    "compare_multi_year",
    SCORE_TOOL,
    "generate_risk_heatmap",
    "generate_radar_chart",
    "generate_trend_chart",
    "investment_advisor",
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
    并用 mock 替换 PDF/Excel 导出与图表工具（避免真实文件 I/O 与 matplotlib 绘图），
    返回五个 recorder（pdf, excel, heatmap, radar, trend）。"""
    monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
    pdf_mock = _RecordingExportTool("http://local/report.pdf")
    excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
    heatmap_mock = _RecordingExportTool("/local_storage/charts/heatmap.png")
    radar_mock = _RecordingExportTool("/local_storage/charts/radar.png")
    trend_mock = _RecordingExportTool("/local_storage/charts/trend.png")
    monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
    monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
    monkeypatch.setattr(agent_module, "generate_risk_heatmap", heatmap_mock)
    monkeypatch.setattr(agent_module, "generate_radar_chart", radar_mock)
    monkeypatch.setattr(agent_module, "generate_trend_chart", trend_mock)
    return pdf_mock, excel_mock, heatmap_mock, radar_mock, trend_mock


class TestToolOrderGate:
    """分级顺序门禁：硬约束（先校验后计算）fail-closed，软约束降级可见警告。"""

    def test_full_correct_order_passes(self, hermetic_env):
        """完整声明链路正序 + 成对导出齐全：后处理应正常完成，不触发兜底导出。"""
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(FULL_ORDER, "分析完成")))
        result = wrapper.invoke({"messages": []})
        assert result is not None
        # 二者均已由「LLM」调用，兜底不应再补调
        assert pdf_mock.calls == []
        assert excel_mock.calls == []

    def test_calculate_before_validate_raises(self, hermetic_env):
        """硬约束：先算后校验（calculate 早于 validate）应 fail-closed 抛顺序违规。

        本用例的消息里无 AIMessage.tool_calls 入参可供代跑补救，
        因此补救不可行，必须维持 fail-closed。
        """
        broken = [CALCULATE_TOOL, VALIDATE_TOOL, DISCLOSURE_TOOL,
                  SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, "分析完成")))
        with pytest.raises(ToolCallOrderViolation):
            wrapper.invoke({"messages": []})

    def test_missing_validate_backfilled_when_data_clean(self, hermetic_env):
        """硬约束补救：LLM 漏调 validate 但 calculate 入参可取且勾稽平衡时，
        系统代跑校验后应继续分析并附可见补救提示（用户不再白等零产出）。"""
        clean_data = json.dumps({
            "total_assets": 100, "total_liabilities": 60, "net_assets": 40,
        })
        # 构造带 tool_calls 入参的 AIMessage + 缺 validate 的工具序列
        msgs = [HumanMessage(content="请分析年报")]
        msgs.append(AIMessage(content="", tool_calls=[{
            "name": CALCULATE_TOOL, "args": {"financial_data_json": clean_data}, "id": "c1",
        }]))
        msgs.extend(_tool_msgs([CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL,
                                SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]))
        msgs.append(AIMessage(content="分析完成"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        final_ai = next(m for m in reversed(result["messages"])
                        if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None))
        assert "数据校验补救提示" in final_ai.content

    def test_missing_validate_still_raises_when_data_broken(self, hermetic_env):
        """硬约束底线：代跑校验发现勾稽不平时，仍必须 fail-closed 中断（
        绝不在校验不通过的数据上产出报告）。"""
        broken_data = json.dumps({
            "total_assets": 100, "total_liabilities": 60, "net_assets": 999,  # 资产≠负债+权益
        })
        msgs = [HumanMessage(content="请分析年报")]
        msgs.append(AIMessage(content="", tool_calls=[{
            "name": CALCULATE_TOOL, "args": {"financial_data_json": broken_data}, "id": "c1",
        }]))
        msgs.extend(_tool_msgs([CALCULATE_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]))
        msgs.append(AIMessage(content="分析完成"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        with pytest.raises(ToolCallOrderViolation):
            wrapper.invoke({"messages": []})

    def test_export_before_score_warns_not_fails(self, hermetic_env):
        """软约束：导出早于综合评分不再炸掉分析，而是附可见顺序提示后照常产出。

        背景：旧版 fail-closed 会连带中断兜底导出导致零产出（实测事故）；
        该次序属推荐顺序而非数据依赖，降级为警告。
        """
        broken = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                  SEARCH_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, "分析完成")))
        result = wrapper.invoke({"messages": []})
        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        # 降级可见：报告末尾附顺序提示，但分析不中断
        assert "工具链顺序提示" in final_ai.content

    def test_soft_warning_appended_after_risk_json(self, hermetic_env):
        """软警告位置契约：只允许追加在正文末尾，不得插入台账 JSON 之前/之中，
        且不得破坏风险 JSON 的可抽取性（下游历史落库/兜底导出都依赖它）。"""
        from agents.agent import _extract_risk_json
        broken = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                  SEARCH_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(broken, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        text = str(final_ai.content)
        warn_pos = text.find("工具链顺序提示")
        json_pos = text.find('"company_info"')
        assert warn_pos != -1 and json_pos != -1
        # 警告必须在台账 JSON 之后（末尾追加）
        assert warn_pos > json_pos
        # 风险 JSON 仍可完整抽取（末尾中文提示不干扰括号配对）
        assert _extract_risk_json(text) is not None

    def test_search_disclosure_order_interchangeable(self, hermetic_env):
        """披露检查与法规检索无数据依赖（共享 rank）：互换顺序应完全合规、无警告。"""
        reordered = [VALIDATE_TOOL, CALCULATE_TOOL, SEARCH_TOOL,
                     DISCLOSURE_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(reordered, "分析完成")))
        result = wrapper.invoke({"messages": []})
        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        assert "工具链顺序提示" not in final_ai.content


class TestChartBackfill:
    """图表兜底：图表已从 LLM 链路摘除，由 _post_process 并行补生（快速模式除外）。"""

    def test_charts_backfilled_in_normal_mode(self, hermetic_env):
        """普通模式：LLM 未调图表 → 热力图/雷达图各兜底补生一次；
        趋势图缺多年数据时降级为可见警告而非静默缺失。"""
        _pdf, _xls, heatmap_mock, radar_mock, trend_mock = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        assert len(heatmap_mock.calls) == 1
        assert len(radar_mock.calls) == 1
        # 无多年数据：仍生成带数据不足说明的占位图，避免交付物缺失
        assert len(trend_mock.calls) == 1
        assert json.loads(trend_mock.calls[0]["trend_data_json"])["years"] == []
        text = _last_ai_text(result)
        assert "风险热力图" in text and "/local_storage/charts/heatmap.png" in text
        assert "财务雷达图" in text and "/local_storage/charts/radar.png" in text
        assert "趋势折线图" in text and "/local_storage/charts/trend.png" in text

    def test_trend_backfilled_from_multi_year_data(self, hermetic_env):
        """多年对比已调用（含 ≥2 年数据）→ 趋势折线图由系统兜底生成并附 URL。"""
        _pdf, _xls, heatmap_mock, radar_mock, trend_mock = hermetic_env
        my_data = json.dumps({
            "years_analyzed": ["2023", "2024"],
            "indicators_by_year": {
                "2023": {"revenue": 100, "net_profit": 10, "operating_cashflow": 5},
                "2024": {"revenue": 120, "net_profit": 12, "operating_cashflow": 8},
            },
        }, ensure_ascii=False)
        msgs = [HumanMessage(content="请分析年报")]
        msgs.extend(_tool_msgs([VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                                SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]))
        msgs.append(ToolMessage(content=my_data, name="compare_multi_year", tool_call_id="call_my", id="tm_my"))
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        assert len(trend_mock.calls) == 1
        text = _last_ai_text(result)
        assert "趋势折线图" in text and "/local_storage/charts/trend.png" in text

    def test_charts_not_backfilled_in_fast_mode(self, hermetic_env):
        """快速模式（消息含关键词）：保持零图表定位，不补图。"""
        _pdf, _xls, heatmap_mock, radar_mock, trend_mock = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        msgs = [HumanMessage(content="请分析年报\n\n（快速模式：速度优先）")]
        msgs.extend(_tool_msgs(order))
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        wrapper.invoke({"messages": []})
        assert heatmap_mock.calls == []
        assert radar_mock.calls == []
        assert trend_mock.calls == []

    def test_llm_risk_charts_refreshed_after_gate_but_trend_is_reused(self, hermetic_env):
        """候选被门禁转入待核查后，风险图重建；跨年趋势图不受采信状态影响。"""
        _pdf, _xls, heatmap_mock, radar_mock, trend_mock = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL,
                 SCORE_TOOL, "generate_risk_heatmap", "generate_radar_chart",
                 "generate_trend_chart",
                 EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        old_url = "/local_storage/charts/pre_review.png"
        messages = _build_messages(order, f"![原热力图]({old_url})\n" + _RISK_JSON)
        for message in messages:
            if isinstance(message, ToolMessage) and message.name == "generate_risk_heatmap":
                message.content = json.dumps({"download_url": old_url})
        wrapper = _AgentWrapper(_FakeAgent(messages))
        result = wrapper.invoke({"messages": []})
        assert len(heatmap_mock.calls) == 1
        assert len(radar_mock.calls) == 1
        assert json.loads(heatmap_mock.calls[0]["risk_report_json"])["accepted_risk_details"] == []
        assert len(trend_mock.calls) == 1
        assert json.loads(trend_mock.calls[0]["trend_data_json"])["years"] == []
        assert old_url not in _last_ai_text(result)
        assert "![原热力图](/local_storage/charts/heatmap.png)" in _last_ai_text(result)


class TestDisclaimerBackfill:
    """AI 生成声明系统兜底：sp 要求回复末尾附声明，LLM 漏附时系统补上，
    已附时不得重复追加（防合规声明缺失 / 重复刷屏）。"""

    def test_disclaimer_backfilled_when_missing(self, hermetic_env):
        """LLM 回复未附声明 → 系统在末尾追加声明。"""
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "AI 辅助生成" in text
        assert "不构成专业审计意见或投资建议" in text

    def test_disclaimer_not_duplicated_when_present(self, hermetic_env):
        """LLM 已附声明 → 系统不重复追加（保持原样）。"""
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
        final_text = (_RISK_JSON +
                      "\n\n⚠️ 以上分析由 AI 辅助生成，仅供审计参考，不构成专业审计意见或投资建议。")
        msgs = [HumanMessage(content="请分析年报")]
        msgs.extend(_tool_msgs([VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                                SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL]))
        msgs.append(AIMessage(content=final_text))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert text.count("AI 辅助生成") == 1


class TestStaleValidationCleanup:
    """幽灵数据清洗：引用已通过校验项的过期风险条目移出明细（防跨版本数据串用）。"""

    def test_stale_check_reference_dropped(self):
        """条目 evidence 精确引用已通过（passed=True）的校验项 → 剔除并留痕。"""
        report = {"risk_details": [
            {"risk_id": "R007", "title": "勾稽差异及担保披露问题可能触发监管关注",
             "evidence": "校验项：现金流勾稽，差异：45541.0（16.71%）"},
            {"risk_id": "R001", "title": "应收账款激增", "evidence": "应收同比 +67.18%"},
        ]}
        vd = {"data_validation": {"all_checks": [
            {"check": "现金流勾稽", "passed": True,
             "message": "现金流勾稽成立，无异常", "difference_pct": "0.34%"},
        ]}}
        n = _drop_stale_validation_risks(report, vd)
        assert n == 1
        assert [r["risk_id"] for r in report["risk_details"]] == ["R001"]
        assert report["excluded_items"][0]["risk_id"] == "R007"

    def test_stale_message_substring_dropped(self):
        """条目整段引用已通过 check 的 message（≥8 字）→ 剔除。"""
        report = {"risk_details": [
            {"risk_id": "V001",
             "title": "现金流勾稽不成立，差45541.00（16.71%），存在数据可靠性风险",
             "evidence": "校验项：现金流勾稽，差异：45541.0"},
        ]}
        vd = {"data_validation": {"all_checks": [
            {"check": "现金流勾稽", "passed": True,
             "message": "现金流勾稽不成立，差45541.00（16.71%），存在数据可靠性风险"},
        ]}}
        assert _drop_stale_validation_risks(report, vd) == 1
        assert report["risk_details"] == []

    def test_failed_check_reference_kept(self):
        """条目引用未通过（passed=False）的校验项 → 保守保留。"""
        report = {"risk_details": [
            {"risk_id": "V001", "title": "现金流勾稽不成立", "evidence": "校验项：现金流勾稽"},
        ]}
        vd = {"data_validation": {"all_checks": [
            {"check": "现金流勾稽", "passed": False,
             "message": "现金流勾稽不成立，差45541.00（16.71%），存在数据可靠性风险"},
        ]}}
        assert _drop_stale_validation_risks(report, vd) == 0
        assert len(report["risk_details"]) == 1

    def test_unmatched_evidence_kept(self):
        """无法对应到具体校验项 → 保守保留（防误删真实风险）。"""
        report = {"risk_details": [
            {"risk_id": "R001", "title": "应收账款激增", "evidence": "应收同比 +67.18%"},
        ]}
        vd = {"data_validation": {"all_checks": [
            {"check": "现金流勾稽", "passed": True, "message": "现金流勾稽成立，无异常"},
        ]}}
        assert _drop_stale_validation_risks(report, vd) == 0
        assert len(report["risk_details"]) == 1


class TestScoreLedgerBackfill:
    """L 下沉：系统评分快照回写台账层（comprehensive_score 字段 + overall_assessment 文本），
    辩论/导出读到的 risk_json 不再含 LLM 写入的旧评分（v23 实证：辩论引用台账 15.7 与系统 12.6 矛盾）。"""

    def test_stale_score_overwritten_in_ledger(self, hermetic_env, monkeypatch):
        """LLM 台账旧评分 15.7 → 导出兜底收到的台账 comprehensive_score=12.6，
        overall_assessment 旧分数表述被吞净替换。"""
        import json as _json
        import tools.risk_scorer as rs_mod
        from unittest.mock import MagicMock

        pdf_mock, _excel, _hm, _rd, _tr = hermetic_env
        monkeypatch.setattr(rs_mod, "calculate_comprehensive_score",
                            MagicMock(invoke=MagicMock(return_value=_json.dumps(
                                {"score": 12.6, "level": "低风险", "level_key": "low",
                                 "base_score": 12.6, "escalation": 0.0,
                                 "breakdown": {"financial": 21.0, "disclosure": 7.1,
                                                "validation": 0.0}},
                                ensure_ascii=False))))
        ledger = ('{"company_info": {"company_name": "测试公司", "report_year": "2025"}, '
                  '"comprehensive_score": {"score": 15.7, "level": "中等风险"}, '
                  '"overall_assessment": "整体风险中等（综合评分15.7分，中等风险），建议关注。", '
                  '"risk_details": []}')
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, ledger)))
        wrapper.invoke({"messages": []})

        exported = _json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert exported["comprehensive_score"]["score"] == 12.6
        assert exported["comprehensive_score"]["level"] == "低风险"
        # 旧分数表述被吞净替换（L 下沉到字段层）
        assert "15.7" not in exported["overall_assessment"]
        assert "12.6分（低风险）" in exported["overall_assessment"]
        # 渲染层快照同步就位（导出端 L 补丁可直接使用）
        assert exported["comprehensive_score_snapshot"]["score"] == 12.6

    def test_message_layer_rewritten_for_frontend(self, hermetic_env, monkeypatch):
        """L 下沉·消息层：前端直接展示的 last_ai.content 也被回写——正文旧分表述
        替换 + 内嵌 ```json 台账块 comprehensive_score 覆盖（v24 实证：正文 18.5 与
        系统评分卡 14.8 同屏矛盾，用户多次反馈的根因）。"""
        import json as _json
        import tools.risk_scorer as rs_mod
        from unittest.mock import MagicMock

        _pdf, _excel, _hm, _rd, _tr = hermetic_env
        monkeypatch.setattr(rs_mod, "calculate_comprehensive_score",
                            MagicMock(invoke=MagicMock(return_value=_json.dumps(
                                {"score": 12.6, "level": "低风险", "level_key": "low",
                                 "base_score": 12.6, "escalation": 0.0,
                                 "breakdown": {"financial": 21.0, "disclosure": 7.1,
                                                "validation": 0.0}},
                                ensure_ascii=False))))
        ledger = ('{"company_info": {"company_name": "测试公司", "report_year": "2025"}, '
                  '"comprehensive_score": {"score": 18.5, "level": "低风险"}, '
                  '"overall_assessment": "整体处于低风险水平（综合评分18.5分），财务结构稳健。", '
                  '"risk_details": []}')
        final_text = ("整体处于低风险水平（综合评分18.5分），财务结构稳健。\n\n"
                      "```json\n" + ledger + "\n```")
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL]
        msgs = [HumanMessage(content="请分析年报")]
        msgs.extend(_tool_msgs(order))
        msgs.append(AIMessage(content=final_text))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        text = str(final_ai.content)
        # 正文旧分表述被替换（吞全 span）
        assert "18.5" not in text
        assert "综合风险评分 12.6分（低风险）" in text
        # 内嵌台账块 comprehensive_score 已覆盖为系统值
        block = re.search(r"```json\s*(\{.*\})\s*```", text, re.S)
        assert block, "消息中应保留 ```json 台账块"
        embedded = _json.loads(block.group(1))
        assert embedded["comprehensive_score"]["score"] == 12.6
        assert embedded["comprehensive_score"]["level"] == "低风险"


class TestReviewGateLedgerSync:
    """门禁后台账必须同步回消息层——网页「报告元数据与完整性」的唯一来源

    实测缺陷（中国石油 2025 年半年度）：C1 辩论三轮与 C2 语义复核都已执行、仲裁也
    回写了 3 条等级调整，网页却显示「审查门禁：未执行审查」。根因是证据门禁
    （review_gate / accepted_risk_details / formal_status）在仲裁回写之后才写入导出
    台账，消息层台账仍停在仲裁前版本，而网页报告元数据读的正是消息层台账，
    与三份 PDF/Excel 的导出台账自相矛盾。
    """

    def test_gate_outcome_written_back_into_message_ledger(self, hermetic_env):
        import json as _json

        ledger = _json.dumps({
            "company_info": {"company_name": "门禁回刷公司", "report_year": "2025"},
            "risk_details": [{
                "risk_id": "R001", "dimension": "financial_misstatement",
                "title": "应收账款激增", "level": "重要", "confidence": 0.7,
                "evidence": "应收账款同比增长 67.2%，显著高于营收增速",
            }],
            "overall_assessment": "存在需关注事项，建议结合人工专业判断复核确认。",
        }, ensure_ascii=False)
        final_text = "分析完成。\n\n```json\n" + ledger + "\n```"
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, final_text)))
        result = wrapper.invoke({"messages": []})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        block = re.search(r"```json\s*(\{.*\})\s*```", str(final_ai.content), re.S)
        assert block, "消息中应保留 ```json 台账块"
        embedded = _json.loads(block.group(1))
        assert "review_gate" in embedded, "门禁结论未同步回消息层台账"
        assert embedded["review_gate"]["status"] in ("passed", "not_passed")
        assert embedded["accepted_risk_details"] == []
        assert embedded["risk_details"][0]["formal_status"] == "unaccepted"

    def test_web_report_metadata_reflects_gate_outcome(self, hermetic_env):
        """网页报告元数据的门禁状态取自消息层台账：不得再是 not_run。"""
        import json as _json

        from main import GraphService

        ledger = _json.dumps({
            "company_info": {"company_name": "门禁回刷公司", "report_year": "2025"},
            "risk_details": [{
                "risk_id": "R001", "dimension": "financial_misstatement",
                "title": "应收账款激增", "level": "重要", "confidence": 0.7,
                "evidence": "应收账款同比增长 67.2%，显著高于营收增速",
            }],
            "overall_assessment": "存在需关注事项，建议结合人工专业判断复核确认。",
        }, ensure_ascii=False)
        final_text = "分析完成。\n\n```json\n" + ledger + "\n```"
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, final_text)))
        result = wrapper.invoke({"messages": []})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        report = GraphService()._build_final_report_from_messages(
            [{"type": "ai", "content": str(final_ai.content)}])
        assert report["report_metadata"]["review_gate_status"] == "not_passed"
        assert report["review_gate"]["status"] == "not_passed"
        assert report["accepted_risk_details"] == []


class TestToolPipelineAnchors:
    """进度锚点与工具注册表的同步（新增工具必须同步进度映射）

    进度条靠 TOOL_PIPELINE 把工具名映射为阶段名 + 百分比。若新增工具未登记锚点，
    用户会看到进度条卡在前一个阶段长时间不动（实测为“卡死”观感）；
    若锚点百分比不递增，进度条会回退。本组用例把两者固定下来。
    """

    def _pipeline(self):
        from main import TOOL_PIPELINE
        return TOOL_PIPELINE

    def _registered_tool_names(self):
        """取全量注册工具名（module=None 即不裁剪）。

        同时覆盖 MODULE_TOOLS（四模块子集）与 LIGHT_MODULE_TOOLS（C 端轻量子集），
        否则新增轻量工具（如 investment_advisor）漏锚点不会被测出。
        """
        from agents.agent import MODULE_TOOLS, LIGHT_MODULE_TOOLS
        names = set()
        for subset in {**MODULE_TOOLS, **LIGHT_MODULE_TOOLS}.values():
            names |= set(subset)
        return names

    def test_anchor_percentages_ascend(self):
        pcts = [p for _n, _s, p in self._pipeline()]
        assert pcts == sorted(pcts), "锚点百分比必须单调递增，否则进度条会回退"
        assert all(0 < p < 100 for p in pcts), "锚点不得占用 0/100 两个终态值"

    def test_anchor_names_unique(self):
        names = [n for n, _s, _p in self._pipeline()]
        assert len(names) == len(set(names))

    def test_new_tools_have_anchors(self):
        """P3 新增的两个工具必须有进度锚点（它们均为耗时可见步骤）。"""
        names = {n for n, _s, _p in self._pipeline()}
        assert "calculate_risk_models" in names
        assert "identify_audit_opinion" in names

    def test_every_module_tool_is_anchored(self):
        """三个模块子集里的工具全部需有锚点，否则该模块运行时进度条会断档。"""
        anchored = {n for n, _s, _p in self._pipeline()}
        missing = self._registered_tool_names() - anchored
        assert not missing, f"以下模块工具缺少进度锚点：{sorted(missing)}"

    def test_anchor_order_matches_declared_chain(self):
        """锚点顺序必须与领域声明链路一致（校验→指标→披露→法规→评分→导出）。"""
        names = [n for n, _s, _p in self._pipeline()]
        idx = {n: i for i, n in enumerate(names)}
        for earlier, later in zip(FULL_ORDER, FULL_ORDER[1:]):
            assert idx[earlier] < idx[later], f"{earlier} 锚点应早于 {later}"

    def test_anchor_steps_are_chinese_labels(self):
        """阶段名面向用户展示，不得遗留工具原名这类未翻译文本。"""
        for name, step, _p in self._pipeline():
            assert step and step != name
            assert any("\u4e00" <= ch <= "\u9fff" for ch in step)


class TestPairedExportGate:
    """成对导出门禁：PDF/Excel 缺一不可，遗漏时兜底后处理必须自动补调。"""

    def test_missing_excel_is_backfilled(self, hermetic_env):
        """LLM 仅导出 PDF、遗漏 Excel：兜底应补调 Excel，PDF 不重复调用。"""
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
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
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
                 SEARCH_TOOL, SCORE_TOOL, EXPORT_EXCEL_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        assert excel_mock.calls == []

    def test_both_missing_are_backfilled(self, hermetic_env):
        """LLM 完全遗漏导出：兜底应同时补调 PDF 与 Excel，保证成对齐全。"""
        pdf_mock, excel_mock, _hm, _rd, _tr = hermetic_env
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        assert len(excel_mock.calls) == 1


class TestPdfModuleBackfill:
    """模块裁剪产物：兜底 PDF 导出必须透传当前模块，点单模块只出对应专项报告，
    避免选财务健康却收到全套三份（产物过滤逻辑在 pdf_export 内，此处验证接线）。"""

    @pytest.mark.parametrize("module,expected", [
        ("financial", "financial"),
        ("compliance", "compliance"),
        ("synthesis", "synthesis"),
        (None, ""),   # 未指定模块：透传空串 → 导出全部三份
    ])
    def test_backfill_pdf_passes_module(self, hermetic_env, module, expected):
        pdf_mock, _excel, _hm, _rd, _tr = hermetic_env
        # 正序但完全未导出，触发 PDF 兜底补调
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL, SCORE_TOOL]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)), module=module)
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        assert pdf_mock.calls[0].get("module") == expected


class _FakeResp:
    """模拟 ChatOpenAI.invoke 返回的响应对象（仅需 .content 属性）。"""

    def __init__(self, content):
        self.content = content


def _review_llm(*, advocate="关注方意见", skeptic="否定方意见", arbiter="",
                other="", c2='{"checks": []}'):
    """构造按系统提示词分派的复核假 LLM。

    覆盖 C2 两次隔离判断（线程池并发）+ C1 正方/反方/仲裁三轮串行，以及
    仲裁补全重试（other 分支）。按 SystemMessage 分派而非依赖调用顺序：
    并发执行的 C2 若按顺序派发会产生竞态。

    arbiter/other 可传字符串序列以模拟多轮裁定（不足时复用最后一个）。
    """
    from unittest.mock import MagicMock

    seq = {
        "arbiter": [arbiter] if isinstance(arbiter, str) else list(arbiter),
        "other": [other] if isinstance(other, str) else list(other),
    }

    def _next(kind):
        values = seq[kind]
        if len(values) > 1:
            return values.pop(0)
        return values[0] if values else ""

    def _dispatch(messages):
        system = str(getattr(messages[0], "content", "")) if messages else ""
        if system == C2_SEMANTIC_SYSTEM_PROMPT:
            return _FakeResp(c2)
        if system == ADVOCATE_SYSTEM_PROMPT:
            return _FakeResp(advocate)
        if system == SKEPTIC_SYSTEM_PROMPT:
            return _FakeResp(skeptic)
        if system == ARBITER_SYSTEM_PROMPT:
            return _FakeResp(_next("arbiter"))
        return _FakeResp(_next("other"))

    llm = MagicMock()
    llm.invoke.side_effect = _dispatch
    return llm


# 仲裁裁定：R005 一般→重要（既有条目等级变更，触发底线与图表重生）
_ARBITER_LEVEL_CHANGE = (
    "【逐条裁定】R005 升级重要\n"
    '【裁定JSON】{"adjustments":[{"risk_id":"R005","final_level":"重要",'
    '"reason":"油价下行叠加减值风险"}],"verdict":"通过"}'
)

# 50f：仲裁烂尾场景（verdict 需重新分析 + 重试失败 → 未完成标记）
# 放宽后「需补充」视为已通过但建议追加程序，仅「需重新分析」触发未完成
_ARBITER_INCOMPLETE = (
    '【裁定JSON】{"adjustments":[],"verdict":"需重新分析"}'
)

# 图表重生场景：R001 重大→重要（既有条目等级变更，verdict 通过）
_ARBITER_R001_DOWNGRADE = (
    '【裁定JSON】{"adjustments":[{"risk_id":"R001","final_level":"重要",'
    '"reason":"趋势优先于绝对值"}],"verdict":"通过"}'
)

# 仲裁裁定：新增披露类风险 R006（信披合规维度，触发一致性注记）
_ARBITER_NEW_DISCLOSURE = (
    '【裁定JSON】{"adjustments":[{"risk_id":"R006","final_level":"重要",'
    '"dimension":"信披合规","title":"关联交易披露不够充分",'
    '"evidence":"年报未完整披露关联交易定价","confidence":0.6,'
    '"reason":"遗漏补充"}],"verdict":"通过"}'
)


def _debate_env(monkeypatch, arbiter_text, *, c2=None):
    """开启辩论（mock 复核链路）并 mock 全部导出/图表工具，返回五个 recorder。"""
    monkeypatch.setattr(agent_module, "REVIEW_ENABLED", True)
    fake = _review_llm(arbiter=arbiter_text, **({"c2": c2} if c2 is not None else {}))
    monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kw: fake)
    pdf_mock = _RecordingExportTool("http://local/report.pdf")
    excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
    heatmap_mock = _RecordingExportTool("/local_storage/charts/heatmap.png")
    radar_mock = _RecordingExportTool("/local_storage/charts/radar.png")
    trend_mock = _RecordingExportTool("/local_storage/charts/trend.png")
    monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
    monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
    monkeypatch.setattr(agent_module, "generate_risk_heatmap", heatmap_mock)
    monkeypatch.setattr(agent_module, "generate_radar_chart", radar_mock)
    monkeypatch.setattr(agent_module, "generate_trend_chart", trend_mock)
    return pdf_mock, excel_mock, heatmap_mock, radar_mock, trend_mock


class TestArbiterChartRegen:
    """50b：仲裁改级后（applied>0 且有 level 实际变更），热力图/雷达图须用仲裁后
    台账强制重生（即使 LLM 主运行阶段已调用过图表工具），且回复注明「仲裁后更新版」。"""

    def test_charts_regen_when_arbiter_changes_level(self, monkeypatch):
        pdf_mock, excel_mock, heatmap_mock, radar_mock, trend_mock = \
            _debate_env(monkeypatch, _ARBITER_R001_DOWNGRADE)
        # 图表工具已在「LLM」阶段调用（called 含图表工具名）——无仲裁改级时不会重生
        order = [VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL, SEARCH_TOOL, SCORE_TOOL,
                 "generate_risk_heatmap", "generate_radar_chart"]
        wrapper = _AgentWrapper(_FakeAgent(_build_messages(order, _RISK_JSON)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 仲裁改级 → 图表强制重生且注明更新版
        assert "（仲裁后更新版）" in text
        assert len(heatmap_mock.calls) == 1
        assert len(radar_mock.calls) == 1
        # 重生输入须为仲裁后台账（R001 已降为重要）
        regen_ledger = json.loads(heatmap_mock.calls[0].get("risk_report_json") or "{}")
        r1 = next(r for r in regen_ledger["risk_details"] if r["risk_id"] == "R001")
        assert r1["level"] == "重要"
        assert r1["original_level"] == "重大"


class TestDisclosureConsistencyNote:
    """50b：仲裁新增披露类风险 + 披露检查满分/无缺失 → 生成一致性注记，
    写入导出台账并前置到网页回复。"""

    def test_note_generated_and_written_to_ledger(self, monkeypatch):
        pdf_mock, excel_mock, _hm, _rd, _tr = _debate_env(monkeypatch, _ARBITER_NEW_DISCLOSURE)
        # 披露检查结果：满分 100 且无问题、无缺失章节（与仲裁新增信披风险矛盾）
        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 100, "risk_score": 0,
                                            "checked_items": 14, "passed_items": 14,
                                            "issues": [], "sections_missing": [],
                                            "reconciliation_checks": 0}, ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 网页回复前置可见提示
        assert "披露合规一致性提示" in text
        assert "仲裁阶段补充披露类风险 1 条" in text
        # 注记写入导出 PDF 的台账（Excel 同一份）
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert "仲裁阶段补充披露类风险 1 条" in exported["disclosure_consistency_note"]

    def test_no_note_when_check_not_full_score(self, monkeypatch):
        """披露检查未满分（含问题）时不生成注记。"""
        pdf_mock, _excel, _hm, _rd, _tr = _debate_env(monkeypatch, _ARBITER_NEW_DISCLOSURE)
        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 80, "risk_score": 20,
                                            "checked_items": 14, "passed_items": 13,
                                            "issues": ["缺失必要章节：公司治理"],
                                            "sections_missing": ["公司治理"],
                                            "reconciliation_checks": 0}, ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert "disclosure_consistency_note" not in exported


# 等级底线用例的正式风险须带可追溯证据：证据门禁下，无可回指 evidence_ids 的
# 条目一律进「待处理事项」，不计入正式风险等级分布（与报告口径一致）。
_FLOOR_RISK_IDS = [f"R{i:03d}" for i in range(1, 6)]


def _floor_evidence_catalog():
    return [
        {"evidence_id": f"E-{rid}", "source_type": "annual_report",
         "source_document": "集成测试公司2025年年度报告.pdf", "page": "12",
         "locator": "第三节 管理层讨论与分析", "excerpt": "年报原文摘录",
         "fact_ids": [f"F-{rid}"], "verified": True, "status": "verified"}
        for rid in _FLOOR_RISK_IDS
    ]


def _c2_aligned_payload():
    """两次隔离判断内容一致的 C2 输出：证据齐备的正式风险据此通过语义门禁。"""
    return json.dumps({"checks": [
        {"risk_id": rid, "decision": "supported", "conditions_aligned": True,
         "evidence_ids": [f"E-{rid}"], "reason": "证据支持"}
        for rid in _FLOOR_RISK_IDS
    ]}, ensure_ascii=False)


# 含 5 条已采信风险（R001-R004 重要 + R005 一般）的台账，仲裁把 R005 升为重要后
# 变为 5 个重要级；仅在最终证据门禁后上调至 51 分。
_FIVE_IMPORTANT_LEDGER = json.dumps({
    "company_info": {"company_name": "集成测试公司", "report_year": "2025"},
    "risk_details": [
        {"risk_id": "R001", "dimension": "财务错报风险", "level": "重要", "title": "应收激增",
         "evidence": "应收账款同比+45%（第12页）", "evidence_ids": ["E-R001"]},
        {"risk_id": "R002", "dimension": "财务错报风险", "level": "重要", "title": "存贷双高",
         "evidence": "货币资金与有息负债同时高企（第15页）", "evidence_ids": ["E-R002"]},
        {"risk_id": "R003", "dimension": "关联交易风险", "level": "重要", "title": "关联交易规模大",
         "evidence": "关联采购占比 32%（第18页）", "evidence_ids": ["E-R003"]},
        {"risk_id": "R004", "dimension": "财务错报风险", "level": "重要", "title": "毛利率异常",
         "evidence": "毛利率高于同业 12pp（第14页）", "evidence_ids": ["E-R004"]},
        {"risk_id": "R005", "dimension": "持续经营风险", "level": "一般", "title": "油价下行盈利承压",
         "evidence": "原油价格下跌致营收下滑（第20页）", "evidence_ids": ["E-R005"]},
    ],
    "evidence": _floor_evidence_catalog(),
    "overall_assessment": "整体风险可控，综合风险评分10.5分（低风险）。",
}, ensure_ascii=False)


def _score_stub_json():
    """构造系统评分兑底使用的确定性评分 JSON（10.5 低风险，将被底线规则上调）。"""
    return json.dumps({
        "score": 10.5, "level": "低风险", "level_key": "low",
        "base_score": 10.5, "escalation": 0.0, "escalation_reasons": [],
        "breakdown": {"financial": 10.0, "disclosure": 10.0, "validation": 10.0},
        "weights": {"financial": 0.5, "disclosure": 0.3, "validation": 0.2},
    }, ensure_ascii=False)


class TestLevelFloorIntegration:
    """50c：仲裁后 5 个重要级 → 风险等级底线触发（评分卡重建 + 消息重刷 + 台账注记）。

    实测 17:59 版同构场景：4 条风险（3 重要 + 1 一般）经仲裁变 5 个重要级，
    系统评分仍为 10.5 低风险——底线规则须强制上调为 51 分高风险。"""

    def test_floor_escalates_after_arbitration(self, monkeypatch):
        from unittest.mock import MagicMock
        # 50f：_ARBITER_LEVEL_CHANGE（verdict 通过，R004 升级+R005 新增 → 5 项重要）
        pdf_mock, excel_mock, _hm, _rd, _tr = _debate_env(
            monkeypatch, _ARBITER_LEVEL_CHANGE, c2=_c2_aligned_payload())
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)

        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=f"分析完成，风险台账如下：\n```json\n{_FIVE_IMPORTANT_LEDGER}\n```"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 底线触发：评分卡重建为 51 分高风险 + 原评分作废警示可见
        assert "风险等级底线规则" in text
        assert "51.0分（高风险）" in text
        # 工具量化分保留，底线调整单列；复核前候选不得先把分数上调至 26。
        assert "原量化评分 10.5 分，底线调整 40.5 分" in text
        # 消息层旧分表述重刷为高风险定级
        assert "10.5分（低风险）" not in text
        # PDF 载荷含底线注记与新评分（Excel 同一份台账）
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert "风险等级底线规则" in exported["level_floor_note"]
        assert exported["comprehensive_score"]["score"] == 51
        assert exported["comprehensive_score"]["level"] == "高风险"
        assert exported["comprehensive_score"]["base_score"] == 10.5
        assert exported["comprehensive_score"]["level_floor_adjustment"] == 40.5

    def test_untraceable_candidates_do_not_raise_score_before_review(self, monkeypatch):
        from unittest.mock import MagicMock
        pdf_mock, excel_mock, _hm, _rd, _tr = _debate_env(
            monkeypatch, _ARBITER_LEVEL_CHANGE, c2=_c2_aligned_payload())
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)
        ledger = json.loads(_FIVE_IMPORTANT_LEDGER)
        ledger["evidence"] = []
        ledger["risk_summary"] = {"total_risks": 5, "important_risks": 5}
        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content='{"failed_checks": 0}', name=VALIDATE_TOOL, tool_call_id="v"),
            ToolMessage(content='{"alerts": []}', name=CALCULATE_TOOL, tool_call_id="c"),
            ToolMessage(content='{"risk_score": 10}', name=DISCLOSURE_TOOL, tool_call_id="d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="sc"),
            AIMessage(content="```json\n" + json.dumps(ledger, ensure_ascii=False) + "\n```"),
        ]
        result = _AgentWrapper(_FakeAgent(msgs)).invoke({"messages": []})
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        excel_report = json.loads(excel_mock.calls[0]["risk_report_json"])
        assert exported["accepted_risk_details"] == []
        assert len(exported["pending_items"]) == 5
        assert exported["risk_summary"]["total_risks"] == 0
        assert exported["comprehensive_score"]["score"] == 10.5
        assert exported["comprehensive_score_snapshot"]["score"] == 10.5
        assert exported["comprehensive_score"]["assessment_status"] == "pending_review"
        assert "level_floor_note" not in exported
        assert excel_report["comprehensive_score"] == exported["comprehensive_score"]
        assert all(r["level_status"] == "provisional" for r in exported["pending_items"])
        assert "系统强制上调" not in _last_ai_text(result)


class TestCashflowPenetrationNote:
    """50c：应收背离告警 → 跨期穿透注记写入台账并前置到回复可见区。"""

    def test_penetration_note_written_and_visible(self, monkeypatch):
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        pdf_mock = _RecordingExportTool("http://local/report.pdf")
        excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
        heatmap_mock = _RecordingExportTool("/local_storage/charts/heatmap.png")
        radar_mock = _RecordingExportTool("/local_storage/charts/radar.png")
        trend_mock = _RecordingExportTool("/local_storage/charts/trend.png")
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
        monkeypatch.setattr(agent_module, "generate_risk_heatmap", heatmap_mock)
        monkeypatch.setattr(agent_module, "generate_radar_chart", radar_mock)
        monkeypatch.setattr(agent_module, "generate_trend_chart", trend_mock)

        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps(
                {"indicators": {}, "alerts": [
                    "应收账款增速(67.18%)显著高于营收增速(-6.74%)，可能存在提前确认收入或放宽信用政策"]},
                ensure_ascii=False), name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 网页回复可见警示
        assert "跨期穿透提示" in text
        assert "经营性应付项目变动" in text
        # PDF 载荷写入确定性注记（Excel 同源）
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert "应收账款账面余额较上年末增长64.06%" in exported["cashflow_penetration_note"]
        assert "两项比较期间不同，暂不作背离判断" in exported["cashflow_penetration_note"]

    def test_no_note_without_ar_alert(self, monkeypatch):
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        pdf_mock = _RecordingExportTool("http://local/report.pdf")
        excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))
        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": ["资产负债率 = 75%，超过 70%，财务杠杆较高"]},
                                           ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        assert "cashflow_penetration_note" not in exported


class TestArbiterVerdictMarking:
    """50d：仲裁 verdict=需重新分析 时降级可见未完成标记（不阻断输出，adjustments 仍应用）。"""

    def test_incomplete_verdict_marked_in_reply_and_ledger(self, monkeypatch):
        from unittest.mock import MagicMock
        # 50f：_ARBITER_INCOMPLETE（verdict 需重新分析）+ 重试失败 → 未完成标记 + 继续导出
        pdf_mock, excel_mock, _hm, _rd, _tr = _debate_env(monkeypatch, _ARBITER_INCOMPLETE)
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)

        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 50f 改：仅「需重新分析」置未完成横幅，且不再真阻断（强可见警告 + 继续导出完整产物）
        assert "仲裁未完成" in text
        assert "需补充" not in text
        # 导出/图表照常执行（不产无效文件的真阻断已关闭）
        assert pdf_mock.calls and excel_mock.calls
        assert _hm.calls and _rd.calls

    def test_supplement_verdict_not_marked_incomplete(self, monkeypatch):
        """放宽后「需补充」视为已通过但建议追加程序：无未完成横幅、无补全重试、导出照常。"""
        from unittest.mock import MagicMock
        pdf_mock, excel_mock, _hm, _rd, _tr = _debate_env(
            monkeypatch,
            '【仲裁结论】需补充\n【裁定JSON】{"adjustments":[],"verdict":"需补充"}')
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)

        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=_RISK_JSON))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 「需补充」不再触发未完成标记与补全重试，产物照常导出
        assert "仲裁未完成" not in text
        assert "仲裁补全重试" not in text
        assert pdf_mock.calls and excel_mock.calls
        assert _hm.calls and _rd.calls


class TestScorePlaceholderAndIndexInjection:
    """50e：正文评分占位符回填 + 风险索引表注入（JSON 驱动）。"""

    def _env(self, monkeypatch, arbiter_text):
        from unittest.mock import MagicMock
        llm = _review_llm(arbiter=arbiter_text, c2=_c2_aligned_payload())
        monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kw: llm)
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", True)
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)
        pdf_mock = _RecordingExportTool("http://local/report.pdf")
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report",
                            _RecordingExportTool("http://local/workpaper.xlsx"))
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))
        return pdf_mock

    def _msgs(self, ai_body):
        msgs = [HumanMessage(content="请分析该公司年报")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=ai_body))
        return msgs

    def test_placeholder_backfilled_with_final_score(self, monkeypatch):
        """正文占位符「系统兜底计算 | —」→ 回填底线分（修复 V4 寻宝缺陷）。
        场景：3 项重要 → 预锁 26；仲裁 R004 升级+新增 R005 → 5 项重要 → 51 高风险。"""
        self._env(monkeypatch, _ARBITER_LEVEL_CHANGE)
        ai_body = ("四、量化风险评分\n| 评分模型 | 分值 | 判定 |\n"
                   "| 综合评分 | 系统兜底计算 | — | 以系统结果为准 |\n\n"
                   f"```json\n{_FIVE_IMPORTANT_LEDGER}\n```")
        wrapper = _AgentWrapper(_FakeAgent(self._msgs(ai_body)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "系统兜底计算" not in text
        assert "51.0分（高风险）" in text

    def test_risk_index_table_injected(self, monkeypatch):
        """风险索引表注入最终消息（含仲裁新增条目，修复 R006 脱节）。"""
        self._env(monkeypatch, _ARBITER_NEW_DISCLOSURE)
        ai_body = f"分析完成：\n```json\n{_RISK_JSON}\n```"
        wrapper = _AgentWrapper(_FakeAgent(self._msgs(ai_body)))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "风险与待复核提示清单（系统生成，与审计底稿同源）" in text
        # 仲裁新增披露合规条目出现在索引表（维度前缀 DIS-）
        assert "DIS-001" in text


class TestArbiterCompletionRetry:
    """50e：仲裁补全重试（verdict 需重新分析 → 重试一轮）。"""

    def test_retry_success_clears_incomplete(self, monkeypatch):
        """重试返回 verdict=通过 → 未完成状态清除，无横幅。"""
        from unittest.mock import MagicMock
        llm = _review_llm(
            arbiter='【仲裁结论】需重新分析\n【裁定JSON】{"adjustments":[],"verdict":"需重新分析"}',
            other='【仲裁结论】通过\n【裁定JSON】{"adjustments":[],"verdict":"通过"}')
        monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kw: llm)
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", True)
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)
        monkeypatch.setattr(agent_module, "export_pdf_report",
                            _RecordingExportTool("http://local/report.pdf"))
        monkeypatch.setattr(agent_module, "export_excel_report",
                            _RecordingExportTool("http://local/workpaper.xlsx"))
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))
        msgs = [HumanMessage(content="请分析")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=f"```json\n{_RISK_JSON}\n```"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "仲裁补全重试" in text              # 保留重试记录
        assert "仲裁未完成" not in text              # 重试成功 → 无横幅
        assert "仲裁状态：需补充" not in text


class TestPendingVerificationMarking:
    """50d：confidence < 0.5 的条目标记 pending_verification（不动等级与统计），
    并写入待核实注记（实测 18:37 版 R005 置信度 0.45 却强行定级「一般」）。"""

    def test_low_confidence_marked_pending(self, monkeypatch):
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        pdf_mock = _RecordingExportTool("http://local/report.pdf")
        excel_mock = _RecordingExportTool("http://local/workpaper.xlsx")
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", excel_mock)
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))

        ledger = json.dumps({"company_info": {"company_name": "集成测试公司", "report_year": "2025"},
                             "risk_details": [
                                 {"risk_id": "R005", "dimension": "持续经营风险", "level": "一般",
                                  "title": "海外子公司净资产为负", "confidence": 0.45},
                                 {"risk_id": "R001", "dimension": "财务错报风险", "level": "重要",
                                  "title": "应收激增", "confidence": 0.62},
                             ], "overall_assessment": "存在风险信号"}, ensure_ascii=False)
        msgs = [HumanMessage(content="请分析该公司年报的审计风险")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
            ToolMessage(content="{}", name=SEARCH_TOOL, tool_call_id="call_s"),
            ToolMessage(content="{}", name=SCORE_TOOL, tool_call_id="call_sc"),
        ]
        msgs.append(AIMessage(content=f"分析完成，风险台账如下：\n```json\n{ledger}\n```"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        wrapper.invoke({"messages": []})
        assert len(pdf_mock.calls) == 1
        exported = json.loads(pdf_mock.calls[0]["risk_report_json"])
        r005 = next(r for r in exported["risk_details"] if r["risk_id"] == "R005")
        assert r005["pending_verification"] is True
        assert "R005" in exported["pending_verification_note"]
        # 等级与统计不动（仍是「一般」）
        assert r005["level"] == "一般"
        r001 = next(r for r in exported["risk_details"] if r["risk_id"] == "R001")
        assert "pending_verification" not in r001


class TestSingleModule50eApplicability:
    """50e 适用性：财务健康度诊断 / 合规与经营风险扫描两个单模块同样走
    _post_process（main.py 仅 synthesis 走三阶段串跑），50e 机制（底线预锁、
    补全重试、占位符回填、索引表注入、语义编号）不门控模块，均适用。"""

    def _env(self, monkeypatch):
        from unittest.mock import MagicMock
        llm = _review_llm(
            arbiter='【仲裁结论】通过\n【裁定JSON】{"adjustments":[],"verdict":"通过"}')
        monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kw: llm)
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", True)
        score_stub = MagicMock()
        score_stub.invoke.return_value = _score_stub_json()
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", score_stub)
        pdf_mock = _RecordingExportTool("http://local/report.pdf")
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report",
                            _RecordingExportTool("http://local/workpaper.xlsx"))
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))
        return pdf_mock

    def test_financial_module_gets_50e_features(self, monkeypatch):
        """财务健康度诊断模块：索引表注入 + 语义编号 + 评分归一化说明均生效。"""
        from unittest.mock import MagicMock
        pdf_mock = self._env(monkeypatch)
        # 单模块缺披露/校验维度 → score 工具返回缺维度结果（归一化说明应进评分卡）
        partial_stub = MagicMock()
        partial_stub.invoke.return_value = json.dumps({
            "score": 10.5, "level": "低风险", "level_key": "low",
            "breakdown": {"financial": 10.5, "disclosure": "未获取", "validation": "未获取"},
            "weights": {"financial": 1.0, "disclosure": 0.0, "validation": 0.0},
            "notes": ["披露合规、数据校验维度未获取，已按剩余维度权重归一化"],
        }, ensure_ascii=False)
        monkeypatch.setattr("tools.risk_scorer.calculate_comprehensive_score", partial_stub)
        ledger = json.dumps({"company_info": {"company_name": "集成测试公司"},
                             "risk_details": [
                                 {"risk_id": "R001", "dimension": "财务错报风险",
                                  "level": "重要", "title": "应收激增", "confidence": 0.65},
                                 {"risk_id": "R002", "dimension": "持续经营风险",
                                  "level": "重要", "title": "现金流为负", "confidence": 0.6},
                             ], "overall_assessment": "存在风险信号"}, ensure_ascii=False)
        msgs = [HumanMessage(content="【模块:财务健康度诊断】请分析")]
        msgs += [
            ToolMessage(content=json.dumps({"failed_checks": 0}, ensure_ascii=False),
                        name=VALIDATE_TOOL, tool_call_id="call_v"),
            ToolMessage(content=json.dumps({"alerts": []}, ensure_ascii=False),
                        name=CALCULATE_TOOL, tool_call_id="call_c"),
        ]
        msgs.append(AIMessage(content=f"分析完成：\n```json\n{ledger}\n```"))
        wrapper = _AgentWrapper(_FakeAgent(msgs), module="financial")
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 50e 索引表与语义编号在单模块同样注入
        assert "风险与待复核提示清单（系统生成，与审计底稿同源）" in text
        assert "FIN-001" in text
        # 单模块缺披露/校验维度 → 评分卡含归一化说明（50d/50e 透明化）
        assert "未获取" in text
        # 单模块导出携带 module 参数（仅财务报告）
        assert pdf_mock.calls[0].get("module") == "financial"

    def test_compliance_module_gets_50e_features(self, monkeypatch):
        """合规与经营风险扫描模块：索引表注入 + DIS 语义编号生效。"""
        self._env(monkeypatch)
        ledger = json.dumps({"company_info": {"company_name": "集成测试公司"},
                             "risk_details": [
                                 {"risk_id": "R003", "dimension": "disclosure_compliance",
                                  "level": "重要", "title": "信披充分性", "confidence": 0.6},
                             ], "overall_assessment": "存在风险信号"}, ensure_ascii=False)
        msgs = [HumanMessage(content="【模块:合规与经营风险扫描】请分析")]
        msgs += [
            ToolMessage(content=json.dumps({"compliance_score": 90, "risk_score": 10},
                                           ensure_ascii=False),
                        name=DISCLOSURE_TOOL, tool_call_id="call_d"),
        ]
        msgs.append(AIMessage(content=f"分析完成：\n```json\n{ledger}\n```"))
        wrapper = _AgentWrapper(_FakeAgent(msgs), module="compliance")
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        assert "风险与待复核提示清单（系统生成，与审计底稿同源）" in text
        assert "DIS-001" in text


class TestScoreBackfillRun:
    """50f：评分兜底代跑——三维度全空 + 长年报文本 → 披露检查代跑，评分非 null
    （治「LLM 跳工具导致诚实 null」，让评分卡尽量有真实数据）。"""

    def test_backfill_run_produces_non_null_score(self, monkeypatch):
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report",
                            _RecordingExportTool("http://local/report.pdf"))
        monkeypatch.setattr(agent_module, "export_excel_report",
                            _RecordingExportTool("http://local/workpaper.xlsx"))
        monkeypatch.setattr(agent_module, "generate_risk_heatmap",
                            _RecordingExportTool("/local_storage/charts/h.png"))
        monkeypatch.setattr(agent_module, "generate_radar_chart",
                            _RecordingExportTool("/local_storage/charts/r.png"))
        monkeypatch.setattr(agent_module, "generate_trend_chart",
                            _RecordingExportTool("/local_storage/charts/t.png"))
        long_text = "中国石油天然气股份有限公司 2025 年半年度报告。" * 100  # >2000 字
        msgs = [HumanMessage(content="请分析以下年报：\n" + long_text)]
        msgs.append(AIMessage(content="分析完成，整体稳健。"))
        wrapper = _AgentWrapper(_FakeAgent(msgs))
        result = wrapper.invoke({"messages": []})
        text = _last_ai_text(result)
        # 披露维度代跑后有数据 → 评分非 null（评分行非「未获取/无法判定」）
        assert "未获取/无法判定" not in text
