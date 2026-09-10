"""信息披露规范性检查工具

对照《上市公司信息披露管理办法》和《年报内容与格式准则第2号》，
检查年报文本是否包含法定必要章节和关键披露事项。

检查维度：
1. 必要章节完整性（公司概况、会计数据、股东情况、董监高、财务报告等）
2. 关联交易披露
3. 会计政策变更说明
4. 重大事项披露（担保、诉讼、质押）
5. 审计意见类型

输出合规评分（0-100）和问题清单，供综合风险评分模块使用。
"""
import re
import json
import logging
from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 年报法定必要章节（依据《年报内容与格式准则第2号》）──
REQUIRED_SECTIONS = [
    ("公司基本情况", ["公司基本情况", "公司简介", "公司概况", "基本信息"], "准则第2号第8条"),
    ("主要会计数据和财务指标", ["主要会计数据", "财务指标", "主要财务数据"], "准则第2号第10条"),
    ("股东及实际控制人情况", ["股东", "实际控制人", "持股情况", "前十名股东"], "准则第2号第24条"),
    ("董事、监事、高级管理人员", ["董事", "监事", "高级管理人员", "董监高"], "准则第2号第30条"),
    ("公司治理", ["公司治理", "治理结构", "内部控制"], "准则第2号第36条"),
    ("财务报告", ["财务报告", "财务报表", "资产负债表", "利润表"], "准则第2号第45条"),
    ("董事会报告", ["董事会报告", "经营情况讨论", "管理层讨论"], "准则第2号第12条"),
]

# ── 关键披露事项检查（含法规条款引用）──
DISCLOSURE_CHECKS = [
    ("关联交易披露", ["关联交易", "关联方", "关联采购", "关联销售"], "存在关联交易但未发现详细披露说明", "信披管理办法第22条"),
    ("担保事项披露", ["担保", "对外担保", "连带担保"], "存在担保事项但未发现详细披露", "信披管理办法第25条"),
    ("诉讼仲裁披露", ["诉讼", "仲裁", "重大诉讼", "未决诉讼"], "存在诉讼/仲裁但未发现详细披露", "信披管理办法第26条"),
    ("股权质押披露", ["质押", "股权质押", "股份质押"], "存在股权质押但未发现详细披露", "信披管理办法第23条"),
    ("会计政策变更说明", ["会计政策变更", "会计估计变更", "政策变更"], "发现会计政策变更但未说明变更理由", "会计准则第28号第8条"),
]

# ── 非标审计意见关键词 ──
NON_STANDARD_OPINIONS = ["保留意见", "否定意见", "无法表示意见", "带强调事项段", "非标准审计意见"]

# 担保/质押豁免语境：对合并报表范围内子公司的担保/质押属常规内部安排，
# 不构成对外担保义务，按披露规则可豁免详细披露（避免对央企/集团误报，实测缺陷）
EXEMPT_CONTEXTS = ["对子公司的担保", "对控股子公司", "对全资子公司", "为子公司",
                   "合并范围内", "下属公司", "全资子公司", "控股子公司", "子公司提供担保",
                   "对下属企业", "集团内部"]


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
    """检查年报文本的信息披露规范性，输出合规评分和问题清单。

    对照《上市公司信息披露管理办法》和《年报内容与格式准则第2号》，
    检查年报是否包含法定必要章节、关键披露事项是否完整。

    Args:
        report_text: 年报提取的全文文本（支持 PDF/Word/HTML 提取后的纯文本）
        validation_json: validate_financial_data 工具结果（可选）。勾稽校验未通过项
            会联动扣减披露合规评分（打破"有重大财务错报但信披合规拿 100 分"）。
        source_metadata_json: 可选来源元数据 JSON，包含 source_document/source_hash/page/
            locator/period/scope；缺失时保持空值，不猜测原文定位。

    Returns:
        JSON 字符串，包含：
        - compliance_score: 合规评分（0-100，越高越合规）
        - risk_score: 披露风险分（0-100，越高越有风险，= 100 - compliance_score）
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

    # ── 1. 必要章节完整性检查 ──
    # 半年度报告不强制披露"公司治理"章节（依《半年报内容与格式准则》，
    # 与年报准则第2号不同），误判会导致合规分虚低与虚假风险条目（实测缺陷）
    is_half_year = "半年度" in report_text or "半年报" in report_text
    sections_found = []
    sections_missing = []
    for section_name, keywords, article_ref in REQUIRED_SECTIONS:
        if is_half_year and section_name == "公司治理":
            # 豁免项计为自动通过：checked 与 passed 同步 +1，保持计数平衡，
            # 否则 passed > checked 导致评分超 100 与负数风险分（实测缺陷：108/-8）
            checked += 1
            passed += 1
            continue
        checked += 1
        found = any(kw in report_text for kw in keywords)
        if found:
            sections_found.append(section_name)
            passed += 1
        else:
            sections_missing.append(section_name)
            issues.append(f"缺失必要章节：{section_name}（依据：{article_ref}）")

    # ── 2. 关键披露事项检查 ──
    for check_name, keywords, issue_msg, article_ref in DISCLOSURE_CHECKS:
        checked += 1
        # 检查是否提及相关事项
        mentioned = any(kw in report_text for kw in keywords)
        if mentioned:
            # 提到了关键词，检查是否有详细说明（至少出现2个不同关键词）
            detail_count = sum(1 for kw in keywords if kw in report_text)
            if detail_count >= 2:
                passed += 1  # 有详细披露
            else:
                # 担保/质押豁免：命中豁免语境（并表范围内子公司常规操作）不视为问题
                if check_name in ("担保事项披露", "股权质押披露") and any(
                        ek in report_text for ek in EXEMPT_CONTEXTS):
                    passed += 1  # 并表范围内常规担保/质押，豁免详细披露
                else:
                    issues.append(f"{check_name}：{issue_msg}（依据：{article_ref}）")
        else:
            # 未提及，可能是无此事项（不扣分）或遗漏披露（轻微扣分）
            passed += 1  # 未涉及则默认通过

    # ── 3. 会计政策变更专项检查 ──
    checked += 1
    policy_change_keywords = ["会计政策变更", "会计估计变更"]
    has_change = any(kw in report_text for kw in policy_change_keywords)
    if has_change:
        # 有变更，检查是否说明了理由
        reason_keywords = ["变更原因", "变更理由", "变更说明", "由于", "因为", "根据"]
        has_reason = any(kw in report_text for kw in reason_keywords)
        if has_reason:
            passed += 1
        else:
            issues.append("会计政策变更未说明变更理由，不符合披露要求")
    else:
        passed += 1  # 无变更则通过

    # ── 4. 审计意见类型检查 ──
    # 修复：不再默认「标准无保留意见」——无审计报告特征的文本（如半年报/正文未含
    # 审计报告段）应输出「未识别」，与 identify_audit_opinion 工具口径一致，
    # 避免合规报告内部「标准无保留 vs 未识别」自相矛盾（实测缺陷）。
    checked += 1
    audit_opinion = "未识别"
    is_non_standard = False
    for opinion_kw in NON_STANDARD_OPINIONS:
        if opinion_kw in report_text:
            audit_opinion = opinion_kw
            is_non_standard = True
            break
    if audit_opinion == "未识别":
        # P6 根治：半年报/中期报告无审计报告特征时，明确判定"未经审计（半年度报告）"，
        # 不依赖 identify_audit_opinion 是否被调用，与前端"未经审计"展示一致，
        # 消除"后端未识别、前端未经审计"的状态矛盾。
        if "半年度报告" in report_text or "中期报告" in report_text:
            audit_opinion = "未经审计（半年度报告）"
        # 仅当文本包含审计报告特征时才判定为无保留意见
        elif any(kw in report_text for kw in ("无保留意见", "公允反映", "审计报告", "审计意见")):
            audit_opinion = "标准无保留意见"
    if is_non_standard:
        issues.append(f"审计意见类型为「{audit_opinion}」，属于非标准审计意见，需重点关注")
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

    fact = _disclosure_anchor(report_text, metadata)
    score_metric_id = "disclosure_compliance_score"
    score_evidence_id = "E-DISCLOSURE-SCORE"
    score_metric = MetricResult(
        metric_id=score_metric_id, name="披露合规评分", formula="通过项/检查项×100%（再扣勾稽与非标意见风险分）",
        inputs=[
            {"field": "checked_items", "value": checked, "unit": "项"},
            {"field": "passed_items", "value": passed, "unit": "项"},
            {"field": "reconciliation_checks", "value": _failed, "unit": "项"},
        ],
        period=fact.get("period", ""), scope=fact.get("scope", ""), unit="分",
        value=compliance_score, display_value=str(compliance_score),
        threshold_source="准则第2号及信披管理办法；具体条款见 issue_records",
        status="calculated", reason="确定性文本扫描结果", evidence_ids=[score_evidence_id],
    ).to_dict()
    evidence = [Evidence(
        evidence_id=score_evidence_id, source_type="disclosure_text_scan",
        source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
        page=fact.get("page", ""), locator=fact.get("locator", ""),
        excerpt="；".join(issues)[:2000] if issues else "必要章节与关键披露事项扫描完成",
        fact_ids=[fact["fact_id"]], metric_ids=[score_metric_id], verified=True,
        status="verified",
    ).to_dict()]
    issue_records = []
    for index, issue in enumerate(issues, 1):
        evidence_id = f"E-DISCLOSURE-ISSUE-{index:03d}"
        evidence.append(Evidence(
            evidence_id=evidence_id, source_type="disclosure_text_scan",
            source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
            page=fact.get("page", ""), locator=fact.get("locator", ""), excerpt=str(issue),
            fact_ids=[fact["fact_id"]], metric_ids=[score_metric_id], verified=True,
            status="verified",
        ).to_dict())
        issue_records.append({"issue_id": f"DISC-{index:03d}", "description": str(issue),
                              "evidence_id": evidence_id, "metric_id": score_metric_id,
                              "status": "identified"})

    result = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "status": "calculated",
        "compliance_score": compliance_score,
        "risk_score": risk_score,
        "checked_items": checked,
        "passed_items": passed,
        "reconciliation_checks": _failed,
        "issues": issues,
        "issue_count": len(issues),
        "sections_found": sections_found,
        "sections_missing": sections_missing,
        "audit_opinion": audit_opinion,
        "is_non_standard_opinion": is_non_standard,
        "issue_records": issue_records,
        "facts": [fact],
        "metric_results": [score_metric],
        "evidence": evidence,
    }

    logger.info(f"披露规范性检查完成：合规评分 {compliance_score}，问题 {len(issues)} 项")
    return json.dumps(result, ensure_ascii=False, indent=2)
