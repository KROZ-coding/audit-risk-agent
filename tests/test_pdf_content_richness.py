"""PDF 报告内容丰富化与综合研判一致性回归测试。

覆盖本次修复的四条链路：
1. 综合评分兜底前移 + 回写 tool_results：LLM 未调用评分工具时，PDF 导出
   仍能拿到评分数据（综合评分解读章不再缺失，与前端评分卡一致）；
2. 辩论复核意见回写台账：综合汇总 PDF 渲染「审计合伙人复核意见」章，
   与前端消息同源；
3. compare_multi_year / identify_audit_opinion 结果透传 PDF：
   财务报告「多年指标趋势分析」章、合规报告「审计意见识别与风险信号」章；
4. 数据来源与完整性说明：三份报告均含说明章，缺失数据源标注原因，
   不再静默消失；LLM 调用导出但参数不全时系统兜底重导（双保险）。
"""
import json

import pytest

from tools.pdf_export import _export_pdf_impl, _review_conclusion_body, _strip_structured_review_json


def _rich_report() -> dict:
    """构造一份数据完整、可渲染全部新增章节的风险台账。"""
    return {
        "company_info": {
            "company_name": "内容增强测试公司",
            "stock_code": "600001",
            "report_year": "2025",
            "industry": "制造业",
            "audit_opinion": "保留意见",
        },
        "risk_summary": {"total_risks": 1, "major_risks": 1, "important_risks": 0,
                         "general_risks": 0,
                         "risk_dimensions": {"financial_misstatement": 1}},
        "risk_details": [{
            "risk_id": "R001",
            "dimension": "财务错报",
            "title": "扣非净利润连续为负，主业盈利能力存疑",
            "level": "重大",
            "confidence": 0.85,
            "evidence": "经营现金流/净利润<0.5，连续2期。",
            "data_analysis": "营收与现金流背离，存在提前确认收入嫌疑",
            "regulatory_basis": "《审计准则1211号》第二十条",
            "case_reference": "某上市公司财务造假案",
            "audit_suggestion": "前5大应收100%函证+替代测试",
            "reasoning_chain": [
                {"step": "数据发现", "detail": "OCF/NP=0.3 < 阈值0.5"},
                {"step": "风险判定", "detail": "重大错报风险嫌疑"},
            ],
        }],
        "overall_assessment": "存在需关注的异常迹象，需结合人工专业判断进行复核确认。",
        "review_conclusion": "\n\n---\n### 🔍 审计合伙人复核意见\n复核认为：重大风险等级成立，建议扩大函证范围。",
    }


def _financial_json() -> str:
    return json.dumps({
        "indicators": {"current_ratio": 0.8, "gross_margin_pct": 21.5,
                       "debt_to_asset_ratio_pct": 68.0},
        "alerts": ["连续两年净利润为负，存在持续经营风险"],
    }, ensure_ascii=False)


def _disclosure_json() -> str:
    return json.dumps({
        "compliance_score": 72.0, "risk_score": 45.0, "checked_items": 10,
        "passed_items": 7, "issues": ["重大事项延迟披露"], "sections_missing": ["公司治理"],
        "audit_opinion": "保留意见",
    }, ensure_ascii=False)


def _score_json() -> str:
    return json.dumps({
        "score": 62.5, "level": "高", "level_key": "high", "base_score": 50.0,
        "escalation": 12.5,
        "breakdown": {"financial": 55.0, "disclosure": 40.0, "validation": 30.0},
        "escalation_reasons": ["Altman Z-Score 落入困境区"],
        "summary": "多项指标偏离行业基准，需重点核查。",
    }, ensure_ascii=False)


def _multi_year_json() -> str:
    return json.dumps({
        "years_analyzed": ["2023", "2024", "2025"],
        "year_count": 3,
        "indicators_by_year": {"2023": {"revenue": 100}, "2024": {"revenue": 90},
                               "2025": {"revenue": 80}},
        "trends": {"营业收入": "持续下降"},
        "timeseries": {"xAxis": ["2023", "2024", "2025"],
                       "series": [{"name": "营业收入", "data": [100, 90, 80]}]},
        "trend_alerts": ["【趋势风险】营业收入连续下滑"],
        "alert_count": 1,
    }, ensure_ascii=False)


def _audit_opinion_json() -> str:
    return json.dumps({
        "audit_opinion": {"identified": True, "opinion_type": "保留意见",
                          "is_standard_opinion": False, "credibility_impact": "高",
                          "risk_level": "重要", "meaning": "整体公允但存在具体事项影响",
                          "implication": "被保留项目数据不可直接采信",
                          "evidence_excerpt": "形成保留意见的基础：存货盘点受限。"},
        "going_concern": {"flagged": True, "signal": "审计报告提示与持续经营相关的重大不确定性",
                          "risk_level": "重大", "implication": "须直接上调持续经营维度风险等级。"},
        "key_audit_matters": {"found": True,
                              "matters": [{"matter": "存货", "risk_direction": "存在减值风险"}],
                              "note": "关键审计事项通常揭示最高风险领域。"},
        "auditor_change": {"flagged": False},
        "linkage_alerts": [],
        "reference": "判定规则依据 knowledge_base/审计意见类型库.txt",
    }, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _stub_upload(monkeypatch):
    """屏蔽真实落盘，收集真实生成文件路径供 pypdf 提取。"""
    saved = []

    def _fake(path, dest, mime):
        saved.append(path)
        return f"/local_storage/{dest}"

    monkeypatch.setattr("local_storage.upload_file_to_storage", _fake)
    return saved


class _RecordingTool:
    """带 .invoke 的 mock 工具（_post_process 兜底导出用 tool_obj.invoke 调用）。"""

    def __init__(self, url="ok"):
        self.calls = []
        self._url = url

    def invoke(self, args):
        self.calls.append(args)
        return self._url


class TestRichChaptersRender:
    """新增章节（多年趋势/审计意见/复核意见/数据源说明）真实渲染。"""

    @pytest.fixture(autouse=True)
    def _no_chart(self, monkeypatch):
        monkeypatch.setattr("tools.pdf_export._embed_chart", lambda *a, **k: None)

    def _export_all(self, report, extra=None):
        args = {
            "financial_indicators_json": _financial_json(),
            "disclosure_check_json": _disclosure_json(),
            "comprehensive_score_json": _score_json(),
            "risk_models_json": "",
            "validation_json": "",
            "compare_multi_year_json": _multi_year_json(),
            "audit_opinion_json": _audit_opinion_json(),
        }
        if extra:
            args.update(extra)
        return _export_pdf_impl(json.dumps(report, ensure_ascii=False), **args)

    def _text(self, saved, suffix):
        import pypdf
        path = [p for p in saved if f"_{suffix}报告_" in p][0]
        return "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(path).pages)

    def test_multi_year_chapter_in_financial_report(self, _stub_upload):
        """compare_multi_year 结果透传 → 财务报告含「多年指标趋势分析」章与预警。"""
        result = self._export_all(_rich_report())
        assert result.count("已生成") == 3
        text = self._text(_stub_upload, "财务健康诊断")
        assert "多年指标趋势分析" in text
        assert "营业收入" in text and "持续下降" in text
        assert "趋势性风险预警" in text and "营业收入连续下滑" in text

    def test_audit_opinion_chapter_in_compliance_report(self, _stub_upload):
        """identify_audit_opinion 结果透传 → 合规报告含「审计意见识别与风险信号」章。"""
        self._export_all(_rich_report())
        text = self._text(_stub_upload, "合规与信息披露")
        assert "审计意见识别与风险信号" in text
        assert "保留意见" in text and "非标准" in text
        assert "持续经营重大不确定性信号" in text
        assert "关键审计事项" in text and "存货" in text

    def test_review_conclusion_chapter_in_synthesis_report(self, _stub_upload):
        """复核意见回写台账 → 综合汇总报告含「审计合伙人复核意见」章（与前端同源）。"""
        self._export_all(_rich_report())
        text = self._text(_stub_upload, "综合汇总")
        assert "审计合伙人复核意见" in text
        assert "扩大函证范围" in text

    def test_data_source_note_present_in_all_reports(self, _stub_upload):
        """三份报告均含「数据来源与完整性说明」章；缺失数据源标注原因。"""
        self._export_all(_rich_report(),
                         extra={"compare_multi_year_json": "", "audit_opinion_json": ""})
        for suffix in ("财务健康诊断", "合规与信息披露", "综合汇总"):
            text = self._text(_stub_upload, suffix)
            assert "数据来源与完整性说明" in text
        # 缺多年对比 → 说明章在「未获取」状态列之外单列原因。
        # 原因不得再带「未获取：」前缀（与状态列重复，实测缺陷）。
        fin_text = self._text(_stub_upload, "财务健康诊断")
        assert "未提供多年财务数据" in fin_text
        assert "未获取：未提供多年财务数据" not in fin_text
        # 缺多年对比时财务报告不出现该章（条件渲染），说明章替代可见
        assert "多年指标趋势分析" not in fin_text

    def test_called_but_incomplete_source_is_not_reported_as_not_acquired(self, _stub_upload):
        """工具返回有限检查时，说明应显示“不完整/有限检查”，不能伪装成未获取。"""
        report = _rich_report()
        report["report_snapshot"] = {
            "company": report["company_info"],
            "risks": {"formal": [], "pending": []},
            "data_quality": {"incomplete_metrics": True},
            "financial": {
                "metric_results": [{"metric_id": "gross_margin_pct", "status": "calculated"}],
            },
            "multi_year": {"years_analyzed": ["2025H1"], "status": "limited_check"},
        }
        self._export_all(report, extra={
            # 传入值仅作兼容参数；存在最终快照时，导出器应以快照中的 financial/multi_year 为准。
            "financial_indicators_json": "{}",
            "compare_multi_year_json": "{}",
        })
        text = self._text(_stub_upload, "财务健康诊断")
        compact = text.replace("\n", "")
        assert "数据源不完整，仅供参考" in compact
        assert "已获取，但部分结果不完整或仅作有限检查，请人工复核" in compact
        assert "未上传年报或文本过短" not in text

    def test_score_chapter_renders(self, _stub_upload):
        """提供评分数据时，综合汇总报告渲染「综合评分解读」章（KPI/分解表/抬升理由）。"""
        self._export_all(_rich_report())
        text = self._text(_stub_upload, "综合汇总")
        assert "综合评分解读" in text
        assert "综合风险评分" in text and "62.5" in text
        assert "风险分抬升理由" in text and "Altman Z-Score" in text

    def test_industry_benchmark_chapter_always_renders(self, _stub_upload):
        """无经核验基准时保留章节并解释限制，不将内部参考值当行业均值。"""
        self._export_all(_rich_report())
        text = self._text(_stub_upload, "综合汇总")
        assert "行业基准对比" in text
        assert "本次未取得经来源、统计期间、样本和可比性核验的行业基准" in text

    def test_structured_review_json_is_rendered_as_summary(self, _stub_upload):
        """C2 原始 JSON 保留在台账，综合 PDF 只展示可读摘要表。"""
        semantic = {
            "overall_status": "pending_review",
            "judgment_1": {"checks": [{"risk_id": "R001", "decision": "supported",
                                         "conditions_aligned": True,
                                         "evidence_ids": ["E1"]}]},
            "judgment_2": {"checks": [{"risk_id": "R001", "decision": "pending",
                                         "conditions_aligned": False,
                                         "evidence_ids": []}]},
            "checks": [{"risk_id": "R001", "state": "disputed",
                         "decision_1": "supported", "decision_2": "pending",
                         "evidence_ids_1": ["E1"], "evidence_ids_2": []}],
        }
        raw = json.dumps(semantic, ensure_ascii=False)
        report = _rich_report()
        report["semantic_review"] = semantic
        report["review_conclusion"] = "复核建议扩大函证范围。\n【裁定JSON】" + raw
        self._export_all(report)
        text = self._text(_stub_upload, "综合汇总")
        assert "C2关键语义复核摘要" in text
        assert "第一次判断" in text and "第二次判断" in text
        assert "待定" in text and "存在分歧" in text
        assert "judgment_1" not in text
        assert "conditions_aligned" not in text
        assert "###" not in text and "**" not in text
        assert "---" not in text
        assert "扩大函证范围" in text

    def test_structured_review_json_strip_handles_nested_objects(self):
        raw = ('结论前文 {"judgment_1":{"checks":[{"risk_id":"R1",'
               '"reason":"含 { 大括号"}]},"judgment_2":{},'
               '"overall_status":"pending_review"} 结论后')
        cleaned = _strip_structured_review_json(raw)
        assert "judgment_1" not in cleaned
        assert "结论前文" in cleaned and "结论后" in cleaned


class TestPostProcessConsistency:
    """_post_process 层的一致性修复（评分回写 / 双保险重导）。"""

    def _wrapper_messages(self, with_llm_export=False, with_score_tool=False):
        from langchain_core.messages import AIMessage, ToolMessage
        msgs = [AIMessage(content="", tool_calls=[], id="a0")]
        ledger_results = {}
        if with_score_tool:
            ledger_results["calculate_comprehensive_score"] = _score_json()
        if with_llm_export:
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "export_pdf_report", "args": {"risk_report_json": "{}"}, "id": "c1"},
            ], id="a1"))
            msgs.append(ToolMessage(content="PDF已生成", name="export_pdf_report",
                                    tool_call_id="c1", id="tm1"))
        final = (f"分析完成\n\n```json\n{json.dumps(_rich_report(), ensure_ascii=False)}\n```")
        msgs.append(AIMessage(content=final, id="a_final"))
        return msgs, ledger_results

    def test_score_backfill_reaches_pdf_export(self, monkeypatch):
        """LLM 未调用评分工具 → 兜底评分回写 tool_results → PDF 导出拿到评分数据。"""
        import agents.agent as agent_module
        from agents.agent import _AgentWrapper

        pdf_mock = _RecordingTool()
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_risk_heatmap", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_radar_chart", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_trend_chart", _RecordingTool())

        msgs, ledger_results = self._wrapper_messages()
        result = {"messages": msgs,
                  "tool_ledger": {"seq": [], "results": ledger_results, "seen": []}}
        wrapper = object.__new__(_AgentWrapper)
        wrapper._module = None
        wrapper._post_process(result)

        assert len(pdf_mock.calls) == 1
        assert "综合风险评分生成失败" not in str(pdf_mock.calls[0].get("comprehensive_score_json"))
        assert '"score"' in str(pdf_mock.calls[0].get("comprehensive_score_json"))

    def test_llm_export_with_incomplete_args_triggers_backfill(self, monkeypatch):
        """双保险：LLM 调用导出但参数不全（无专项入参）→ 系统仍兜底重导。"""
        import agents.agent as agent_module
        from agents.agent import _AgentWrapper

        pdf_mock = _RecordingTool()
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_risk_heatmap", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_radar_chart", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_trend_chart", _RecordingTool())

        msgs, ledger_results = self._wrapper_messages(with_llm_export=True)
        result = {"messages": msgs,
                  "tool_ledger": {"seq": ["export_pdf_report"],
                                  "results": ledger_results, "seen": []}}
        wrapper = object.__new__(_AgentWrapper)
        wrapper._module = None
        wrapper._post_process(result)

        # LLM 调用参数不全（只有空台账）→ 系统兜底重导，用完整数据生成
        assert len(pdf_mock.calls) == 1
        assert pdf_mock.calls[0]["risk_report_json"]  # 兜底版台账非空（来自消息提取）

    def test_llm_export_complete_args_skips_backfill(self, monkeypatch):
        """双保险：LLM 调用导出且参数完整 → 系统不重复导出（保持旧行为）。"""
        import agents.agent as agent_module
        from agents.agent import _AgentWrapper

        pdf_mock = _RecordingTool()
        monkeypatch.setattr(agent_module, "REVIEW_ENABLED", False)
        monkeypatch.setattr(agent_module, "export_pdf_report", pdf_mock)
        monkeypatch.setattr(agent_module, "export_excel_report", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_risk_heatmap", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_radar_chart", _RecordingTool())
        monkeypatch.setattr(agent_module, "generate_trend_chart", _RecordingTool())

        from langchain_core.messages import AIMessage, ToolMessage
        msgs = [AIMessage(content="", tool_calls=[
            {"name": "export_pdf_report",
             "args": {"risk_report_json": json.dumps(_rich_report(), ensure_ascii=False),
                      "financial_indicators_json": _financial_json()},
             "id": "c1"},
        ], id="a1")]
        msgs.append(ToolMessage(content="PDF已生成", name="export_pdf_report",
                                tool_call_id="c1", id="tm1"))
        final = (f"分析完成\n\n```json\n{json.dumps(_rich_report(), ensure_ascii=False)}\n```")
        msgs.append(AIMessage(content=final, id="a_final"))
        result = {"messages": msgs,
                  "tool_ledger": {"seq": ["export_pdf_report"],
                                  "results": {}, "seen": []}}
        wrapper = object.__new__(_AgentWrapper)
        wrapper._module = None
        wrapper._post_process(result)

        assert pdf_mock.calls == []

class TestSystemFactsInjection:
    """S1 事实层注入：系统量化事实从确定性工具结果回填。"""

    def test_reliability_risk_injects_reconciliation(self):
        from tools.pdf_export import _system_facts_text
        risk = {"risk_id": "R003", "dimension": "数据可靠性风险",
                "title": "未分配利润勾稽不一致", "evidence": "未分配利润变动38,122百万元 vs 合并净利润-分红47,911百万元"}
        val = json.dumps({"data_validation": {"all_checks": [
            {"check": "未分配利润一致性", "passed": None, "difference_pct": "20.43%",
             "message": "未提供归母净利润，仅作口径提示"}]}})
        text = _system_facts_text(risk, validation=val)
        assert "勾稽校验·未分配利润一致性" in text
        assert "20.43%" in text
        assert "口径提示" in text

    def test_financial_risk_injects_alerts(self):
        from tools.pdf_export import _system_facts_text
        risk = {"risk_id": "R001", "dimension": "财务错报风险", "title": "应收激增", "evidence": "应收+67%"}
        fin = json.dumps({"alerts": ["应收账款增速显著高于营收增速"], "indicators": {}})
        text = _system_facts_text(risk, financial=fin)
        assert "财务预警" in text and "应收账款" in text

    def test_no_matching_facts_returns_empty(self):
        from tools.pdf_export import _system_facts_text
        risk = {"risk_id": "R009", "dimension": "市场风险", "title": "油价敏感性", "evidence": "无"}
        assert _system_facts_text(risk, validation="{}") == ""
