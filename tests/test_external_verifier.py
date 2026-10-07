"""E1 外部数据核验工具测试

锁定行为：
- 未配置 EXTERNAL_VERIFY_MCP_URL：诚实返回 external_unavailable（不冒充核验）
- 一致性核对：容差内 consistent、超差 inconsistent、缺字段 not_comparable
- 工具已注册（config tools + build_agent）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.external_verifier import verify_against_external_source


def _invoke(data, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv("EXTERNAL_VERIFY_MCP_URL", raising=False)
    result = verify_against_external_source.invoke({"financial_data_json": json.dumps(data, ensure_ascii=False)})
    return json.loads(result)


class TestExternalUnavailable:
    def test_unconfigured_returns_honest_unavailable(self, monkeypatch):
        out = _invoke({"total_assets": 100, "company_name": "测试"}, monkeypatch)
        assert out["status"] == "external_unavailable"
        assert "未经外部核验" in out["message"]
        assert out["external_source"] is None

    def test_unavailable_does_not_claim_verified(self, monkeypatch):
        """降级语义：不可用时不得输出 verified 证据"""
        out = _invoke({"total_assets": 100}, monkeypatch)
        assert "evidence" not in out or not out.get("evidence")


class TestConsistencyCheck:
    def _fake_external(self, fields, source="cninfo"):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"source": source, "fields": fields}).encode("utf-8")
        return _FakeResp()

    def test_consistent_within_tolerance(self, monkeypatch):
        import urllib.request
        monkeypatch.setenv("EXTERNAL_VERIFY_MCP_URL", "http://fake-mcp.local")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: self._fake_external({
            "total_assets": 1000000.0, "revenue": 500000.0, "net_profit": 50000.0}))
        out = _invoke({"total_assets": 1001000.0, "revenue": 499000.0,
                       "net_profit": 50000.0, "company_name": "测试", "period": "2025年度"})
        assert out["status"] == "consistent"
        assert all(c["status"] in ("consistent", "not_comparable") for c in out["checks"])

    def test_inconsistent_beyond_tolerance(self, monkeypatch):
        import urllib.request
        monkeypatch.setenv("EXTERNAL_VERIFY_MCP_URL", "http://fake-mcp.local")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: self._fake_external({
            "total_assets": 1000000.0, "revenue": 500000.0}))
        out = _invoke({"total_assets": 1200000.0, "revenue": 500000.0,
                       "company_name": "测试", "period": "2025年度"})
        assert out["status"] == "inconsistent"
        bad = [c for c in out["checks"] if c["status"] == "inconsistent"]
        assert any(c["label"] == "总资产" for c in bad)
        assert "人工核查" in out["message"]


class TestRegistration:
    def test_registered_in_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "agent_llm_config.json")
        cfg = json.load(open(config_path, encoding="utf-8"))
        assert "verify_against_external_source" in cfg.get("tools", [])
        assert len(cfg["tools"]) == 17

    def test_registered_in_build_agent(self):
        from agents.agent import verify_against_external_source
        assert verify_against_external_source is not None
