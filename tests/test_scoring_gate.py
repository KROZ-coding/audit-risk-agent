"""F3 评分可评分性门控与权重外置测试

锁定行为：
- 财务维度：零告警 + 数据稀疏 → None（不可评分，不参与计分）
- 财务维度：零告警 + 指标充分 → 0.0（真实健康）
- 权重可经环境变量覆盖并归一化
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.risk_scorer import _calc_financial_risk, _load_weights


class TestScorabilityGate:
    def test_sparse_data_with_no_alerts_is_unscorable(self):
        """提取失败/字段极稀：零告警不能被解读为低风险 → 维度未获取"""
        sparse = json.dumps({
            "indicators": {"equity_ratio": 0.8},
            "alerts": [],
            "statement_items": {"balance_sheet": {"total_assets_current": 100}},
        }, ensure_ascii=False)
        assert _calc_financial_risk(sparse) is None

    def test_rich_data_with_no_alerts_is_zero(self):
        """指标充分且零告警 → 真实 0 分（健康）"""
        indicators = {f"indicator_{i}": float(i) for i in range(8)}
        statement = {"balance_sheet": {f"item_{i}": float(i) for i in range(8)}}
        rich = json.dumps({"indicators": indicators, "alerts": [],
                           "statement_items": statement}, ensure_ascii=False)
        assert _calc_financial_risk(rich) == 0.0

    def test_alerts_still_score(self):
        data = json.dumps({"indicators": {f"i{i}": float(i) for i in range(8)},
                           "alerts": ["应收账款占比 40%，超过 30% 告警线"]}, ensure_ascii=False)
        assert _calc_financial_risk(data) == 8.0


class TestWeightsOverride:
    def test_default_weights(self, monkeypatch):
        for var in ("RISK_WEIGHT_FINANCIAL", "RISK_WEIGHT_DISCLOSURE", "RISK_WEIGHT_VALIDATION"):
            monkeypatch.delenv(var, raising=False)
        w = _load_weights()
        assert abs(w["financial"] - 0.5) < 1e-9
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_env_override_normalized(self, monkeypatch):
        monkeypatch.setenv("RISK_WEIGHT_FINANCIAL", "0.6")
        monkeypatch.setenv("RISK_WEIGHT_DISCLOSURE", "0.3")
        monkeypatch.setenv("RISK_WEIGHT_VALIDATION", "0.1")
        w = _load_weights()
        assert abs(w["financial"] - 0.6) < 1e-9
        assert abs(sum(w.values()) - 1.0) < 1e-9
