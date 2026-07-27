"""company_info 别名兼容与历史字段抽取测试

背景：LLM 输出的 company_info 键名不总是等于模板约定（实测出现过 name、
report_period），导致图表/报告文件名变「未知公司_未知」、历史记录无公司名无评分。
修复为工具层别名归一化（utils.filename.resolve_company_year）+ 历史抽取兜底，
本测试锁定：
- 别名表按优先级解析（标准键优先于别名）
- 三类导出/图表的文件名前缀不再产出「未知公司」
- _extract_history_fields 在别名键与无 marker 场景下仍能取到公司名与评分
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from utils.filename import resolve_company_year


class TestResolveCompanyYear:
    """别名表解析：标准键 > 常见别名 > 中文键"""

    def test_standard_keys_first(self):
        ci = {"company_name": "招商银行", "name": "别名不应生效", "report_year": "2025", "report_period": "x"}
        assert resolve_company_year(ci) == ("招商银行", "2025")

    def test_alias_keys(self):
        # 实测事故场景：LLM 用了 name + report_period
        ci = {"name": "招商银行股份有限公司", "stock_code": "600036.SH", "report_period": "2016-2025年"}
        company, year = resolve_company_year(ci)
        assert company == "招商银行股份有限公司"
        assert year == "2016-2025年"

    def test_chinese_keys_and_empty(self):
        assert resolve_company_year({"公司名称": "茅台", "年度": "2022"}) == ("茅台", "2022")
        assert resolve_company_year({}) == ("", "")
        assert resolve_company_year(None) == ("", "")


class TestFilePrefixAliases:
    """导出/图表文件名前缀：别名键下不再产出「未知公司_未知」"""

    def test_pdf_excel_visualizer_prefix(self):
        from tools.excel_export import _build_file_prefix as excel_prefix
        from tools.pdf_export import _build_file_prefix as pdf_prefix
        from tools.visualizer import _build_file_prefix as viz_prefix

        report = {"company_info": {"name": "招商银行", "report_period": "2025"}}
        for fn in (pdf_prefix, excel_prefix, viz_prefix):
            prefix = fn(report)
            assert "招商银行" in prefix and "2025" in prefix, fn.__module__
            assert "未知公司" not in prefix

    def test_prefix_falls_back_when_truly_unknown(self):
        from tools.pdf_export import _build_file_prefix
        assert "未知公司" in _build_file_prefix({"company_info": {}})

    def test_spaces_sanitized_in_filename(self):
        """实测事故：公司名含英文空格（互太纺织（Pacific Textiles））时，
        文件名带空格会被前后端 URL 提取正则拦腰截断导致 404。
        锁定：sanitize_filename 后不含任何空白（含全角空格/tab）。"""
        from utils.filename import sanitize_filename
        s = sanitize_filename("互太纺织（Pacific Textiles Holdings Limited）")
        assert " " not in s and "\u3000" not in s and "\t" not in s
        assert "Pacific_Textiles" in s
        # 全角空格与连续空格同样清洗
        assert " " not in sanitize_filename("A\u3000B  C")


class TestHistoryFieldAliases:
    """main._extract_history_fields：别名键 + 无 marker 时从内嵌 comprehensive_score 兜底"""

    def test_alias_company_and_embedded_score(self):
        from main import _extract_history_fields
        # 实测事故场景：company_info 用 name/report_period，评分内嵌于台账、无 marker
        ai_text = (
            '风险台账：{"company_info": {"name": "招商银行股份有限公司", "report_period": "2016-2025年"}, '
            '"risk_details": [], '
            '"comprehensive_score": {"score": 15.5, "level": "低风险", "note": "基于有限数据"}}'
        )
        fields = _extract_history_fields({"ai_text": ai_text})
        assert fields["company_name"] == "招商银行股份有限公司"
        assert fields["report_year"] == "2016-2025年"
        assert fields["score"] == 15.5
        assert fields["risk_level"] == "低风险"

    def test_marker_takes_priority_over_embedded(self):
        from main import _extract_history_fields
        ai_text = (
            '{"company_info": {"company_name": "测试公司", "report_year": "2025"}, "risk_details": [], '
            '"comprehensive_score": {"score": 99, "level": "极高风险"}}\n\n'
            '<!--COMPREHENSIVE_SCORE-->\n'
            '{"score": 42.0, "level": "中等风险", "summary": "以兜底评分为准"}'
        )
        fields = _extract_history_fields({"ai_text": ai_text})
        # marker（系统兜底评分）优先于台账内嵌评分
        assert fields["score"] == 42.0
        assert fields["risk_level"] == "中等风险"
