"""信息披露规范性检查工具

参照适用的定期报告披露规则，筛查文本中的章节和关键披露事项。
关键词缺失只是待核查线索，不代表已经确认的披露违规。

检查维度：
1. 必要章节完整性（公司概况、会计数据、股东情况、董监高、财务报告等）
2. 关联交易披露
3. 会计政策变更说明
4. 重大事项披露（担保、诉讼、质押）
5. 审计意见类型

输出文本筛查评分（0-100）和待核查清单，不能替代实质合规复核。
"""
import re
import json
import logging
from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 章节名允许同义表述；此清单不判定具体条文的适用性。
REQUIRED_SECTIONS = [
    ("公司基本情况", ["公司基本情况", "公司简介", "公司概况", "基本信息"]),
    ("主要会计数据和财务指标", ["主要会计数据", "财务指标", "主要财务数据"]),
    ("股东及实际控制人情况", ["股东", "实际控制人", "持股情况", "前十名股东"]),
    ("董事、监事、高级管理人员", ["董事", "监事", "高级管理人员", "董监高"]),
    ("公司治理", ["公司治理", "治理结构", "内部控制"]),
    ("财务报告", ["财务报告", "财务报表", "资产负债表", "利润表"]),
    ("董事会报告", ["董事会报告", "经营情况讨论", "管理层讨论"]),
]

# ── 关键披露事项检查（含法规条款引用）──
DISCLOSURE_CHECKS = [
    ("关联交易披露", ["关联交易", "关联方", "关联采购", "关联销售"]),
    ("担保事项披露", ["担保", "对外担保", "连带担保"]),
    ("诉讼仲裁披露", ["诉讼", "仲裁", "重大诉讼", "未决诉讼"]),
    ("股权质押披露", ["股权质押", "股份质押", "质押股份", "质押股权"]),
]

DETAIL_TERMS = {
    "关联交易披露": [("定价", "市场价格", "政府定价"), ("金额", "余额"),
                   ("关联交易方", "关联方名称"), ("结算", "利率")],
    "担保事项披露": [("担保金额", "担保余额"), ("担保期限",), ("担保对象", "被担保方"),
                   ("担保类型", "连带责任", "一般担保"), ("反担保",), ("决策程序", "审议")],
    "诉讼仲裁披露": [("涉及金额", "涉案金额"), ("进展", "判决", "裁决"),
                   ("预计负债",), ("原告", "被告", "申请人")],
    "股权质押披露": [("质押数量", "质押股份", "质押比例"), ("质权人",),
                   ("到期", "期限"), ("用途", "融资金额")],
}

NO_MATTER_PATTERNS = {
    "关联交易披露": r"(?:无|未发生|不存在|不涉及)(?:重大)?关联交易",
    "担保事项披露": r"(?:无|未发生|不存在|不涉及)(?:重大)?(?:对外)?担保(?:事项|情况)",
    "诉讼仲裁披露": r"(?:无|未发生|不存在|不涉及)(?:重大)?诉讼(?:、仲裁|或仲裁|及仲裁)?(?:事项|情况)",
    "股权质押披露": r"(?:无|未发生|不存在|不涉及)(?:股权|股份)?质押(?:事项|情况)",
    "会计政策变更说明": r"(?:无|未发生|不存在|不涉及)(?:重要)?会计(?:政策|估计)变更",
}


def _topic_context(report_text: str, keywords: list[str]) -> str:
    paragraphs = re.split(r"[。；;]", re.sub(r"\s+", " ", report_text))
    return "。".join(part for part in paragraphs if any(kw in part for kw in keywords))


def _policy_change_contexts(report_text: str) -> list[str]:
    # 变更原因和影响常在标题后的多个句子中，不能只保留含标题关键词的行。
    sentences = re.split(r"[。；;]", re.sub(r"\s+", "", report_text))
    return ["。".join(sentences[index:index + 6])[:2500]
            for index, sentence in enumerate(sentences)
            if any(term in sentence for term in ("会计政策变更", "会计估计变更", "会计政策的变更"))
            and not re.search(NO_MATTER_PATTERNS["会计政策变更说明"], sentence)]


def _disclosure_reference(check_name: str, is_half_year: bool) -> str:
    if is_half_year:
        article = {"关联交易披露": "第38条", "担保事项披露": "第39条",
                   "诉讼仲裁披露": "第35条"}.get(check_name)
        if article:
            return f"《半年度报告内容与格式准则第3号》（2025年版）{article}；须核对披露时点适用版本及事项范围"
    if check_name == "会计政策变更说明":
        return "《企业会计准则第28号》披露要求；须区分变更类型及适用期间"
    return "适用版本的定期报告内容与格式准则及交易所规则；须核对事项范围和披露条件"


def _source_metadata(source_metadata_json: str) -> dict:
    """读取可选来源定位；文本扫描本身不应伪造页码或文件哈希。"""
    if isinstance(source_metadata_json, dict):
        return source_metadata_json
    if not source_metadata_json:
        return {}
    try:
        value = json.loads(source_metadata_json)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _disclosure_anchor(report_text: str, metadata: dict) -> dict:
    return make_fact(
        "report_text", report_text, fact_id="F-DISCLOSURE-REPORT",
        currency="", period=str(metadata.get("period", "") or ""),
        scope=str(metadata.get("scope", "") or ""),
        source_document=str(metadata.get("source_document", "") or ""),
        source_hash=str(metadata.get("source_hash", "") or ""),
        page=str(metadata.get("page", metadata.get("page_number", "")) or ""),
        locator=str(metadata.get("locator", metadata.get("table_locator", "")) or ""),
        excerpt=str(report_text or "")[:1000],
        extraction_method=str(metadata.get("extraction_method", "text_scan") or "text_scan"),
    ).to_dict()


def _disclosure_failure(report_text: str, metadata: dict, message: str) -> str:
    fact = _disclosure_anchor(report_text or "", metadata)
    metric_id = "disclosure_compliance_score"
    evidence_id = "E-DISCLOSURE-SCORE"
    metric = MetricResult(
        metric_id=metric_id, name="披露合规评分", formula="通过项/检查项×100%",
        inputs=[], period=fact.get("period", ""), scope=fact.get("scope", ""),
        unit="分", value=None, display_value="未获取", status="insufficient_data",
        reason=message, evidence_ids=[evidence_id],
    ).to_dict()
    evidence = Evidence(
        evidence_id=evidence_id, source_type="disclosure_text_scan",
        source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
        page=fact.get("page", ""), locator=fact.get("locator", ""), excerpt=message,
        fact_ids=[fact["fact_id"]], metric_ids=[metric_id], verified=False,
        status="insufficient_data",
    ).to_dict()
    return json.dumps({
        "result_schema_version": RESULT_SCHEMA_VERSION, "rule_version": RULE_VERSION,
        "status": "insufficient_data", "compliance_score": 0, "risk_score": 100,
        "error": message, "checked_items": 0, "passed_items": 0,
        "issues": [message], "issue_count": 1, "facts": [fact],
        "metric_results": [metric], "evidence": [evidence],
    }, ensure_ascii=False, indent=2)


@tool
def check_disclosure_compliance(report_text: str, validation_json: str = "",
                                source_metadata_json: str = "") -> str:
    """筛查定期报告中的披露线索，输出文本筛查评分和待核查清单。

    关键词命中不证明披露完整，未命中不证明违规。需结合全文、附注、
    交叉引用公告以及报告类型、规则版本和事项重要性进行实质复核。

    Args:
        report_text: 年报提取的全文文本（支持 PDF/Word/HTML 提取后的纯文本）
        validation_json: validate_financial_data 工具结果（可选）。勾稽校验未通过项
            会联动扣减披露合规评分（打破"有重大财务错报但信披合规拿 100 分"）。
        source_metadata_json: 可选来源元数据 JSON，包含 source_document/source_hash/page/
            locator/period/scope；input_scope="full_document" 表示全文，"excerpt" 或
            "summary" 表示节选或摘要（兼容 text_scope；也可用 is_full_report=false）。
            节选仅返回扫描覆盖率，不提供合规或风险评分。

    Returns:
        JSON 字符串，包含：
        - compliance_score: 文本筛查参考分（0-100，不代表已确认合规；节选为 null）
        - risk_score: 筛查参考风险分（0-100，不代表违规概率；节选为 null）
        - checked_items: 总检查项数
        - passed_items: 通过项数
        - issues: 问题清单列表
        - sections_found: 已找到的章节
        - sections_missing: 缺失的章节
        - audit_opinion: 审计意见类型
    """
    metadata = _source_metadata(source_metadata_json)
    if not report_text or len(report_text.strip()) < 100:
        return _disclosure_failure(report_text or "", metadata, "输入文本不足100字符，无法执行检查")

    issues = []
    checked = 0
    passed = 0
    items_not_assessed = []
    observations = []

    # ── 1. 必要章节完整性检查 ──
    # 半年度报告不强制披露"公司治理"章节（依《半年报内容与格式准则》，
    # 与年报准则第2号不同），误判会导致合规分虚低与虚假风险条目（实测缺陷）
    is_half_year = any(term in str(metadata.get("period", "")) + report_text
                       for term in ("半年度", "半年报", "中期报告"))
    input_scope = str(metadata.get("input_scope") or metadata.get("text_scope") or
                      ("full_document" if metadata.get("is_full_report") is True else "unknown"))
    is_excerpt = (metadata.get("is_full_report") is False
                  or input_scope in ("excerpt", "summary", "节选", "摘要"))
    sections_found = []
    sections_missing = []
    for section_name, keywords in REQUIRED_SECTIONS:
        if is_half_year and section_name == "公司治理":
            items_not_assessed.append("公司治理：不按年度报告独立章节要求检查半年度报告")
            continue
        checked += 1
        found = any(kw in report_text for kw in keywords)
        if found:
            sections_found.append(section_name)
            passed += 1
        else:
            sections_missing.append(section_name)
            issues.append(f"待核查章节：所提供文本未识别到「{section_name}」，"
                          "需核对全文、章节别名及报告类型，不能据此认定披露缺失")

    # ── 2. 关键披露事项检查 ──
    for check_name, keywords in DISCLOSURE_CHECKS:
        context = _topic_context(report_text, keywords)
        if check_name == "股权质押披露" and not context and re.search(
                r"质押[^。]{0,20}(?:标记|冻结)[^。]{0,20}股份数量", re.sub(r"\s+", "", report_text)):
            checked += 1
            passed += 1
            observations.append("股权质押披露：已识别股东质押、标记或冻结股份数量的表格字段，"
                                "应结合表格核对数量；资产或收费权质押借款不归为股东股份质押")
            continue
        if not context:
            items_not_assessed.append(f"{check_name}：未命中事项关键词，适用性尚未确认")
            continue
        checked += 1
        has_exposure = bool(re.search(
            r"(?:担保金额|担保余额|涉案金额|质押数量|关联交易金额)[^。]{0,12}[1-9]\d*", context))
        if re.search(NO_MATTER_PATTERNS[check_name], context) and not has_exposure:
            passed += 1
            observations.append(f"{check_name}：已识别无相关事项的披露声明，仍需核对声明范围")
            continue
        detail_count = sum(any(term in context for term in terms)
                           for terms in DETAIL_TERMS[check_name])
        has_cross_reference = bool(re.search(r"(?:详见|参见|见本报告|见.*?附注)", context))
        if has_cross_reference:
            passed += 1
            issues.append(f"{check_name}待核查：存在交叉引用，需查阅所引附注或公告并确认"
                          "披露日期、事项进展和适用范围；不因正文未重复列示认定违规"
                          f"（核查参考：{_disclosure_reference(check_name, is_half_year)}）")
        elif detail_count >= 3:
            passed += 1
            observations.append(f"{check_name}：已识别部分明细字段，内容完整性与实质合规仍待复核")
        else:
            issues.append(f"{check_name}待核查：所提供文本未识别到足够明细字段；"
                          "需核对事项重要性、全文附注及既有公告，不能仅凭关键词认定违规"
                          f"（核查参考：{_disclosure_reference(check_name, is_half_year)}）")

    # ── 3. 会计政策变更专项检查 ──
    change_contexts = _policy_change_contexts(report_text)
    if change_contexts:
        checked += 1
        missing_details = []
        for context in change_contexts:
            mandated_change = bool(re.search(
                r"(?:财政部|国际会计准则理事会|会计准则)[^。]{0,180}(?:修订|解释|新准则)"
                r"[^。]{0,100}(?:生效|实施|执行|采用)", context))
            has_reason = mandated_change or bool(re.search(
                r"变更原因|变更理由|(?:由于|因为|根据)[^。]{0,100}(?:准则|会计|估计|政策|信息)", context))
            has_impact = any(term in context for term in ("影响", "追溯", "未来适用"))
            if not has_reason:
                missing_details.append("变更理由")
            elif not has_impact:
                missing_details.append("变更影响或适用方法")
        if missing_details:
            issues.append("会计政策变更待核查：相关附注未识别到" + "、".join(dict.fromkeys(missing_details))
                          + "，需结合完整附注及相关会计准则复核")
        else:
            passed += 1
            observations.append("会计政策变更：已识别变更依据和影响说明；"
                                "按各附注的会计准则及期间分别记录，不仅因采用新准则认定风险")
    else:
        items_not_assessed.append("会计政策变更：未识别到需检查的变更事项")

    # 当前中期未经审计的声明优先于对上年审计意见的引用。
    checked += 1
    audit_opinion = "未识别"
    is_non_standard = False
    compact_text = re.sub(r"\s+", "", report_text)
    current_unaudited = bool(re.search(
        r"(?:本(?:半年度|中期|报告期)[^。；]{0,60}未经审计|"
        r"(?:半年度|中期)报告[，,:：]*未经审计|未经审计(?:中期)?(?:合并|财务))",
        compact_text))
    if is_half_year and current_unaudited:
        audit_opinion = "未经审计（半年度报告）"
        observations.append("本期财务报告披露为未经审计；中期报告可不经审计，"
                            "但证监会和交易所另有规定的除外，不作为独立公司风险")
        if re.search(r"(?:上年|上年度|以前年度)[^。]{0,60}(?:非标准|(?<!无)保留|否定|无法表示)意见",
                     compact_text):
            issues.append("上年审计意见延续影响待核查：需核对相关事项本期的变化与处理情况，"
                          "不将上年意见类型记为本期意见")
    else:
        for opinion, pattern in (
            ("无法表示意见", r"无法表示意见|无法对.{0,30}发表意见"),
            ("否定意见", r"否定意见"),
            ("保留意见", r"(?<!无)保留意见"),
            ("带强调事项段的无保留意见", r"带强调事项段"),
            ("非标准审计意见", r"非标准审计意见"),
        ):
            if re.search(pattern, compact_text):
                audit_opinion = opinion
                is_non_standard = True
                break
        if audit_opinion == "未识别" and (
                "无保留意见" in compact_text
                or re.search(r"我们认为[^。]{0,100}(?<!未能)公允反映", compact_text)):
            audit_opinion = "标准无保留意见"
    if is_non_standard:
        issues.append(f"审计意见待核查：文本含「{audit_opinion}」，需核对意见所属期间、"
                      "审计对象及完整报告；不直接等同于信息披露违规")
        # 非标意见不直接扣分，但标记为风险项
        passed += 1
    else:
        passed += 1

    # ── 计算合规评分（clamp 到 [0,100]，防御通过项溢出导致 108 分/负数风险分）──
    if passed > checked:
        logger.warning(f"披露检查计数异常：通过项 {passed} > 检查项 {checked}，已按检查项封顶")
        passed = checked
    compliance_score = round(min(100.0, max(0.0, passed / max(checked, 1) * 100)), 1)
    risk_score = round(min(100.0, max(0.0, 100 - compliance_score)), 1)

    # 非标审计意见额外加风险分
    if is_non_standard:
        risk_score = min(100, risk_score + 15)
        compliance_score = max(0, 100 - risk_score)

    # P4: 勾稽差异联动扣披露分（打破"有重大财务错报但信披合规拿 100 分"）：
    # 每个未通过勾稽校验项扣 15 分（下限 0），并追加 issue 说明。
    try:
        vd = json.loads(validation_json) if isinstance(validation_json, str) and validation_json else {}
        _dv = (vd.get("data_validation") or {}) if isinstance(vd, dict) else {}
        _failed = int(_dv.get("failed_checks", 0) or 0)
    except (ValueError, TypeError):
        _failed = 0
    if _failed > 0:
        _penalty = min(compliance_score, _failed * 15)
        compliance_score = max(0, round(compliance_score - _penalty, 1))
        risk_score = min(100, round(risk_score + _penalty, 1))
        issues.append(f"数据勾稽差异影响披露质量：{_failed} 项勾稽校验未通过，信披合规评分已相应扣减")
        # P7: 实质数据勾稽校验计入检查项（评分按扣减前形式项计算，见上方顺序），
        # 使「检查项/通过项」计数与扣分原因在 UI 上透明自洽（实测缺陷：
        # 14/14 全绿通过却扣 15 分）
        checked += 1

    scan_score = compliance_score
    if is_excerpt:
        compliance_score = None
        risk_score = None
    limitations = [
        "本结果为文本筛查参考，关键词命中不证明披露完整，未命中不证明违规。",
        "须核对原始全文、附注、交叉引用公告、适用版本和事项重要性；未识别事项不等同于不存在。",
        "分值是内部筛查规则的结果，不是监管标准、违规概率或已确认风险数量。",
        "为合并范围内子公司提供担保不自动免除审议和披露义务，须核对具体规则。",
    ]
    if is_excerpt:
        limitations.append("输入已标记为节选或摘要，不能据此评定整份报告的披露合规分或风险分。")
    fact = _disclosure_anchor(report_text, metadata)
    score_metric_id = "disclosure_compliance_score"
    score_evidence_id = "E-DISCLOSURE-SCORE"
    score_metric = MetricResult(
        metric_id=score_metric_id, name="披露文本筛查参考分",
        formula="文本命中项/适用检查项×100%（再扣勾稽与非标意见筛查分）",
        inputs=[
            {"field": "checked_items", "value": checked, "unit": "项"},
            {"field": "passed_items", "value": passed, "unit": "项"},
            {"field": "reconciliation_checks", "value": _failed, "unit": "项"},
        ],
        period=fact.get("period", ""), scope=fact.get("scope", ""), unit="分",
        value=compliance_score, display_value="未评定" if is_excerpt else str(compliance_score),
        threshold_source="内部文本筛查规则；法规条文仅作为待复核提示的参考，不作为评分阈值",
        status="insufficient_data" if is_excerpt else "calculated",
        reason="输入为节选，无法评定全文合规" if is_excerpt else "确定性文本筛查参考分，不构成合规结论",
        evidence_ids=[score_evidence_id],
    ).to_dict()
    evidence = [Evidence(
        evidence_id=score_evidence_id, source_type="disclosure_text_scan",
        source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
        page=fact.get("page", ""), locator=fact.get("locator", ""),
        excerpt="；".join(issues)[:2000] if issues else "必要章节与关键披露事项扫描完成",
        fact_ids=[fact["fact_id"]], metric_ids=[score_metric_id], verified=not is_excerpt,
        status="insufficient_data" if is_excerpt else "calculated",
    ).to_dict()]
    issue_records = []
    for index, issue in enumerate(issues, 1):
        evidence_id = f"E-DISCLOSURE-ISSUE-{index:03d}"
        evidence.append(Evidence(
            evidence_id=evidence_id, source_type="disclosure_text_scan",
            source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
            page=fact.get("page", ""), locator=fact.get("locator", ""), excerpt=str(issue),
            fact_ids=[fact["fact_id"]], metric_ids=[score_metric_id], verified=False,
            status="needs_review",
        ).to_dict())
        issue_records.append({"issue_id": f"DISC-{index:03d}", "description": str(issue),
                              "evidence_id": evidence_id, "metric_id": score_metric_id,
                              "status": "needs_review", "formal_status": "candidate",
                              "pending_verification": True})

    result = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "status": "insufficient_data" if is_excerpt else "calculated",
        "compliance_score": compliance_score,
        "risk_score": risk_score,
        "text_scan_score": scan_score,
        "score_basis": "internal_text_screening",
        "compliance_conclusion": "待核查，尚未作出实质合规认定",
        "confirmed_issue_count": 0,
        "input_scope": "excerpt" if is_excerpt else input_scope,
        "checked_items": checked,
        "passed_items": passed,
        "reconciliation_checks": _failed,
        "issues": issues,
        "issue_count": len(issues),
        "sections_found": sections_found,
        "sections_missing": sections_missing,
        "items_not_assessed": items_not_assessed,
        "observations": observations,
        "limitations": limitations,
        "audit_opinion": audit_opinion,
        "is_non_standard_opinion": is_non_standard,
        "issue_records": issue_records,
        "facts": [fact],
        "metric_results": [score_metric],
        "evidence": evidence,
    }

    logger.info(f"披露规范性检查完成：合规评分 {compliance_score}，问题 {len(issues)} 项")
    return json.dumps(result, ensure_ascii=False, indent=2)
