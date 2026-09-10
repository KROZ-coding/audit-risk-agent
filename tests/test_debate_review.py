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
import re
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.agent as agent_mod
from agents.agent import (
    ADVOCATE_SYSTEM_PROMPT,
    ARBITER_SYSTEM_PROMPT,
    C2_SEMANTIC_SYSTEM_PROMPT,
    SKEPTIC_SYSTEM_PROMPT,
    _AgentWrapper,
    _amount_mismatch_warning,
    _anchor_system_alerts,
    _apply_arbiter_adjustments,
    _count_risk_levels,
    _drop_llm_fabricated_validation_risks,
    _enforce_risk_level_floor,
    _extract_arbiter_adjustments,
    _score_mismatch_warning,
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


def _review_llm(*, advocate=ADVOCATE_TEXT, skeptic=SKEPTIC_TEXT, arbiter=ARBITER_TEXT,
                c2='{"checks": []}'):
    """按系统提示词分派的复核假 LLM（C2 两次隔离判断 + C1 三轮串行）。

    以 SystemMessage 内容分派，而非依赖调用顺序——C2 的两次判断在线程池中
    并发执行，按顺序派发会产生竞态。advocate/skeptic/arbiter 可传字符串或
    字符串序列（模拟重试等多次调用，不足时复用最后一个）。
    """
    seq = {
        "advocate": [advocate] if isinstance(advocate, str) else list(advocate),
        "skeptic": [skeptic] if isinstance(skeptic, str) else list(skeptic),
        "arbiter": [arbiter] if isinstance(arbiter, str) else list(arbiter),
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
            return _FakeResp(_next("advocate"))
        if system == SKEPTIC_SYSTEM_PROMPT:
            return _FakeResp(_next("skeptic"))
        if system == ARBITER_SYSTEM_PROMPT:
            return _FakeResp(_next("arbiter"))
        return _FakeResp("")

    llm = MagicMock()
    llm.invoke.side_effect = _dispatch
    return llm


def _fake_llm_ok():
    """构造正常复核链路的假 LLM：C2 两次判断 + 正方/反方/仲裁三方意见。"""
    return _review_llm()


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
        # C2 两次隔离判断 + C1 严格三轮串行（关注方 / 否定方 / 裁判）
        assert fake.invoke.call_count == 5
        assert "【C2关键语义复核】" in out

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
    "【仲裁结论】通过\n"
    '【裁定JSON】{"adjustments":[{"risk_id":"R001","final_level":"重要",'
    '"reason":"证据链{注:函证未回}不完整"},'
    '{"risk_id":"R999","final_level":"重大","reason":"不存在的条目"},'
    '{"risk_id":"R002","final_level":"致命","reason":"非法等级"}],"verdict":"通过"}'
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
        adjustments, verdict = _extract_arbiter_adjustments(ARBITER_WITH_JSON)
        assert len(adjustments) == 3
        assert adjustments[0]["risk_id"] == "R001"
        assert "函证未回" in adjustments[0]["reason"]
        # 50d：verdict 同步解析（50f：正常路径 fixture 用「通过」，避免触发真阻断）
        assert verdict == "通过"

    def test_extract_returns_empty_without_marker_or_bad_json(self):
        """无标记 / 非法 JSON 均降级为 (空列表, 空 verdict)，不阻断主流程。"""
        assert _extract_arbiter_adjustments("无裁定段落的普通文本") == ([], "")
        assert _extract_arbiter_adjustments("【裁定JSON】{broken") == ([], "")
        # 无 verdict 字段时 verdict 为空串（adjustments 仍解析）
        adjustments, verdict = _extract_arbiter_adjustments(
            '【裁定JSON】{"adjustments":[{"risk_id":"R001","final_level":"重要"}]}')
        assert len(adjustments) == 1 and verdict == ""

    def test_apply_whitelist_and_traceability(self):
        """回写规则：未知 risk_id 丢弃、非法等级丢弃、变更保留 original_level。"""
        adjustments, _v = _extract_arbiter_adjustments(ARBITER_WITH_JSON)
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

    def test_level_change_rewrites_judgment_chain(self):
        """等级变更必须重写 [风险判定] 推理链（模板兜底），杜绝表头与结论互搏。"""
        ledger = json.dumps({
            "risk_details": [{
                "risk_id": "R001", "dimension": "财务错报风险", "title": "未分配利润勾稽差异",
                "level": "一般",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "未分配利润变动38,122百万元"},
                    {"step": "风险判定", "detail": "未分配利润勾稽差异较大，存在数据可靠性风险嫌疑，等级一般"},
                ],
            }],
        }, ensure_ascii=False)
        out_json, applied = _apply_arbiter_adjustments(
            ledger, [{"risk_id": "R001", "final_level": "重要", "reason": "勾稽差异率远超5%阈值"}])
        assert applied == 1
        r1 = next(r for r in json.loads(out_json)["risk_details"] if r["risk_id"] == "R001")
        judge = next(s for s in r1["reasoning_chain"] if s["step"] == "风险判定")
        assert "重要" in judge["detail"] and "等级一般" not in judge["detail"]
        assert "勾稽差异率远超5%阈值" in judge["detail"]

    def test_llm_rewrite_used_when_available(self):
        """传入 LLM 时 [风险判定] 采用 LLM 重写输出；无 LLM/失败时模板兜底。"""
        ledger = json.dumps({
            "risk_details": [{
                "risk_id": "R001", "dimension": "财务错报风险", "level": "一般",
                "reasoning_chain": [{"step": "风险判定", "detail": "原结论，等级一般"}],
            }],
        }, ensure_ascii=False)
        fake = MagicMock()
        fake.invoke.return_value = _FakeResp("经复核确认存在重大错报风险嫌疑，等级调整为重要")
        out_json, applied = _apply_arbiter_adjustments(
            ledger, [{"risk_id": "R001", "final_level": "重要", "reason": "证据链完整"}], llm=fake)
        r1 = next(r for r in json.loads(out_json)["risk_details"] if r["risk_id"] == "R001")
        judge = next(s for s in r1["reasoning_chain"] if s["step"] == "风险判定")
        assert judge["detail"] == "经复核确认存在重大错报风险嫌疑，等级调整为重要"

    def test_fallback_export_receives_adjusted_ledger(self, monkeypatch):
        """集成：辩论前置于兜底导出后，导出工具收到的是仲裁回写后的台账。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = _review_llm(arbiter=ARBITER_WITH_JSON)
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)

        # 捕获兜底导出收到的 risk_report_json；图表也 mock 掉避免真实 matplotlib 绘图落盘
        pdf_mock = MagicMock(); pdf_mock.invoke.return_value = "/local_storage/reports/x.pdf"
        excel_mock = MagicMock(); excel_mock.invoke.return_value = "/local_storage/reports/x.xlsx"
        monkeypatch.setattr(agent_mod, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_mod, "export_excel_report", excel_mock)
        monkeypatch.setattr(agent_mod, "generate_risk_heatmap", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/h.png")))
        monkeypatch.setattr(agent_mod, "generate_radar_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/r.png")))
        monkeypatch.setattr(agent_mod, "generate_trend_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/t.png")))

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


class TestScoreReferenceGuard:
    """辩论评分引用防伪：引用与系统评分不一致时附可见警示（防 72 分类幻觉）。"""

    def test_mismatched_reference_returns_warning(self):
        ctx = "综合评分（系统计算，禁止臆造或修改）：12.6 分（低风险）；基础分 12.6；模型/意见抬升 0.0。"
        text = "关注方：综合评分72分与台账内部风险评分矛盾，建议核查。"
        warn = _score_mismatch_warning(text, ctx)
        assert "与系统评分不一致的评分引用" in warn
        assert "72分" in warn and "12.6分" in warn

    def test_consistent_reference_no_warning(self):
        ctx = "综合评分（系统计算，禁止臆造或修改）：12.6 分（低风险）；基础分 12.6；模型/意见抬升 0.0。"
        text = "双方一致认可综合评分12.6分（低风险），无需调整。"
        assert _score_mismatch_warning(text, ctx) == ""

    def test_unparseable_context_no_warning(self):
        ctx = "综合评分（系统计算）：未能解析，辩论中不得引用具体评分数字。"
        text = "综合评分72分存在矛盾。"
        assert _score_mismatch_warning(text, ctx) == ""

    def test_post_process_appends_warning_for_fake_score(self, monkeypatch):
        """集成：辩论结果引用不存在的评分 → review_block 附警示行（不篡改原文）。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = _review_llm(
            advocate="关注方：系统综合评分72分与台账内部评分存在显著矛盾，据此提出信披风险。")
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)
        monkeypatch.setattr(agent_mod, "export_pdf_report", MagicMock(invoke=MagicMock(return_value="/local_storage/reports/x.pdf")))
        monkeypatch.setattr(agent_mod, "export_excel_report", MagicMock(invoke=MagicMock(return_value="/local_storage/reports/x.xlsx")))
        monkeypatch.setattr(agent_mod, "generate_risk_heatmap", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/h.png")))
        monkeypatch.setattr(agent_mod, "generate_radar_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/r.png")))
        monkeypatch.setattr(agent_mod, "generate_trend_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/t.png")))

        messages = [
            HumanMessage(content="请分析测试公司 2025 年报"),
            _tool_msg("validate_financial_data", json.dumps({"failed_checks": 0}, ensure_ascii=False)),
            _tool_msg("calculate_financial_indicators", json.dumps({"alerts": []}, ensure_ascii=False)),
            _tool_msg("check_disclosure_compliance", json.dumps({"risk_score": 20}, ensure_ascii=False)),
            _tool_msg("search_regulations", json.dumps({"hits": []}, ensure_ascii=False)),
            _tool_msg("calculate_comprehensive_score", json.dumps({"score": 12.6}, ensure_ascii=False)),
            AIMessage(content=FINAL_RISK_JSON),
        ]
        wrapper = _AgentWrapper(object())
        result = wrapper._post_process({"messages": messages})
        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        # 警示行已追加（降级可见），且 LLM 原文未被篡改
        assert "与系统评分不一致的评分引用" in final_ai.content
        assert "72分" in final_ai.content
        assert "AI 辩论可能产生幻觉" in final_ai.content


    def test_arbiter_new_item_dimension_normalized(self):
        """仲裁新增条目维度归一：LLM 裁定写"资产质量"（v28 实证 R005）→ 回写为
        financial_misstatement，且可被 _split_risks 归入财务子集（丢包根因修复）。"""
        from tools.pdf_export import _split_risks, FINANCIAL_DIMS
        ledger = json.dumps({"company_info": {"company_name": "测试公司"},
                             "risk_details": [], "overall_assessment": "x"},
                            ensure_ascii=False)
        out_json, applied = _apply_arbiter_adjustments(
            ledger,
            [{"risk_id": "R005", "final_level": "重要", "dimension": "资产质量",
              "title": "坏账准备计提充分性未单独评估", "evidence": "应收激增67.18%",
              "confidence": 0.6, "reason": "新增立项"}])
        assert applied == 1
        new = json.loads(out_json)["risk_details"][0]
        assert new["dimension"] == "financial_misstatement"
        assert new["source"] == "仲裁新增"
        # 归一后命中财务子集（财务健康报告不再漏该项）
        assert len(_split_risks([new], FINANCIAL_DIMS)) == 1

    def test_arbiter_writeback_syncs_message_layer(self, monkeypatch):
        """仲裁回写后，前端消息内嵌台账同步为最新——
        v27 实证：仲裁新增/改级条目只写入导出台账，消息内嵌块仍旧（5 条 vs 6 条）。"""
        monkeypatch.setattr(agent_mod, "REVIEW_ENABLED", True)
        fake = _review_llm(arbiter=ARBITER_WITH_JSON)
        monkeypatch.setattr(agent_mod, "ChatOpenAI", lambda **kw: fake)
        monkeypatch.setattr(agent_mod, "export_pdf_report", MagicMock(invoke=MagicMock(return_value="/local_storage/reports/x.pdf")))
        monkeypatch.setattr(agent_mod, "export_excel_report", MagicMock(invoke=MagicMock(return_value="/local_storage/reports/x.xlsx")))
        monkeypatch.setattr(agent_mod, "generate_risk_heatmap", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/h.png")))
        monkeypatch.setattr(agent_mod, "generate_radar_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/r.png")))
        monkeypatch.setattr(agent_mod, "generate_trend_chart", MagicMock(invoke=MagicMock(return_value="/local_storage/charts/t.png")))

        ledger_text = ('{"company_info": {"company_name": "测试公司"}, '
                       '"risk_details": [{"risk_id": "R001", "dimension": "财务错报风险", '
                       '"level": "重大", "title": "应收激增"}], '
                       '"overall_assessment": "存在风险信号"}')
        messages = [
            HumanMessage(content="请分析测试公司 2025 年报"),
            _tool_msg("validate_financial_data", json.dumps({"failed_checks": 0}, ensure_ascii=False)),
            _tool_msg("calculate_financial_indicators", json.dumps({"alerts": []}, ensure_ascii=False)),
            _tool_msg("check_disclosure_compliance", json.dumps({"risk_score": 20}, ensure_ascii=False)),
            _tool_msg("search_regulations", json.dumps({"hits": []}, ensure_ascii=False)),
            _tool_msg("calculate_comprehensive_score", json.dumps({"score": 20}, ensure_ascii=False)),
            AIMessage(content=f"分析完成：\n```json\n{ledger_text}\n```"),
        ]
        wrapper = _AgentWrapper(object())
        result = wrapper._post_process({"messages": messages})
        final_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
        block = re.search(r"```json\s*(\{.*\})\s*```", str(final_ai.content), re.S)
        assert block, "消息中应保留 ```json 台账块"
        embedded = json.loads(block.group(1))
        r1 = next(r for r in embedded["risk_details"] if r["risk_id"] == "R001")
        # 仲裁将 R001 从重大降为重要 → 消息层内嵌台账已同步（v27 缺陷修复）
        assert r1["level"] == "重要"


class TestPromptDataDiscipline:
    """辩论 prompt 数据纪律：内部证据锚定约束（禁止自行推导衍生数字）。"""

    def test_all_roles_anchor_internal_evidence(self):
        """三方 prompt 均须包含内部证据锚定约束（防 1370.86亿/48.2% 类自行推导）。"""
        from agents.agent import (ADVOCATE_SYSTEM_PROMPT, ARBITER_SYSTEM_PROMPT,
                                  SKEPTIC_SYSTEM_PROMPT)
        for p in (ADVOCATE_SYSTEM_PROMPT, SKEPTIC_SYSTEM_PROMPT, ARBITER_SYSTEM_PROMPT):
            assert "内部证据锚定" in p, "三方 prompt 均须含内部证据锚定约束"
            assert "禁止对台账金额做加减乘除" in p
            assert "台账中不存在的数字一律视为编造" in p

    def test_arbiter_prompt_has_red_flag_and_trend_discipline(self):
        """50b：仲裁 prompt 须含红旗信号锚定与趋势优先于绝对值两条数据纪律。"""
        from agents.agent import ARBITER_SYSTEM_PROMPT
        assert "红旗信号锚定" in ARBITER_SYSTEM_PROMPT
        assert "不得以企业性质/股东背景/行业地位为由降级" in ARBITER_SYSTEM_PROMPT
        assert "趋势优先于绝对值" in ARBITER_SYSTEM_PROMPT
        assert "不得仅以「绝对值低于行业基准」维持" in ARBITER_SYSTEM_PROMPT
        assert "必须正面回应趋势异常" in ARBITER_SYSTEM_PROMPT

    def test_prompts_have_50c_disciplines(self):
        """50c：仲裁/否定方 prompt 须含评分一致性、跨期穿透、期后事项口径、
        量化模型预警四条数据纪律（治 55/10.5 双分、OCF/NP 刻舟求剑、400 亿双标、
        Z-Score 央企洗白）。"""
        from agents.agent import ARBITER_SYSTEM_PROMPT, SKEPTIC_SYSTEM_PROMPT
        for p in (ARBITER_SYSTEM_PROMPT, SKEPTIC_SYSTEM_PROMPT):
            assert "跨期穿透" in p, "均须含跨期穿透纪律"
            assert "期后事项口径" in p, "均须含期后事项口径纪律"
            assert "量化模型预警" in p, "均须含量化模型预警纪律"
        assert "不得自行宣称「与综合评分一致/相符」" in ARBITER_SYSTEM_PROMPT
        assert "与低风险评分相符" in ARBITER_SYSTEM_PROMPT

    def test_prompts_have_50d_disciplines(self):
        """50d：config sp 第 9/10/11 条（评分纪律、红旗初始定级下限、企业性质不作
        降级独立理由+金额口径）与 ARBITER 口径统一裁定/证据不足纪律均有测试守护。"""
        import json as _json
        import os as _os
        from agents.agent import ARBITER_SYSTEM_PROMPT
        _cfg_path = _os.path.join(_os.path.dirname(__file__), "..", "config", "agent_llm_config.json")
        sp = _json.load(open(_cfg_path, encoding="utf-8"))["sp"]
        # sp 第 9 条：评分工具失败不得自行编造评分
        assert "我将自行计算评分" in sp and "自填评分" in sp
        # sp 第 10 条：红旗信号初始定级不得低于重要
        assert "初始定级不得低于" in sp and "存贷双高" in sp
        # sp 第 11 条：企业性质不作降级独立理由 + 金额口径标注
        assert "不得以企业性质" in sp and "辅助参考因素" in sp
        assert "主体与方向口径" in sp
        # ARBITER 50d 两条纪律
        assert "口径统一裁定" in ARBITER_SYSTEM_PROMPT
        assert "证据不足不得强行定级" in ARBITER_SYSTEM_PROMPT


class TestFabricatedValidationRiskInterception:
    """勾稽伪风险拦截（50b）：已通过校验科目 + 自编数字条目移入 excluded_items。"""

    VD_PASSED = {"data_validation": {"all_checks": [{
        "check": "未分配利润一致性",
        "passed": True,
        "retained_earnings_begin": 45755,
        "retained_earnings_end": 83993,
        "actual_change": 38238,
        "expected_change": 38122,
        "difference": 116,
        "difference_pct": "0.30%",
        "message": "未分配利润勾稽关系成立",
    }]}}

    def test_fabricated_arithmetic_entry_dropped(self):
        """条目引用已通过校验科目但数字系 LLM 自编（不含任何权威结果数字）→ 移入备查录。"""
        report_obj = {"risk_details": [
            {"risk_id": "R003", "title": "未分配利润勾稽差异异常",
             "evidence": "期末 83993 减期初 45755 应为 47911，实际差异 9789",
             "data_analysis": "勾稽差异 9789 万元，存在数据可靠性风险"},
            {"risk_id": "R004", "title": "存货跌价风险",
             "evidence": "存货占比较高", "data_analysis": ""},
        ]}
        dropped = _drop_llm_fabricated_validation_risks(report_obj, self.VD_PASSED)
        assert dropped == 1
        assert [r["risk_id"] for r in report_obj["risk_details"]] == ["R004"]
        assert report_obj["excluded_items"][0]["risk_id"] == "R003"

    def test_entry_quoting_authoritative_numbers_kept(self):
        """条目数字与权威结果有交集（引用真实差额）→ 保守保留。"""
        report_obj = {"risk_details": [
            {"risk_id": "R003", "title": "未分配利润勾稽核对",
             "evidence": "未分配利润勾稽差异 116，实际变动 38238",
             "data_analysis": "与校验器输出一致，无异常"},
        ]}
        dropped = _drop_llm_fabricated_validation_risks(report_obj, self.VD_PASSED)
        assert dropped == 0
        assert len(report_obj["risk_details"]) == 1
        assert "excluded_items" not in report_obj

    def test_failed_check_not_armed(self):
        """未通过的校验项不参与拦截（其差异条目属合法 V 系列风险）。"""
        vd = {"data_validation": {"all_checks": [{
            "check": "未分配利润一致性", "passed": False,
            "actual_change": 38238, "difference": 9789,
        }]}}
        report_obj = {"risk_details": [
            {"risk_id": "R003", "title": "未分配利润勾稽差异",
             "evidence": "差异 9789，勾稽未通过", "data_analysis": ""},
        ]}
        assert _drop_llm_fabricated_validation_risks(report_obj, vd) == 0
        assert len(report_obj["risk_details"]) == 1


class TestSystemAlertAnchoring:
    """系统红旗告警锚定保护（50b）：锚定标记 + 仲裁降级拦截。"""

    def test_anchor_marks_existing_entry(self):
        report_obj = {"risk_details": [
            {"risk_id": "R001", "title": "货币资金与短期借款并存异常",
             "evidence": "货币资金高企但利息支出远大于利息收入", "level": "重要"},
            {"risk_id": "R002", "title": "行业竞争加剧", "evidence": "", "level": "一般"},
        ]}
        tool_results = {"calculate_financial_indicators": json.dumps(
            {"alerts": ["货币资金(100)高于短期借款(50)，但利息支出(10)远大于利息收入(2)，存在'存贷双高'异常，需核查资金真实性"]},
            ensure_ascii=False)}
        n = _anchor_system_alerts(report_obj, tool_results)
        assert n == 1
        r1 = next(r for r in report_obj["risk_details"] if r["risk_id"] == "R001")
        assert r1["system_anchored"] is True
        r2 = next(r for r in report_obj["risk_details"] if r["risk_id"] == "R002")
        assert "system_anchored" not in r2

    def test_anchor_creates_entry_when_no_match(self):
        report_obj = {"risk_details": [
            {"risk_id": "R001", "title": "行业竞争加剧", "evidence": "", "level": "一般"},
        ]}
        tool_results = {"calculate_financial_indicators": json.dumps(
            {"alerts": ["营收增长但经营现金流为负，盈利质量存疑"]}, ensure_ascii=False)}
        n = _anchor_system_alerts(report_obj, tool_results)
        assert n == 1
        new = report_obj["risk_details"][1]
        assert new["system_anchored"] is True
        assert new["source"] == "系统红旗告警"
        assert new["dimension"] == "financial_misstatement"
        assert new["level"] == "重要"

    def test_arbiter_downgrade_of_anchored_blocked(self):
        """锚定条目被仲裁降级至「一般」→ 拒绝，保留原等级并留痕存档原裁定理由。"""
        ledger = json.dumps({"risk_details": [
            {"risk_id": "R001", "dimension": "财务错报风险", "title": "存贷双高异常",
             "level": "重要", "system_anchored": True},
        ]}, ensure_ascii=False)
        out_json, applied = _apply_arbiter_adjustments(
            ledger, [{"risk_id": "R001", "final_level": "一般", "reason": "央企背景雄厚"}])
        assert applied == 1
        r1 = json.loads(out_json)["risk_details"][0]
        assert r1["level"] == "重要"
        assert "系统红旗告警锚定：降级被拦截" in r1["arbiter_note"]
        assert "央企背景雄厚" in r1["arbiter_note"]

    def test_arbiter_upgrade_of_anchored_allowed(self):
        """锚定条目升级（重大）不受拦截。"""
        ledger = json.dumps({"risk_details": [
            {"risk_id": "R001", "dimension": "财务错报风险", "title": "存贷双高异常",
             "level": "重要", "system_anchored": True},
        ]}, ensure_ascii=False)
        out_json, applied = _apply_arbiter_adjustments(
            ledger, [{"risk_id": "R001", "final_level": "重大", "reason": "证据确凿"}])
        assert applied == 1
        assert json.loads(out_json)["risk_details"][0]["level"] == "重大"


class TestAmountReferenceGuard:
    """辩论金额引用防伪（50b）：未见于风险台账的金额附可见警示。"""

    LEDGER = json.dumps({"risk_details": [
        {"risk_id": "R001", "title": "应收激增",
         "evidence": "应收账款 47,911 百万元，同比增 67.18%"},
        {"risk_id": "R002", "title": "现金流为负",
         "evidence": "经营现金流 -402.65 亿元"},
    ]}, ensure_ascii=False)

    def test_unknown_amount_returns_warning(self):
        text = ("关注方：货币资金 402.65亿 与台账经营现金流 402.65亿 存在勾稽疑点，"
                "另有 47,911 与 9,789 差异，货币资金 200亿 未见于台账。")
        warn = _amount_mismatch_warning(text, self.LEDGER)
        assert "未见于风险台账的金额引用" in warn
        assert "9,789" in warn
        assert "200亿" in warn
        # 台账内已有金额（402.65亿/47,911）不列入警示
        assert "402.65亿" not in warn
        assert "47,911" not in warn

    def test_known_amounts_no_warning(self):
        text = "关注方：应收账款 47,911 百万元与营收增速背离，402.65 亿元现金流为负。"
        assert _amount_mismatch_warning(text, self.LEDGER) == ""

    def test_bad_ledger_no_warning(self):
        assert _amount_mismatch_warning("关注方：金额 9,789 异常", "{broken") == ""


class TestAmountToleranceAndPctGuard:
    """金额防伪容差匹配 + 百分比口径防伪（50d）：同额不同口径不得误报，
    辩论自算比例须警示仲裁统一口径（实测 18:37 版警告误报 951亿/24亿/1,536.68）。"""

    def test_unit_conversion_tolerance_no_warning(self):
        ledger = json.dumps({"risk_details": [
            {"risk_id": "R003", "title": "关联交易",
             "evidence": "关联方提供资金余额153,668百万元"},
            {"risk_id": "R005", "title": "海外子公司净资产为负",
             "evidence": "净资产-95,094百万元，本期净利润-2,438百万元"},
        ]}, ensure_ascii=False)
        text = "关注方：关联方提供资金1,536.68亿元，海外子公司净资产951亿元、亏损24亿元。"
        warn = _amount_mismatch_warning(text, ledger)
        assert "未见于风险台账的金额引用" not in warn

    def test_derived_pct_not_in_ledger_warns(self):
        """辩论自算比例（15.7%）未见于台账 → 警示裁定统一口径；台账已有比例不警示。"""
        ledger = json.dumps({"risk_details": [
            {"risk_id": "R004", "title": "油气资产减值",
             "evidence": "减值准备计提比例约13.55%（推导值）"},
        ]}, ensure_ascii=False)
        text = "否定方：计提比例约15.7%（台账数据直接计算），与13.55%口径不同。"
        warn = _amount_mismatch_warning(text, ledger)
        assert "比例引用" in warn
        assert "15.7%" in warn
        assert "13.55%" not in warn

    def test_fabricated_amounts_still_warn(self):
        """完全编造金额（200亿/9,789）在容差化后仍触发警示（回归防护）。"""
        ledger = json.dumps({"risk_details": [
            {"risk_id": "R001", "title": "应收激增",
             "evidence": "应收账款 47,911 百万元，同比增 67.18%"},
        ]}, ensure_ascii=False)
        text = "关注方：货币资金 200亿 未见于台账，另有 9,789 差异。"
        warn = _amount_mismatch_warning(text, ledger)
        assert "未见于风险台账的金额引用" in warn
        assert "200亿" in warn and "9,789" in warn


class TestRedFlagAnchorFloorRaise:
    """红旗锚定强制等级下限（50d）：命中红旗词的条目初始等级「一般」时
    强制提升为「重要」并留痕（实测 18:37 版 R002 锚定但等级仍一般）。"""

    def test_red_flag_raises_general_entry_to_important(self):
        report_obj = {"risk_details": [
            {"risk_id": "R002", "title": "货币资金与短期借款并存异常",
             "evidence": "货币资金高企但利息支出远大于利息收入", "level": "一般"},
        ]}
        tool_results = {"calculate_financial_indicators": json.dumps(
            {"alerts": ["货币资金(100)高于短期借款(50)，但利息支出(10)远大于利息收入(2)，存在'存贷双高'异常，需核查资金真实性"]},
            ensure_ascii=False)}
        n = _anchor_system_alerts(report_obj, tool_results)
        assert n >= 1
        r2 = report_obj["risk_details"][0]
        assert r2["level"] == "重要"
        assert r2["original_level"] == "一般"
        assert r2["system_anchored"] is True

    def test_anchored_new_entry_has_reasoning_chain(self):
        """锚定新建条目补 reasoning_chain（50d：消除与 LLM 条目的结构差异）。"""
        report_obj = {"risk_details": []}
        tool_results = {"calculate_financial_indicators": json.dumps(
            {"alerts": ["营收增长但经营现金流为负，盈利质量存疑"]}, ensure_ascii=False)}
        _anchor_system_alerts(report_obj, tool_results)
        new = report_obj["risk_details"][0]
        assert isinstance(new.get("reasoning_chain"), list)
        assert new["reasoning_chain"][0]["step"] == "系统预警"


class TestSemanticIds:
    """语义化编号（50e：维度前缀，确定性无冲突，解决 50d 指纹顺序冲突）。"""

    def test_assign_semantic_ids_by_dimension_prefix(self):
        from agents.agent import _assign_semantic_ids
        report_obj = {"risk_details": [
            {"risk_id": "R001", "dimension": "financial_misstatement", "title": "应收激增"},
            {"risk_id": "R002", "dimension": "related_party", "title": "存贷双高"},
            {"risk_id": "R003", "dimension": "financial_misstatement", "title": "炼化亏损"},
            {"risk_id": "S006", "dimension": "related_party", "title": "储气库收购定价"},
            {"risk_id": "R006", "dimension": "disclosure_compliance", "title": "信披充分性"},
            {"risk_id": "R999", "dimension": "其他维度", "title": "其他事项"},
        ]}
        _assign_semantic_ids(report_obj)
        ids = {r["risk_id"]: r["semantic_id"] for r in report_obj["risk_details"]}
        assert ids["R001"] == "FIN-001"
        assert ids["R003"] == "FIN-002"          # 组内按台账顺序编号
        assert ids["R002"] == "REL-001"
        assert ids["S006"] == "REL-002"          # S/R 前缀统一按维度（无指纹冲突）
        assert ids["R006"] == "DIS-001"
        assert ids["R999"] == "R999"             # 未知维度保留原 id

    def test_placeholder_backfill(self):
        """50e：正文评分占位符回填——「系统兜底计算 | —」→ 实际分数，
        底线触发时含上调说明（修复 V4 读者须滑到几千字后寻宝的缺陷）。"""
        from agents.agent import _sync_score_into_message

        class _Msg:
            content = ("四、量化风险评分\n| 评分模型 | 分值 | 判定 |\n"
                       "| 综合评分 | 系统兜底计算 | — | 以系统结果为准 |\n")
        msg = _Msg()
        changed = _sync_score_into_message(
            msg, {"score": 26.0, "level": "中等风险"},
            floor_note="风险等级底线规则：含 3 项重要级风险，强制上调至中等风险")
        assert changed
        assert "系统兜底计算" not in msg.content
        assert "26.0分（中等风险）" in msg.content
        assert "含 3 项重要级风险" in msg.content

    def test_placeholder_default_note(self):
        """无底线触发时占位符回填默认说明（不含占位关键词子串）。"""
        from agents.agent import _sync_score_into_message

        class _Msg:
            content = "| 综合评分 | 系统兜底计算 | — | 以系统结果为准 |"
        msg = _Msg()
        _sync_score_into_message(msg, {"score": 12.0, "level": "低风险"})
        assert "12.0分（低风险）" in msg.content and "系统计算结果" in msg.content
        assert "系统兜底计算" not in msg.content

    def test_risk_index_md_includes_all_entries(self):
        """50e：风险索引表（JSON 驱动）——含全部条目（含仲裁新增）、semantic_id、
        待核实标注（修复 V4 仲裁新增 R006 未写入正文的脱节）。"""
        from agents.agent import _assign_semantic_ids, _build_risk_index_md
        report_obj = {"risk_details": [
            {"risk_id": "R001", "dimension": "financial_misstatement", "title": "应收激增",
             "level": "重要", "confidence": 0.65},
            {"risk_id": "R006", "dimension": "disclosure_compliance", "title": "信披充分性",
             "level": "重要", "confidence": 0.45, "pending_verification": True,
             "source": "仲裁新增"},
        ]}
        _assign_semantic_ids(report_obj)
        md = _build_risk_index_md(report_obj)
        assert "最终风险清单（系统生成，与审计底稿同源）" in md
        assert "FIN-001" in md and "DIS-001" in md
        assert "R006" in md                       # 仲裁新增条目必然在索引表
        assert "【待核实】" in md                  # 置信度低的待核实标注


class TestDimensionCorrection:
    """50f：维度标签白名单 + 规则纠正（治分类器幻觉：V5 把期后关联收购标为监管处罚）。"""

    def test_related_acquisition_mislabeled_penalty_corrected(self):
        from agents.agent import _correct_dimensions
        report_obj = {"risk_details": [
            {"risk_id": "R004", "dimension": "regulatory_penalty",
             "title": "报告期后 400 亿关联收购", "evidence": "关联方收购储气库公司，对价400.16亿元"},
        ]}
        n = _correct_dimensions(report_obj)
        r = report_obj["risk_details"][0]
        assert n == 1
        assert r["dimension"] == "related_party"
        assert r["dimension_corrected_from"] == "regulatory_penalty"

    def test_penalty_keyword_keeps_penalty(self):
        """含处罚事实（警示函）的条目保留 regulatory_penalty，不误纠。"""
        from agents.agent import _correct_dimensions
        report_obj = {"risk_details": [
            {"risk_id": "R007", "dimension": "regulatory_penalty",
             "title": "公司收到警示函", "evidence": "交易所出具警示函"},
        ]}
        assert _correct_dimensions(report_obj) == 0
        assert report_obj["risk_details"][0]["dimension"] == "regulatory_penalty"

    def test_unknown_dimension_keyword_fallback(self):
        """白名单外维度 → 关键词归类兜底（油价/减值 → going_concern）。"""
        from agents.agent import _correct_dimensions
        report_obj = {"risk_details": [
            {"risk_id": "R008", "dimension": "市场风险",
             "title": "油价下行盈利承压", "evidence": "原油价格下跌致营收利润下滑"},
        ]}
        n = _correct_dimensions(report_obj)
        assert n == 1
        assert report_obj["risk_details"][0]["dimension"] == "going_concern"


class TestFinalSweepAndSystemConclusion:
    """50f：终局扫荡无条件化 + 系统结论块（治 V5 结论段捏造 72/100）。"""

    def test_final_sweep_replaces_fabricated_score_with_none(self):
        """快照 score=None 时，正文捏造的「72/100」被替换为「未获取/无法判定」。"""
        from agents.agent import _sync_score_into_message

        class _Msg:
            content = "六、综合结论\n综合风险评分72/100，处于70-80关注区间。\n"
        msg = _Msg()
        changed = _sync_score_into_message(
            msg, {"score": None, "level": "未获取/无法判定"})
        assert changed
        assert "72/100" not in msg.content
        assert "未获取/无法判定" in msg.content

    def test_system_conclusion_md_contains_index_and_notes(self):
        from agents.agent import _build_system_conclusion_md
        report_obj = {
            "comprehensive_score_snapshot": {"score": None, "level": "未获取/无法判定"},
            "level_floor_note": "风险等级底线规则：含 3 项重要级风险",
            "risk_details": [
                {"risk_id": "R001", "dimension": "financial_misstatement",
                 "semantic_id": "FIN-001", "title": "应收激增",
                 "level": "重要", "confidence": 0.65},
            ],
        }
        md = _build_system_conclusion_md(report_obj)
        assert "系统结论（模板渲染，与审计底稿同源，唯一权威）" in md
        assert "综合风险评分：未获取/无法判定（请人工复核）" in md
        assert "风险等级底线规则" in md
        assert "FIN-001" in md


class TestLevelFloorRule:
    """风险等级底线规则（50c）：最终台账等级分布与综合评级矛盾时强制上调。

    实测 17:59 版：最终台账 5 个重要级风险（R001-R005）仍评 10.5 分低风险，
    仲裁人书写「与低风险综合评分相符」的荒谬闭环——底线规则在确定性层强制纠偏。
    """

    def _report(self, levels, score=10.5, level="低风险", level_key="low"):
        return {
            "risk_details": [
                {"risk_id": f"R{i:03d}", "level": lv} for i, lv in enumerate(levels, 1)
            ],
            "comprehensive_score": {
                "score": score, "level": level, "level_key": level_key,
                "base_score": 10.5, "escalation": 0.0, "escalation_reasons": [],
            },
            "comprehensive_score_snapshot": {"score": score, "level": level},
            "overall_assessment": f"整体风险可控，综合风险评分{score:g}分（{level}）。",
        }

    def test_three_important_escalates_to_medium(self):
        report = self._report(["重要", "重要", "重要"])
        out = _enforce_risk_level_floor(report)
        assert out is not None
        new_json, warn = out
        import json as _json
        sd = _json.loads(new_json)
        assert sd["score"] == 26 and sd["level"] == "中等风险" and sd["level_key"] == "medium"
        assert "风险等级底线规则" in warn and "26 分" in warn
        assert report["level_floor_note"].startswith("风险等级底线规则")
        assert report["comprehensive_score_snapshot"] == {"score": 26.0, "level": "中等风险"}
        # overall_assessment 旧分表述被新快照重刷
        assert "10.5" not in report["overall_assessment"]
        assert "26.0分（中等风险）" in report["overall_assessment"]

    def test_five_important_escalates_to_high(self):
        report = self._report(["重要"] * 5)
        out = _enforce_risk_level_floor(report)
        assert out is not None
        import json as _json
        sd = _json.loads(out[0])
        assert sd["score"] == 51 and sd["level"] == "高风险" and sd["level_key"] == "high"

    def test_one_major_escalates_to_high(self):
        report = self._report(["重大", "一般", "一般"], score=40, level="中等风险", level_key="medium")
        out = _enforce_risk_level_floor(report)
        assert out is not None
        import json as _json
        sd = _json.loads(out[0])
        assert sd["score"] == 51 and sd["level"] == "高风险"

    def test_two_important_no_escalation(self):
        report = self._report(["重要", "重要", "一般"])
        assert _enforce_risk_level_floor(report) is None
        assert "level_floor_note" not in report

    def test_score_already_at_floor_no_escalation(self):
        report = self._report(["重要"] * 4, score=60, level="中等风险", level_key="medium")
        assert _enforce_risk_level_floor(report) is None

    def test_missing_score_returns_none(self):
        report = {"risk_details": [{"risk_id": "R001", "level": "重要"}] * 4}
        assert _enforce_risk_level_floor(report) is None

    def test_level_alias_counting(self):
        """等级别名同口径：高→major、中→important（防非标准取值漏计）。"""
        major, important = _count_risk_levels([
            {"level": "高"}, {"level": "中"}, {"level": "中"}, {"level": "低"},
        ])
        assert major == 1 and important == 2


class TestModelSignalAnchoring:
    """量化模型信号锚定（50c）：Z-Score 灰色/困境区、M-Score 超阈值不得被
    「央企背景」话术洗白——锚定为可见条目，仅可辩、不可删、须正面回应。"""

    def _tool_results(self, z_zone=None, z_score=1.62, m_score=-2.05):
        rm = {"risk_models": {
            "altman_z_score": {"available": z_zone is not None, "score": z_score,
                               "zone": z_zone} if z_zone else {"available": False},
            "beneish_m_score": {"available": True, "score": m_score},
        }}
        return {"calculate_risk_models": json.dumps(rm, ensure_ascii=False)}

    def test_z_grey_zone_anchors_general(self):
        report_obj = {"risk_details": []}
        n = _anchor_system_alerts(report_obj, self._tool_results(z_zone="灰色预警区"))
        assert n == 1
        new = report_obj["risk_details"][0]
        assert new["system_anchored"] is True
        assert new["source"] == "系统量化模型预警"
        assert new["level"] == "一般"          # 灰色区不抬升等级底线
        assert "灰色预警区" in new["title"]

    def test_z_distress_zone_anchors_important(self):
        report_obj = {"risk_details": []}
        n = _anchor_system_alerts(report_obj, self._tool_results(z_zone="财务困境区"))
        assert n == 1
        assert report_obj["risk_details"][0]["level"] == "重要"

    def test_m_score_above_threshold_anchors_important(self):
        report_obj = {"risk_details": []}
        n = _anchor_system_alerts(report_obj, self._tool_results(z_zone=None, m_score=-1.5))
        assert n == 1
        new = report_obj["risk_details"][0]
        assert new["level"] == "重要" and "M-Score" in new["title"]

    def test_no_model_signal_no_op(self):
        report_obj = {"risk_details": [{"risk_id": "R001", "title": "行业竞争", "level": "一般"}]}
        assert _anchor_system_alerts(report_obj, self._tool_results(z_zone=None, m_score=-2.5)) == 0
        assert len(report_obj["risk_details"]) == 1
