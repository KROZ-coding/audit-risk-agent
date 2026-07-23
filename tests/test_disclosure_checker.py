"""信息披露规范性检查工具的单元测试

覆盖 check_disclosure_compliance 的核心场景：
- 文本过短的防御
- 完整章节应获得高合规分
- 缺失章节应生成问题条目
- 非标准审计意见识别与风险加分
- 合规分与风险分互补关系
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.disclosure_checker import check_disclosure_compliance

# 含全部法定必要章节关键词的合规样例文本
FULL_REPORT = (
    "公司基本情况：本公司为一家制造业企业，公司概况如下。"
    "主要会计数据和财务指标显示营业收入稳定增长。"
    "前十名股东及实际控制人持股情况已列示。"
    "董事、监事、高级管理人员构成及董监高履历完整。"
    "公司治理结构健全，内部控制有效运行。"
    "财务报告包含资产负债表、利润表及现金流量表。"
    "董事会报告对经营情况讨论与管理层讨论进行了充分说明。"
) * 3


class TestDisclosureChecker:
    """信息披露规范性检查工具测试集"""

    def _invoke(self, text):
        """辅助方法：调用工具并解析返回 JSON"""
        result = check_disclosure_compliance.invoke({"report_text": text})
        return json.loads(result)

    def test_too_short_text(self):
        """文本不足100字符时应返回错误且风险分为100"""
        result = self._invoke("公司概况")
        assert result["compliance_score"] == 0
        assert result["risk_score"] == 100
        assert "error" in result

    def test_full_report_high_compliance(self):
        """包含全部必要章节的完整年报应获得较高合规分且无缺失章节"""
        result = self._invoke(FULL_REPORT)
        assert result["compliance_score"] >= 80
        assert result["sections_missing"] == []
        assert result["audit_opinion"] == "标准无保留意见"

    def test_missing_sections_generate_issues(self):
        """缺失章节应生成"缺失必要章节"问题并计入 sections_missing"""
        text = "本公司仅披露了公司基本情况与主要会计数据，其余内容从略。" * 5
        result = self._invoke(text)
        assert len(result["sections_missing"]) > 0
        assert any("缺失必要章节" in issue for issue in result["issues"])

    def test_non_standard_opinion_detected(self):
        """非标准审计意见应被识别并追加风险分"""
        text = FULL_REPORT + "审计机构对本年度财务报表出具了保留意见。"
        result = self._invoke(text)
        assert result["is_non_standard_opinion"] is True
        assert result["audit_opinion"] == "保留意见"

    def test_score_complementary(self):
        """合规分与风险分应保持互补关系（合计约为100，非标另计加分）"""
        result = self._invoke(FULL_REPORT)
        assert 0 <= result["compliance_score"] <= 100
        assert 0 <= result["risk_score"] <= 100
        assert result["compliance_score"] + result["risk_score"] == 100
