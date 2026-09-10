"""审计意见类型识别工具

从年报/审计报告文本中识别注册会计师出具的审计意见类型，并映射其对年报数据
可信度的影响程度，为后续风险研判提供"数据可信度权重"依据。

识别的五种意见类型（依据审计准则第1501/1502/1503号）：
无保留意见、带强调事项段的无保留意见、保留意见、否定意见、无法表示意见。

设计要点：
1. 采用「特征短语 + 优先级」匹配，而非单一关键词，降低误判。
2. 优先级从严到宽：无法表示 > 否定 > 保留 > 带强调事项段 > 无保留。因为严重
   意见的报告中通常也包含"我们认为"等标准表述，若按宽松优先会误判为无保留。
3. 额外单独识别「持续经营重大不确定性」与「关键审计事项」，二者不受意见类型
   影响，均为独立风险信号。
4. 判定规则与 knowledge_base/审计意见类型库.txt 一致，便于报告引用溯源。
"""
import json
import logging
import re

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 意见类型定义表：按严重程度从高到低排列，匹配时先严后宽（顺序即优先级）
_OPINION_RULES = [
    {
        "type": "无法表示意见",
        "patterns": [r"无法表示意见", r"无法对上述财务报表发表意见", r"不发表意见"],
        "credibility_impact": "极高",
        "risk_level": "重大",
        "is_standard": False,
        "meaning": "注册会计师无法获取充分适当的审计证据，且影响重大且具有广泛性。",
        "implication": "数据完整性与真实性均无法验证，整份财务报表不可作为可信分析基础。",
    },
    {
        "type": "否定意见",
        "patterns": [r"否定意见", r"未能公允反映", r"未按照.{0,20}公允反映"],
        "credibility_impact": "极高",
        "risk_level": "重大",
        "is_standard": False,
        "meaning": "财务报表整体存在重大且具有广泛性的错报，未能公允反映。",
        "implication": "整份财务报表不可作为可信分析基础。",
    },
    {
        "type": "保留意见",
        "patterns": [r"保留意见的基础", r"形成保留意见的基础", r"除上述事项.{0,10}影响外"],
        "credibility_impact": "高",
        "risk_level": "重要",
        "is_standard": False,
        "meaning": "财务报表整体公允，但存在影响重大而不具广泛性的具体事项。",
        "implication": "被保留的具体项目数据不可直接采信，其他部分可参考。",
    },
    {
        "type": "带强调事项段的无保留意见",
        "patterns": [r"强调事项段", r"强调事项", r"其他事项段",
                     r"提醒财务报表使用者.{0,10}关注", r"在不影响.{0,10}审计意见"],
        "credibility_impact": "中等偏低",
        "risk_level": "一般",
        "is_standard": False,
        "meaning": "意见本身为无保留，但注册会计师提醒关注特定重大事项。",
        "implication": "报表数据本身未被质疑，但被强调的事项通常指向特定重大风险领域。"
                       "仅此项不构成直接判定高风险的充分条件，须结合强调事项内容判断。",
    },
    {
        "type": "无保留意见",
        "patterns": [r"在所有重大方面.{0,30}公允反映", r"公允反映了.{0,30}财务状况",
                     r"标准无保留意见", r"无保留意见"],
        "credibility_impact": "无",
        "risk_level": "一般",
        "is_standard": True,
        "meaning": "财务报表在所有重大方面按照会计准则编制并公允反映。",
        "implication": "年报数据可作为分析的基础可信来源；但审计意见标准不等于无风险，"
                       "仍须以指标交叉验证为准。",
    },
]

# 独立风险信号：与意见类型无关，命中即单独提示
_GOING_CONCERN_PATTERNS = [
    r"与持续经营相关的重大不确定性", r"持续经营.{0,10}重大不确定性",
    r"持续经营能力存在.{0,10}不确定性",
]
# 关键审计事项及其指向的风险方向（用于提示高风险领域）
_KAM_TOPICS = {
    "收入确认": "收入真实性、跨期确认、虚增收入嫌疑",
    "应收账款": "客户信用风险、坏账计提充分性",
    "存货": "存货积压、跌价计提不足",
    "商誉": "并购标的业绩不达预期、商誉减值风险",
    "减值": "资产质量与减值计提充分性",
    "公允价值": "估值参数主观性、利润调节空间",
    "关联交易": "利润操纵嫌疑",
    "长期股权投资": "投资收益质量与估值主观性",
}
# 会计师事务所变更（"意见购买"嫌疑的联动信号）
_AUDITOR_CHANGE_PATTERNS = [r"变更会计师事务所", r"改聘.{0,10}会计师事务所",
                            r"更换.{0,6}会计师事务所", r"续聘.{0,10}变更"]


def _source_metadata(source_metadata_json: str) -> dict:
    if isinstance(source_metadata_json, dict):
        return source_metadata_json
    try:
        value = json.loads(source_metadata_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _structured_output(text: str, opinion: dict, going_concern: dict,
                       kam: dict, auditor_change: dict, linkages: list,
                       metadata: dict) -> dict:
    """把识别信号包装成可供报告和导出的事实、指标、证据记录。"""
    fact = make_fact(
        "audit_report_text", text, fact_id="F-AUDIT-REPORT",
        period=str(metadata.get("period", "") or ""), scope=str(metadata.get("scope", "") or ""),
        source_document=str(metadata.get("source_document", "") or ""),
        source_hash=str(metadata.get("source_hash", "") or ""),
        page=str(metadata.get("page", metadata.get("page_number", "")) or ""),
        locator=str(metadata.get("locator", metadata.get("table_locator", "")) or ""),
        excerpt=text[:1000],
        extraction_method=str(metadata.get("extraction_method", "opinion_pattern_scan") or "opinion_pattern_scan"),
    ).to_dict()
    evidence = []
    opinion_evidence_id = "E-AUDIT-OPINION"
    opinion_excerpt = str(opinion.get("evidence_excerpt", "") or opinion.get("note", ""))
    evidence.append(Evidence(
        evidence_id=opinion_evidence_id, source_type="audit_opinion_text_scan",
        source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
        page=fact.get("page", ""), locator=fact.get("locator", ""), excerpt=opinion_excerpt,
        fact_ids=[fact["fact_id"]], metric_ids=["audit_opinion_identification"],
        verified=bool(opinion.get("identified")),
        status="verified" if opinion.get("identified") else "insufficient_data",
    ).to_dict())
    opinion["evidence_ids"] = [opinion_evidence_id]
    metric = MetricResult(
        metric_id="audit_opinion_identification", name="审计意见类型识别",
        formula="审计意见特征短语优先级匹配", inputs=[{
            "field": "audit_report_text", "fact_id": fact["fact_id"],
            "raw_value": "[见事实记录]", "value": opinion.get("opinion_type"),
            "unit": "", "period": fact.get("period", ""), "scope": fact.get("scope", ""),
        }], period=fact.get("period", ""), scope=fact.get("scope", ""),
        unit="类别", value=opinion.get("opinion_type"),
        display_value=str(opinion.get("opinion_type", "未识别")),
        threshold_source="knowledge_base/审计意见类型库.txt",
        status="calculated" if opinion.get("identified") else "insufficient_data",
        reason=str(opinion.get("note", "") or ""), evidence_ids=[opinion_evidence_id],
    ).to_dict()
    if going_concern.get("flagged"):
        gc_id = "E-AUDIT-GOING-CONCERN"
        going_concern["evidence_ids"] = [gc_id]
        evidence.append(Evidence(
            evidence_id=gc_id, source_type="audit_opinion_text_scan",
            source_document=fact.get("source_document", ""), source_hash=fact.get("source_hash", ""),
            page=fact.get("page", ""), locator=fact.get("locator", ""),
            excerpt=str(going_concern.get("signal", "")), fact_ids=[fact["fact_id"]],
            metric_ids=["audit_opinion_identification"], verified=True, status="verified",
        ).to_dict())
    risk_findings = []
    if opinion.get("identified") and opinion.get("is_standard_opinion") is False:
        risk_findings.append({
            "risk_id": "AO-001", "dimension": "financial_misstatement",
            "title": f"审计意见为{opinion.get('opinion_type', '非标准意见')}",
            "level": opinion.get("risk_level", "待定级"), "status": "candidate",
            "source": "audit_opinion_text_scan", "evidence_ids": [opinion_evidence_id],
            "metric_ids": [metric["metric_id"]],
            "reason": "审计意见性质须按原文陈述，不能据此直接推断舞弊或违法。",
        })
    if going_concern.get("flagged"):
        risk_findings.append({
            "risk_id": "AO-GC-001", "dimension": "going_concern", "title": "审计报告提示持续经营重大不确定性",
            "level": "重大", "status": "candidate", "source": "audit_opinion_text_scan",
            "evidence_ids": going_concern.get("evidence_ids", []), "metric_ids": [metric["metric_id"]],
            "reason": "持续经营信号独立于审计意见类型，须结合原文和财务事实核查。",
        })
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "status": "calculated" if opinion.get("identified") or going_concern.get("flagged") else "insufficient_data",
        "facts": [fact], "metric_results": [metric], "evidence": evidence,
        "risk_findings": risk_findings,
    }


def _match_opinion(text: str) -> dict:
    """按优先级匹配审计意见类型；未命中任何特征时返回未识别。"""
    for rule in _OPINION_RULES:
        for pat in rule["patterns"]:
            m = re.search(pat, text)
            if m:
                # 截取命中位置上下文，便于报告中溯源引用
                start = max(0, m.start() - 60)
                return {
                    "identified": True,
                    "opinion_type": rule["type"],
                    "is_standard_opinion": rule["is_standard"],
                    "opinion_nature": "标准" if rule["is_standard"] else "非标准",
                    "credibility_impact": rule["credibility_impact"],
                    "risk_level": rule["risk_level"],
                    "meaning": rule["meaning"],
                    "implication": rule["implication"],
                    "matched_pattern": pat,
                    "evidence_excerpt": text[start:m.end() + 60].strip(),
                }
    return {
        "identified": False,
        "opinion_type": "未识别",
        # P 补丁：未识别映射为"未获取"（is_standard_opinion=None，不抬升、不渲染"非标准"）
        "opinion_nature": "未获取",
        "is_standard_opinion": None,
        "note": "文本中未找到审计意见特征表述，可能是年报正文未包含审计报告部分。"
                "建议补充审计报告全文后重新识别，不得据此推断意见类型。",
    }


@tool
def identify_audit_opinion(report_text: str, source_metadata_json: str = "") -> str:
    """识别年报中的审计意见类型，并评估其对年报数据可信度的影响程度。

    识别五类审计意见（无保留/带强调事项段的无保留/保留/否定/无法表示），
    同时单独提示持续经营重大不确定性、关键审计事项与会计师事务所变更等
    独立风险信号。

    Args:
        report_text: 年报或审计报告的文本内容（应包含审计报告章节）

        source_metadata_json: 可选来源元数据 JSON，缺失时不猜测页码、文件哈希或定位。

    Returns:
        JSON 字符串，含 audit_opinion 部分：意见类型、是否标准意见、可信度影响
        程度、风险等级、证据摘录，以及 going_concern（持续经营）、
        key_audit_matters（关键审计事项）、auditor_change（事务所变更）等信号。
    """
    text = str(report_text or "")
    metadata = _source_metadata(source_metadata_json)
    # 阈值取 8 字：仅拦空值与无意义碎片。真实意见特征可能很短（如“形成否定意见的基础”
    # 仅 9 字），阈值过高会误拒合法短片段；未命中特征时自然会返回“未识别”，无需预先拦。
    if len(text.strip()) < 8:
        # 短文本早退也必须返回完整 schema，否则下游取 going_concern 等键会 KeyError
        opinion = {"identified": False, "opinion_type": "未识别",
                   "opinion_nature": "未获取", "is_standard_opinion": None,
                   "note": "输入文本过短，无法识别审计意见"}
        going_concern = {"flagged": False}
        kam = {"found": False, "matters": []}
        auditor_change = {"flagged": False}
        output = _structured_output(text, opinion, going_concern, kam, auditor_change, [], metadata)
        output.update({"audit_opinion": opinion, "going_concern": going_concern,
                       "key_audit_matters": kam, "auditor_change": auditor_change,
                       "linkage_alerts": []})
        return json.dumps(output, ensure_ascii=False, indent=2)

    opinion = _match_opinion(text)

    # 半年报/中期报告属性识别：无审计报告特征时，若文本为半年报/中期报告，
    # 明确输出"未经审计（半年度报告）"，与前端展示一致（实测缺陷：后端"未识别"
    # 而前端硬编码"未经审计"，状态矛盾）
    if not opinion.get("identified"):
        if re.search(r"半年度报告|中期报告", text):
            opinion = {
                "identified": True,
                "opinion_type": "未经审计（半年度报告）",
                # P 补丁：未经审计不是一种审计意见，意见性质标"不适用"（防误触非标抬升）
                "opinion_nature": "不适用",
                "is_standard_opinion": None,
                "meaning": "半年度/中期报告通常不强制审计，本报告未包含审计报告段落。",
                "implication": "财务数据未经注册会计师审计，可信度需结合其他来源判断。",
                "note": "半年报属性识别：文本无审计报告特征，按半年报惯例判定为未经审计。",
            }

    # 持续经营重大不确定性（独立信号，与意见类型无关）
    going_concern = None
    for pat in _GOING_CONCERN_PATTERNS:
        if re.search(pat, text):
            going_concern = {
                "flagged": True,
                "signal": "审计报告提示与持续经营相关的重大不确定性",
                "risk_level": "重大",
                "implication": "该信号独立于审计意见类型，须直接上调持续经营维度风险等级。",
            }
            break
    if going_concern is None:
        going_concern = {"flagged": False}

    # 关键审计事项：命中主题即提示其指向的风险方向
    kam_hits = []
    if re.search(r"关键审计事项", text):
        for topic, direction in _KAM_TOPICS.items():
            if re.search(topic, text):
                kam_hits.append({"matter": topic, "risk_direction": direction})

    # 会计师事务所变更
    auditor_change = any(re.search(p, text) for p in _AUDITOR_CHANGE_PATTERNS)

    # 联动判断：非标意见 + 事务所变更 → "意见购买"嫌疑
    linkages = []
    if not opinion.get("is_standard_opinion", True) and opinion.get("identified") and auditor_change:
        linkages.append("非标准审计意见与会计师事务所变更同时出现，需关注是否存在"
                        "「意见购买」嫌疑，建议核查变更原因与前任事务所意见。")
    if going_concern["flagged"] and opinion.get("opinion_type") == "带强调事项段的无保留意见":
        linkages.append("强调事项为持续经营重大不确定性，按裁定规则应上调至重大风险关注，"
                        "不适用「带强调事项段不直接判高风险」的宽免。")
    if kam_hits and not opinion.get("is_standard_opinion", True):
        linkages.append("非标准意见叠加关键审计事项，所涉领域已被注册会计师识别为高风险，"
                        "应作为重点核查方向。")

    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "status": "calculated",
        "audit_opinion": opinion,
        "going_concern": going_concern,
        "key_audit_matters": {
            "found": bool(kam_hits),
            "matters": kam_hits,
            "note": "关键审计事项通常揭示被审计单位最高风险的领域，应优先核查。",
        },
        "auditor_change": {"flagged": auditor_change},
        "linkage_alerts": linkages,
        "reference": "判定规则依据 knowledge_base/审计意见类型库.txt；"
                     "引用时须客观陈述意见类型与依据，不得据此直接推断财务造假。",
    }
    structured = _structured_output(text, opinion, going_concern,
                                    output["key_audit_matters"],
                                    output["auditor_change"], linkages, metadata)
    output.update(structured)
    return json.dumps(output, ensure_ascii=False, indent=2)
