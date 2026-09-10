"""C 端轻量工具路由测试

锁定轻量工具（投资参考卡/行业风向标）与主审计链路的隔离契约：
- _detect_light_module 只认最后一条消息（最新用户意图）
- LIGHT_MODULE_TOOLS 独立字典，MODULE_TOOLS 三键集合不被污染
- domain_guard 门禁对新工具零感知（不拦截、不误报）
- TOOL_NAME_TO_STEP 含新工具进度映射
- astream(post_process=False) 不产出后处理标记（不辩论/不兜底导出/不评分）
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import (
    LIGHT_MARKERS, LIGHT_TOOL_MARKERS, TOOL_NAME_TO_STEP,
    _detect_light_module, _detect_module, GraphService,
)
from agents.agent import LIGHT_MODULE_TOOLS, MODULE_TOOLS, _AgentWrapper
from tools.domain_guard import assert_hard_order, check_soft_order


def _payload(*contents):
    return {"messages": [{"role": "user", "content": c} for c in contents]}


class TestLightModuleDetection:
    """_detect_light_module：只扫最后一条消息"""

    def test_hit_advisor_and_industry(self):
        assert _detect_light_module(_payload("【工具:投资参考】请生成参考卡")) == "advisor"
        assert _detect_light_module(_payload("【工具:行业风向】分析锂电行业")) == "industry"

    def test_miss_returns_none(self):
        assert _detect_light_module(_payload("请分析这份年报")) is None
        assert _detect_light_module({"messages": []}) is None
        assert _detect_light_module(None) is None

    def test_only_last_message_counts(self):
        """历史消息里的旧轻量标记不应污染后续普通提问"""
        p = _payload("【工具:投资参考】旧请求", "这家公司的商誉情况如何？")
        assert _detect_light_module(p) is None

    def test_light_marker_in_history_does_not_block_module(self):
        """历史含轻量标记时，模块检测仍按原逻辑工作（路由优先级在 stream_sse 组合）"""
        p = _payload("【工具:行业风向】旧请求", "【模块:综合研判】请综合研判")
        assert _detect_light_module(p) is None
        assert _detect_module(p) == "synthesis"


class TestLightToolSubsets:
    """LIGHT_MODULE_TOOLS：独立子集与主字典隔离"""

    def test_module_tools_not_polluted(self):
        """红线：模块集合必须保持不变（test_module_routing 同款断言）"""
        assert set(MODULE_TOOLS) == {"financial", "compliance", "synthesis", "outlook"}

    def test_light_subsets_defined(self):
        assert set(LIGHT_MODULE_TOOLS) == {"advisor", "industry"}
        adv = LIGHT_MODULE_TOOLS["advisor"]
        assert {"investment_advisor", "validate_financial_data",
                "calculate_financial_indicators", "calculate_comprehensive_score"} <= adv
        # 轻量路径不含导出与辩论相关工具
        assert "export_pdf_report" not in adv
        ind = LIGHT_MODULE_TOOLS["industry"]
        assert {"industry_outlook", "search_regulations"} <= ind
        assert "export_pdf_report" not in ind

    def test_markers_and_pipeline_registered(self):
        assert set(LIGHT_MARKERS) == {"advisor", "industry"}
        assert "investment_advisor" in TOOL_NAME_TO_STEP
        assert "industry_outlook" in TOOL_NAME_TO_STEP
        assert set(LIGHT_TOOL_MARKERS) == {"investment_advisor", "industry_outlook"}


class TestDomainGuardIgnoresLightTools:
    """domain_guard 白名单机制：表外工具零感知"""

    def test_hard_order_not_triggered(self):
        """新工具穿插在合法链路中不触发 fail-closed"""
        assert_hard_order([
            "validate_financial_data", "calculate_financial_indicators",
            "investment_advisor", "industry_outlook",
        ])  # 不抛异常即通过

    def test_soft_order_no_warning(self):
        """仅调用轻量工具时软约束不误报"""
        ok, _ = check_soft_order(["industry_outlook", "search_regulations", "investment_advisor"])
        assert ok


class TestAstreamPostProcessSkip:
    """astream(post_process=False)：透传消息但不补跑后处理"""

    def _collect(self, post_process):
        from langchain_core.messages import AIMessage

        class FakeAgent:
            async def astream(self, payload, config=None, **kw):
                yield {"agent": {"messages": [AIMessage(content="轻量回复", id="m1")]}}

        async def run():
            wrapper = _AgentWrapper(FakeAgent())
            chunks = []
            async for c in wrapper.astream({"messages": []}, post_process=post_process):
                chunks.append(c)
            return chunks

        return asyncio.run(run())

    def test_skip_yields_no_post_markers(self):
        chunks = self._collect(post_process=False)
        assert len(chunks) == 1  # 只有透传的原始 chunk
        assert not any(isinstance(c, dict) and (c.get("__post_processing__") or c.get("__post_processed__"))
                       for c in chunks)


class TestLightCardMarkerInjection:
    """_inject_light_card_markers：从 ToolMessage 原文确定性注入前端渲染 marker"""

    def test_inject_latest_tool_json(self):
        report = {
            "ai_text": "分析完成。",
            "tool_results": [
                {"name": "investment_advisor", "content": json.dumps({"tier": "旧"}, ensure_ascii=False)},
                {"name": "investment_advisor", "content": json.dumps({"tier": "中性"}, ensure_ascii=False)},
                {"name": "search_regulations", "content": "非 JSON 文本"},
            ],
        }
        out = GraphService._inject_light_card_markers(report)
        assert "<!--INVESTMENT_CARD-->" in out["ai_text"]
        assert '"tier": "中性"' in out["ai_text"]  # 同名工具取最后一次
        assert out["ai_text"].count("<!--INVESTMENT_CARD-->") == 1

    def test_non_json_and_missing_tools_skipped(self):
        report = {"ai_text": "正文", "tool_results": [
            {"name": "industry_outlook", "content": "{broken json"},
        ]}
        out = GraphService._inject_light_card_markers(report)
        assert "<!--INDUSTRY_OUTLOOK-->" not in out["ai_text"]
        assert out["ai_text"] == "正文"
