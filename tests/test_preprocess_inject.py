# -*- coding: utf-8 -*-
"""P1 预处理并行注入测试（GraphService._preprocess_inject）

背景：性能优化 P1——上传文件场景下，进 Agent 前由系统提取财务数据并
并行预跑 校验/指标/披露 三工具，以「已完成工具轨迹」注入上下文，
省 2-3 次 LLM 往返。本测试锁定：
- 注入轨迹的消息结构与真实调用形态一致（AIMessage.tool_calls + 3 ToolMessage 正序）
- 短文本 / 提取失败 时优雅回退 None（fail-open）
- 注入顺序 validate → calculate → disclosure 满足硬约束门禁
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import GraphService
from langchain_core.messages import AIMessage, ToolMessage

_FAKE_DATA = json.dumps({
    "total_assets": 100, "total_liabilities": 60, "net_assets": 40,
    "revenue_current": 50, "net_profit_current": 8,
}, ensure_ascii=False)

_LONG_TEXT = "某公司年报数据：总资产100万元，总负债60万元。" * 100  # >800 字符


def _service_with_mock_extract(monkeypatch, extract_result):
    svc = GraphService()

    async def _fake_extract(self, text):
        return extract_result

    monkeypatch.setattr(GraphService, "_extract_financial_json", _fake_extract)
    return svc


class TestPreprocessInject:

    def test_injects_valid_tool_trajectory(self, monkeypatch):
        """提取成功：注入 1 条 AIMessage(3 tool_calls) + 3 条 ToolMessage，顺序正确。"""
        svc = _service_with_mock_extract(monkeypatch, _FAKE_DATA)
        payload = {"messages": [{"role": "user", "content": _LONG_TEXT}]}
        msgs = asyncio.run(svc._preprocess_inject(payload))
        assert msgs is not None and len(msgs) == 4
        ai = msgs[0]
        assert isinstance(ai, AIMessage)
        names = [tc["name"] for tc in ai.tool_calls]
        # 顺序 validate → calculate → disclosure：满足硬约束「先校验后计算」
        assert names == ["validate_financial_data",
                         "calculate_financial_indicators",
                         "check_disclosure_compliance"]
        tool_msgs = msgs[1:]
        assert all(isinstance(m, ToolMessage) for m in tool_msgs)
        assert [m.name for m in tool_msgs] == names
        # tool_call_id 与 AIMessage 中的调用一一对应（OpenAI 协议要求）
        assert [m.tool_call_id for m in tool_msgs] == [tc["id"] for tc in ai.tool_calls]
        # 校验工具真实执行过（内容含勾稽校验结果结构）
        assert "data_validation" in str(tool_msgs[0].content)

    def test_short_text_returns_none(self, monkeypatch):
        """短文本（无数据密度）：不预跑，返回 None 走原链路。"""
        svc = _service_with_mock_extract(monkeypatch, _FAKE_DATA)
        payload = {"messages": [{"role": "user", "content": "分析一下茅台"}]}
        assert asyncio.run(svc._preprocess_inject(payload)) is None

    def test_extract_failure_returns_none(self, monkeypatch):
        """提取失败（LLM 输出无法解析）：fail-open 返回 None，不阻塞主链路。"""
        svc = _service_with_mock_extract(monkeypatch, None)
        payload = {"messages": [{"role": "user", "content": _LONG_TEXT}]}
        assert asyncio.run(svc._preprocess_inject(payload)) is None

    def test_extract_exception_returns_none(self, monkeypatch):
        """提取抛异常（网络/超时）：同样静默回退 None。"""
        svc = GraphService()

        async def _boom(self, text):
            raise TimeoutError("extract timeout")

        monkeypatch.setattr(GraphService, "_extract_financial_json", _boom)
        payload = {"messages": [{"role": "user", "content": _LONG_TEXT}]}
        assert asyncio.run(svc._preprocess_inject(payload)) is None
