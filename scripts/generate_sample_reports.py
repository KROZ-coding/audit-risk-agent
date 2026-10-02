"""三份 PDF 报告样张生成脚本（打样用，数据自洽版）。

构造虚构公司「华信智造科技」的完整台账与专项数据：
- 综合评分与多年对比由**真实工具计算**（risk_scorer / compare_multi_year），
  保证 base_score=加权和、趋势预警与数据一致（修复手工编造导致的自相矛盾）；
- 金额统一「元」口径（_fmt_num 自动换算万/亿元）；
- 7 条风险覆盖五大维度，含商誉减值/存货真实性（演示信号联动规则）；
- 持续经营风险 R003 已按 going_concern 信号上调为「重要」。

用法: uv run python scripts/generate_sample_reports.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.pdf_export import _export_pdf_impl


def _sample_report() -> dict:
    """虚构公司「华信智造科技」完整台账：7 条风险覆盖财务/合规双维度。"""
    return {
        "company_info": {
            "company_name": "华信智造科技股份有限公司",
            "stock_code": "600123",
            "report_year": "2025",
            "industry": "制造业",
            "audit_opinion": "保留意见",
            "company_profile": "华信智造科技股份有限公司成立于2003年，主营高端数控机床与工业机器人核心部件，"
                               "客户覆盖汽车与航空航天领域，行业地位为国内细分市场前三。报告期内营业收入同比下滑8.5%，"
                               "扣非净利润连续三年为负且亏损扩大，经营现金流持续紧张。",
        },
        "risk_summary": {
            "total_risks": 7, "major_risks": 1, "important_risks": 4, "general_risks": 2,
            "risk_dimensions": {"财务错报": 4, "持续经营": 1, "信披合规": 1, "关联交易": 1},
        },
        "risk_details": [
            {
                "risk_id": "R001", "dimension": "财务错报", "title": "扣非净利润连续三年为负，主业盈利能力存疑",
                "level": "重大", "confidence": 0.85, "verification_status": "高度关注",
                "evidence": "2023-2025年扣非净利润分别为-1.2亿、-2.1亿、-2.8亿元；经营现金流/净利润=0.3<0.5，连续2期。",
                "data_analysis": "营收下滑背景下应收账款占比升至28%，存在提前确认收入以粉饰业绩的嫌疑，"
                                 "需结合现金流量表勾稽验证。",
                "regulatory_basis": "《企业会计准则第14号——收入》第五条；《审计准则1211号》第二十条。",
                "case_reference": "康得新案：虚增营业收入与货币资金，连续多年扣非亏损后爆雷，处罚结果为退市并重罚。",
                "audit_suggestion": "对前5大客户应收款项100%函证并执行替代测试；核查收入确认时点与合同条款。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "扣非净利润连续3年为负且亏损扩大，经营现金流/净利润仅0.3"},
                    {"step": "指标异常", "detail": "毛利率21.5%低于制造业基准25%，应收账款/营收28%高于基准20%"},
                    {"step": "法规依据", "detail": "《企业会计准则第14号》收入确认五步法，风险报酬转移时点存疑"},
                    {"step": "案例对照", "detail": "康得新案同型：营收虚增+现金流背离，最终退市处罚"},
                    {"step": "风险判定", "detail": "重大错报风险嫌疑成立，置信度0.85"},
                ],
                "trend_analysis": {"direction": "恶化", "periods": 3, "note": "亏损额逐年扩大"},
            },
            {
                "risk_id": "R002", "dimension": "财务错报", "title": "应收账款增速持续高于营收增速，回款风险加剧",
                "level": "重要", "confidence": 0.65, "verification_status": "建议关注",
                "evidence": "应收账款同比+22%，营收同比-8.5%，增速差超30pp；账龄1年以上占比升至35%。",
                "data_analysis": "收入质量下滑与放宽信用政策并存，存在通过赊销虚增收入的商业逻辑嫌疑。",
                "regulatory_basis": "《企业会计准则第22号——金融工具确认和计量》预期信用损失模型。",
                "case_reference": "乐视网案：应收账款激增后集中爆雷，坏账准备计提不足。",
                "audit_suggestion": "抽样核查大额应收回款流水，评估坏账准备计提充分性。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "应收增速22% vs 营收增速-8.5%，增速剪刀差超30pp"},
                    {"step": "指标异常", "detail": "应收账款/营收28%超行业基准20%达40%"},
                    {"step": "法规依据", "detail": "《企业会计准则第22号》预期信用损失三阶段模型"},
                    {"step": "案例对照", "detail": "乐视网应收爆雷案，坏账计提不足导致财务重述"},
                    {"step": "风险判定", "detail": "重要风险嫌疑，置信度0.65"},
                ],
            },
            {
                "risk_id": "R003", "dimension": "持续经营", "title": "持续经营重大不确定性信号下流动比率跌破1，短期偿债能力不足",
                "level": "重要", "confidence": 0.6, "verification_status": "高度关注",
                "evidence": "审计报告提示持续经营重大不确定性；流动比率0.78（基准1.5）、速动比率0.60；"
                           "2025年短期借款到期12亿元，货币资金仅3.5亿元。",
                "data_analysis": "存贷双高特征明显，短期偿债压力与再融资依赖并存；按信号联动规则，"
                                 "持续经营重大不确定性信号对应风险等级不低于重要。",
                "regulatory_basis": "《审计准则1324号——持续经营》第七条；《审计准则1324号》应用指南。",
                "case_reference": "某上市公司流动性危机案：流动比率连续低于1后爆发债务违约。",
                "audit_suggestion": "获取管理层持续经营评估报告，复核未来12个月现金流预测。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "审计报告提示持续经营重大不确定性；流动比率0.78<1，短债12亿 vs 货币资金3.5亿"},
                    {"step": "指标异常", "detail": "流动比率低于制造业基准1.5达48%"},
                    {"step": "法规依据", "detail": "《审计准则1324号》持续经营评估要求"},
                    {"step": "案例对照", "detail": "流动性危机案例：短期偿债缺口引发债务违约连锁"},
                    {"step": "风险判定", "detail": "信号联动规则要求持续经营信号对应等级不低于重要，置信度0.6"},
                ],
            },
            {
                "risk_id": "R004", "dimension": "信披合规", "title": "重大诉讼事项延迟披露且会计政策变更缺乏充分说明",
                "level": "重要", "confidence": 0.62, "verification_status": "高度关注",
                "evidence": "2025年8月收到监管问询函后第10日方披露诉讼事项（超过法定期限）；"
                           "存货计价方法由先进先出变更为加权平均，未量化影响金额。",
                "data_analysis": "延迟披露与政策变更叠加，存在调节利润平滑业绩的嫌疑；叠加非标意见与"
                                 "会计师事务所变更同现，需关注「意见购买」嫌疑。",
                "regulatory_basis": "《上市公司信息披露管理办法》（2025修订）第四十七条；《证券法》第七十八条。",
                "case_reference": "某上市公司信披违规案：重大事项延迟披露被出具警示函并罚款。",
                "audit_suggestion": "核查变更时点及会计政策变更对利润的影响金额，评估披露义务履行情况。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "诉讼事项问询后第10日方披露（超法定期限），存货计价方法变更未量化影响"},
                    {"step": "指标异常", "detail": "变更口径测算下毛利率较未变更口径抬升约3pp（反事实测算）"},
                    {"step": "法规依据", "detail": "《信披管理办法》第四十七条及时性要求"},
                    {"step": "案例对照", "detail": "同类案例被出具警示函+罚款处罚"},
                    {"step": "风险判定", "detail": "重要风险嫌疑，置信度0.62"},
                ],
            },
            {
                "risk_id": "R005", "dimension": "关联交易", "title": "关联采购占比超30%且定价公允性证据不足",
                "level": "一般", "confidence": 0.5, "verification_status": "待进一步核实",
                "evidence": "向关联方采购原材料占比32%，高于30%关注阈值；未披露定价公允性分析。",
                "data_analysis": "关联采购集中度偏高，存在通过关联交易输送利益或调节成本的风险路径。",
                "regulatory_basis": "《企业会计准则第36号——关联方披露》；交易所关联交易披露规则。",
                "case_reference": "某上市公司关联交易输送案：定价不公允被认定利益输送。",
                "audit_suggestion": "获取关联交易定价依据，对比第三方报价，核查资金流向。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "关联采购占比32%超30%阈值"},
                    {"step": "指标异常", "detail": "关联采购毛利率低于非关联采购约5pp"},
                    {"step": "法规依据", "detail": "《企业会计准则第36号》充分披露要求"},
                    {"step": "案例对照", "detail": "关联交易输送案：定价不公允被处罚"},
                    {"step": "风险判定", "detail": "一般风险嫌疑（证据不足待核实），置信度0.5"},
                ],
            },
            {
                "risk_id": "R006", "dimension": "财务错报", "title": "商誉占净资产25%显著高于行业基准，减值测试充分性存疑",
                "level": "重要", "confidence": 0.58, "verification_status": "建议关注",
                "evidence": "商誉/净资产=25%（行业基准10%，偏离+150%）；被收购子公司2025年业绩承诺完成率仅62%。",
                "data_analysis": "净利润连续为负背景下商誉敞口显著高于行业，存在减值计提不足、"
                                 "通过商誉调节利润的嫌疑，按信号联动规则须单独识别商誉减值风险。",
                "regulatory_basis": "《企业会计准则第8号——资产减值》商誉减值测试要求。",
                "case_reference": "某上市公司商誉爆雷案：业绩承诺不达标后集中计提大额商誉减值。",
                "audit_suggestion": "复核商誉减值测试模型假设与现金流预测，评估减值计提充分性。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "商誉/净资产25% vs 基准10%，偏离+150%；业绩承诺完成率62%"},
                    {"step": "指标异常", "detail": "净利润连续为负且商誉敞口高企，减值风险与利润调节动机并存"},
                    {"step": "法规依据", "detail": "《企业会计准则第8号》商誉至少每年年度终了进行减值测试"},
                    {"step": "案例对照", "detail": "商誉爆雷案：承诺不达标后集中计提，股价与信用双杀"},
                    {"step": "风险判定", "detail": "重要风险嫌疑，置信度0.58"},
                ],
            },
            {
                "risk_id": "R007", "dimension": "财务错报", "title": "存货盘点受限且存货周转放缓，存货真实性风险",
                "level": "一般", "confidence": 0.52, "verification_status": "建议关注",
                "evidence": "审计报告保留意见基础为存货盘点受限；存货周转率4.2次低于基准6次；"
                           "存货跌价准备计提比例低于同行业可比公司。",
                "data_analysis": "盘点受限导致存货存在性与计价准确性无法充分验证，结合周转放缓与跌价计提偏低，"
                                 "存在存货虚增或跌价不足的风险路径。",
                "regulatory_basis": "《审计准则1312号——存货监盘》；《企业会计准则第1号——存货》。",
                "case_reference": "某上市公司存货造假案：虚增存货与跌价准备计提不足被处罚。",
                "audit_suggestion": "对主要存货仓库执行补充监盘与计价测试，复核跌价准备计提。",
                "reasoning_chain": [
                    {"step": "数据发现", "detail": "存货盘点受限（保留意见基础），存货周转率4.2低于基准6"},
                    {"step": "指标异常", "detail": "跌价准备计提比例低于可比公司约2pp"},
                    {"step": "法规依据", "detail": "《审计准则1312号》存货监盘要求"},
                    {"step": "案例对照", "detail": "存货造假案：虚增存货+计提不足被重罚"},
                    {"step": "风险判定", "detail": "一般风险嫌疑（盘点受限证据链不足），置信度0.52"},
                ],
            },
        ],
        "cross_validation_analysis": "R001与R003交叉印证：现金流恶化（0.3）与流动比率跌破1（0.78）共同指向偿债能力下降；"
                                     "R002与R004存在间接关联：应收激增与会计政策变更均指向利润调节动机；"
                                     "R006与R001联动：净利润连续亏损背景下商誉减值计提充分性存疑；"
                                     "R007与保留意见（存货盘点受限）直接对应；R005独立于财务链路。",
        "risk_chain_analysis": "应收激增→经营现金流恶化→流动比率跌破1→偿债能力下降→持续经营风险嫌疑；"
                               "净利润连续亏损→商誉减值风险上升→净资产收缩→偿债指标进一步恶化。",
        "overall_assessment": "公司2025年度存在多项需关注的风险嫌疑：主业持续亏损叠加偿债指标恶化与持续经营重大不确定性信号，"
                              "财务错报、信披合规与持续经营维度均出现印证性证据，整体风险等级偏高，"
                              "建议优先核查应收账款真实性、商誉减值测试、存货盘点受限事项与关联交易定价，"
                              "需结合人工专业判断进行复核确认。",
        "review_conclusion": "\n\n---\n### 🔍 审计合伙人复核意见\n"
                             "复核结论：R001重大等级成立（现金流与应收双重印证）；R003按信号联动规则上调为重要"
                             "（与审计报告持续经营重大不确定性信号一致）；R004重要等级维持；R006商誉减值与R007存货真实性"
                             "按信号联动规则补充识别；R005证据链不足，维持一般等级并标注待核实。"
                             "综合风险评分与台账风险分布一致。",
    }


def _financial_json() -> str:
    """财务指标（金额字段统一元口径，_fmt_num 自动换算亿元）。"""
    return json.dumps({
        "indicators": {
            "revenue_yoy_change_pct": -8.5, "net_profit_yoy_change_pct": -33.3,
            "gross_margin_pct": 21.5, "operating_cashflow_to_net_profit_ratio": 0.3,
            "accounts_receivable_to_revenue_ratio": 28.0,
            "accounts_receivable_yoy_change_pct": 22.0, "inventory_turnover_ratio": 4.2,
            "debt_to_asset_ratio_pct": 68.3, "current_ratio": 0.78, "quick_ratio": 0.60,
            "goodwill_to_net_assets_ratio_pct": 25.0, "net_profit_current": -280000000,
            "net_profit_previous": -210000000,
        },
        "alerts": [
            "扣非净利润连续3年为负且亏损扩大（-1.2亿→-2.1亿→-2.8亿）",
            "经营现金流/净利润=0.3，低于0.5阈值，盈利质量存疑",
            "流动比率0.78低于行业基准1.5，短期偿债能力不足",
            "应收账款同比+22%远超营收增速（-8.5%），回款风险加剧",
        ],
    }, ensure_ascii=False)


def _disclosure_json() -> str:
    return json.dumps({
        "compliance_score": 68.0, "risk_score": 45.0,
        "checked_items": 12, "passed_items": 9,
        "issues": ["重大诉讼事项延迟披露（问询后第10日方披露）", "会计政策变更未量化影响金额"],
        "sections_missing": ["公司治理", "董事、监事和高级管理人员情况"],
        "audit_opinion": "保留意见",
    }, ensure_ascii=False)


def _risk_models_json() -> str:
    return json.dumps({
        "risk_models": {
            "altman_z_score": {"available": True, "score": 1.52, "zone": "财务困境区（<1.81）",
                               "interpretation": "破产风险较高，与持续经营风险信号相互印证"},
            "beneish_m_score": {"available": True, "score": -1.12, "zone": "操纵嫌疑区（>-1.78）",
                                "interpretation": "存在盈余操纵特征，与应收激增/毛利率异常抬升相互印证"},
            "cross_interpretation": {
                "interpretation": "两模型同时预警：Z-Score 指向财务困境，M-Score 指向盈余操纵，"
                                  "交叉印证财务错报风险链条，触发综合评分抬升。",
            },
        },
    }, ensure_ascii=False)


def _validation_json() -> str:
    return json.dumps({
        "data_validation": {
            "validation_result": "存在勾稽差异，需人工复核",
            "passed_checks": 10, "failed_checks": 1,
            "details": ["经营现金流与净利润勾稽差异超阈值"],
        },
    }, ensure_ascii=False)


def _audit_opinion_json() -> str:
    return json.dumps({
        "audit_opinion": {
            "identified": True, "opinion_type": "保留意见", "is_standard_opinion": False,
            "credibility_impact": "高", "risk_level": "重要",
            "meaning": "财务报表整体公允，但存在影响重大而不具广泛性的具体事项。",
            "implication": "被保留的具体项目数据不可直接采信，其他部分可参考。",
            "evidence_excerpt": "形成保留意见的基础：存货盘点受限，无法获取充分适当的审计证据。",
        },
        "going_concern": {
            "flagged": True, "signal": "审计报告提示与持续经营相关的重大不确定性",
            "risk_level": "重大",
            "implication": "该信号独立于审计意见类型，须直接上调持续经营维度风险等级。",
        },
        "key_audit_matters": {
            "found": True,
            "matters": [
                {"matter": "存货", "risk_direction": "存在跌价与盘点受限风险"},
                {"matter": "应收账款", "risk_direction": "存在减值与回款风险"},
                {"matter": "收入确认", "risk_direction": "存在提前确认收入嫌疑"},
            ],
            "note": "关键审计事项通常揭示被审计单位最高风险的领域，应优先核查。",
        },
        "auditor_change": {"flagged": True},
        "linkage_alerts": [
            "非标准审计意见与会计师事务所变更同时出现，需关注是否存在「意见购买」嫌疑，"
            "建议核查变更原因与前任事务所意见。",
        ],
        "reference": "判定规则依据 knowledge_base/审计意见类型库.txt",
    }, ensure_ascii=False)


def _multi_year_input() -> dict:
    """多年财务数据（元口径），供真实 compare_multi_year 工具计算。"""
    years = [
        # 2022：盈利年份
        {"year": "2022", "revenue": 1850000000, "net_profit": 80000000,
         "operating_cashflow": 95000000, "total_assets": 2800000000,
         "total_liabilities": 1540000000, "accounts_receivable": 350000000,
         "inventory": 280000000, "cost_of_goods": 1332000000,
         "current_assets": 1350000000, "current_liabilities": 1000000000,
         "goodwill": 300000000},
        # 2023：转亏
        {"year": "2023", "revenue": 1720000000, "net_profit": -120000000,
         "operating_cashflow": 30000000, "total_assets": 3000000000,
         "total_liabilities": 1800000000, "accounts_receivable": 420000000,
         "inventory": 320000000, "cost_of_goods": 1273000000,
         "current_assets": 1380000000, "current_liabilities": 1200000000,
         "goodwill": 380000000},
        # 2024：亏损扩大
        {"year": "2024", "revenue": 1550000000, "net_profit": -210000000,
         "operating_cashflow": -60000000, "total_assets": 3300000000,
         "total_liabilities": 2112000000, "accounts_receivable": 490000000,
         "inventory": 360000000, "cost_of_goods": 1178000000,
         "current_assets": 1410000000, "current_liabilities": 1480000000,
         "goodwill": 420000000},
        # 2025：持续恶化
        {"year": "2025", "revenue": 1420000000, "net_profit": -280000000,
         "operating_cashflow": -84000000, "total_assets": 3500000000,
         "total_liabilities": 2390000000, "accounts_receivable": 518000000,
         "inventory": 380000000, "cost_of_goods": 1115000000,
         "current_assets": 1400000000, "current_liabilities": 1790000000,
         "goodwill": 500000000},
    ]
    return {"years": years}


def main():
    from tools.risk_scorer import calculate_comprehensive_score
    from tools.multi_year_comparison import compare_multi_year

    fin = _financial_json()
    dc = _disclosure_json()
    rm = _risk_models_json()
    vd = _validation_json()
    ao = _audit_opinion_json()

    # 综合评分：真实工具计算（保证 base_score=加权和、breakdown 自洽、可追溯）
    score = calculate_comprehensive_score.invoke({
        "financial_analysis_json": fin,
        "disclosure_check_json": dc,
        "validation_json": vd,
        "risk_models_json": rm,
        "audit_opinion_json": ao,
    })
    # 多年对比：真实工具计算（保证趋势预警与底层数据一致，不再手工编造）
    my = compare_multi_year.invoke({"multi_year_data_json": json.dumps(_multi_year_input(), ensure_ascii=False)})
    print("综合评分（真实工具）:", json.loads(score).get("score"),
          "| 多年预警数（真实工具）:", json.loads(my).get("alert_count"))

    report = _sample_report()
    result = _export_pdf_impl(
        json.dumps(report, ensure_ascii=False),
        financial_indicators_json=fin,
        disclosure_check_json=dc,
        comprehensive_score_json=score,
        risk_models_json=rm,
        validation_json=vd,
        compare_multi_year_json=my,
        audit_opinion_json=ao,
    )
    print("=" * 20, "导出结果", "=" * 20)
    print(result)
    # 转首页预览图（可选）
    import fitz
    import re
    # 兼容两种链接形态：旧平铺 /local_storage/reports/xxx.pdf 与新批次
    # /local_storage/<YYYYMMDD_HHMMSS>/reports/xxx.pdf
    links = re.findall(r"/local_storage/(?:\d{8}_\d{6}/)?reports/([^\s)]+\.pdf)", result)
    # 预览图也进入当前批次子目录（与 PDF 同批隔离），避免平铺 charts/ 混入旧批次
    from local_storage import current_batch_stamp
    _stamp = current_batch_stamp()
    preview_dir = os.path.join(os.path.dirname(__file__), "..", "local_storage", _stamp, "charts")
    for name in links:
        path = os.path.join(os.path.dirname(__file__), "..", "local_storage", _stamp, "reports", name)
        if not os.path.exists(path):
            # 旧平铺结构兜底（未走批次隔离的历史产物）
            legacy = os.path.join(os.path.dirname(__file__), "..", "local_storage", "reports", name)
            if os.path.exists(legacy):
                path = legacy
            else:
                continue
        doc = fitz.open(path)
        png = os.path.join(preview_dir, f"样张预览_{os.path.splitext(name)[0][-12:]}.png")
        page = doc[0]
        pix = page.get_pixmap(dpi=110)
        pix.save(png)
        print(f"首页预览: {png}（总页数 {doc.page_count}）")
        doc.close()


if __name__ == "__main__":
    main()
