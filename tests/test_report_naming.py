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


def _patch_filename_module_path(tmp_path, monkeypatch):
    """把 utils.filename.__file__ 指向 tmp_path 下的伪路径，隔离真实 local_storage。

    必须先真实创建 src/utils 目录再打补丁：count_existing_runs 用
    dirname(__file__)/../../local_storage 定位产物目录，而 Windows 的 Win32 会把
    ".." 先做词法折叠（中间目录不存在也判定 isdir 为 True），Linux 内核则逐段真实
    遍历（中间目录缺失即 ENOENT）——不建目录会让这几个用例只在 Linux CI 上失败。

    Returns:
        已打过补丁的 utils.filename 模块对象
    """
    fake_dir = tmp_path / "src" / "utils"
    fake_dir.mkdir(parents=True, exist_ok=True)
    import utils.filename as fn_mod
    monkeypatch.setattr(fn_mod, "__file__", str(fake_dir / "filename.py"))
    return fn_mod


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

    def test_roman_numeral_suffix_in_prefix(self):
        """文件名末尾追加罗马数字序号：同一公司同日多轮运行不再互相覆盖。"""
        from utils.filename import build_file_prefix, to_roman
        import re
        # 显式传入 run_number
        report = {"company_info": {"company_name": "中石油", "report_year": "2025"}}
        prefix = build_file_prefix(report, run_number=1)
        assert prefix.endswith("_I"), prefix
        assert "中石油" in prefix and "2025" in prefix
        # 格式：YYYYMMDD_公司_年份_罗马数字
        assert re.match(r"^\d{8}_中石油_2025_I$", prefix), prefix

        prefix3 = build_file_prefix(report, run_number=3)
        assert prefix3.endswith("_III"), prefix3

        prefix12 = build_file_prefix(report, run_number=12)
        assert prefix12.endswith("_XII"), prefix12

    def test_to_roman_helper(self):
        """to_roman 辅助函数：正整数 → 大写罗马数字。"""
        from utils.filename import to_roman
        assert to_roman(1) == "I"
        assert to_roman(4) == "IV"
        assert to_roman(9) == "IX"
        assert to_roman(14) == "XIV"
        assert to_roman(42) == "XLII"
        assert to_roman(99) == "XCIX"
        assert to_roman(3999) == "MMMCMXCIX"

    def test_prefix_auto_computes_run_number(self, tmp_path, monkeypatch):
        """未显式传入 run_number 时，按既有产物罗马数字后缀 max+1 自动计算。"""
        from datetime import datetime
        from utils.filename import build_file_prefix
        import re
        date_str = datetime.now().strftime("%Y%m%d")
        fake_reports = tmp_path / "local_storage" / "reports"
        fake_reports.mkdir(parents=True)
        # 一轮运行产出多个文件共享同一序号 _I
        (fake_reports / f"{date_str}_中石油_2025_I_审计风险报告.pdf").write_text("")
        (fake_reports / f"{date_str}_中石油_2025_I_审计底稿.xlsx").write_text("")
        # 让 count_existing_runs 扫描 tmp_path 下的 local_storage
        _patch_filename_module_path(tmp_path, monkeypatch)
        report = {"company_info": {"company_name": "中石油", "report_year": "2025"}}
        # max=1 → 下一序号 2（旧“数文件个数”口径会误算为 3）
        prefix = build_file_prefix(report)
        assert re.match(rf"^{date_str}_中石油_2025_II$", prefix), prefix

    def test_count_max_plus_one_no_skip_no_collision(self, tmp_path, monkeypatch):
        """回归实测事故：部分失败轮只落盘 2 个文件时，旧文件数口径会跳号
        （I→III→IX）甚至碰撞覆盖；max+1 口径下序号严格递增。"""
        from datetime import datetime
        from utils.filename import count_existing_runs
        date_str = datetime.now().strftime("%Y%m%d")
        charts = tmp_path / "local_storage" / "charts"
        charts.mkdir(parents=True)
        # 复现真实目录分布：I/III/IX/XI 各 2 个文件（部分轮次只出 2 图）
        for roman in ("I", "III", "IX", "XI"):
            (charts / f"{date_str}_测试公司_{roman}_风险热力图.png").write_text("")
            (charts / f"{date_str}_测试公司_{roman}_财务雷达图.png").write_text("")
        _patch_filename_module_path(tmp_path, monkeypatch)
        # max=11 → 下一序号 12（旧口径 count=8 → 9 → 与既有 _IX 撞名覆盖）
        assert count_existing_runs("测试公司") == 11

    def test_count_ignores_substring_and_legacy_hex(self, tmp_path, monkeypatch):
        """子串污染与旧格式残留不计入：
        “中国石油”不命中“中国石油天然气股份有限公司”的产物；
        旧 run8 hex 前缀文件名不含罗马数字段，自然排除。"""
        from datetime import datetime
        from utils.filename import count_existing_runs
        date_str = datetime.now().strftime("%Y%m%d")
        reports = tmp_path / "local_storage" / "reports"
        reports.mkdir(parents=True)
        (reports / f"{date_str}_中国石油天然气股份有限公司_2025_II_审计风险报告.pdf").write_text("")
        (reports / f"{date_str}_aa2b8ee9_中国石油_2025_审计风险报告.pdf").write_text("")
        _patch_filename_module_path(tmp_path, monkeypatch)
        assert count_existing_runs("中国石油") == 0

    def test_from_roman_helper(self):
        """from_roman 反解：标准形态解析，非标准/非法串返回 0。"""
        from utils.filename import from_roman
        assert from_roman("I") == 1
        assert from_roman("IX") == 9
        assert from_roman("XIV") == 14
        assert from_roman("XCIX") == 99
        assert from_roman("MMMCMXCIX") == 3999
        # 非标准形态不参与统计，避免污染序号
        assert from_roman("IIII") == 0
        assert from_roman("趋势图") == 0
        assert from_roman("") == 0
        assert from_roman("2025") == 0

    def test_run_number_injection_type_guard(self):
        """company_info 内预注入的 run_number 必须是正整数；
        LLM 幻觉出的字符串值应被丢弃并回退自动计算（不抛 TypeError）。"""
        from utils.filename import build_file_prefix
        report = {"company_info": {"company_name": "中石油", "report_year": "2025",
                                   "run_number": "abc"}}
        prefix = build_file_prefix(report)  # 不应抛异常
        assert prefix.endswith("_I"), prefix
        # 布尔值同样不接受（bool 是 int 子类）
        report2 = {"company_info": {"company_name": "中石油", "report_year": "2025",
                                    "run_number": True}}
        assert build_file_prefix(report2).endswith("_I")

    def test_trend_chart_run_number_from_data(self, monkeypatch):
        """趋势图文件名罗马数字序号：优先取 trend_data 内注入的 run_number，
        次选自动计算（count_existing_runs + 1）。"""
        import json
        import tools.visualizer as viz
        captured = {}
        def fake_upload(label, path, filename):
            captured["filename"] = filename
            return f"/local_storage/charts/{filename}"
        monkeypatch.setattr(viz, "_upload", fake_upload)

        data = {"company_name": "测试公司",
                "run_number": 2,
                "years": [{"year": "2023", "revenue": 100}, {"year": "2024", "revenue": 120}]}
        viz.generate_trend_chart.invoke({"trend_data_json": json.dumps(data, ensure_ascii=False)})
        assert "测试公司_趋势图_II.png" in captured["filename"], captured["filename"]

    def test_trend_chart_auto_run_number(self, monkeypatch):
        """trend_data 无 run_number 时自动计算序号。"""
        import json
        import tools.visualizer as viz
        captured = {}
        def fake_upload(label, path, filename):
            captured["filename"] = filename
            return f"/local_storage/charts/{filename}"
        monkeypatch.setattr(viz, "_upload", fake_upload)
        data = {"company_name": "测试公司",
                "years": [{"year": "2023", "revenue": 100}, {"year": "2024", "revenue": 120}]}
        viz.generate_trend_chart.invoke({"trend_data_json": json.dumps(data, ensure_ascii=False)})
        # 自动计算：无已有文件 → run_number=1 → _I
        assert "_I.png" in captured["filename"] or "测试公司_趋势图" in captured["filename"], captured["filename"]

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
