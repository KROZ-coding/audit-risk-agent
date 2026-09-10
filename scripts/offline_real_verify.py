"""离线真实数据验证：中国石油 2025 半年报完整工具链打样。

流程（全部真实工具，无 LLM）：
1. 读取已解析的半年报全文（.tmp_cnpc.txt，由 parse_pdf_report 生成）
2. 手工摘录半年报关键财务数据（百万元口径，来自公开半年报正文）
3. 依次跑 validate → calculate → check_disclosure → identify_audit_opinion
   → compare_multi_year → calculate_risk_models → calculate_comprehensive_score
4. 台账构造器：仅基于工具输出（预警/问题/校验失败/意见信号）生成风险条目，
   每条注明数据来源（可追溯，不编造）
5. _export_pdf_impl 生成三份 PDF 到 local_storage/reports/

用法: uv run python scripts/offline_real_verify.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators
from tools.disclosure_checker import check_disclosure_compliance
from tools.audit_opinion import identify_audit_opinion
from tools.multi_year_comparison import compare_multi_year
from tools.risk_models import calculate_risk_models
from tools.risk_scorer import calculate_comprehensive_score
from tools.pdf_export import _export_pdf_impl

# ── 中国石油 2025 半年报关键数据（单位：人民币元，摘自半年报正文，百万元×1e6）──
# 单位约定：与预处理提取提示词/展示层 _fmt_num 一致，金额统一为元口径
CALC_JSON = {
    "revenue_current": 1450099000000, "revenue_previous": 1554973000000,
    "net_profit_current": 84007000000, "net_profit_previous": 88806000000,
    "operating_cashflow_current": 227063000000, "operating_cashflow_previous": 218419000000,
    "total_assets_current": 2849390000000, "total_assets_previous": 2752751000000,
    "total_liabilities_current": 1096474000000, "total_liabilities_previous": 1043128000000,
    "accounts_receivable_current": 119715000000, "accounts_receivable_previous": 71610000000,
    "inventory_current": 155724000000, "inventory_previous": 168338000000,
    "current_assets_current": 710678000000, "current_assets_previous": 590844000000,
    "current_liabilities_current": 684270000000, "current_liabilities_previous": 637317000000,
    "cash_and_equivalents_current": 284493000000, "cash_and_equivalents_previous": 216246000000,
    "goodwill_current": 7424000000, "goodwill_previous": 7436000000,
    "net_assets_current": 1752916000000, "short_term_debt_current": 46409000000,
}

VD_JSON = {
    "total_assets": 2849390000000, "total_liabilities": 1096474000000, "net_assets": 1752916000000,
    "net_profit": 84007000000, "operating_cashflow": 227063000000,
    "depreciation": 0, "amortization": 0, "working_capital_change": 0,
    "retained_earnings_begin": 0, "retained_earnings_end": 0, "dividends": 0,
}

MY_INPUT = {
    "years": [
        {"year": "2024H1", "revenue": 1554973000000, "net_profit": 88806000000,
         "operating_cashflow": 218419000000, "total_assets": 2752751000000,
         "total_liabilities": 1043128000000, "accounts_receivable": 71610000000,
         "inventory": 168338000000, "cost_of_goods": 1228848000000,
         "current_assets": 590844000000,
         "current_liabilities": 637317000000, "goodwill": 7436000000},
        {"year": "2025H1", "revenue": 1450099000000, "net_profit": 84007000000,
         "operating_cashflow": 227063000000, "total_assets": 2849390000000,
         "total_liabilities": 1096474000000, "accounts_receivable": 119715000000,
         "inventory": 155724000000, "cost_of_goods": 1147144000000,
         "current_assets": 710678000000,
         "current_liabilities": 684270000000, "goodwill": 7424000000},
    ]
}


def _level_of(text: str, base="一般"):
    """按信号强度映射风险等级（工具输出关键词驱动，不编造）。

    修复：仅「连续」上升/下滑不直接判重大（健康公司也可能连续微降），
    重大仅保留强信号关键词；「连续/显著/远超/跌破/持续」归重要。
    """
    if any(k in text for k in ("重大", "严重", "资不抵债", "无法表示")):
        return "重大"
    if any(k in text for k in ("连续", "显著", "远超", "跌破", "大幅", "持续")):
        return "重要"
    return base


def _dim_of(text: str):
    if any(k in text for k in ("持续经营", "偿债", "流动性", "亏损")):
        return "持续经营"
    if any(k in text for k in ("披露", "延迟", "变更", "意见")):
        return "信披合规"
    if any(k in text for k in ("关联")):
        return "关联交易"
    return "财务错报"


def build_ledger(fin, dc, vd, ao, my) -> dict:
    """台账构造器：仅基于工具输出生成风险条目（来源可追溯）。"""
    details = []

    def add(rid, dim, title, level, evidence, basis, suggestion, conf, chain_steps):
        details.append({
            "risk_id": rid, "dimension": dim, "title": title, "level": level,
            "confidence": conf, "verification_status": "高度关注" if conf >= 0.7
            else ("建议关注" if conf >= 0.5 else "待进一步核实"),
            "evidence": evidence, "data_analysis": "（离线验证模式）该条目由工具输出自动构造，"
                       "详细数据分析建议以人工复核为准。",
            "regulatory_basis": basis, "case_reference": "（离线验证模式）未进行案例库对照。",
            "audit_suggestion": suggestion,
            "reasoning_chain": [{"step": s, "detail": d} for s, d in chain_steps],
        })

    # 1) 财务指标工具预警
    for i, alert in enumerate(fin.get("alerts", []), 1):
        dim = _dim_of(str(alert))
        lv = _level_of(str(alert))
        add(f"R{i:03d}", dim, f"财务指标预警：{str(alert)[:40]}", lv,
            f"（数据来源：calculate_financial_indicators 预警输出）{alert}",
            "《企业会计准则》相关列报与计量要求；阈值判定依据行业基准库。",
            "结合半年报附注与现金流量表复核该指标对应的业务实质。",
            0.75 if lv == "重大" else (0.6 if lv == "重要" else 0.5),
            [("数据发现", f"指标计算工具输出预警：{alert}"),
             ("风险判定", f"按关键词映射风险等级：{lv}（离线模板）")])

    # 2) 披露检查问题
    for j, issue in enumerate(dc.get("issues", []), len(details) + 1):
        add(f"R{j:03d}", "信披合规", f"披露问题：{str(issue)[:40]}",
            _level_of(str(issue), "重要"),
            f"（数据来源：check_disclosure_compliance 问题清单）{issue}",
            "《上市公司信息披露管理办法》（2025修订）；《证券法》第七十八条。",
            "核查披露时点与披露内容完整性，评估对年报可信度的影响。",
            0.6,
            [("数据发现", f"披露检查工具输出问题：{issue}"),
             ("风险判定", "按披露违规性质映射风险等级（离线模板）")])

    # 3) 数据校验未通过项（validator 自带 risks 字段）
    vd_risks = ((vd.get("data_validation") or {}).get("risks") or [])
    for k, vr in enumerate(vd_risks, len(details) + 1):
        if not isinstance(vr, dict):
            continue
        add(f"R{k:03d}", "财务错报", f"数据可靠性：{str(vr.get('message', ''))[:40]}",
            "一般", f"（数据来源：validate_financial_data 勾稽校验未通过项）{vr.get('message', '')}",
            "三大勾稽校验规则（资产负债表平衡/现金流勾稽/利润分配一致性）。",
            "核对原始报表附注数据，修正后重新校验。",
            0.55,
            [("数据发现", f"勾稽校验未通过：{vr.get('message', '')}"),
             ("风险判定", "数据可靠性风险（离线模板）")])

    # 4) 审计意见信号（非标/持续经营/关键审计事项）
    op = (ao.get("audit_opinion") or {})
    gc = (ao.get("going_concern") or {})
    if not op.get("is_standard_opinion", True) and op.get("identified"):
        add(f"R{len(details) + 1:03d}", "信披合规",
            f"审计意见为非标准（{op.get('opinion_type', '')}），年报可信度受影响",
            _level_of(str(op.get("risk_level", "")), "重要"),
            f"（数据来源：identify_audit_opinion 意见识别）{op.get('meaning', '')}",
            "《中国注册会计师审计准则》意见类型判定；审计意见类型库。",
            "重点核查保留/强调事项对应的报表项目，评估数据可信度影响。",
            0.7,
            [("数据发现", f"识别到非标准审计意见：{op.get('opinion_type', '')}"),
             ("风险判定", "非标意见直接影响年报可信度（离线模板）")])
    if gc.get("flagged"):
        add(f"R{len(details) + 1:03d}", "持续经营",
            "审计报告提示持续经营重大不确定性",
            "重要",
            f"（数据来源：identify_audit_opinion 持续经营信号）{gc.get('signal', '')}",
            "《审计准则1324号——持续经营》第七条。",
            "获取管理层持续经营评估报告，复核未来12个月现金流预测。",
            0.65,
            [("数据发现", f"持续经营重大不确定性信号：{gc.get('signal', '')}"),
             ("风险判定", "信号联动规则要求持续经营维度等级不低于重要（离线模板）")])

    # 5) 多年对比趋势预警
    for m, ta in enumerate(my.get("trend_alerts", []), len(details) + 1):
        add(f"R{m:03d}", _dim_of(str(ta)), f"趋势预警：{str(ta)[:40]}",
            _level_of(str(ta)), f"（数据来源：compare_multi_year 趋势预警）{ta}",
            "多年趋势判定规则（连续下滑/恶化模式识别）。",
            "结合行业景气度与公司经营计划复核趋势可持续性。",
            0.55,
            [("数据发现", f"多年对比工具输出趋势预警：{ta}"),
             ("风险判定", "按趋势恶化模式映射风险等级（离线模板）")])

    # 风险统计（以明细为唯一事实源）
    counts = {"重大": 0, "重要": 0, "一般": 0}
    dims = {}
    for r in details:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
        dims[r["dimension"]] = dims.get(r["dimension"], 0) + 1
    return {
        "company_info": {
            "company_name": "中国石油天然气股份有限公司", "stock_code": "601857",
            "report_year": "2025（半年度）", "industry": "能源",
            "audit_opinion": str(op.get("opinion_type", "未识别")),
            "company_profile": "中国石油天然气股份有限公司是中国油气行业占主导地位的油气生产和销售商，"
                               "主要业务涵盖原油和天然气的勘探、开发、生产和销售，以及炼油、化工、"
                               "成品油销售和天然气管道运输等。报告期（2025年上半年）营业收入14,500.99亿元，"
                               "同比下降6.7%；归母净利润840.07亿元，同比下降5.4%。",
        },
        "risk_summary": {
            "total_risks": len(details),
            "major_risks": counts.get("重大", 0), "important_risks": counts.get("重要", 0),
            "general_risks": counts.get("一般", 0), "risk_dimensions": dims,
        },
        "risk_details": details,
        "cross_validation_analysis": "（离线验证模式）交叉验证矩阵未生成：需要 LLM 结合两阶段证据逐条比对，"
                                     "离线链路仅提供工具输出驱动的风险条目。",
        "risk_chain_analysis": "（离线验证模式）风险传导链未生成：需要 LLM 结构化推理。",
        "overall_assessment": f"（离线验证模式）本报告由真实工具链自动生成：共识别 {len(details)} 条风险"
                              f"（重大{counts.get('重大', 0)}/重要{counts.get('重要', 0)}/一般{counts.get('一般', 0)}），"
                              "全部来自校验/指标/披露/审计意见/多年对比工具输出，未经 LLM 研判，"
                              "结论方向仅供参考，需结合人工专业判断复核确认。",
    }


def main():
    text = open(os.path.join(os.path.dirname(__file__), "..", ".tmp_cnpc.txt"),
                encoding="utf-8").read()

    # 1. 真实工具链
    vd_raw = validate_financial_data.invoke({"financial_data_json": json.dumps(VD_JSON)})
    fin_raw = calculate_financial_indicators.invoke({"financial_data_json": json.dumps(CALC_JSON)})
    dc_raw = check_disclosure_compliance.invoke({"report_text": text[:200000]})
    ao_raw = identify_audit_opinion.invoke({"report_text": text[:200000]})
    my_raw = compare_multi_year.invoke({"multi_year_data_json": json.dumps(MY_INPUT)})
    rm_raw = calculate_risk_models.invoke({"financial_data_json": json.dumps(CALC_JSON)})

    fin = json.loads(fin_raw)
    dc = json.loads(dc_raw)
    vd = json.loads(vd_raw)
    ao = json.loads(ao_raw)
    my = json.loads(my_raw) if isinstance(my_raw, str) and my_raw.startswith("{") else {}
    rm = rm_raw if isinstance(rm_raw, str) else "{}"

    print("工具链输出摘要：")
    print("  alerts:", len(fin.get("alerts", [])), "| 披露问题:", len(dc.get("issues", [])),
          "| 校验:", (vd.get("data_validation") or {}).get("validation_result"),
          "| 审计意见:", (ao.get("audit_opinion") or {}).get("opinion_type"),
          "| 持续经营信号:", bool((ao.get("going_concern") or {}).get("flagged")),
          "| 趋势预警:", my.get("alert_count", 0))

    # 2. 台账构造器
    report = build_ledger(fin, dc, vd, ao, my)
    print("台账风险条目:", len(report["risk_details"]),
          "| 等级分布:", {k: v for k, v in report["risk_summary"].items()
                          if k in ("major_risks", "important_risks", "general_risks")})

    # 3. 综合评分（真实工具）
    score = calculate_comprehensive_score.invoke({
        "financial_analysis_json": fin_raw,
        "disclosure_check_json": dc_raw,
        "validation_json": vd_raw,
        "risk_models_json": rm,
        "audit_opinion_json": ao_raw,
    })
    print("综合评分:", json.loads(score).get("score"), json.loads(score).get("level"))

    # 4. 导出三份 PDF
    result = _export_pdf_impl(
        json.dumps(report, ensure_ascii=False),
        financial_indicators_json=fin_raw,
        disclosure_check_json=dc_raw,
        comprehensive_score_json=score,
        risk_models_json=rm,
        validation_json=vd_raw,
        compare_multi_year_json=my_raw if isinstance(my_raw, str) and my_raw.startswith("{") else "",
        audit_opinion_json=ao_raw,
    )
    print("=" * 20, "导出结果", "=" * 20)
    print(result)


if __name__ == "__main__":
    main()
