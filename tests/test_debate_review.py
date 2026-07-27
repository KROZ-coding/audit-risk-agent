"""多智能体辩论复核路径的最小隔离测试

背景（回归防护）：
    _post_process 在 REVIEW_ENABLED 开启时会调用 _run_debate 触发三轮 LLM 辩论
    （风险关注方 → 风险否定方 → 裁判仲裁）。既有测试为规避真实 LLM 网络调用，
    统一将 REVIEW_ENABLED 置为 False，导致该复核路径长期缺乏覆盖：无法保证
    ① 三方意见被正确拼接进最终 AIMessage；② LLM 异常时的降级行为符合预期。

    本测试通过 mock 掉 agents.agent.ChatOpenAI（返回可控的假响应/抛异常），
    在不发起真实 LLM 调用的前提下隔离验证复核路径两条分支：
      - 正常路径：三方意见拼接进最终 AIMessage（含结构化三段标记）。
      - 异常路径：_run_debate 静默返回 None（保留既有约定），且 _post_process
        遵循「降级可见原则」在报告中追加醒目的复核失败提示。
"""
import sys
import os
import json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.agent as agent_mod
from agents.agent import (
    _AgentWrapper,
    _apply_arbiter_adjustments,
    _extract_arbiter_adjustments,
)


# ── 三方辩论的可控假响应（用于断言拼接内容来源清晰可辨）──
ADVOCATE_TEXT = "关注方意见_AAA"
SKEPTIC_TEXT = "否定方意见_BBB"
ARBITER_TEXT = "裁判仲裁_CCC"

# 最终 AIMessage 携带的风险台账 JSON（须含 company_info / risk_details 供提取）
FINAL_RISK_JSON = (
    "分析完成，风险台账如下：\n"
    '{"company_info": {"company_name": "测试公司", "report_year": "2025"}, '
    '"risk_details": [{"dimension": "财务错报风险", "level": "重大"}], '
    '"overall_assessment": "存在一定风险信号"}'
)


class _FakeResp:
    """模拟 ChatOpenAI.invoke 返回的响应对象（仅需 .content 属性）。"""

    def __init__(self, content):
        self.content = content


def _fake_llm_ok():
    """构造正常三轮辩论的假 LLM：三次 invoke 依次返回三方意见。"""
    llm = MagicMock()
    llm.invoke.side_effect = [
        _FakeResp(ADVOCATE_TEXT),
        _FakeResp(SKEPTIC_TEXT),
        _FakeResp(ARBITER_TEXT),
    ]
    return llm


def _fake_llm_raises():
    """构造首轮即抛异常的假 LLM，复现 LLM 调用失败场景。"""
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("模拟 LLM 服务不可用")
    return llm


def _tool_msg(name, content):
    """构造带稳定 id / tool_call_id 的 ToolMessage。"""
    return ToolMessage(content=content, name=name, tool_call_id=f"call_{name}", id=f"tm_{name}")


def _build_pipeline_messages():
    """构造一条完整且顺序合法的工具链消息序列 + 最终风险台账 AIMessage。

    包含 export_pdf_report / export_excel_report，使 _post_process 判定导出已完成、
    不触发兜底导出（避免文件系统 / 网络副作用），从而聚焦于复核路径的验证。
    """
    return [
        HumanMessage(content="请分析测试公司 2025 年报"),
        AIMessage(content="开始分析"),
        _tool_msg("validate_financial_data", json.dumps({"failed_checks": 0, "passed": True}, ensure_ascii=False)),
        _tool_msg("calculate_financial_indicators", json.dumps({"alerts": []}, ensure_ascii=False)),
        _tool_msg("check_disclosure_compliance", json.dumps({"risk_score": 20}, ensure_ascii=False)),
        _tool_msg("search_regulations", json.dumps({"hits": []}, ensure_ascii=False)),
        _tool_msg("calculate_comprehensive_score", json.dumps({"score": 20}, ensure_ascii=False)),
        _tool_msg("export_pdf_report", "pdf_url"),
        _tool_msg("export_excel_report", "excel_url"),
        AIMessage(content=FINAL_RISK_JSON),
    ]


class TestRunDebateUnit:
    """直接对 _run_debate 做单元级隔离验证（不经 _post_process）。"""

    def test_concatenates_three_opinions(self, monkeypatch):
        """正常路径：返回文本以结构化三段标记拼接三方意见。"""
        fake = _fake_llm_ok()
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        wrapper = _AgentWrapper(object())
        out = wrapper._run_debate('{"risk_details": []}')

        assert out is not None
        # 三段结构化标记 + 三方意见内容均须出现，且顺序为 关注方 → 否定方 → 裁判
        assert "【风险关注方】" in out and ADVOCATE_TEXT in out
        assert "【风险否定方】" in out and SKEPTIC_TEXT in out
        assert "【裁判仲裁】" in out and ARBITER_TEXT in out
        assert out.index(ADVOCATE_TEXT) < out.index(SKEPTIC_TEXT) < out.index(ARBITER_TEXT)
        # 严格三轮 LLM 调用（关注方 / 否定方 / 裁判）
        assert fake.invoke.call_count == 3

    def test_returns_none_on_llm_exception(self, monkeypatch):
        """异常路径：LLM 抛异常时静默返回 None（保留既有约定行为）。"""
        fake = _fake_llm_raises()
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        wrapper = _AgentWrapper(object())
        assert wrapper._run_debate("{}") is None


class TestPostProcessDebateAppending:
    """经 _post_process 端到端验证复核意见对最终 AIMessage 的可见影响。"""

    def test_debate_opinions_appended_to_final_ai_message(self, monkeypatch):
        """REVIEW_ENABLED=True 且 mock LLM 时，三方意见拼接进最终 AIMessage。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = _fake_llm_ok()
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        wrapper = _AgentWrapper(object())
        result = wrapper._post_process({"messages": _build_pipeline_messages()})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        assert "审计合伙人复核意见" in final_ai.content
        assert ADVOCATE_TEXT in final_ai.content
        assert SKEPTIC_TEXT in final_ai.content
        assert ARBITER_TEXT in final_ai.content

    def test_visible_notice_on_debate_failure(self, monkeypatch):
        """LLM 异常导致复核失败时，报告中须追加醒目的失败提示（降级可见）。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = _fake_llm_raises()
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        wrapper = _AgentWrapper(object())
        result = wrapper._post_process({"messages": _build_pipeline_messages()})

        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        # 降级可见原则：不得静默——须出现复核失败的醒目提示
        assert "⚠️ 多智能体复核未能完成" in final_ai.content
        # 且不得出现任何伪造的正常复核意见
        assert ADVOCATE_TEXT not in final_ai.content


# ── 仲裁回写相关的可控裁定文本（含干扰项：理由内嵌大括号）──
ARBITER_WITH_JSON = (
    "【逐条裁定】R001 采纳否定方意见，降为重要\n"
    "【仲裁结论】需补充\n"
    '【裁定JSON】{"adjustments":[{"risk_id":"R001","final_level":"重要",'
    '"reason":"证据链{注:函证未回}不完整"},'
    '{"risk_id":"R999","final_level":"重大","reason":"不存在的条目"},'
    '{"risk_id":"R002","final_level":"致命","reason":"非法等级"}],"verdict":"需补充"}'
)

# 含 risk_id 的风险台账（供回写匹配）
RISK_JSON_WITH_IDS = json.dumps({
    "company_info": {"company_name": "测试公司", "report_year": "2025"},
    "risk_details": [
        {"risk_id": "R001", "dimension": "财务错报风险", "level": "重大"},
        {"risk_id": "R002", "dimension": "持续经营风险", "level": "一般"},
    ],
    "overall_assessment": "存在一定风险信号",
}, ensure_ascii=False)


class TestArbiterWriteback:
    """仲裁【裁定JSON】解析与风险台账回写（白名单 + 可追溯 + 防污染）。"""

    def test_extract_adjustments_handles_braces_in_reason(self):
        """裁定理由内嵌大括号时，字符串感知扫描仍能完整截取裁定 JSON。"""
        adjustments = _extract_arbiter_adjustments(ARBITER_WITH_JSON)
        assert len(adjustments) == 3
        assert adjustments[0]["risk_id"] == "R001"
        assert "函证未回" in adjustments[0]["reason"]

    def test_extract_returns_empty_without_marker_or_bad_json(self):
        """无标记 / 非法 JSON 均降级为空列表，不阻断主流程。"""
        assert _extract_arbiter_adjustments("无裁定段落的普通文本") == []
        assert _extract_arbiter_adjustments("【裁定JSON】{broken") == []

    def test_apply_whitelist_and_traceability(self):
        """回写规则：未知 risk_id 丢弃、非法等级丢弃、变更保留 original_level。"""
        adjustments = _extract_arbiter_adjustments(ARBITER_WITH_JSON)
        out_json, applied = _apply_arbiter_adjustments(RISK_JSON_WITH_IDS, adjustments)
        # 仅 R001 被应用（R999 无匹配条目、R002 等级不在白名单）
        assert applied == 1
        parsed = json.loads(out_json)
        r1 = next(rd for rd in parsed["risk_details"] if rd["risk_id"] == "R001")
        assert r1["level"] == "重要"
        assert r1["original_level"] == "重大"           # 可追溯
        assert "函证未回" in r1["arbiter_note"]
        # R002 不受非法等级污染
        r2 = next(rd for rd in parsed["risk_details"] if rd["risk_id"] == "R002")
        assert r2["level"] == "一般" and "original_level" not in r2

    def test_fallback_export_receives_adjusted_ledger(self, monkeypatch):
        """集成：辩论前置于兜底导出后，导出工具收到的是仲裁回写后的台账。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = MagicMock()
        fake.invoke.side_effect = [
            _FakeResp(ADVOCATE_TEXT),
            _FakeResp(SKEPTIC_TEXT),
            _FakeResp(ARBITER_WITH_JSON),
        ]
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        # 捕获兜底导出收到的 risk_report_json；图表也 mock 掉避免真实 matplotlib 绘图落盘
        pdf_mock = MagicMock(); pdf_mock.invoke.return_value = "/local_storage/reports/x.pdf"
        excel_mock = MagicMock(); excel_mock.invoke.return_value = "/local_storage/reports/x.xlsx"
        monkeypatch.setattr(agent_mod, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_mod, "export_excel_report", excel_mock)
        monkeypatch.setattr(agent_mod, "generate_risk_heatmap", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/h.png")))
        monkeypatch.setattr(agent_mod, "generate_radar_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/r.png")))

        # 构造未导出的合法链路（触发兜底导出），最后 AI 消息携带含 risk_id 的台账
        messages = [
            HumanMessage(content="请分析测试公司 2025 年报"),
            _tool_msg("validate_financial_data", json.dumps({"failed_checks": 0}, ensure_ascii=False)),
            _tool_msg("calculate_financial_indicators", json.dumps({"alerts": []}, ensure_ascii=False)),
            _tool_msg("check_disclosure_compliance", json.dumps({"risk_score": 20}, ensure_ascii=False)),
            _tool_msg("search_regulations", json.dumps({"hits": []}, ensure_ascii=False)),
            _tool_msg("calculate_comprehensive_score", json.dumps({"score": 20}, ensure_ascii=False)),
            AIMessage(content=f"分析完成，风险台账如下：\n{RISK_JSON_WITH_IDS}"),
        ]

        wrapper = _AgentWrapper(object())
        wrapper._post_process({"messages": messages})

        # 导出工具收到的台账应已包含仲裁后等级与追溯字段
        exported = pdf_mock.invoke.call_args[0][0]["risk_report_json"]
        parsed = json.loads(exported)
        r1 = next(rd for rd in parsed["risk_details"] if rd["risk_id"] == "R001")
        assert r1["level"] == "重要" and r1["original_level"] == "重大"
        # Excel 与 PDF 收到同一份回写后台账
        assert excel_mock.invoke.call_args[0][0]["risk_report_json"] == exported
