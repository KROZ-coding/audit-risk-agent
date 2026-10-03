"""Regression coverage for lossless decisions in compact stage handoffs."""

import copy
import json
import re

from agents.pipeline import STAGE_INSTRUCTIONS, build_stage_payload, summarize_tool_result
from main import GraphService


def test_large_facts_do_not_hide_late_metrics_alerts_or_evidence():
    result = {
        "facts": [{"fact_id": "F-report", "raw_value": "原文" * 5000}],
        "metric_results": [
            {"metric_id": f"M{i}", "value": i, "status": "calculated", "evidence_ids": [f"E{i}"]}
            for i in range(80)
        ] + [{"metric_id": "M-missing", "value": None, "status": "not_comparable",
              "reason": "期初应收不能与上年同期营业收入配比", "evidence_ids": ["E-missing"]}],
        "alerts": ["后置的回款关注提示"],
        "evidence": [{"evidence_id": "E-missing", "fact_ids": ["F-ar"], "metric_ids": ["M-missing"],
                      "page": "47", "verified": False, "status": "pending", "excerpt": "重复原文" * 2000}],
    }
    summary = json.loads(summarize_tool_result(json.dumps(result, ensure_ascii=False)))
    assert summary["metric_results"] == result["metric_results"]
    assert summary["alerts"] == result["alerts"]
    assert summary["evidence"][0]["evidence_id"] == "E-missing"
    assert summary["evidence"][0]["page"] == "47"
    assert summary["evidence"][0]["verified"] is False
    assert summary["_handoff_summary"]["omitted_fields"] == ["excerpt", "facts"]


def test_validation_checks_and_unknown_limitations_survive_projection():
    result = {
        "results": [{"check": "现金流勾稽", "passed": None, "status": "limited_check",
                     "reason": "缺少完整间接法调整项", "evidence_id": "E-check"}],
        "data_validation": {"failed_checks": 0, "skipped_checks": 1, "validation_result": "部分完成"},
        "limitations": ["不构成间接法完整勾稽"],
        "issues": ["特定披露待核实"],
    }
    summary = json.loads(summarize_tool_result(result))
    assert all(summary[key] == value for key, value in result.items())


def test_projection_does_not_mutate_tool_ledger_objects():
    result = {"facts": [{"value": 10}], "metric_results": [{"metric_id": "M1", "value": 10}]}
    original = copy.deepcopy(result)
    summarize_tool_result(result)
    assert result == original


def test_unavailable_models_remain_visible_in_synthesis_payload():
    models = {"risk_models": {
        "altman_z_score": {"available": False, "status": "not_applicable", "reason": "缺少本期期间，无法确认模型适用条件"},
        "beneish_m_score": {"available": False, "status": "insufficient_data", "missing_factors": ["DEPI"]},
    }}
    payload = build_stage_payload({"messages": [{"role": "user", "content": "原报表"}]},
                                  "synthesis", "综合研判", [],
                                  {"calculate_risk_models": json.dumps(models, ensure_ascii=False)})
    text = payload["messages"][-1]["content"]
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    assert json.loads(blocks[0])["risk_models"] == models["risk_models"]


def test_invalid_json_is_an_explicit_unavailable_result():
    summary = json.loads(summarize_tool_result('{"facts": [{"field": "broken"'))
    assert summary["status"] == "invalid_tool_result"
    assert "不能据此" in summary["reason"]
    assert "alerts" not in summary


def test_regulation_text_is_json_wrapped_without_losing_citation():
    text = "《规定》第25条\n来源：https://example.test/law\n核实相关条款的适用范围"
    summary = json.loads(summarize_tool_result(text, tool_name="search_regulations"))
    assert summary["regulation_text"] == text


def test_nonfinite_values_do_not_emit_invalid_json_constants():
    summary = json.loads(summarize_tool_result({"value": float("nan")}))
    assert summary["status"] == "invalid_tool_result"


def test_stage_instructions_reuse_results_and_delegate_artifacts_to_system():
    instruction = STAGE_INSTRUCTIONS["synthesis"]
    assert "复用" in instruction
    assert "not_applicable / insufficient_data" in instruction
    assert "interim_note/limitation" in instruction
    assert "图表、PDF 报告与 Excel 底稿由系统" in instruction
    assert "generate_risk_heatmap" not in instruction
    assert "已完成" in STAGE_INSTRUCTIONS["financial"]


def test_financial_extraction_excerpt_prefers_cas_statement_pages():
    text = "\n\n".join([
        "--- 第 6 页 ---\n按国际财务报告会计准则编制的主要财务数据\n总资产 2,399,800",
        "--- 第 21 页 ---\n总负债 960,100",
        "--- 第 49 页 ---\n按中国企业会计准则编制的合并资产负债表\n资产总计 2,400,000",
        "--- 第 50 页 ---\n负债合计 960,000\n未分配利润 935,800",
    ])
    excerpt = GraphService._financial_extraction_excerpt(text)
    assert "--- 第 49 页 ---" in excerpt
    assert "--- 第 50 页 ---" in excerpt
    assert "--- 第 21 页 ---" not in excerpt


def test_source_table_backfill_recovers_receivable_and_equity_changes():
    text = "\n\n".join([
        "--- 第 84 页 ---\n示例集团未经审计财务报表附注（除特别注明外，金额单位为人民币百万元）\n"
        "本集团 2027 年 6 月 30 日 2026 年 12 月 31 日\n"
        "应收账款 100,000 62,500 16,844 7,807\n减：坏账准备 (2,400) (2,500) (537) (588)",
        "--- 第 49 页 ---\n示例集团 2027 年6 月30日未经审计合并及公司资产负债表 "
        "(除特别注明外，金额单位为人民币百万元) 2027 年6月30日 2026年12月31日 "
        "合并 合并 公司 公司\n其他应收款 12 30,000 28,000 12,349 8,454\n"
        "非流动资产\n固定资产 16 380,000 400,000 247,377 262,146\n"
        "在建工程 18 190,000 170,000 142,846 129,145",
        "--- 第 53 页 ---\n截至2027年6月30日止6个月期间未经审计合并股东权益变动表 "
        "(除特别注明外，金额单位为人民币百万元) 2027年6月30日 2026年12月31日 "
        "项目 未分配利润\n2027年1月1日余额 183,021 121,812 6,747 (30,748) 252,305 900,000 1,515,371 194,492 1,709,863\n"
        "综合收益总额 - - - (170) - 72,000 71,830 9,496 (93,319)\n"
        "对股东的分配 - - - - - (36,000) (36,000) (7,048) (43,048)\n"
        "其他 - (83) - (14) - (200) (297) 65 (148)\n"
        "2027年6月30日余额 183,021 121,729 9,414 (30,932) 252,305 935,800 1,479,546 197,249 1,676,795",
    ])
    facts = GraphService._backfill_source_facts(text, {})
    assert facts["accounts_receivable_gross_current"] == 100000
    assert facts["accounts_receivable_gross_same_period_previous"] == 62500
    assert facts["bad_debt_provision_current"] == 2400
    assert facts["bad_debt_provision_same_period_previous"] == 2500
    assert facts["other_receivables_current"] == 30000
    assert facts["other_receivables_previous"] == 28000
    assert facts["fixed_assets_current"] == 380000
    assert facts["fixed_assets_previous"] == 400000
    assert facts["construction_in_progress_current"] == 190000
    assert facts["construction_in_progress_previous"] == 170000
    assert facts["retained_earnings_other_changes"] == -200
    assert facts["retained_earnings_begin"] == 900000
    assert facts["retained_earnings_end"] == 935800
    assert facts["dividends"] == 36000
    assert facts["amount_unit"] == "人民币百万元"
    assert facts["_field_metadata"]["bad_debt_provision_current"]["unit"] == "人民币百万元"
    assert facts["_field_metadata"]["bad_debt_provision_current"]["page"] == "84"
    assert facts["_field_metadata"]["retained_earnings_begin"]["period"] == "2027-01-01"
    assert facts["_field_metadata"]["retained_earnings_end"]["period"] == "2027-06-30"
    assert facts["_field_metadata"]["dividends"]["period"] == "2027年1-6月"
    assert facts["_field_metadata"]["other_receivables_current"]["period"] == "2027-06-30"
    assert facts["_field_metadata"]["other_receivables_previous"]["period"] == "2026-12-31"
    assert facts["_field_metadata"]["retained_earnings_other_changes"]["period"] == "2027年1-6月"


def test_source_table_backfill_requires_scope_dates_and_unit():
    """相似数字行缺少表头口径时不得猜测期间、单位或公司范围。"""
    text = "--- 第 1 页 ---\n其他应收款 30,000 28,000 12,349 8,454\n"
    facts = GraphService._backfill_source_facts(text, {})
    assert "other_receivables_current" not in facts
    assert "amount_unit" not in facts


def test_source_table_backfill_respects_declared_scope_and_unit():
    text = ("--- 第 49 页 ---\n未经审计合并及公司资产负债表（金额单位为人民币百万元） "
            "2027年6月30日 2026年12月31日 合并 合并 公司 公司 "
            "其他应收款 30,000 28,000 12,349 8,454")
    facts = {"scope": "母公司", "amount_unit": "人民币元"}
    GraphService._backfill_source_facts(text, facts)
    assert "other_receivables_current" not in facts


def test_source_bound_text_applies_local_rules_via_override_module(synthetic_case_overrides):
    from agents.agent import _normalize_source_bound_text
    text = "甲集团持股比例55.00%（含间接持有H股）；应收账款账面余额同比增长12.00%"
    normalized = _normalize_source_bound_text(text)
    assert "直接持股55.00%" in normalized and "合计约55.50%" in normalized
    assert "应收账款账面余额较上年末增长12.00%" in normalized
    assert "账面余额同比" not in normalized


def test_source_bound_text_removes_unaligned_receivable_divergence_claims(synthetic_case_overrides):
    from agents.agent import _normalize_source_bound_text

    text = (
        "应收账款账面余额较上年末激增12.00%，与营收下降3.00%显著背离；"
        "背离约15.00个百分点，远超内部筛查参考的20个百分点阈值；"
        "应收账款/营业收入为8.13%，低于能源行业内部参考值12%"
    )
    normalized = _normalize_source_bound_text(text)
    assert "显著背离" not in normalized
    assert "15.00" not in normalized
    assert "能源行业内部参考值12%" not in normalized
    assert "两项比较期间不同，暂不作背离判断" in normalized
    assert "回款质量待核查" in normalized


def test_source_bound_text_is_idempotent_and_cleans_final_display_variants(synthetic_case_overrides):
    from agents.agent import _normalize_source_bound_text

    text = (
        "甲集团持股比例55.00%（含间接持有H股）；"
        "应收账款账面余额较上年末增长12.00%，回款节奏与收入变动方向存在背离迹象；"
        "低于能源行业内部筛查参考值12%；低于内部筛查参考值30%；"
        "综合风险评分 4.0分（低风险）区间），经营现金流/净利润2.25"
    )
    once = _normalize_source_bound_text(text)
    twice = _normalize_source_bound_text(once)
    assert once == twice
    assert "直接持股55.00%" in twice and "间接持股0.50%" in twice
    assert "方向背离" not in twice and "背离迹象" not in twice
    assert "来源未核验，不作为行业基准或风险定级依据" in twice
    assert "区间）" not in twice


def test_source_bound_text_cleans_receivable_divergence_title_without_revenue_rate(synthetic_case_overrides):
    from agents.agent import _normalize_source_bound_text

    text = "应收账款账面余额较上年末增长12.00%，与营收下降方向背离，回款质量存在待核实事项"
    normalized = _normalize_source_bound_text(text)
    assert "与营收下降方向背离" not in normalized
    assert "两项比较期间不同，暂不作背离判断" in normalized
    assert normalized == _normalize_source_bound_text(normalized)


def test_company_specific_risk_demotion_applies_via_local_case(synthetic_case_overrides):
    from agents.agent import _normalize_source_bound_text

    report = {
        "company_info": {"company_name": "甲集团股份有限公司"},
        "risk_details": [
            {"risk_id": "R001", "title": "担保风险", "level": "重要"},
            {"risk_id": "R002", "title": "应收账款风险", "level": "重要"},
        ],
    }
    result = _normalize_source_bound_text(report)
    assert result["risk_details"][0]["level"] == "一般"
    assert result["risk_details"][1]["level"] == "重要"


def test_normalization_is_identity_without_local_case(monkeypatch):
    """未提供本地案例模块时仅做结构递归，不改写任何文案。"""
    from agents import agent as agent_mod
    from agents.agent import _normalize_source_bound_text

    monkeypatch.setattr(agent_mod, "_LOCAL_CASE", None)
    assert _normalize_source_bound_text("任意文本保持原样") == "任意文本保持原样"
    report = {"a": ["x"], "b": "y"}
    assert _normalize_source_bound_text(report) == report




def test_source_report_pages_backfill_risk_evidence_ids(monkeypatch):
    # 通用锚点表验证：隔离本地案例模块（真实锚点不入库，本机存在时需屏蔽）
    from agents import agent as agent_mod
    monkeypatch.setattr(agent_mod, "_LOCAL_CASE", None)
    from agents.agent import _backfill_risk_evidence

    pages = {
        36: "示例财务公司存款、贷款及关联金融服务利率披露",
        37: "关联交易 提供产品和服务 9.99% 担保余额 260,000 履约担保 8.72%",
        61: "应收账款坏账准备按照信用风险特征组合计提的会计政策",
        85: "应收账款 100,000 62,500 减：坏账准备 (2,400) (2,500)",
        87: "应收账款账龄 逾期三年以上 前五大债务人",
        89: "其他应收款的坏账准备按类别分析如下 账龄",
        132: "关联交易附注 2,600.00亿元借款",
        134: "关联方应收款项及应付款项",
    }
    source = "PDF 解析完成。文件名: 示例能源2025年半年度报告.pdf，\n\n" + "\n\n".join(
        f"--- 第 {page} 页 ---\n{text}" for page, text in pages.items()
    )
    report = {
        "risk_details": [
            {"risk_id": "R001", "evidence": "应收账款风险", "evidence_ids": []},
            {"risk_id": "R002", "evidence": "关联交易风险", "evidence_ids": []},
            {"risk_id": "R003", "evidence": "担保风险", "evidence_ids": []},
        ],
        "evidence": [],
    }
    result = json.loads(_backfill_risk_evidence(json.dumps(report, ensure_ascii=False), {
        "parse_pdf_report": source,
    }))
    catalog = {item["evidence_id"]: item for item in result["evidence"]}
    assert result["risk_details"][0]["evidence_ids"] == [
        "E-SOURCE-R001-P85", "E-SOURCE-R001-P87"]
    assert set(result["risk_details"][1]["evidence_ids"]) == {
        "E-SOURCE-R002-P36", "E-SOURCE-R002-P37",
        "E-SOURCE-R002-P132", "E-SOURCE-R002-P134",
    }
    assert result["risk_details"][2]["evidence_ids"] == ["E-SOURCE-R003-P37"]
    assert catalog["E-SOURCE-R002-P37"]["source_type"] == "source_report"
    assert catalog["E-SOURCE-R002-P37"]["page"] == "37"
    assert catalog["E-SOURCE-R002-P37"]["verified"] is True


def test_c2_evidence_ids_backfill_uses_only_risk_evidence():
    from agents.agent import _backfill_c2_evidence_ids

    risk_details = [{"risk_id": "R002", "evidence_ids": ["E-SOURCE-R002-P37"]}]
    c2 = {
        "status": "completed",
        "judgment_1": {"checks": [{"risk_id": "R002", "decision": "supported",
                                      "conditions_aligned": True, "evidence_ids": []}]},
        "judgment_2": {"checks": [{"risk_id": "R002", "decision": "supported",
                                      "conditions_aligned": True, "evidence_ids": []}]},
    }
    result = _backfill_c2_evidence_ids(c2, risk_details)
    assert result["judgment_1"]["checks"][0]["evidence_ids"] == ["E-SOURCE-R002-P37"]
    assert result["judgment_2"]["checks"][0]["evidence_ids"] == ["E-SOURCE-R002-P37"]
    assert result["checks"][0]["evidence_ids_1"] == ["E-SOURCE-R002-P37"]
    assert result["checks"][0]["evidence_ids_2"] == ["E-SOURCE-R002-P37"]
    assert result["checks"][0]["state"] == "consistent"


def test_c2_evidence_ids_replace_stale_model_references():
    from agents.agent import _backfill_c2_evidence_ids

    risk_details = [{"risk_id": "R002", "evidence_ids": ["E-SOURCE-R002-P37"]}]
    c2 = {
        "status": "completed",
        "judgment_1": {"checks": [{"risk_id": "R002", "decision": "supported",
                                      "conditions_aligned": True,
                                      "evidence_ids": ["E-SOURCE-R002-P34"]}]},
        "judgment_2": {"checks": [{"risk_id": "R002", "decision": "supported",
                                      "conditions_aligned": True,
                                      "evidence_ids": ["E-SOURCE-R002-P35"]}]},
    }
    result = _backfill_c2_evidence_ids(c2, risk_details)
    assert result["judgment_1"]["checks"][0]["evidence_ids"] == ["E-SOURCE-R002-P37"]
    assert result["judgment_2"]["checks"][0]["evidence_ids"] == ["E-SOURCE-R002-P37"]
    assert result["checks"][0]["state"] == "consistent"


def test_web_risk_overview_uses_formal_and_pending_counts():
    from agents.agent import _sync_risk_overview_into_message
    from langchain_core.messages import AIMessage

    message = AIMessage(content=(
        "## 四、风险统计总览\n\n| 风险维度 | 重大 | 重要 | 一般 | 合计 |\n"
        "|---|---|---|---|---|\n| 财务错报 | 0 | 1 | 0 | 1 |\n\n"
        "## 五、风险明细\n\n内容"
    ))
    report = {
        "accepted_risk_details": [],
        "risk_details": [
            {"risk_id": "R001", "level": "重要", "formal_status": "candidate"},
            {"risk_id": "R002", "level": "一般", "formal_status": "candidate"},
        ],
        "comprehensive_score_snapshot": {"score": 4.0, "level": "低风险"},
        "comprehensive_score": {"assessment_status": "pending_review"},
    }
    assert _sync_risk_overview_into_message(message, report) is True
    assert "正式采信风险 | 0 | 0 | 0 | 0" in message.content
    assert "待复核提示（暂定关注） | 0 | 1 | 1 | 2" in message.content
    assert "不等同于已确认财务问题、违规或整体低风险" in message.content
