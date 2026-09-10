# -*- coding: utf-8 -*-
"""预跑/前序阶段工具结果必须进入最终报告的数据源清单（实测缺陷回归）

现象：一次真实运行（中国石油 2025 年半年度，financial 模块）日志显示预处理注入
成功、五工具已预跑、导出的 PDF 有完整财务指标章节，但前端「报告元数据与完整性」
卡片显示「数据源 0/7 已获取」、指标视图判定未获取——正文与元数据自相矛盾。

根因：stream_sse 优先用后处理后的 final_messages 构建最终报告，而 astream 的增量
里不含图输入侧消息：P1 预跑注入的 ToolMessage、串跑前两阶段的工具结果都只存在于
输入/段内状态，于是 tool_results 为空 → 七项数据源全部判为「未获取」。

修复：这些工具结果单独登记进 seed_tool_results，构建报告时补在流内消息之前
（它们真实发生在最前，且同名工具以预跑的确定性结果为准）。

不依赖真实 LLM：预跑链路用假提取结果驱动真实工具；Agent 用假 astream。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import MODULE_MARKERS, GraphService

_FAKE_DATA = json.dumps({
    "total_assets": 100000, "total_liabilities": 60000, "net_assets": 40000,
    "revenue_current": 50000, "net_profit_current": 8000,
    "operating_cashflow_current": 9000, "net_profit_parent": 7500,
}, ensure_ascii=False)

# 触发预跑注入的文本门槛：_PREPROCESS_MIN_CHARS = 800
_LONG_TEXT = "某公司年报数据：总资产100000万元，总负债60000万元，净利润8000万元。" * 60

_LEDGER_TEXT = (
    "## 财务健康度诊断\n\n结论如下。\n\n"
    '{"company_info": {"company_name": "数据源测试公司", "report_year": "2025"},'
    ' "risk_details": []}'
)


class _FakeAI:
    def __init__(self, content, mid="ai1"):
        self.type = "ai"
        self.content = content
        self.id = mid
        self.tool_calls = []
        self.name = ""


class _FakeTool:
    def __init__(self, name, content, mid):
        self.type = "tool"
        self.name = name
        self.content = content
        self.id = mid


class _FakeAgent:
    """假 Agent：吐最终 AI 消息，并按 _AgentWrapper.astream 契约推后处理标记。"""

    def __init__(self, stream_messages, post_processed=True):
        self._stream_messages = stream_messages
        self._post_processed = post_processed

    async def astream(self, payload, config=None, **kw):
        yield {"agent": {"messages": list(self._stream_messages)}}
        if not self._post_processed:
            return
        yield {"__post_processing__": True}
        yield {"__post_processed__": True, "messages": list(self._stream_messages)}


def _collect_report(svc, payload):
    async def _run():
        report = None
        async for raw in svc.stream_sse(payload):
            for line in str(raw).splitlines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "final_report":
                        report = ev
        return report
    return asyncio.run(_run())


def _wire_financial(monkeypatch, agent, extract_result=_FAKE_DATA):
    import main as main_mod

    monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})

    async def _fake_extract(self, text):
        return extract_result

    monkeypatch.setattr(GraphService, "_extract_financial_json", _fake_extract)
    svc = GraphService()
    monkeypatch.setattr(svc, "_get_agent",
                        lambda ctx=None, fast=False, module=None: agent)
    return svc


def _sources(report):
    meta = report["report_metadata"]
    return {item["name"]: item for item in meta["data_sources"]}, meta


class TestPrerunToolsReachReport:
    """P1 预跑注入的五工具必须计入数据源清单"""

    def test_prerun_tools_count_as_used_sources(self, monkeypatch):
        agent = _FakeAgent([_FakeAI(_LEDGER_TEXT)])
        svc = _wire_financial(monkeypatch, agent)
        report = _collect_report(svc, {"messages": [
            {"role": "user", "content": _LONG_TEXT}]})
        assert report is not None, "未收到 final_report 事件"
        sources, meta = _sources(report)
        assert sources["财务指标数据"]["used"] is True, (
            f"预跑指标结果未计入数据源：{meta['data_sources']}")
        assert sources["披露规范性检查"]["used"] is True
        assert sources["综合风险评分"]["used"] is True
        assert meta["source_total"] == 7
        assert meta["source_used_count"] >= 5, (
            f"已预跑五项却只统计到 {meta['source_used_count']} 项：{meta['data_sources']}")
        # 指标视图同样以 tool_result_index 为权威来源，必须可用
        assert report["indicator_view"]["available"] is True

    def test_prerun_tools_appear_in_tool_call_chain(self, monkeypatch):
        """前端工具调用链展示读取 tool_results，预跑工具不能缺席。"""
        agent = _FakeAgent([_FakeAI(_LEDGER_TEXT)])
        svc = _wire_financial(monkeypatch, agent)
        report = _collect_report(svc, {"messages": [
            {"role": "user", "content": _LONG_TEXT}]})
        names = [item["name"] for item in report["tool_results"]]
        assert "calculate_financial_indicators" in names
        assert "check_disclosure_compliance" in names

    def test_llm_recall_does_not_flip_used_source(self, monkeypatch):
        """LLM 在流内重调同名工具且失败时，不得把已预跑的数据源翻成「未获取」。"""
        stream = [
            _FakeTool("calculate_financial_indicators", '{"error": "缺少入参"}', "t1"),
            _FakeAI(_LEDGER_TEXT),
        ]
        agent = _FakeAgent(stream)
        svc = _wire_financial(monkeypatch, agent)
        report = _collect_report(svc, {"messages": [
            {"role": "user", "content": _LONG_TEXT}]})
        sources, _meta = _sources(report)
        assert sources["财务指标数据"]["used"] is True, (
            "预跑结果应优先于流内失败的重复调用（同名工具以首次结果为准）")
        assert report["indicator_view"]["available"] is True


class TestFallbackPathUnchanged:
    """无后处理标记（底层 agent 无 _post_process）时走 all_messages，行为不变"""

    def test_fallback_path_still_counts_sources(self, monkeypatch):
        agent = _FakeAgent([_FakeAI(_LEDGER_TEXT)], post_processed=False)
        svc = _wire_financial(monkeypatch, agent)
        report = _collect_report(svc, {"messages": [
            {"role": "user", "content": _LONG_TEXT}]})
        sources, meta = _sources(report)
        assert sources["财务指标数据"]["used"] is True
        assert meta["source_used_count"] >= 5


class TestSynthesisStageToolsReachReport:
    """串跑 ①/② 段的工具结果同样只存在于段内状态，必须回传登记"""

    def _wire(self, monkeypatch):
        from langchain_core.messages import ToolMessage
        from tools.financial_calculator import calculate_financial_indicators

        calls = []
        agents = {}

        # 用真实指标工具输出作为 ① 段结果：指标视图要求 metric_results 明细
        fin_content = str(calculate_financial_indicators.invoke(
            {"financial_data_json": _FAKE_DATA}))

        class _StageAgent:
            def __init__(self, module, entries):
                self.module = module
                self._entries = entries

            async def ainvoke(self, payload, config=None, **kw):
                calls.append((self.module, "ainvoke", payload))
                msgs = [_FakeAI(f"{self.module} 阶段结论")]
                for mid, name, content in self._entries:
                    msgs.append(ToolMessage(content=content, name=name,
                                            tool_call_id=mid, id=mid))
                return {"messages": msgs}

            async def astream(self, payload, config=None, **kw):
                calls.append((self.module, "astream", payload))
                yield {"agent": {"messages": [_FakeAI(_LEDGER_TEXT)]}}
                yield {"__post_processing__": True}
                yield {"__post_processed__": True,
                       "messages": [_FakeAI(_LEDGER_TEXT)]}

        agents["financial"] = _StageAgent("financial", [
            ("s1", "calculate_financial_indicators", fin_content),
        ])
        agents["compliance"] = _StageAgent("compliance", [
            ("s2", "check_disclosure_compliance", '{"compliance_score": 85.0}'),
        ])
        agents["synthesis"] = _StageAgent("synthesis", [])

        import main as main_mod
        monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})
        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: agents[module])
        return svc, calls

    def test_synthesis_stage_tools_count_as_used_sources(self, monkeypatch):
        svc, _calls = self._wire(monkeypatch)
        payload = {"messages": [{"role": "user", "content":
                                 MODULE_MARKERS["synthesis"] + " 请综合研判"}]}
        report = _collect_report(svc, payload)
        assert report is not None, "未收到 final_report 事件"
        sources, meta = _sources(report)
        assert sources["财务指标数据"]["used"] is True, (
            f"① 段指标结果未进入数据源清单：{meta['data_sources']}")
        assert sources["披露规范性检查"]["used"] is True, (
            f"② 段披露检查结果未进入数据源清单：{meta['data_sources']}")
        assert report["indicator_view"]["available"] is True
        assert meta["source_used_count"] >= 2
