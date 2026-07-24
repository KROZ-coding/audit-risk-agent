"""领域约束机制化校验（domain_guard）的单元测试

覆盖两条关键领域约束：
- 约束一：工具调用顺序「先 validate 后 calculate」
    * 乱序调用（calculate 先于 validate）应被捕获并报错
    * 缺失 validate 直接 calculate 应被捕获并报错
    * 正序调用应通过
- 约束二：导出内容必须包含 AI 免责声明
    * check/assert 层面：缺失声明应被拦截
    * 导出集成层面：PDF/Excel 声明缺失时应被拦截、不产出文件
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.domain_guard import (
    check_tool_call_order,
    assert_tool_call_order,
    check_disclaimer_present,
    assert_disclaimer_present,
    collect_flowable_texts,
    ToolCallOrderViolation,
    DisclaimerMissingError,
    VALIDATE_TOOL,
    CALCULATE_TOOL,
    DISCLOSURE_TOOL,
    SEARCH_TOOL,
    SCORE_TOOL,
    EXPORT_PDF_TOOL,
    EXPORT_EXCEL_TOOL,
    DISCLAIMER_MARKER,
)


class TestToolCallOrder:
    """约束一：工具调用顺序「先 validate 后 calculate」。"""

    def test_out_of_order_is_caught(self):
        """乱序用例：calculate 先于 validate 应被捕获并报错。"""
        seq = [CALCULATE_TOOL, VALIDATE_TOOL]
        ok, msg = check_tool_call_order(seq)
        assert ok is False
        assert "先校验后计算" in msg
        # 报错信息须包含明确修复方向
        assert "修复方向" in msg
        # 断言版本应抛出异常
        with pytest.raises(ToolCallOrderViolation):
            assert_tool_call_order(seq)

    def test_calculate_without_validate_is_caught(self):
        """缺失 validate 直接 calculate 应被捕获并报错。"""
        seq = ["parse_pdf_report", CALCULATE_TOOL, "export_pdf_report"]
        ok, msg = check_tool_call_order(seq)
        assert ok is False
        assert "未调用" in msg and VALIDATE_TOOL in msg
        with pytest.raises(ToolCallOrderViolation):
            assert_tool_call_order(seq)

    def test_correct_order_passes(self):
        """正序：validate 先于 calculate 应通过。"""
        seq = ["parse_pdf_report", VALIDATE_TOOL, CALCULATE_TOOL, "export_pdf_report"]
        ok, msg = check_tool_call_order(seq)
        assert ok is True
        # 断言版本不应抛异常
        assert_tool_call_order(seq)

    def test_no_calculate_is_skipped(self):
        """从未调用 calculate 时无需校验，视为通过。"""
        seq = ["parse_pdf_report", VALIDATE_TOOL, "export_excel_report"]
        ok, _ = check_tool_call_order(seq)
        assert ok is True

    def test_empty_sequence_passes(self):
        """空调用序列应视为通过（无可校验对象）。"""
        ok, _ = check_tool_call_order([])
        assert ok is True


class TestFullPipelineOrder:
    """完整声明链路顺序不变量（fail-closed 强制门禁）。

    声明链路：
        validate → calculate → check_disclosure → search → score
        → export_pdf + export_excel（导出对为并行，彼此无先后约束）。
    """

    def test_full_pipeline_out_of_order_raises(self):
        """乱序用例：后置步骤早于前置步骤（export 早于 score）应 fail-closed 抛异常。

        该序列已满足「先 validate 后 calculate」，专门验证完整链路相对次序层。
        """
        seq = [VALIDATE_TOOL, CALCULATE_TOOL, EXPORT_PDF_TOOL, SCORE_TOOL]
        ok, msg = check_tool_call_order(seq)
        assert ok is False
        assert "工具链声明顺序" in msg
        assert "修复方向" in msg
        # 断言版本应 fail-closed 抛出顺序违规
        with pytest.raises(ToolCallOrderViolation):
            assert_tool_call_order(seq)

    def test_disclosure_after_search_raises(self):
        """乱序用例：check_disclosure 晚于 search 与 score 调用，应招异常。"""
        seq = [VALIDATE_TOOL, CALCULATE_TOOL, SEARCH_TOOL, SCORE_TOOL, DISCLOSURE_TOOL]
        with pytest.raises(ToolCallOrderViolation):
            assert_tool_call_order(seq)

    def test_full_pipeline_correct_order_passes(self):
        """正序：完整声明链路依次调用应通过且不抛异常。"""
        seq = [
            "parse_pdf_report",
            VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
            SEARCH_TOOL, SCORE_TOOL, EXPORT_PDF_TOOL, EXPORT_EXCEL_TOOL,
        ]
        ok, _ = check_tool_call_order(seq)
        assert ok is True
        # 断言版本不应抛异常
        assert_tool_call_order(seq)

    def test_export_pair_order_is_interchangeable(self):
        """并行导出对：export_excel 先于 export_pdf 仍应通过（共享 rank，无先后约束）。"""
        seq = [
            VALIDATE_TOOL, CALCULATE_TOOL, DISCLOSURE_TOOL,
            SEARCH_TOOL, SCORE_TOOL, EXPORT_EXCEL_TOOL, EXPORT_PDF_TOOL,
        ]
        ok, _ = check_tool_call_order(seq)
        assert ok is True


class TestDisclaimerCheck:
    """约束二（纯函数层）：导出内容必须包含免责声明。"""

    def test_disclaimer_present_passes(self):
        texts = ["公司名称", f"【{DISCLAIMER_MARKER}】本报告由大语言模型自动生成"]
        ok, _ = check_disclaimer_present(texts)
        assert ok is True

    def test_disclaimer_missing_is_caught(self):
        """缺失免责声明应被捕获并给出修复方向。"""
        texts = ["公司名称", "风险明细", "整体评估结论"]
        ok, msg = check_disclaimer_present(texts)
        assert ok is False
        assert "修复方向" in msg
        with pytest.raises(DisclaimerMissingError):
            assert_disclaimer_present(texts, doc_kind="PDF风险报告")

    def test_collect_flowable_texts(self):
        """从鸭子类型的 flowable 元素中提取文本（含容器 _content）。"""
        class _Para:
            def __init__(self, text):
                self.text = text

        class _Keep:
            def __init__(self, content):
                self._content = content

        elements = [_Para("封面"), _Keep([_Para("内部段落"), _Para("免责声明")])]
        texts = collect_flowable_texts(elements)
        assert "封面" in texts
        assert "内部段落" in texts
        assert "免责声明" in texts


# ── 导出集成层：声明缺失时应拦截，不产出文件 ──

_SAMPLE_REPORT = json.dumps({
    "company_info": {"company_name": "测试公司", "report_year": "2025"},
    "risk_summary": {"total_risks": 1, "major_risks": 1, "important_risks": 0, "general_risks": 0},
    "risk_details": [{"risk_id": "R001", "dimension": "financial_misstatement",
                      "title": "测试风险", "level": "重大", "confidence": 0.9,
                      "evidence": "证据", "audit_suggestion": "建议"}],
    "overall_assessment": "整体评估结论文本",
}, ensure_ascii=False)


class TestExportInterception:
    """约束二（导出集成层）：声明缺失应被拦截。"""

    def test_pdf_export_intercepts_missing_disclaimer(self, monkeypatch, tmp_path):
        """将 PDF 免责声明常量置空后导出，应被拦截且不产出文件。"""
        import tools.pdf_export as pdf_export
        monkeypatch.setattr(pdf_export, "AI_DISCLAIMER", "")
        out = str(tmp_path / "should_not_exist.pdf")
        result = pdf_export._export_pdf_impl(_SAMPLE_REPORT, output_path=out)
        assert result.startswith("导出被拦截")
        assert not os.path.exists(out)

    def test_excel_export_intercepts_missing_disclaimer(self, monkeypatch, tmp_path):
        """将 Excel 免责声明常量置空后导出，应被拦截且不产出文件。"""
        import tools.excel_export as excel_export
        monkeypatch.setattr(excel_export, "AI_DISCLAIMER", "")
        out = str(tmp_path / "should_not_exist.xlsx")
        result = excel_export._export_excel_impl(_SAMPLE_REPORT, output_path=out)
        assert result.startswith("导出被拦截")
        assert not os.path.exists(out)
