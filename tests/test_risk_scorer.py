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

    def test_all_empty_is_low_risk(self):
        """三维度均为空时：财务0 + 披露默认30 + 校验0 → 综合分低风险"""
        result = self._invoke()
        # 0*0.5 + 30*0.3 + 0*0.2 = 9.0
        assert result["score"] == 9.0
        assert result["level_key"] == "low"

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

    def test_invalid_json_falls_back(self):
        """非法 JSON 输入应降级为默认值而非抛异常"""
        result = calculate_comprehensive_score.invoke({
            "financial_analysis_json": "not-a-json",
            "disclosure_check_json": "also-bad",
            "validation_json": "{broken",
        })
        data = json.loads(result)
        # 财务50*0.5 + 披露30*0.3 + 校验20*0.2 = 25+9+4 = 38.0
        assert data["score"] == 38.0
        assert data["level_key"] == "medium"

    def test_output_structure(self):
        """输出应包含分数、等级、三维度分解与权重字段"""
        result = self._invoke(validation={"failed_checks": 1})
        assert set(result["breakdown"].keys()) == {"financial", "disclosure", "validation"}
        assert result["weights"]["financial"] == 0.50
        assert 0 <= result["score"] <= 100
        assert result["summary"]

    def test_validation_nested_key(self):
        """校验风险应兼容嵌套在 data_validation 下的 failed_checks"""
        result = self._invoke(validation={"data_validation": {"failed_checks": 2}})
        # min(2*30,100)=60 → breakdown.validation 应为 60
        assert result["breakdown"]["validation"] == 60.0
