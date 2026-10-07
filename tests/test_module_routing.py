# -*- coding: utf-8 -*-
"""三模块架构测试：模块路由、工具子集裁剪、综合研判串跑逻辑。

背景：架构定稿为「单桌面程序载体 + 内部三层任务单元」，采用混合运行模式——
点单模块只跑该模块（工具裁剪后更快），点综合研判则串跑 ①→②→③ 并交叉验证。
本测试锁定：
- 模块标记能被正确识别，未命中时回退全量链路（兼容传统文字输入）
- 三个模块的工具子集边界正确（关键：合规模块不含财务计算工具，反之亦然）
- 串跑的阶段定义、载荷构造与摘要提取行为正确
- Agent 缓存键按「模式+模块」区分，避免不同模块串用同一实例
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import MODULE_MARKERS, _detect_module, GraphService
from agents.agent import MODULE_TOOLS
from agents.pipeline import (
    SYNTHESIS_STAGES, STAGE_INSTRUCTIONS, CROSS_VALIDATION_INSTRUCTION,
    build_stage_payload, extract_stage_summary,
)


def _payload(text):
    return {"messages": [{"role": "user", "content": text}]}


class TestModuleRouting:
    """_detect_module：消息关键词标记 → 模块标识"""

    def test_detects_each_module(self):
        for key, marker in MODULE_MARKERS.items():
            assert _detect_module(_payload(f"{marker}请分析这份年报")) == key

    def test_no_marker_returns_none(self):
        """无标记时返回 None，走全量工具链路（保持对传统用法的兼容）。"""
        assert _detect_module(_payload("请分析贵州茅台的年报风险")) is None

    def test_empty_payload_safe(self):
        assert _detect_module({}) is None
        assert _detect_module(None) is None

    def test_marker_anywhere_in_text(self):
        """短消息里标记出现在首部前缀（64 字符）内即可识别（前端注入位置小幅变化不失效）。"""
        marker = MODULE_MARKERS["financial"]
        assert _detect_module(_payload(f"年报内容……{marker}……")) == "financial"

    def test_langchain_message_object(self):
        """兼容对象形态消息（与 _payload_wants_fast_mode 同构要求）。"""
        class Msg:
            content = MODULE_MARKERS["synthesis"] + " 分析"
        assert _detect_module({"messages": [Msg()]}) == "synthesis"

    def test_marker_buried_in_long_document_text_is_ignored(self):
        """G1：年报正文（不可信长文本）中埋入模块标记不得切换工具链路由"""
        marker = MODULE_MARKERS["compliance"]
        report_text = "年报正文，" * 1200 + f"正文引用了{marker}的定义作背景说明。"
        assert len(report_text) > 2000
        assert _detect_module(_payload(report_text)) is None

    def test_marker_deep_in_medium_message_ignored(self):
        """G1：标记不在首部前缀内（即使消息不长）也不路由——只认首部注入"""
        marker = MODULE_MARKERS["financial"]
        filler = "背景说明。" * 30
        assert _detect_module(_payload(f"{filler}{marker}")) is None


class TestModuleToolSubsets:
    """MODULE_TOOLS：三模块工具子集的边界"""

    def test_all_modules_defined(self):
        assert set(MODULE_TOOLS) == {"financial", "compliance", "synthesis", "outlook"}

    def test_outlook_subset_is_industry_not_financial(self):
        """行业风向研判模块含在线新闻/法规/监管检索，不含财务计算与导出（行业维度）。"""
        out = MODULE_TOOLS["outlook"]
        assert {"industry_outlook", "search_regulations", "search_regulatory_inquiries"} <= out
        assert "calculate_financial_indicators" not in out
        assert "export_pdf_report" not in out

    def test_financial_has_calculation_not_disclosure(self):
        """财务模块含校验/指标/量化模型，不含披露检查与导出。"""
        fin = MODULE_TOOLS["financial"]
        assert {"validate_financial_data", "calculate_financial_indicators",
                "calculate_risk_models"} <= fin
        assert "check_disclosure_compliance" not in fin
        assert "export_pdf_report" not in fin

    def test_compliance_has_disclosure_not_calculation(self):
        """合规模块含披露检查与审计意见识别，不含财务指标计算（避免重复劳动）。"""
        com = MODULE_TOOLS["compliance"]
        assert {"check_disclosure_compliance", "identify_audit_opinion",
                "search_regulations"} <= com
        assert "calculate_financial_indicators" not in com

    def test_synthesis_has_scoring_and_export(self):
        """综合研判负责评分与量化模型；导出工具不注册给 LLM（系统兜底唯一路径）。

        历史缺陷：LLM 主动调用 export_pdf_report 时无法传全专项入参（财务/披露/评分
        JSON 不在其上下文），产出缺失专项章的残缺版报告，且兜底因「已调用」被跳过。
        """
        syn = MODULE_TOOLS["synthesis"]
        assert "calculate_comprehensive_score" in syn
        assert "calculate_risk_models" in syn
        # 导出工具不得出现在任何 LLM 工具子集中（export 由 _post_process 兜底执行）
        assert "export_pdf_report" not in syn
        assert "export_excel_report" not in syn

    def test_parse_pdf_in_both_entry_modules(self):
        """前两个模块都可能直接接收年报文件，故都保留解析工具。"""
        assert "parse_pdf_report" in MODULE_TOOLS["financial"]
        assert "parse_pdf_report" in MODULE_TOOLS["compliance"]


class TestAgentCacheKey:
    """_get_agent：缓存键需按「模式 + 模块」区分，否则不同模块会串用工具子集"""

    def test_cache_key_distinguishes_module(self, monkeypatch):
        built = []

        class FakeAgent:
            pass

        def fake_get_instance(_mod, _ctx, **kwargs):
            built.append(kwargs)
            return FakeAgent()

        import main as main_mod
        monkeypatch.setattr(main_mod.graph_helper, "get_agent_instance", fake_get_instance)

        svc = GraphService()
        svc._get_agent(None, fast=False, module="financial")
        svc._get_agent(None, fast=False, module="compliance")
        svc._get_agent(None, fast=False, module="financial")  # 应命中缓存
        assert len(built) == 2, "同模块应复用缓存，不同模块必须分别构建"
        assert built[0]["module"] == "financial"
        assert built[1]["module"] == "compliance"

    def test_module_none_builds_full_agent(self, monkeypatch):
        built = []

        def fake_get_instance(_mod, _ctx, **kwargs):
            built.append(kwargs)
            return object()

        import main as main_mod
        monkeypatch.setattr(main_mod.graph_helper, "get_agent_instance", fake_get_instance)
        GraphService()._get_agent(None, fast=False, module=None)
        assert "module" not in built[0], "module 为 None 时不应传该参数，保持全量工具行为"


class TestSynthesisPipeline:
    """pipeline：三阶段定义、载荷构造与摘要提取"""

    def test_three_stages_in_order(self):
        mods = [m for m, _, _, _ in SYNTHESIS_STAGES]
        assert mods == ["financial", "compliance", "synthesis"]

    def test_progress_ranges_ascending(self):
        """进度区间需单调递增，否则前端进度条会回退。"""
        pcts = [start for _, _, start, _ in SYNTHESIS_STAGES]
        assert pcts == sorted(pcts)
        assert SYNTHESIS_STAGES[-1][3] <= 100

    def test_stage_payload_injects_marker_and_instruction(self):
        base = _payload("这里是年报全文")
        p = build_stage_payload(base, "financial", MODULE_MARKERS["financial"], [])
        content = p["messages"][-1]["content"]
        assert MODULE_MARKERS["financial"] in content, "必须含模块标记，否则路由不到工具子集"
        assert STAGE_INSTRUCTIONS["financial"][:12] in content
        assert "这里是年报全文" in content, "原始数据必须保留"

    def test_stage_payload_injects_prior_summaries(self):
        base = _payload("年报全文")
        p = build_stage_payload(base, "synthesis", MODULE_MARKERS["synthesis"],
                                [("第一阶段", "财务结论A"), ("第二阶段", "合规结论B")])
        content = p["messages"][-1]["content"]
        assert "财务结论A" in content and "合规结论B" in content
        assert "交叉验证" in content, "第三阶段必须带交叉验证硬性要求"

    def test_stage_payload_marks_failed_stage(self):
        """前置阶段失败（空摘要）时必须显式说明，避免第三阶段误判该维度无风险。"""
        p = build_stage_payload(_payload("年报"), "synthesis",
                                MODULE_MARKERS["synthesis"], [("第一阶段", "")])
        assert "未产出有效结论" in p["messages"][-1]["content"]

    def test_stage_payload_does_not_mutate_input(self):
        base = _payload("原文")
        original = base["messages"][-1]["content"]
        build_stage_payload(base, "financial", MODULE_MARKERS["financial"], [])
        assert base["messages"][-1]["content"] == original

    def test_extract_summary_takes_last_ai_text(self):
        msgs = [
            {"type": "human", "content": "请分析"},
            {"type": "tool", "name": "validate_financial_data", "content": "工具结果"},
            {"type": "ai", "content": "最终财务诊断结论"},
        ]
        assert extract_stage_summary(msgs) == "最终财务诊断结论"

    def test_extract_summary_skips_tool_messages(self):
        """工具消息不能被当作阶段结论。"""
        msgs = [{"type": "ai", "content": "真结论"},
                {"type": "tool", "name": "x", "content": "工具输出"}]
        assert extract_stage_summary(msgs) == "真结论"

    def test_extract_summary_truncates(self):
        long_text = "结" * 5000
        out = extract_stage_summary([{"type": "ai", "content": long_text}], max_chars=100)
        assert len(out) < 200 and "截断" in out

    def test_extract_summary_empty_safe(self):
        assert extract_stage_summary([]) == ""
        assert extract_stage_summary(None) == ""

    def test_cross_validation_instruction_has_three_states(self):
        for state in ("相互印证", "存在矛盾", "单方发现"):
            assert state in CROSS_VALIDATION_INSTRUCTION
