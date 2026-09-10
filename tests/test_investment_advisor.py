"""智能投资参考卡工具单元测试

覆盖 investment_advisor 的核心场景：
- 四档参考位区间边界值映射
- 亮点/警示 TOP3 截断
- 免责声明字段必存在
- 输出全文无「买入」「卖出」措辞（合规红线）
- 畸形入参（标量/列表/坏 JSON/嵌套标量）降级不崩溃
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.investment_advisor import investment_advisor


class TestInvestmentAdvisor:
    """投资参考卡工具测试集"""

    def _invoke(self, score=None, indicators=None):
        """辅助方法：以 JSON 字符串传入评分与指标结果并解析返回 JSON"""
        payload = {
            "comprehensive_score_json": json.dumps(score or {}),
            "financial_indicators_json": json.dumps(indicators or {}),
        }
        return json.loads(investment_advisor.invoke(payload))

    # ── 四档参考位边界值 ──

    def test_tier_boundaries(self):
        """区间边界：25→关注、26→中性、50→中性、51→谨慎、75→谨慎、76→回避"""
        cases = [
            (0, "watch"), (25, "watch"),
            (26, "neutral"), (50, "neutral"),
            (51, "caution"), (75, "caution"),
            (76, "avoid"), (100, "avoid"),
        ]
        for score, expected_key in cases:
            result = self._invoke(score={"score": score})
            assert result["tier_key"] == expected_key, f"score={score} 应映射到 {expected_key}"
            assert result["tier_rule"]  # 命中规则可追溯

    def test_score_out_of_range_clamped(self):
        """越界分数应 clamp 到 0-100 而非报错"""
        assert self._invoke(score={"score": 150})["tier_key"] == "avoid"
        assert self._invoke(score={"score": -10})["tier_key"] == "watch"

    def test_missing_score_defaults_neutral(self):
        """缺 score 字段时默认 50 分 → 中性"""
        assert self._invoke()["tier_key"] == "neutral"

    # ── 亮点/警示提取 ──

    def test_highlights_top3_truncation(self):
        """全部亮点规则命中时只保留 TOP3"""
        indicators = {"indicators": {
            "gross_margin_pct": 45, "operating_cashflow_to_net_profit_ratio": 1.5,
            "current_ratio": 2.5, "debt_to_asset_ratio_pct": 30,
            "revenue_yoy_change_pct": 20, "net_profit_yoy_change_pct": 15,
        }}
        result = self._invoke(indicators=indicators)
        assert len(result["highlights"]) == 3
        assert all(h["point"] and h["reason"] for h in result["highlights"])

    def test_warnings_top3_from_alerts_and_escalation(self):
        """警示优先取 alerts，不足时补评分抬升理由，合计不超过 3 条"""
        result = self._invoke(
            score={"score": 60, "escalation_reasons": ["Z-Score 落入困境区", "M-Score 超阈值"]},
            indicators={"alerts": ["应收激增", "现金流恶化", "存贷双高", "商誉高企"]},
        )
        assert len(result["warnings"]) == 3
        # alerts 优先：前三条应全部来自财务预警
        assert all("财务指标预警" in w["reason"] for w in result["warnings"])

    def test_checklist_expands_with_high_dimension(self):
        """维度分 ≥40 时追加针对性检查项"""
        base = self._invoke(score={"score": 10, "breakdown": {"financial": 0, "disclosure": 0, "validation": 0}})
        expanded = self._invoke(score={"score": 60, "breakdown": {"financial": 80, "disclosure": 50, "validation": 10}})
        assert len(expanded["checklist"]) == len(base["checklist"]) + 2

    def test_negative_profit_blocks_ocf_highlight(self):
        """亏损企业的 OCF/NP 负负得正不应命中「现金含量充足」亮点（实测事故形态）"""
        result = self._invoke(indicators={"indicators": {
            "operating_cashflow_to_net_profit_ratio": 1.5,  # -1.2/-0.8 的假象
            "net_profit_current": -80000000,
        }})
        assert all("现金含量" not in h["point"] for h in result["highlights"])

    def test_yoy_below_minus_100_also_blocks_ocf_highlight(self):
        """net_profit_current 被 LLM 丢弃时，同比 < -100%（由盈转亏）作第二信号判负"""
        result = self._invoke(indicators={"indicators": {
            "operating_cashflow_to_net_profit_ratio": 1.5,
            "net_profit_yoy_change_pct": -260.0,  # 上期为正、本期必为负
        }})
        assert all("现金含量" not in h["point"] for h in result["highlights"])

    # ── 合规红线 ──

    def test_disclaimer_always_present(self):
        """免责声明字段必须存在且含关键短语"""
        result = self._invoke(score={"score": 30})
        assert "AI 辅助生成" in result["disclaimer"]
        assert "不构成任何投资建议" in result["disclaimer"]

    def test_no_buy_sell_wording(self):
        """输出全文（含所有档位与画像文案）严禁出现「买入」「卖出」"""
        for score in (10, 40, 60, 90):
            raw = investment_advisor.invoke({
                "comprehensive_score_json": json.dumps({"score": score}),
                "financial_indicators_json": "{}",
            })
            assert "买入" not in raw and "卖出" not in raw

    # ── 畸形入参回归（照抄 risk_scorer 的事故形态）──

    def test_scalar_and_list_inputs_do_not_crash(self):
        """标量/列表入参（LLM 从摘要重构时的真实形态）应降级不崩溃"""
        for bad in ("42.5", "[1,2,3]", "null", "not-a-json", "{broken"):
            raw = investment_advisor.invoke({
                "comprehensive_score_json": bad,
                "financial_indicators_json": bad,
            })
            data = json.loads(raw)
            assert data["tier"]  # 保留核心字段
            assert data["disclaimer"]
            assert "error" not in data  # 类型守卫应静默降级，不触发顶层兜底

    def test_scalar_nested_fields_do_not_crash(self):
        """嵌套字段为标量（indicators/alerts/breakdown 全畸形）不崩溃"""
        result = self._invoke(
            score={"score": 55, "breakdown": 3.14, "escalation_reasons": "一句话"},
            indicators={"indicators": 7, "alerts": 99},
        )
        assert result["tier_key"] == "caution"
        assert result["highlights"] == []
        assert "error" not in result

    # ── 联网证据（监管问询/行业新闻）──

    def test_regulatory_findings_elevate_tier_and_warnings(self):
        """监管问询记录把中性参考位抬升至谨慎，并进入警示与 regulatory_alerts。"""
        reg = {"items": [{"title": "关于年报的问询函", "date": "2025-06-01", "inquiry_type": "监管问询"}]}
        result = json.loads(investment_advisor.invoke({
            "comprehensive_score_json": "{}",   # 无财务数据 → 本应中性
            "financial_indicators_json": "{}",
            "regulatory_inquiry_json": json.dumps(reg, ensure_ascii=False),
        }))
        assert result["tier_key"] == "caution"   # 联网监管证据抬升下限
        assert result["regulatory_alerts"] == ["关于年报的问询函"]
        assert any("监管问询在线查询" in w["reason"] for w in result["warnings"])

    def test_no_regulatory_data_keeps_score_tier(self):
        """无联网监管数据时参考位仍纯按评分映射（向后兼容）。"""
        result = json.loads(investment_advisor.invoke({
            "comprehensive_score_json": json.dumps({"score": 30}),
            "financial_indicators_json": "{}",
            "regulatory_inquiry_json": "{}",
        }))
        assert result["tier_key"] == "neutral"
        assert result["regulatory_alerts"] == []

    def test_industry_sentiment_included(self):
        """行业新闻景气度作为联网证据纳入输出。"""
        outlook = {"thermometer": 7.5, "outlook_3_6m": "景气偏暖", "data_mode": "online"}
        result = json.loads(investment_advisor.invoke({
            "comprehensive_score_json": "{}",
            "financial_indicators_json": "{}",
            "industry_outlook_json": json.dumps(outlook, ensure_ascii=False),
        }))
        assert result["industry_sentiment"]["thermometer"] == 7.5
        assert result["industry_sentiment"]["data_mode"] == "online"

    def test_malformed_online_inputs_do_not_crash(self):
        """监管/行业入参畸形（标量/坏 JSON）应静默降级不崩溃。"""
        for bad in ("42.5", "[1,2]", "not-json", "{broken"):
            data = json.loads(investment_advisor.invoke({
                "comprehensive_score_json": "{}",
                "financial_indicators_json": "{}",
                "regulatory_inquiry_json": bad,
                "industry_outlook_json": bad,
            }))
            assert data["tier"]
            assert data["regulatory_alerts"] == []
            assert data["industry_sentiment"] is None
            assert "error" not in data
