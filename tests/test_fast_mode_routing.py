"""快速/普通模式的模型路由测试

锁定分级用模行为：
- 消息含「快速模式」关键词 → GraphService 路由到 flash Agent（model_override=FAST_MODEL）
- 普通消息 → pro Agent（不传 model_override，使用 config 主模型）
- 双实例按模式各缓存一份，不重复构建
- 关键词检测兼容 dict 消息与 LangChain 消息对象
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import HumanMessage

import main as main_mod
from main import GraphService, _payload_wants_fast_mode


class TestFastModeDetection:
    """_payload_wants_fast_mode 关键词检测"""

    def test_dict_message_with_keyword(self):
        payload = {"messages": [{"role": "user", "content": "分析年报\n\n（快速模式，跳过辩论复核）"}]}
        assert _payload_wants_fast_mode(payload) is True

    def test_langchain_message_with_keyword(self):
        payload = {"messages": [HumanMessage(content="快速模式分析")]}
        assert _payload_wants_fast_mode(payload) is True

    def test_normal_message_and_empty_payload(self):
        assert _payload_wants_fast_mode({"messages": [{"role": "user", "content": "请分析年报"}]}) is False
        assert _payload_wants_fast_mode({}) is False
        assert _payload_wants_fast_mode(None) is False


class TestAgentModelRouting:
    """GraphService 按模式路由并缓存双 Agent 实例"""

    def _patched_service(self, monkeypatch):
        """构建独立 GraphService，mock 掉真实 Agent 构建，记录构建参数"""
        calls = []

        def _fake_get_agent_instance(module_path, ctx=None, **kwargs):
            calls.append(kwargs)
            return object()  # 哑 Agent 实例，仅用于身份比较

        monkeypatch.setattr(main_mod.graph_helper, "get_agent_instance", _fake_get_agent_instance)
        return GraphService(), calls

    def test_fast_uses_override_normal_uses_config(self, monkeypatch):
        service, calls = self._patched_service(monkeypatch)

        pro_agent = service._get_agent(fast=False)
        flash_agent = service._get_agent(fast=True)

        # 普通模式不传覆盖（用 config 主模型），快速模式传 FAST_MODEL 覆盖
        assert calls[0] == {}
        assert calls[1] == {"model_override": main_mod.FAST_MODEL}
        # 两种模式是不同实例
        assert pro_agent is not flash_agent

    def test_instances_cached_per_mode(self, monkeypatch):
        service, calls = self._patched_service(monkeypatch)

        a1 = service._get_agent(fast=True)
        a2 = service._get_agent(fast=True)
        b1 = service._get_agent(fast=False)
        b2 = service._get_agent(fast=False)

        # 同模式命中缓存（各构建一次），跨模式互不复用
        assert a1 is a2 and b1 is b2 and a1 is not b1
        assert len(calls) == 2
