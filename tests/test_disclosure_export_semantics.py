"""Disclosure exports preserve the distinction between screening and compliance."""

import pytest

from tools import pdf_export as pe


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    if hasattr(value, "getPlainText"):
        return value.getPlainText()
    if hasattr(value, "_cellvalues"):
        return _text(value._cellvalues)
    if hasattr(value, "_content"):
        return _text(value._content)
    return ""


@pytest.fixture
def styles():
    font = pe._register_chinese_font()
    return pe._build_styles(font), font


def _screening(**updates):
    result = {
        "score_basis": "internal_text_screening",
        "text_scan_score": 100,
        "compliance_score": 100,
        "risk_score": 0,
        "checked_items": 10,
        "passed_items": 10,
        "input_scope": "full_document",
        "issues": ["担保交叉引用待核查"],
        "sections_missing": [],
        "confirmed_issue_count": 0,
    }
    result.update(updates)
    return result


def test_full_screening_score_is_not_rendered_as_compliance(styles):
    st, font = styles
    text = _text(pe._disclosure_section_body(_screening(), st, font))
    assert "文本筛查参考分" in text and "100" in text
    assert "待复核提示" in text
    assert "不构成实质合规判断" in text
    assert "披露形式合规" not in text
    assert "披露合规评分" not in text
    assert "披露风险评分" not in text
    assert "发现的披露问题" not in text


def test_unaudited_half_year_remains_attribute_with_observations(styles):
    st, font = styles
    data = _screening(audit_opinion="未经审计（半年度报告）",
                      observations=["已识别会计政策变更依据和无重大影响说明"])
    old_opinion = {"audit_opinion": {"identified": True, "opinion_type": "保留意见"}}
    text = _text(pe._disclosure_section_body(data, st, font, old_opinion))
    assert "未经审计（半年度报告）" in text
    assert "仅作资料属性记录，不作为独立公司风险" in text
    assert "财报可信度" not in text
    assert "保留意见" not in text
    assert "已识别会计政策变更依据" in text


def test_excerpt_reference_score_does_not_imply_full_report_rating(styles):
    st, font = styles
    data = _screening(input_scope="excerpt", compliance_score=None, risk_score=None,
                      sections_missing=["主要会计数据和财务指标"])
    text = _text(pe._disclosure_section_body(data, st, font))
    assert "100" in text
    assert "风险分未评定" in text
    assert "当前文本未识别的章节" in text
    assert "尚未认定披露缺失" in text
    assert "年报缺失章节" not in text


def test_half_year_standard_table_uses_verified_midyear_references(styles):
    st, font = styles
    data = _screening(facts=[{"period": "2025年半年度"}],
                      audit_opinion="未经审计（半年度报告）")
    text = _text(pe._compliance_standard_body(data, st, font))
    assert "半年度报告内容与格式准则第3号" in text
    assert "第38条" in text and "第39条" in text and "第9条" in text
    assert "年度报告的内容与格式准则第2号" not in text
    assert "年度报告内容与格式准则第2号" not in text
    assert "整体披露较为规范" not in text
    assert "不构成实质合规判断" in text
    assert "交叉引用" in text and "重大性" in text and "披露时点" in text


def test_unknown_report_type_does_not_default_to_annual_rule(styles):
    st, font = styles
    text = _text(pe._compliance_standard_body(_screening(), st, font))
    assert "报告类型待核验" in text
    assert "第2号" not in text and "第3号" not in text
    assert "第38条" not in text and "第39条" not in text


def test_old_half_year_period_does_not_use_2025_article_numbers(styles):
    st, font = styles
    text = _text(pe._compliance_standard_body(
        _screening(facts=[{"period": "2024年半年度"}]), st, font))
    assert "半年度报告内容与格式准则第3号" in text
    assert "2025年版" not in text
    assert "第38条" not in text and "第39条" not in text


def test_known_annual_report_uses_annual_rule_without_midyear_assumptions(styles):
    st, font = styles
    text = _text(pe._compliance_standard_body(_screening(report_type="annual", period="2025"), st, font))
    assert "年度报告内容与格式准则第2号" in text
    assert "半年度报告内容与格式准则第3号" not in text


def test_candidate_ledger_does_not_become_confirmed_disclosure_problem(styles):
    st, font = styles
    risks = [{"risk_id": "R002", "dimension": "related_party", "formal_status": "candidate"}]
    text = _text(pe._compliance_standard_body(_screening(issues=[]), st, font, risks))
    assert "R002" in text and "按各自复核状态另行列示" in text
    assert "已确认交易合规" in text
    assert "发现关联交易相关披露问题" not in text
    assert "整体披露较为规范" not in text


def test_legacy_disclosure_contract_keeps_existing_scores(styles):
    st, font = styles
    legacy = {"compliance_score": 85.7, "risk_score": 14.3, "checked_items": 14,
              "passed_items": 12, "issues": [], "sections_missing": []}
    section = _text(pe._disclosure_section_body(legacy, st, font))
    standards = _text(pe._compliance_standard_body(legacy, st, font))
    assert "85.7" in section and "14.3" in section
    assert "85.7" in standards
    assert "披露合规评分" in section
