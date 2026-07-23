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


@tool
def check_disclosure_compliance(report_text: str) -> str:
    """检查年报文本的信息披露规范性，输出合规评分和问题清单。

    对照《上市公司信息披露管理办法》和《年报内容与格式准则第2号》，
    检查年报是否包含法定必要章节、关键披露事项是否完整。

    Args:
        report_text: 年报提取的全文文本（支持 PDF/Word/HTML 提取后的纯文本）

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
    if not report_text or len(report_text.strip()) < 100:
        return json.dumps({
            "compliance_score": 0,
            "risk_score": 100,
            "error": "文本内容过短，无法进行披露规范性检查",
            "checked_items": 0,
            "passed_items": 0,
            "issues": ["输入文本不足100字符，无法执行检查"],
        }, ensure_ascii=False)

    issues = []
    checked = 0
    passed = 0

    # ── 1. 必要章节完整性检查 ──
    sections_found = []
    sections_missing = []
    for section_name, keywords, article_ref in REQUIRED_SECTIONS:
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
    checked += 1
    audit_opinion = "标准无保留意见"
    is_non_standard = False
    for opinion_kw in NON_STANDARD_OPINIONS:
        if opinion_kw in report_text:
            audit_opinion = opinion_kw
            is_non_standard = True
            break
    if is_non_standard:
        issues.append(f"审计意见类型为「{audit_opinion}」，属于非标准审计意见，需重点关注")
        # 非标意见不直接扣分，但标记为风险项
        passed += 1
    else:
        passed += 1

    # ── 计算合规评分 ──
    compliance_score = round((passed / max(checked, 1)) * 100, 1)
    risk_score = round(100 - compliance_score, 1)

    # 非标审计意见额外加风险分
    if is_non_standard:
        risk_score = min(100, risk_score + 15)
        compliance_score = max(0, 100 - risk_score)

    result = {
        "compliance_score": compliance_score,
        "risk_score": risk_score,
        "checked_items": checked,
        "passed_items": passed,
        "issues": issues,
        "issue_count": len(issues),
        "sections_found": sections_found,
        "sections_missing": sections_missing,
        "audit_opinion": audit_opinion,
        "is_non_standard_opinion": is_non_standard,
    }

    logger.info(f"披露规范性检查完成：合规评分 {compliance_score}，问题 {len(issues)} 项")
    return json.dumps(result, ensure_ascii=False, indent=2)
