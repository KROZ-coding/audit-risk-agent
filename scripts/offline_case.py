"""离线验收黄金案例（合成数据，无 LLM）。

数据合规约定：本文件内嵌的财务数据为**构造的合成数据**，不对应任何真实企业，
可安全入库并供 CI 使用。真实年报的验证请通过 ``--case-file`` 加载本地案例
（放置于 ``scripts/_local_case/``，已被 .gitignore 排除，不入库）。

流程（全部真实工具，无 LLM）：
1. 读取验收 PDF 文本（默认仓库内脱敏 fixture，可 --pdf 指定）
2. 加载案例财务数据（默认内嵌合成案例；--case-file 可替换为本地真实案例）
3. 依次跑 validate → calculate → check_disclosure → identify_audit_opinion
   → compare_multi_year → calculate_risk_models → calculate_comprehensive_score
4. 台账构造器：仅基于工具输出（预警/问题/校验失败/意见信号）生成风险条目，
   每条注明数据来源（可追溯，不编造）
5. _export_pdf_impl 生成三份 PDF，落入 local_storage/<YYYYMMDD_HHMMSS>/reports/

用法:
    uv run python scripts/offline_case.py                     # 合成案例 + 仓库 fixture
    uv run python scripts/offline_case.py --case-file <json>  # 本地真实案例（不入库）
"""
import argparse
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

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
DEFAULT_PDF_PATH = os.path.join(
    REPO_ROOT, "tests", "fixtures", "sanitized_energy_h1_report.pdf")

# ── 合成案例公司（构造数据，不对应任何真实企业）──
COMPANY_INFO = {
    "company_name": "示例能源集团股份有限公司", "stock_code": "600000",
    "report_year": "2025（半年度）", "industry": "能源",
    "company_profile": "示例能源集团股份有限公司是用于离线验收的合成案例公司，"
                       "全部财务数据为构造数据，仅用于验证工具链的计算与勾稽逻辑，"
                       "不对应任何真实企业。",
}

# ── 合成案例关键数据（单位：人民币元；各科目间满足勾稽恒等式）──
# 恒等式：总资产 = 负债 + 净资产；应收净额 = 账面余额 − 坏账准备；
#         未分配利润期末 = 期初 + 归母净利润 − 分红 + 其他权益变动。
CALC_JSON = {
    "amount_unit": "人民币元", "period": "2025年半年度", "scope": "中国准则合并",
    "industry": "能源", "previous_period": "2024年半年度（追溯后）",
    "revenue_current": 1200000000000, "revenue_previous": 1280000000000,
    "net_profit_current": 80000000000, "net_profit_previous": 85000000000,
    "net_profit_parent_current": 72000000000, "net_profit_parent_previous": 76000000000,
    "net_profit_parent_deducted_current": 72500000000,
    "net_profit_parent_deducted_previous": 77000000000,
    "cost_of_goods_current": 950000000000, "cost_of_goods_previous": 1020000000000,
    "operating_cashflow_current": 180000000000, "operating_cashflow_previous": 170000000000,
    "total_assets_current": 2400000000000, "total_assets_previous": 2320000000000,
    "total_liabilities_current": 960000000000, "total_liabilities_previous": 950000000000,
    "accounts_receivable_current": 97600000000, "accounts_receivable_previous": 60000000000,
    "accounts_receivable_gross_current": 100000000000,
    "accounts_receivable_gross_same_period_previous": 62500000000,
    "bad_debt_provision_current": 2400000000, "bad_debt_provision_same_period_previous": 2500000000,
    "inventory_current": 130000000000, "inventory_previous": 140000000000,
    "current_assets_current": 600000000000, "current_assets_previous": 520000000000,
    "current_liabilities_current": 560000000000, "current_liabilities_previous": 520000000000,
    "cash_and_equivalents_current": 190000000000, "cash_and_equivalents_previous": 150000000000,
    "goodwill_current": 6000000000, "goodwill_previous": 6100000000,
    "net_assets_current": 1440000000000, "short_term_debt_current": 40000000000,
    "other_receivables_current": 30000000000,
    "fixed_assets_current": 380000000000, "fixed_assets_previous": 400000000000,
    "construction_in_progress_current": 190000000000,
}

VD_JSON = {
    "amount_unit": "人民币元", "period": "2025年半年度", "scope": "中国准则合并",
    "total_assets_current": 2400000000000, "total_liabilities_current": 960000000000,
    "equity_total": 1440000000000, "net_profit_current": 80000000000,
    "net_profit_parent_current": 72000000000,
    "operating_cashflow_current": 180000000000,
    # 现金流量表还包含减值、投资收益、递延税等调整，不能把未摘录项目静默写成0。
    # 缺少完整间接法调整项时 validator 会返回 limited_check，不伪造勾稽失败。
    "retained_earnings_begin": 900000000000, "retained_earnings_end": 935800000000,
    "dividends": 36000000000, "retained_earnings_other_changes": -200000000,
    "_field_metadata": {
        "total_assets_current": {"period": "2025-06-30", "page": "11", "locator": "合并资产负债表：资产总计"},
        "total_liabilities_current": {"period": "2025-06-30", "page": "12", "locator": "合并资产负债表：负债合计"},
        "equity_total": {"period": "2025-06-30", "page": "12", "locator": "合并资产负债表：股东权益合计"},
        "retained_earnings_begin": {"period": "2025-01-01", "page": "15", "locator": "合并股东权益变动表：未分配利润期初"},
        "retained_earnings_end": {"period": "2025-06-30", "page": "15", "locator": "合并股东权益变动表：未分配利润期末"},
        "retained_earnings_other_changes": {"period": "2025年1-6月", "page": "15", "locator": "其他权益变动—其他：未分配利润"},
        "dividends": {"period": "2025年1-6月", "page": "15", "locator": "利润分配：对股东的分配"},
    },
}

MY_INPUT = {
    "years": [
        {"year": "2024H1（追溯调整后）", "revenue": 1280000000000, "net_profit": 85000000000,
         "operating_cashflow": 170000000000, "total_assets": 2320000000000,
         "total_liabilities": 950000000000, "accounts_receivable": 60000000000,
         "inventory": 140000000000, "cost_of_goods": 1020000000000,
         "current_assets": 520000000000,
         "current_liabilities": 520000000000, "goodwill": 6100000000},
        {"year": "2025H1", "revenue": 1200000000000, "net_profit": 80000000000,
         "operating_cashflow": 180000000000, "total_assets": 2400000000000,
         "total_liabilities": 960000000000, "accounts_receivable": 97600000000,
         "inventory": 130000000000, "cost_of_goods": 950000000000,
         "current_assets": 600000000000,
         "current_liabilities": 560000000000, "goodwill": 6000000000},
    ]
}


def load_case_file(path: str) -> dict:
    """加载本地案例 JSON（scripts/_local_case/ 下，不入库），覆盖内嵌合成案例。"""
    with open(path, encoding="utf-8") as f:
        case = json.load(f)
    return {
        "CALC_JSON": case.get("calc", CALC_JSON),
        "VD_JSON": case.get("vd", VD_JSON),
        "MY_INPUT": case.get("my", MY_INPUT),
        "COMPANY_INFO": case.get("company_info", COMPANY_INFO),
        "PDF_PATH": case.get("pdf_path"),
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
    return "财务风险"


def build_ledger(fin, dc, vd, ao, my, company_info: dict | None = None) -> dict:
    """台账构造器：仅基于工具输出生成风险条目（来源可追溯）。"""
    info = dict(COMPANY_INFO if company_info is None else company_info)
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
        # 披露 issues 为「待核查线索」而非已确认违规：工具措辞均带"待核查/须核对/
        # 不因正文未重复列示认定违规"，等级按一般（待核查）记录，避免与 risk_score=0
        # 的合规高分自相矛盾；确需重点关注的由人工复核升级。
        add(f"R{j:03d}", "信披合规", f"披露问题：{str(issue)[:40]}",
            _level_of(str(issue), "一般"),
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
        add(f"R{k:03d}", "财务风险", f"数据可靠性：{str(vr.get('message', ''))[:40]}",
            "一般", f"（数据来源：validate_financial_data 勾稽校验未通过项）{vr.get('message', '')}",
            "三大勾稽校验规则（资产负债表平衡/现金流勾稽/利润分配一致性）。",
            "核对原始报表附注数据，修正后重新校验。",
            0.55,
            [("数据发现", f"勾稽校验未通过：{vr.get('message', '')}"),
             ("风险判定", "数据可靠性风险（离线模板）")])

    # 4) 审计意见信号（非标/持续经营/关键审计事项）
    op = (ao.get("audit_opinion") or {})
    gc = (ao.get("going_concern") or {})
    # 仅「明确非标意见」才立为风险：未经审计（半年度报告）的 is_standard_opinion=None
    # 属资料属性而非非标意见，不得误立（与全系统"未经审计仅是资料属性"口径一致）。
    if op.get("identified") and (
            op.get("is_standard_opinion") is False
            or str(op.get("opinion_nature", "") or "") == "非标准"):
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
    info.setdefault("audit_opinion", str(op.get("opinion_type", "未识别")))
    return {
        "company_info": info,
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
    parser = argparse.ArgumentParser(description="离线验收黄金案例运行器（无 LLM）")
    parser.add_argument("--case-file", default=None,
                        help="本地案例 JSON（scripts/_local_case/ 下，不入库）；缺省用内嵌合成案例")
    parser.add_argument("--pdf", default=None, help="验收 PDF 路径；缺省用仓库内脱敏 fixture")
    args = parser.parse_args()

    calc, vd, my, company_info = CALC_JSON, VD_JSON, MY_INPUT, COMPANY_INFO
    pdf_path = args.pdf or DEFAULT_PDF_PATH
    if args.case_file:
        case = load_case_file(args.case_file)
        calc, vd, my = case["CALC_JSON"], case["VD_JSON"], case["MY_INPUT"]
        company_info = case["COMPANY_INFO"]
        if case.get("PDF_PATH") and not args.pdf:
            pdf_path = case["PDF_PATH"]
    if not os.path.isfile(pdf_path):
        print(f"验收 PDF 缺失：{pdf_path}（可用 --pdf 指定，或运行 "
              "scripts/generate_sanitized_pdf_fixture.py 生成）")
        return 1

    # 直接读取验收 PDF；不依赖可能过期或不存在的临时抽取文件。
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    text = "\n\n".join(
        f"--- 第 {i + 1} 页 ---\n{page.extract_text() or ''}"
        for i, page in enumerate(reader.pages)
    )[:200_000]

    # 1. 真实工具链
    vd_raw = validate_financial_data.invoke({"financial_data_json": json.dumps(vd)})
    fin_raw = calculate_financial_indicators.invoke({"financial_data_json": json.dumps(calc)})
    dc_raw = check_disclosure_compliance.invoke({"report_text": text[:200000]})
    ao_raw = identify_audit_opinion.invoke({"report_text": text[:200000]})
    my_raw = compare_multi_year.invoke({"multi_year_data_json": json.dumps(my)})
    rm_raw = calculate_risk_models.invoke({"financial_data_json": json.dumps(calc)})

    fin = json.loads(fin_raw)
    dc = json.loads(dc_raw)
    vd_out = json.loads(vd_raw)
    ao = json.loads(ao_raw)
    my_out = json.loads(my_raw) if isinstance(my_raw, str) and my_raw.startswith("{") else {}
    rm = rm_raw if isinstance(rm_raw, str) else "{}"

    print("工具链输出摘要：")
    print("  alerts:", len(fin.get("alerts", [])), "| 披露问题:", len(dc.get("issues", [])),
          "| 校验:", (vd_out.get("data_validation") or {}).get("validation_result"),
          "| 审计意见:", (ao.get("audit_opinion") or {}).get("opinion_type"),
          "| 持续经营信号:", bool((ao.get("going_concern") or {}).get("flagged")),
          "| 趋势预警:", my_out.get("alert_count", 0))

    # 2. 台账构造器
    report = build_ledger(fin, dc, vd_out, ao, my_out, company_info)
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
    score_data = json.loads(score) if isinstance(score, str) else (score or {})
    print("综合评分:", score_data.get("score"), score_data.get("level"))

    # 评分快照写入台账：结论章与封面分数一致（L 补丁）
    report["comprehensive_score_snapshot"] = {
        "score": score_data.get("score"),
        "level": score_data.get("level") or "",
        "basis": "综合评分由真实工具链计算，与封面一致",
    }

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
