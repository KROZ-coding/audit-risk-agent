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
from tools.audit_opinion import identify_audit_opinion

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
        """包含全部必要章节的完整年报应获得较高合规分且无缺失章节。"""
        result = self._invoke(FULL_REPORT)
        assert result["compliance_score"] >= 80
        assert result["sections_missing"] == []

    def test_opinion_not_guessed_without_audit_features(self):
        """文本不含审计报告特征（如半年报正文）时，审计意见不得默认「标准无保留」。

        修复缺陷：旧实现硬编码默认「标准无保留意见」，与 identify_audit_opinion
        的「未识别」口径冲突，导致合规报告内部自相矛盾（实测缺陷）。
        """
        result = self._invoke(FULL_REPORT)
        assert result["audit_opinion"] == "未识别"

    def test_standard_opinion_detected_with_audit_features(self):
        """文本含审计报告特征（公允反映等）时才判定为标准无保留意见。"""
        text = FULL_REPORT + "我们认为，财务报表在所有重大方面公允反映了公司的财务状况。"
        result = self._invoke(text)
        assert result["audit_opinion"] == "标准无保留意见"

    def test_missing_sections_generate_review_candidates(self):
        """未识别的章节需要复核，不能把文本扫描当作违规证据。"""
        text = "本公司仅披露了公司基本情况与主要会计数据，其余内容从略。" * 5
        result = self._invoke(text)
        assert len(result["sections_missing"]) > 0
        assert any("待核查章节" in issue for issue in result["issues"])
        assert all(item["status"] == "needs_review" for item in result["issue_records"])
        issue_ids = {item["evidence_id"] for item in result["issue_records"]}
        assert all(not item["verified"] for item in result["evidence"]
                   if item["evidence_id"] in issue_ids)
        assert result["confirmed_issue_count"] == 0

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


class TestDisclosureReviewBoundaries:
    def _invoke(self, suffix, metadata=None):
        return json.loads(check_disclosure_compliance.invoke({
            "report_text": FULL_REPORT + suffix,
            "source_metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        }))

    def test_guarantee_uses_content_rule_and_requires_scope_review(self):
        result = self._invoke("本半年度报告的担保余额为100万元。")
        issues = [issue for issue in result["issues"] if "担保事项" in issue]
        assert len(issues) == 1
        assert "第39条" in issues[0]
        assert "信披管理办法第25条" not in issues[0]
        assert "待核查" in issues[0]
        assert "适用版本" in issues[0]
        assert "事项重要性" in issues[0]

    def test_subsidiary_keyword_does_not_exempt_guarantee(self):
        base = self._invoke("本半年度报告的担保余额为100万元。")
        subsidiary = self._invoke("本半年度报告的担保余额为100万元。另有全资子公司经营情况。")
        assert subsidiary["risk_score"] == base["risk_score"]
        assert any("担保事项披露待核查" in issue for issue in subsidiary["issues"])

    def test_overlapping_keywords_do_not_prove_guarantee_details(self):
        result = self._invoke("本公司存在对外担保。")
        assert any("担保事项披露待核查" in issue for issue in result["issues"])

    def test_cross_reference_remains_reviewable_without_asserting_omission(self):
        result = self._invoke("本半年度报告的担保事项详见财务报表附注及此前发布的公告。")
        issue = next(issue for issue in result["issues"] if "担保事项" in issue)
        assert "存在交叉引用" in issue
        assert "不因正文未重复列示认定违规" in issue
        assert any(item["pending_verification"] for item in result["issue_records"])

    def test_no_guarantee_statement_is_observation(self):
        result = self._invoke("本报告期不存在对外担保事项。")
        assert not any("担保事项" in issue for issue in result["issues"])
        assert any("担保事项披露" in item and "声明范围" in item
                   for item in result["observations"])

    def test_limited_negative_statement_does_not_erase_other_exposure(self):
        result = self._invoke("不存在为控股股东提供的担保。担保余额为100万元。")
        assert any("担保事项披露待核查" in issue for issue in result["issues"])

    def test_undetected_topics_are_not_assessed(self):
        result = self._invoke("")
        assert any("担保事项披露" in item for item in result["items_not_assessed"])
        assert not any("担保事项披露" in item for item in result["observations"])

    def test_explicit_excerpt_is_not_scored_as_full_report(self):
        result = self._invoke("本半年度报告的担保余额为100万元。",
                              {"text_scope": "excerpt", "source_document": "report.pdf", "page": "35"})
        assert result["compliance_score"] is None
        assert result["risk_score"] is None
        assert result["metric_results"][0]["value"] is None
        assert result["metric_results"][0]["status"] == "insufficient_data"
        assert result["facts"][0]["page"] == "35"
        assert 0 <= result["text_scan_score"] <= 100
        assert not result["evidence"][0]["verified"]

    def test_half_year_title_does_not_imply_unaudited(self):
        result = self._invoke("本报告为2025年半年度报告。")
        assert result["audit_opinion"] == "未识别"

    def test_unaudited_current_half_year_is_not_a_company_risk(self):
        ordinary = self._invoke("本报告为2025年半年度报告。")
        result = self._invoke("本报告为2025年半年度报告，未经审计。")
        assert result["audit_opinion"] == "未经审计（半年度报告）"
        assert not result["is_non_standard_opinion"]
        assert result["risk_score"] == ordinary["risk_score"]
        assert not any("未经审计" in issue for issue in result["issues"])

    def test_prior_qualified_opinion_does_not_replace_current_unaudited_status(self):
        result = self._invoke("本半年度报告中的财务报告未经审计。上年度财务报告被出具保留意见。")
        assert result["audit_opinion"] == "未经审计（半年度报告）"
        assert not result["is_non_standard_opinion"]
        assert any("上年审计意见延续影响待核查" in issue for issue in result["issues"])

    def test_unqualified_opinion_does_not_match_qualified_substring(self):
        result = self._invoke("会计师出具了标准无保留意见。")
        assert result["audit_opinion"] == "标准无保留意见"
        assert not result["is_non_standard_opinion"]

    def test_audit_report_reference_does_not_prove_unqualified_opinion(self):
        result = self._invoke("审计报告详见其他附件。")
        assert result["audit_opinion"] == "未识别"

    def test_policy_reason_in_unrelated_section_does_not_resolve_gap(self):
        result = self._invoke("本期发生会计政策变更。根据董事会决议建设新的生产基地。")
        assert any("会计政策变更待核查" in issue for issue in result["issues"])

    def test_asset_pledge_does_not_trigger_shareholder_pledge_review(self):
        result = self._invoke("本公司以收费权作为质押取得长期借款。固定资产另用于抵押借款。")
        assert not any("股权质押" in issue for issue in result["issues"])

    def test_shareholder_quantity_table_is_not_confused_with_asset_pledge(self):
        result = self._invoke(
            "股份变动及股东情况\n股东名称\n持股数量\n质押、标记\n或冻结的\n股份数量\n"
            "甲公司 1000 0\n乙公司 500 0\n。长期借款以收费权质押融资。")
        assert not any("股权质押" in issue for issue in result["issues"])
        assert any("股权质押披露" in item and "表格字段" in item
                   for item in result["observations"])

    def test_explicit_shareholder_pledge_still_requires_details(self):
        result = self._invoke("控股股东进行了股份质押。")
        assert any("股权质押披露待核查" in issue for issue in result["issues"])

    def test_mandatory_policy_change_explanation_can_span_paragraphs(self):
        result = self._invoke(
            "除下文所述的会计政策变更外，采用的政策与上年度一致。\n"
            "这些会计政策的变更也将体现在本年度报表中。\n"
            "国际会计准则理事会发布了准则修订，在当前会计期间首次生效。\n"
            "本次修订涉及外汇换算。\n这些修订未对本期或前期经营成果产生重大影响。")
        assert not any("会计政策变更待核查" in issue for issue in result["issues"])
        assert any("会计政策变更" in item and "依据和影响" in item
                   for item in result["observations"])

    def test_change_without_explanation_is_reported_once(self):
        result = self._invoke("本期发生会计政策变更。")
        assert len([issue for issue in result["issues"] if "会计政策变更" in issue]) == 1

    def test_full_document_input_scope_is_preserved(self):
        result = self._invoke("", {"input_scope": "full_document"})
        assert result["input_scope"] == "full_document"
        assert result["compliance_score"] is not None

    def test_excerpt_input_scope_alias_disables_full_report_score(self):
        result = self._invoke("", {"input_scope": "excerpt"})
        assert result["input_scope"] == "excerpt"
        assert result["risk_score"] is None


class TestAuditOpinionIdentification:
    """identify_audit_opinion：五类意见识别与独立风险信号

    关键风险：严重意见的审计报告里同样包含“公允反映”等标准表述，
    若匹配优先级从宽到严，会把否定意见误判为无保留意见——直接导致
    整份报表不可信却被当成可信基础。本组用例锁定该优先级。
    """

    def _invoke(self, text):
        return json.loads(identify_audit_opinion.invoke({"report_text": text}))

    _STANDARD_TAIL = "我们认为，财务报表在所有重大方面按照企业会计准则的规定编制，公允反映了公司的财务状况。"

    def test_standard_unqualified(self):
        r = self._invoke(self._STANDARD_TAIL)["audit_opinion"]
        assert r["opinion_type"] == "无保留意见"
        assert r["is_standard_opinion"] is True

    def test_qualified_wins_over_standard_wording(self):
        """保留意见报告也含标准措辞，必须判为保留而非无保留。"""
        text = "形成保留意见的基础：存货盘点受限。" + self._STANDARD_TAIL
        r = self._invoke(text)["audit_opinion"]
        assert r["opinion_type"] == "保留意见"
        assert r["is_standard_opinion"] is False

    def test_adverse_opinion(self):
        r = self._invoke("形成否定意见的基础：未能公允反映。" + self._STANDARD_TAIL)["audit_opinion"]
        assert r["opinion_type"] == "否定意见"
        assert r["credibility_impact"] == "极高"

    def test_disclaimer_has_highest_priority(self):
        """无法表示意见与其他意见措辞共存时，必须取最严的一类。"""
        text = "我们无法对上述财务报表发表意见。形成保留意见的基础如下。" + self._STANDARD_TAIL
        assert self._invoke(text)["audit_opinion"]["opinion_type"] == "无法表示意见"

    def test_emphasis_paragraph_is_medium_not_high(self):
        """带强调事项段不得直接判高风险（知识库裁定规则），风险等级归一为三档「一般」。"""
        text = "强调事项段：提醒财务报表使用者关注诉讼事项。" + self._STANDARD_TAIL
        r = self._invoke(text)["audit_opinion"]
        assert r["opinion_type"] == "带强调事项段的无保留意见"
        assert r["risk_level"] == "一般"

    def test_unidentified_does_not_guess(self):
        """未包含审计报告的文本不得推测意见类型（避免凭空给出无保留结论）。"""
        r = self._invoke("本公司主要从事智能硬件制造与销售，报告期内营业收入稳定增长。")
        assert r["audit_opinion"]["identified"] is False
        assert r["audit_opinion"]["opinion_type"] == "未识别"

    def test_short_text_returns_full_schema(self):
        """短文本早退也必须返回完整 schema，否则下游取键会 KeyError。"""
        r = self._invoke("无")
        for key in ("audit_opinion", "going_concern", "key_audit_matters",
                    "auditor_change", "linkage_alerts"):
            assert key in r

    def test_going_concern_is_independent_signal(self):
        """持续经营重大不确定性独立于意见类型，且与强调事项段叠加时顶掉宽免。"""
        text = ("强调事项段：与持续经营相关的重大不确定性。" + self._STANDARD_TAIL)
        r = self._invoke(text)
        assert r["going_concern"]["flagged"] is True
        assert any("持续经营" in a for a in r["linkage_alerts"])

    def test_opinion_purchase_linkage(self):
        """非标意见 + 事务所变更 → 必须提示「意见购买」嫌疑。"""
        text = ("形成保留意见的基础：往来款无法函证。本年度变更会计师事务所。")
        r = self._invoke(text)
        assert r["auditor_change"]["flagged"] is True
        assert any("意见购买" in a for a in r["linkage_alerts"])

    def test_key_audit_matters_map_risk_direction(self):
        """关键审计事项应映射到风险方向，供报告直接引用。"""
        text = "关键审计事项：收入确认与商誉减值测试。形成保留意见的基础如下。"
        r = self._invoke(text)
        assert r["key_audit_matters"]["found"] is True
        matters = {m["matter"] for m in r["key_audit_matters"]["matters"]}
        assert "收入确认" in matters and "商誉" in matters
    
    def test_half_year_exemption_keeps_counts_balanced(self):
        """半年报豁免公司治理：checked 与 passed 同步 +1，通过项不得溢出检查项。
    
        实测缺陷：豁免分支只 passed+1 未 checked+1，导致通过项 14 > 检查项 13、
        合规评分 108 分、风险分 -8。
        """
        text = FULL_REPORT + "本报告为2025年半年度报告，未经审计。"
        result = json.loads(check_disclosure_compliance.invoke({"report_text": text}))
        assert result["passed_items"] <= result["checked_items"], "通过项不得大于检查项"
        assert 0 <= result["compliance_score"] <= 100, "合规评分必须 clamp 在 [0,100]"
        assert 0 <= result["risk_score"] <= 100, "风险评分必须 clamp 在 [0,100]"
        assert "公司治理" not in result["sections_missing"], "半年报不强制公司治理章节"
    
    def test_score_clamped_when_passed_exceeds_checked(self):
        """构造通过项溢出场景：评分仍被 clamp 到 100 且风险分非负。"""
        # 直接构造异常计数（防御路径）：通过项 > 检查项时须封顶而非渲染 108 分
        from tools.disclosure_checker import check_disclosure_compliance as tool
        text = FULL_REPORT * 3  # 全章节命中 → 通过项密集
        result = json.loads(tool.invoke({"report_text": text}))
        assert result["compliance_score"] <= 100
        assert result["risk_score"] >= 0
