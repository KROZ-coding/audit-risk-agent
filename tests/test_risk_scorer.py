"""综合风险评分工具的单元测试

覆盖 calculate_comprehensive_score 的核心场景：
- 三维度加权合成与等级映射
- 财务/披露/校验各维度风险计算
- 严重关键词加分
- 非法 JSON 的默认值降级（不崩溃）
- 输出结构完整性
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.risk_scorer import calculate_comprehensive_score


class TestRiskScorer:
    """综合风险评分工具测试集"""

    def _invoke(self, financial=None, disclosure=None, validation=None):
        """辅助方法：以 JSON 字符串传入三维度输入并解析返回 JSON"""
        payload = {
            "financial_analysis_json": json.dumps(financial or {}),
            "disclosure_check_json": json.dumps(disclosure or {}),
            "validation_json": json.dumps(validation or {}),
        }
        result = calculate_comprehensive_score.invoke(payload)
        return json.loads(result)

    def test_all_empty_is_unavailable(self):
        """三维度均为空（无数据）→ score=None、等级"未获取/无法判定"（N 补丁）。

        旧行为：空维度给默认分 30/20/50 算出 9.0 低风险——把"无数据"说成"低风险"
        比崩溃更隐蔽（实测缺陷：披露工具失败仍计 30 分）。
        """
        result = self._invoke()
        assert result["score"] is None
        assert result["level_key"] == "unavailable"
        assert result["breakdown"] == {"financial": "未获取", "disclosure": "未获取",
                                        "validation": "未获取"}

    def test_partial_missing_reweights(self):
        """部分维度未获取：剩余维度权重按比例重归一化（N 补丁）。"""
        result = self._invoke(disclosure={"risk_score": 30})
        # 仅披露可用：30 * 0.3/0.3 = 30.0
        assert result["score"] == 30.0
        assert result["breakdown"]["disclosure"] == 30.0
        assert result["breakdown"]["financial"] == "未获取"

    def test_partial_missing_notes_renormalization(self):
        """50d：维度未获取时 notes 含归一化说明（评分透明化，防「分数无法解释」误读）。"""
        result = self._invoke(
            financial={"alerts": ["存贷双高异常"]},
            validation={"failed_checks": 0},
        )
        notes = "；".join(str(n) for n in (result.get("notes") or []))
        assert "披露合规维度未获取" in notes
        assert "归一化" in notes
        assert "归一化" in str(result.get("summary", ""))
        assert "财务指标 0.71、数据校验 0.29" in notes

    def test_high_risk_all_dimensions(self):
        """三维度均高风险时应合成为极高风险(critical)"""
        result = self._invoke(
            financial={"alerts": ["重大风险" for _ in range(10)]},
            disclosure={"risk_score": 80},
            validation={"failed_checks": 3},
        )
        # 财务封顶100 → 50 ; 披露 80*0.3=24 ; 校验 min(90,100)*0.2=18 ; 合计92
        assert result["score"] > 75
        assert result["level_key"] == "critical"

    def test_severe_keyword_bonus(self):
        """含严重关键词的 alert 相比普通 alert 应获得更高财务风险分"""
        plain = self._invoke(financial={"alerts": ["普通提示"]})
        severe = self._invoke(financial={"alerts": ["持续经营存在重大不确定性"]})
        assert severe["breakdown"]["financial"] > plain["breakdown"]["financial"]

    def test_invalid_json_returns_unavailable(self):
        """非法 JSON 输入 → 维度未获取（score=None），不抛异常（N 补丁）。"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": "not-a-json",
            "disclosure_check_json": "also-bad",
            "validation_json": "{broken",
        })
        data = json.loads(result)
        assert data["score"] is None
        assert data["level_key"] == "unavailable"

    def test_output_structure(self):
        """输出应包含分数、等级、三维度分解与权重字段"""
        result = self._invoke(validation={"failed_checks": 1})
        assert set(result["breakdown"].keys()) == {"financial", "disclosure", "validation"}
        # 50d：权重输出归一化（仅校验维度可用 → 权重 1.0，未获取维度 0）
        assert result["weights"]["validation"] == 1.0
        assert result["weights"]["financial"] == 0.0
        assert result["weights"]["disclosure"] == 0.0
        assert 0 <= result["score"] <= 100
        assert result["summary"]

    def test_validation_nested_key(self):
        """校验风险应兼容嵌套在 data_validation 下的 failed_checks"""
        result = self._invoke(validation={"data_validation": {"failed_checks": 2}})
        # min(2*30,100)=60 → breakdown.validation 应为 60
        assert result["breakdown"]["validation"] == 60.0

    # ── 畸形入参回归（串跑第三阶段 LLM 从摘要重构入参的真实事故形态）──
    # 修复前：json.loads("27.5") 得到 float，后续 float.get 抛 AttributeError，
    # 被 langgraph ToolNode 上抛后炸掉整条 SSE 流（见 app.log 2026-07-30 22:46 事故）

    def test_scalar_inputs_do_not_crash(self):
        """三个入参均为标量 JSON（数字/字符串）时：不抛异常，维度未获取。"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": "27.5",
            "disclosure_check_json": '"高风险"',
            "validation_json": "0",
        })
        data = json.loads(result)
        assert data["score"] is None
        assert data["level_key"] == "unavailable"
        assert "level" in data

    def test_list_inputs_do_not_crash(self):
        """入参为列表时同样不应崩溃（维度未获取）。"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": '["alert1", "alert2"]',
            "disclosure_check_json": "[1, 2, 3]",
            "validation_json": "[]",
        })
        data = json.loads(result)
        assert data["score"] is None or 0 <= data["score"] <= 100
        assert "level_key" in data

    def test_scalar_nested_fields_do_not_crash(self):
        """嵌套字段为标量（如 data_validation: 0、alerts: 数字）时不崩溃。"""
        result = self._invoke(
            financial={"alerts": 3},              # alerts 不是列表
            disclosure={"risk_score": {"a": 1}},  # risk_score 不是数字
            validation={"data_validation": 0},    # 嵌套字段是标量
        )
        assert result["score"] is None or 0 <= result["score"] <= 100

    def test_malformed_risk_models_and_opinion_do_not_crash(self):
        """可选的模型/意见入参为标量时不崩溃且不抬升（三维度空 → 未获取）。"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": "{}",
            "disclosure_check_json": "{}",
            "validation_json": "{}",
            "risk_models_json": "9.9",
            "audit_opinion_json": "null",
        })
        data = json.loads(result)
        assert data["escalation"] == 0.0
        assert data["score"] is None  # 三维度空 → 未获取（N 补丁），未被畸形可选入参扰动

    def test_scalar_nested_model_fields_do_not_crash(self):
        """模型/意见结构体内的嵌套字段为标量分值（实测事故形态）时：
        不走顶层降级兜底，维度级守卫直接消化，无 error 字段"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": "{}",
            "disclosure_check_json": "{}",
            "validation_json": "{}",
            "risk_models_json": json.dumps({
                "risk_models": {"altman_z_score": 1.2, "beneish_m_score": -2.8}
            }),
            "audit_opinion_json": json.dumps({
                "audit_opinion": "保留意见", "going_concern": 1
            }),
        })
        data = json.loads(result)
        assert "error" not in data          # 维度级守卫生效，未落入顶层降级
        assert data["escalation"] == 0.0    # 标量字段无法判定抬升，安全忽略
        assert data["score"] is None        # 三维度空 → 未获取
