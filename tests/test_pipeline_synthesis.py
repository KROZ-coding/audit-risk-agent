# -*- coding: utf-8 -*-
"""综合研判串跑编排测试（P1 混合运行模式的核心链路）

`test_module_routing.py` 覆盖的是 pipeline 的纯逻辑（阶段定义/载荷构造/摘要提取），
本测试覆盖 **编排本身**——`GraphService._run_synthesis_stages` 这个异步生成器：

1. 三段必须按 ①财务 → ②合规 → ③综合 的顺序真实执行，且每段用各自模块的 Agent
   （若缓存键或 module 传参被改坏，三段会串用同一工具子集 → 本测试变红）
2. 段间摘要必须真的注入下一段载荷（否则 ③ 无从交叉验证，退化为独立第三次分析）
3. 前两段用 ainvoke（不吐 chunk），仅 ③ 段 astream 透传给前端
4. 进度事件按 SYNTHESIS_STAGES 的起始百分比递增下发，前端进度条不回退
5. 单段失败不得中断整条流水线（降级为「该段无有效结论」继续走完）

不依赖真实 LLM：用假 Agent 记录调用轨迹；不依赖 pytest-asyncio：用 asyncio.run 驱动。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import MODULE_MARKERS, GraphService
from agents.pipeline import SYNTHESIS_STAGES


class _FakeStageAgent:
    """假阶段 Agent：记录收到的载荷，ainvoke 返回预置结论，astream 吐预置 chunk。"""

    def __init__(self, module, calls, summary_text="", fail=False, chunks=None):
        self.module = module
        self._calls = calls
        self._summary = summary_text
        self._fail = fail
        self._chunks = chunks or []

    async def ainvoke(self, payload, config=None, **kw):
        self._calls.append((self.module, "ainvoke", payload))
        if self._fail:
            raise RuntimeError("stage boom")
        return {"messages": [_AI(self._summary)]}

    async def astream(self, payload, config=None, **kw):
        self._calls.append((self.module, "astream", payload))
        for c in self._chunks:
            yield c


class _AI:
    """最小 AIMessage 替身：extract_stage_summary 只看 content 与 type 归属。"""

    def __init__(self, content):
        self.content = content
        self.type = "ai"


def _last_text(payload):
    """取阶段载荷中最后一条消息的文本（模块标记与摘要都注入在这里）。"""
    msg = payload["messages"][-1]
    return msg.get("content") if isinstance(msg, dict) else str(getattr(msg, "content", ""))


def _run(svc, payload, fast=False):
    """驱动异步生成器，收集全部 (kind, ...) 事件。"""
    async def _collect():
        return [ev async for ev in svc._run_synthesis_stages(payload, None, fast)]
    return asyncio.run(_collect())


def _wire(monkeypatch, **stage_kwargs):
    """把 _get_agent 换成按 module 分发的假 Agent，返回 (service, calls, agents)。"""
    import main as main_mod
    monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})
    calls = []
    agents = {}
    for module, _name, _s, _e in SYNTHESIS_STAGES:
        agents[module] = _FakeStageAgent(module, calls, **stage_kwargs.get(module, {}))

    svc = GraphService()
    monkeypatch.setattr(svc, "_get_agent",
                        lambda ctx=None, fast=False, module=None: agents[module])
    return svc, calls, agents


def _payload():
    return {"messages": [{"role": "user", "content": "请对该公司年报做综合研判"}]}


class TestStageOrchestration:
    """三段串跑的顺序、模块归属与调用方式"""

    def test_three_stages_run_in_declared_order(self, monkeypatch):
        svc, calls, _ = _wire(monkeypatch)
        _run(svc, _payload())
        assert [c[0] for c in calls] == [s[0] for s in SYNTHESIS_STAGES]

    def test_first_two_invoke_last_one_streams(self, monkeypatch):
        """前两段必须 ainvoke（结论只做内部输入），只有 ③ 段 astream 面向用户。"""
        svc, calls, _ = _wire(monkeypatch)
        _run(svc, _payload())
        kinds = {c[0]: c[1] for c in calls}
        assert kinds["financial"] == "ainvoke"
        assert kinds["compliance"] == "ainvoke"
        assert kinds["synthesis"] == "astream"

    def test_each_stage_gets_its_own_module_marker(self, monkeypatch):
        svc, calls, _ = _wire(monkeypatch)
        _run(svc, _payload())
        for module, _kind, payload in calls:
            assert MODULE_MARKERS[module] in _last_text(payload)

    def test_stage_payload_does_not_leak_across_stages(self, monkeypatch):
        """各段载荷相互独立：②的载荷不得带上①的模块标记（否则工具选择会被污染）。"""
        svc, calls, _ = _wire(monkeypatch)
        _run(svc, _payload())
        compliance_text = next(p for m, _k, p in calls if m == "compliance")
        assert MODULE_MARKERS["financial"] not in _last_text(compliance_text)


class TestSummaryHandoff:
    """段间摘要传递——③ 能否交叉验证的前提"""

    def test_prior_summaries_injected_into_synthesis(self, monkeypatch):
        svc, calls, _ = _wire(
            monkeypatch,
            financial={"summary_text": "资产负债率 78%，显著高于行业均值"},
            compliance={"summary_text": "审计意见为保留意见，关联交易披露不完整"},
        )
        _run(svc, _payload())
        text = _last_text(next(p for m, _k, p in calls if m == "synthesis"))
        assert "资产负债率 78%" in text
        assert "保留意见" in text

    def test_second_stage_does_not_see_first_summary_when_parallel(self, monkeypatch):
        """前两阶段并行执行时，② 无法看到 ① 的结论（并行无先后）。"""
        svc, calls, _ = _wire(
            monkeypatch, financial={"summary_text": "毛利率异常抬升"})
        _run(svc, _payload())
        text = _last_text(next(p for m, _k, p in calls if m == "compliance"))
        assert "毛利率异常抬升" not in text

    def test_first_stage_has_no_prior_summary_section(self, monkeypatch):
        """① 段无前序结论，不得凭空出现摘要段落（否则模型会去分析不存在的内容）。"""
        svc, calls, _ = _wire(monkeypatch, financial={"summary_text": "X"})
        _run(svc, _payload())
        text = _last_text(next(p for m, _k, p in calls if m == "financial"))
        assert "【前序阶段已完成的分析结论】" not in text

    def test_failed_stage_does_not_abort_pipeline(self, monkeypatch):
        """① 段抛异常时，②③ 仍须执行，且 ③ 载荷标注该段无有效结论。"""
        svc, calls, _ = _wire(monkeypatch, financial={"fail": True})
        _run(svc, _payload())
        assert [c[0] for c in calls] == [s[0] for s in SYNTHESIS_STAGES]
        text = _last_text(next(p for m, _k, p in calls if m == "synthesis"))
        assert "未产出有效结论" in text or "无有效结论" in text


class TestProgressAndChunks:
    """对外事件契约：progress 递增 + ③ 段 chunk 透传"""

    def test_progress_events_ascending(self, monkeypatch):
        svc, _calls, _ = _wire(monkeypatch)
        events = _run(svc, _payload())
        pcts = [e[2] for e in events if e[0] == "progress"]
        assert pcts == sorted(pcts), "进度条不得回退"
        # 并行模式下先推并行准备锚点，再按财务、合规、综合顺序发布阶段锚点。
        assert pcts == [5, 5, 35, 65]

    def test_progress_step_names_match_stage_names(self, monkeypatch):
        svc, _calls, _ = _wire(monkeypatch)
        events = _run(svc, _payload())
        names = [e[1] for e in events if e[0] == "progress"]
        assert names == [
            "并行运行财务诊断与合规扫描",
            "第一阶段 · 财务健康度诊断",
            "第二阶段 · 合规与经营风险扫描",
            "第三阶段 · 综合研判与交叉验证",
        ]

    def test_only_final_stage_chunks_are_yielded(self, monkeypatch):
        svc, _calls, _ = _wire(
            monkeypatch, synthesis={"chunks": [{"agent": 1}, {"agent": 2}]})
        events = _run(svc, _payload())
        chunks = [e[1] for e in events if e[0] == "chunk"]
        assert chunks == [{"agent": 1}, {"agent": 2}]

    def test_no_chunks_before_first_progress(self, monkeypatch):
        """首个事件必须是 progress，前端才能立刻显示阶段名而非空白等待。"""
        svc, _calls, _ = _wire(monkeypatch, synthesis={"chunks": [{"a": 1}]})
        events = _run(svc, _payload())
        assert events[0][0] == "progress"


class TestRuntimeProgressMonotonicity:
    """运行时进度单调不递减（实测缺陷回归）

    TOOL_PIPELINE 的锚点表本身是递增的，但 Agent 实际调用顺序由 LLM 自主决定。
    一次真实的财务模块跑测到的序列为 18 → 30 → 60 → 36 → 55 → 87 → 84，
    进度条当场倒退两次。本组用例直接驱动 stream_sse，锁定这条修复。
    """

    def _sse_percents(self, tool_names, monkeypatch):
        """用假 astream 按指定顺序吐工具调用 chunk，收集 SSE 下发的百分比。"""
        import json
        import main as main_mod

        class FakeAgent:
            async def astream(self, payload, config=None, **kw):
                for i, name in enumerate(tool_names):
                    yield {"agent": {"messages": [_ToolCallMsg(name, f"c{i}")]}}

        monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})
        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: FakeAgent())
        # 短文本，不触发预处理注入（避免真实 LLM 调用）
        payload = {"messages": [{"role": "user", "content": "分析"}]}

        async def _collect():
            out = []
            async for raw in svc.stream_sse(payload):
                for line in str(raw).splitlines():
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        if ev.get("type") == "progress":
                            out.append(ev["percent"])
            return out

        return asyncio.run(_collect())

    def test_out_of_order_tool_calls_do_not_regress_progress(self, monkeypatch):
        """工具乱序调用（实测序列）时，下发百分比仍必单调不递减。"""
        pcts = self._sse_percents([
            "validate_financial_data",        # 18
            "calculate_financial_indicators",  # 30
            "compare_multi_year",              # 60
            "calculate_risk_models",           # 36（锚点低于上一步）
            "search_regulations",              # 55
            "generate_trend_chart",            # 87
            "generate_radar_chart",            # 84（锚点低于上一步）
        ], monkeypatch)
        assert pcts == sorted(pcts), f"进度百分比出现回退：{pcts}"

    def test_progress_still_advances_in_declared_order(self, monkeypatch):
        """正序调用时锁定不回退不会把进度压死（仍需逐步抬升）。"""
        pcts = self._sse_percents([
            "validate_financial_data", "calculate_financial_indicators",
            "search_regulations", "calculate_comprehensive_score",
        ], monkeypatch)
        assert pcts == sorted(pcts)
        assert max(pcts) >= 68, f"正序调用应推进到评分锚点：{pcts}"


class TestStageCheckpointIsolation:
    """段间 checkpoint 隔离（实测缺陷回归）

    三段若共用 thread_id，前一段异常中断遗留的悬空 AIMessage.tool_calls
    会被下一段从 checkpointer 加载进历史，撞上 langgraph 的 INVALID_CHAT_HISTORY
    校验。实测现象：① 段 parse_pdf_report 取数失败后，②③ 段连锁报错，
    整个 /stream_run 直接 500——与「单段失败降级继续」的设计相矛盾。
    """

    def _thread_ids(self, monkeypatch):
        import main as main_mod
        seen = []

        class Agent:
            async def ainvoke(self, payload, config=None, **kw):
                seen.append(config["configurable"]["thread_id"])
                return {"messages": [_AI("结论")]}

            async def astream(self, payload, config=None, **kw):
                seen.append(config["configurable"]["thread_id"])
                if False:  # pragma: no cover - 仅为保持异步生成器语义
                    yield {}

        monkeypatch.setattr(main_mod, "init_agent_config",
                            lambda _a, ctx: {"configurable": {"thread_id": "abc123"},
                                             "recursion_limit": 50})
        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: Agent())
        _run(svc, _payload())
        return seen

    def test_each_stage_uses_distinct_thread(self, monkeypatch):
        ids = self._thread_ids(monkeypatch)
        assert len(ids) == len(SYNTHESIS_STAGES)
        assert len(set(ids)) == len(ids), f"段间 thread_id 重复，会串状态：{ids}"

    def test_stage_threads_derive_from_run_id(self, monkeypatch):
        """派生 thread 必须保留原 run_id 前缀，便于会话删除时按前缀清理 checkpoint。"""
        ids = self._thread_ids(monkeypatch)
        assert all(i.startswith("abc123-") for i in ids), ids
        assert {i.split("-", 1)[1] for i in ids} == {s[0] for s in SYNTHESIS_STAGES}

    def test_other_config_keys_preserved(self, monkeypatch):
        """只改 thread_id，recursion_limit 等其他配置不得丢。"""
        import main as main_mod
        monkeypatch.setattr(main_mod, "init_agent_config",
                            lambda _a, ctx: {"configurable": {"thread_id": "t"},
                                             "recursion_limit": 50})
        cfg = GraphService()._stage_run_config(None, None, "financial")
        assert cfg["recursion_limit"] == 50
        assert cfg["configurable"]["thread_id"] == "t-financial"


class _ToolCallMsg:
    """带 tool_calls 的 AI 消息替身（_msg_to_dict 只读 type / tool_calls / content）。"""

    def __init__(self, name, call_id):
        self.type = "ai"
        self.content = ""
        self.tool_calls = [{"name": name, "args": {}, "id": call_id}]
        self.id = call_id


class TestSynthesisPostProcessingPreserved:
    """实测缺陷回归：串跑模式丢失评分卡/复核意见/兜底图表

    根因：_post_process 在 _extract_risk_json 失败且 export 已被 LLM 调用时早退，
    导致辩论复核、雷达图兜底、综合评分均不执行。在综合研判串跑模式下，
    export_pdf_report / export_excel_report 属于 synthesis 模块工具集，LLM 自行调用，
    触发 need_pdf=False 的早退条件。修复后 _post_process 应始终构造兜底
    risk_json 并继续完整后处理。
    """

    def test_post_process_appends_score_when_exports_already_called(self, monkeypatch):
        """即使 LLM 已调用导出工具且 risk_json 提取失败，_post_process 仍应追加评分。"""
        from langchain_core.messages import AIMessage, ToolMessage
        from agents.agent import _AgentWrapper, _extract_risk_json

        # 模拟 synthesis 阶段：LLM 输出不含标准 company_info/risk_details 的 JSON
        ai_content = (
            "## 综合研判报告\n\n"
            "交叉验证矩阵如下：\n"
            "Final JSON: {\"overall_score\": 72, \"risks\": []}\n"
        )
        messages = [
            AIMessage(content="", tool_calls=[
                {"name": "generate_risk_heatmap", "args": {}, "id": "c1"},
            ], id="a1"),
            ToolMessage(content='/local_storage/charts/heatmap.png',
                        name="generate_risk_heatmap", tool_call_id="c1"),
            AIMessage(content="", tool_calls=[
                {"name": "export_pdf_report", "args": {}, "id": "c2"},
                {"name": "export_excel_report", "args": {}, "id": "c3"},
            ], id="a2"),
            ToolMessage(content='/local_storage/reports/r.pdf',
                        name="export_pdf_report", tool_call_id="c2"),
            ToolMessage(content='/local_storage/reports/r.xlsx',
                        name="export_excel_report", tool_call_id="c3"),
            AIMessage(content=ai_content, id="a_final"),
        ]

        # 前置条件确认：risk_json 提取失败
        assert _extract_risk_json(ai_content) is None

        class _Dummy:
            pass
        wrapper = _AgentWrapper(_Dummy())

        # 禁用辩论（需要网络），只验证评分尾缀和图表兜底逻辑得以执行
        monkeypatch.setattr("agents.agent.REVIEW_ENABLED", False)
        wrapper._post_process({"messages": messages})

        last_ai = messages[-1]
        assert "<!--COMPREHENSIVE_SCORE-->" in last_ai.content, (
            f"评分标记缺失，_post_process 可能早退了：{last_ai.content[-200:]}"
        )

    def test_pipeline_stream_sse_includes_post_processing(self, monkeypatch):
        """综合研判串跑模式下 stream_sse 的 final_report 必须包含后处理产物。"""
        import json
        import main as main_mod
        from langchain_core.messages import AIMessage, ToolMessage

        # 产物存在性门禁（真实运行中生效）在此放行：该用例聚焦链接归档与评分标记。
        monkeypatch.setattr(main_mod, "_artifact_is_accessible", lambda path: True)

        POST_CONTENT = (
            "## 综合研判报告\n"
            "Final: {\"overall_score\": 72}\n"
            "\n\n<!--COMPREHENSIVE_SCORE-->\n{\"score\":72,\"level\":\"medium\"}\n"
            "\n\n\U0001f4ca 财务雷达图: /local_storage/charts/radar.png"
        )

        class _FakeAI:
            def __init__(self, content, mid="ai1"):
                self.type = "ai"
                self.content = content
                self.id = mid
                self.tool_calls = []
                self.name = ""

        class _SynthPipelineAgent:
            """Fake agent simulating _AgentWrapper.astream output for synthesis."""
            async def astream(self, payload, config=None, **kw):
                yield {"agent": {"messages": [_FakeAI("raw LLM output")]}}
                yield {"__post_processing__": True}
                yield {"__post_processed__": True, "messages": [
                    _FakeAI(POST_CONTENT)
                ]}

            async def ainvoke(self, payload, config=None, **kw):
                return {"messages": [_FakeAI("stage summary")]}

        monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})
        svc = GraphService()
        svc._get_agent = lambda ctx=None, fast=False, module=None: _SynthPipelineAgent()

        payload = {"messages": [{"role": "user", "content":
                    MODULE_MARKERS["synthesis"] + " 分析"}]}

        import asyncio
        async def _collect():
            report = None
            async for raw in svc.stream_sse(payload):
                for line in str(raw).splitlines():
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        if ev.get("type") == "final_report":
                            report = ev
            return report

        report = asyncio.run(_collect())
        assert report is not None, "No final_report event"
        ai_text = report["ai_text"]
        assert "<!--COMPREHENSIVE_SCORE-->" in ai_text, (
            f"串跑模式 final_report 缺失评分标记: {ai_text[-200:]}"
        )
        assert "/local_storage/charts/radar.png" in str(report.get("images", [])), (
            f"串跑模式 final_report 缺失兜底雷达图: {report.get('images')}"
        )


class TestToolLedgerHandoff:
    """段间工具结果透传——③ 段兜底导出/评分的数据源（T3 回归）

    综合研判工具子集不含财务计算/披露检查/校验工具，前两阶段的工具结果
    必须透传进第三阶段 payload 的 tool_ledger，否则兜底导出的财务/合规
    专项章节拿不到数据源（PDF 缺「财务指标四维判读」「披露规范性检查结果」章）。
    """

    def test_stage_tool_results_reach_synthesis_payload(self, monkeypatch):
        from langchain_core.messages import ToolMessage

        calls = []
        agents = {}

        class _AgentWithTools:
            def __init__(self, module, tool_entries):
                self.module = module
                self._entries = tool_entries

            async def ainvoke(self, payload, config=None, **kw):
                calls.append((self.module, "ainvoke", payload))
                msgs = [_AI("结论")]
                for mid, name, content in self._entries:
                    msgs.append(ToolMessage(content=content, name=name,
                                            tool_call_id=mid, id=mid))
                return {"messages": msgs}

            async def astream(self, payload, config=None, **kw):
                calls.append((self.module, "astream", payload))
                if False:  # pragma: no cover - 仅为保持异步生成器语义
                    yield {}

        agents["financial"] = _AgentWithTools(
            "financial", [("t1", "calculate_financial_indicators", '{"indicators":{}}')])
        agents["compliance"] = _AgentWithTools(
            "compliance", [("t2", "check_disclosure_compliance", '{"score":80}')])
        agents["synthesis"] = _AgentWithTools("synthesis", [])

        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: agents[module])
        _run(svc, _payload())

        synth_payload = next(p for m, _k, p in calls if m == "synthesis")
        entries = synth_payload.get("tool_ledger", {}).get("entries", [])
        names = [e[1] for e in entries]
        assert "calculate_financial_indicators" in names
        assert "check_disclosure_compliance" in names
        # 内容原文保留（兜底导出需读完整 JSON 渲染专项章节）
        assert any(e[2] == '{"indicators":{}}' for e in entries)

    def test_failed_stage_contributes_no_entries(self, monkeypatch):
        """① 段失败时不产出工具结果，③ 段台账无伪造条目但流水线不中断。"""
        calls = []
        agents = {}

        class _Agent:
            def __init__(self, module, fail=False):
                self.module = module
                self._fail = fail

            async def ainvoke(self, payload, config=None, **kw):
                calls.append((self.module, "ainvoke", payload))
                if self._fail:
                    raise RuntimeError("stage boom")
                return {"messages": [_AI("结论")]}

            async def astream(self, payload, config=None, **kw):
                calls.append((self.module, "astream", payload))
                if False:  # pragma: no cover
                    yield {}

        agents["financial"] = _Agent("financial", fail=True)
        agents["compliance"] = _Agent("compliance")
        agents["synthesis"] = _Agent("synthesis")

        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: agents[module])
        _run(svc, _payload())

        synth_payload = next(p for m, _k, p in calls if m == "synthesis")
        assert [c[0] for c in calls] == [s[0] for s in SYNTHESIS_STAGES]
        assert "tool_ledger" not in synth_payload  # 无可用结果时不种入空台账

class TestSynthesisRunsPreprocess:
    """综合研判串跑必须先做 P1 预处理（实测缺陷回归）

    根因：synthesis 分支原本完全跳过 _preprocess_inject，财务数据全靠 LLM 从
    17 万字符原文自行抽取，导致指标/模型/数据源大面积「未获取」。修复后预跑
    结果必须同时出现在：① 财务阶段载荷尾部（避免重复抽取）；② synthesis 阶段
    的 tool_ledger（兜底导出/评分/数据源清单的数据来源）。
    """

    class _PreAgent:
        def __init__(self, module, calls):
            self.module = module
            self._calls = calls

        async def ainvoke(self, payload, config=None, **kw):
            self._calls.append((self.module, "ainvoke", payload))
            return {"messages": [_AI("结论")]}

        async def astream(self, payload, config=None, **kw):
            self._calls.append((self.module, "astream", payload))
            if False:  # pragma: no cover
                yield {}

    def _wire_preprocess(self, monkeypatch, pre_msgs):
        import main as main_mod
        from langchain_core.messages import AIMessage, ToolMessage

        monkeypatch.setattr(main_mod, "init_agent_config", lambda _a, _c: {})
        calls = []
        agents = {module: self._PreAgent(module, calls) for module, *_ in SYNTHESIS_STAGES}
        svc = GraphService()
        monkeypatch.setattr(svc, "_get_agent",
                            lambda ctx=None, fast=False, module=None: agents[module])

        async def _fake_preprocess(self, payload):
            return pre_msgs

        monkeypatch.setattr(GraphService, "_preprocess_inject", _fake_preprocess)
        return svc, calls

    def _pre_messages(self):
        from langchain_core.messages import AIMessage, ToolMessage
        return [
            AIMessage(content="", tool_calls=[{"name": "calculate_financial_indicators",
                                               "args": {}, "id": "pre_c"}]),
            ToolMessage(content='{"indicators":{"revenue_current":1}}',
                        name="calculate_financial_indicators",
                        tool_call_id="pre_c", id="pre_tc"),
        ]

    def test_financial_stage_absorbs_preprocess_trajectory(self, monkeypatch):
        svc, calls = self._wire_preprocess(monkeypatch, self._pre_messages())
        long_payload = {"messages": [{"role": "user", "content": "年报正文" * 300}]}
        _run(svc, long_payload)
        fin_payload = next(p for m, _k, p in calls if m == "financial")
        names = [getattr(m, "name", "") for m in fin_payload["messages"]]
        assert "calculate_financial_indicators" in names

    def test_synthesis_payload_carries_preprocess_ledger(self, monkeypatch):
        svc, calls = self._wire_preprocess(monkeypatch, self._pre_messages())
        long_payload = {"messages": [{"role": "user", "content": "年报正文" * 300}]}
        _run(svc, long_payload)
        synth_payload = next(p for m, _k, p in calls if m == "synthesis")
        entries = synth_payload.get("tool_ledger", {}).get("entries", [])
        names = [e[1] for e in entries]
        assert "calculate_financial_indicators" in names
        # 内容原文保留，兜底导出需要完整 JSON
        assert any(e[2] == '{"indicators":{"revenue_current":1}}' for e in entries)

    def test_short_text_skips_preprocess_entirely(self, monkeypatch):
        svc, calls = self._wire_preprocess(monkeypatch, self._pre_messages())
        _run(svc, _payload())  # 短文本
        fin_payload = next(p for m, _k, p in calls if m == "financial")
        names = [getattr(m, "name", "") for m in fin_payload["messages"]]
        assert "calculate_financial_indicators" not in names


def test_direct_stream_sse_binds_request_context_for_batch_identity():
    """直接调用 stream_sse 时也必须把 ctx 绑定到 request_context。"""
    from local_shims import new_context, request_context

    async def _probe():
        ctx = new_context("test")
        stream = GraphService().stream_sse(
            {"messages": [{"role": "user", "content": "【模块:综合研判】分析"}]}, ctx=ctx)
        await stream.__anext__()
        assert request_context.get() is ctx
        await stream.aclose()

    asyncio.run(_probe())
