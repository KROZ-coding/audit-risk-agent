"""审计产物发布链路回归（方案 §7 统一快照 + §11 审计数据准确性）。

覆盖目标：
- _apply_review_gates 统一快照：snapshot_id 由分析批次派生，旧批次/旧代码产物
  不得伪装成当前结果；run_id 存在则取 run_id，缺失时以 snap- 前缀兜底；
- report_metadata 把 snapshot_id/validation_status/source_hash 一并带出，
  供网页元数据卡渲染，确保"网页/PDF/Excel/API 同源同版本"；
- _build_final_report_from_messages：产物清单按 key 匹配真实文件，单 PDF 可访问
  → partial，全失败 → failed（失败不静默）；每条 manifest 带 analysis_id 与
  rule_version；
- 旧批次隔离：manifest 的 analysis_id 必须等于当前批次 run_id。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as main_mod
from agents.agent import _apply_review_gates
from core.result_contract import RULE_VERSION
from core.report_snapshot import build_final_snapshot


def _ledger(run_id="run-abc1", source_hash="sha-xyz"):
    return {
        "analysis_id": run_id,
        "company_info": {
            "run_id": run_id,
            "company_name": "中国石油天然气股份有限公司",
            "stock_code": "601857",
            "report_year": "2025（半年度）",
            "industry": "能源",
            "audit_opinion": "未经审计（半年度报告）",
            "source_file_sha256": source_hash,
        },
        "data_validation": {"validation_result": "通过"},
        "risk_details": [],
        "report_snapshot": {
            "snapshot_id": run_id,
            "validation_status": "通过",
            "source_hash": source_hash,
        },
        "review_gate": {"status": "not_run"},
    }


def _ai_message(ledger, pdf_path):
    text = "以下为审计报告：\n" + json.dumps(ledger, ensure_ascii=False)
    if pdf_path:
        text += f"\n📎 PDF报告: {pdf_path}"
    return {"type": "ai", "content": text, "tool_calls": None}


def _build(accessible=True, pdf_path="/local_storage/reports/中国石油_财务健康诊断报告.pdf"):
    """module-level builder attached to both test classes."""
    main_mod._artifact_is_accessible = lambda p: accessible
    messages = [_ai_message(_ledger(), pdf_path)]
    return main_mod.GraphService()._build_final_report_from_messages(messages)


class TestReviewGateSnapshot:
    """统一快照：所有端从同一份快照取数，批次/版本/校验状态一并固化。"""

    def test_snapshot_id_derived_from_run_id(self):
        report = {
            "company_info": {"run_id": "run-abc1", "source_file_sha256": "sha-xyz"},
            "data_validation": {"validation_result": "通过"},
            "facts": [{"field": "revenue_current"}],
            "metric_results": [{"metric_id": "gross_margin_pct"}],
            "risk_details": [{"risk_id": "R001"}],
            "accepted_risk_details": [],
            "pending_items": [],
            "comprehensive_score": {"score": 60},
        }
        _apply_review_gates(report, {}, False)
        assert report["snapshot_id"] == "run-abc1"
        snap = report["report_snapshot"]
        assert snap["snapshot_id"] == "run-abc1"
        assert snap["analysis_id"] == "run-abc1"
        assert snap["source_hash"] == "sha-xyz"
        assert snap["validation_status"] == "通过"
        assert report["result_schema_version"] == "1.0"
        assert report["rule_version"] == RULE_VERSION
        # 快照必须携带渲染所需的结构化数据，而不是让各端重新拼接
        assert snap["facts"] == report["facts"]
        assert snap["metric_results"] == report["metric_results"]
        assert snap["risk_details"] == report["risk_details"]
        assert snap["comprehensive_score"] == {"score": 60}

    def test_snapshot_id_fallback_prefix_when_no_run_id(self):
        report = {"company_info": {}}
        _apply_review_gates(report, {}, False)
        assert str(report["snapshot_id"] or "").startswith("snap-")

    def test_snapshot_preserved_when_already_present(self):
        report = {
            "company_info": {"run_id": "run-abc1"},
            "report_snapshot": {"snapshot_id": "run-abc1", "facts": "kept"},
        }
        _apply_review_gates(report, {}, False)
        assert report["report_snapshot"]["snapshot_id"] == "run-abc1"
        assert report["report_snapshot"]["facts"] == "kept"


class TestPublicationManifest:
    """产物清单：key 匹配、partial/failed 表达、失败不静默。"""

    def test_single_pdf_accessible_is_partial(self):
        out = _build(accessible=True)
        assert out["task_status"] == "partial"
        successes = [m for m in out["artifact_manifest"] if m["status"] == "success"]
        failures = [m for m in out["artifact_manifest"] if m["status"] == "failed"]
        assert len(successes) == 2  # PDF 与 JSON 结构化台账均可访问
        pdf = successes[0]
        assert pdf["kind"] == "pdf"
        assert pdf["analysis_id"] == "run-abc1"
        assert pdf["rule_version"] == RULE_VERSION
        # 其余期望项必须逐项记录失败，不得静默丢弃
        assert len(failures) == 6
        assert all(m["status"] == "failed" and m["error"] for m in failures)

    def test_single_pdf_accessible_is_partial(self):
        out = _build(accessible=True)
        assert out["task_status"] == "partial"
        successes = [m for m in out["artifact_manifest"] if m["status"] == "success"]
        failures = [m for m in out["artifact_manifest"] if m["status"] == "failed"]
        assert len(successes) == 2  # PDF 与 JSON 结构化台账均可访问
        pdf = successes[0]
        assert pdf["kind"] == "pdf"
        assert pdf["analysis_id"] == "run-abc1"
        assert pdf["rule_version"] == RULE_VERSION
        # 其余期望项必须逐项记录失败，不得静默丢弃
        assert len(failures) == 6
        assert all(m["status"] == "failed" and m["error"] for m in failures)

    def test_timestamp_subdir_pdf_is_partial(self):
        """批次隔离后 PDF 落在 <stamp>/reports/ 子路径：同样按 key 匹配为 partial。"""
        out = _build(accessible=True,
                     pdf_path="/local_storage/20260911_101530/reports/中国石油_财务健康诊断报告.pdf")
        assert out["task_status"] == "partial"
        successes = [m for m in out["artifact_manifest"] if m["status"] == "success"]
        assert len(successes) == 2
        assert successes[0]["kind"] == "pdf"
        assert successes[0]["analysis_id"] == "run-abc1"

    def test_all_failed_is_failed_not_silent(self):
        out = _build(accessible=False)
        assert out["task_status"] == "failed"
        assert len(out["artifact_manifest"]) == 8
        assert all(m["status"] == "failed" for m in out["artifact_manifest"])
        assert all(m["error"] for m in out["artifact_manifest"])

    def test_manifest_carries_run_id_rule_version(self):
        out = _build(accessible=True)
        for m in out["artifact_manifest"]:
            assert m["analysis_id"] == "run-abc1"
            assert m["rule_version"] == RULE_VERSION
            assert m["data_version"] == RULE_VERSION

    def test_metadata_propagates_snapshot_fields(self):
        out = _build(accessible=True)
        meta = out["report_metadata"]
        assert meta["snapshot_id"] == "run-abc1"
        assert meta["validation_status"] == "通过"
        assert meta["source_hash"] == "sha-xyz"
        assert meta["analysis_id"] == "run-abc1"
        assert meta["rule_version"] == RULE_VERSION

    def test_manifest_is_written_back_to_final_snapshot(self):
        out = _build(accessible=True)
        assert out["report_snapshot"]["artifact_manifest"] == out["artifact_manifest"]
        assert out["risk_ledger"]["artifact_manifest"] == out["artifact_manifest"]
        assert {item["snapshot_id"] for item in out["artifact_manifest"]} == {out["snapshot_id"]}

    def test_manifest_is_written_back_to_persisted_ai_text(self):
        out = _build(accessible=True)
        from agents.agent import _extract_risk_json

        raw = _extract_risk_json(out["ai_text"])
        assert raw
        persisted = json.loads(raw)
        assert persisted["artifact_manifest"] == out["artifact_manifest"]
        assert persisted["report_snapshot"]["artifact_manifest"] == out["artifact_manifest"]

    def test_risk_json_extractor_handles_final_snapshot_wrapper(self):
        """真实终局块先放 report_snapshot 时，旧兼容提取仍能取到根台账。"""
        from agents.agent import _extract_risk_json

        legacy = {
            "company_info": {"company_name": "快照包装公司"},
            "risk_details": [{"risk_id": "R001"}],
            "artifact_manifest": [{"artifact_id": "artifact-001", "status": "success"}],
        }
        wrapper = {
            "report_snapshot": {"snapshot_id": "snap-wrapper", "risks": {"formal": []}},
            **legacy,
        }
        text = "自然语言说明\n```json\n" + json.dumps(wrapper, ensure_ascii=False) + "\n```"
        extracted = _extract_risk_json(text)
        assert extracted
        assert json.loads(extracted) == wrapper

    def test_complete_snapshot_is_authoritative_without_ai_json(self):
        """新链路不应因可读正文没有 JSON 而丢失结构化报告。"""
        snapshot = build_final_snapshot({
            "analysis_id": "run-snapshot",
            "company_info": {
                "company_name": "快照事实源公司",
                "report_year": "2025",
                "report_period": "2025年半年度",
                "accounting_standard": "中国企业会计准则",
            },
            "risk_details": [],
            "artifact_expectations": [
                {"key": "pdf_synthesis", "kind": "pdf", "label": "综合汇总报告"},
            ],
        }, {})
        main_mod._artifact_is_accessible = lambda _path: True
        out = main_mod.GraphService()._build_final_report_from_messages([
            {"type": "ai", "content": "报告已生成：/local_storage/reports/综合汇总报告.pdf"},
        ], final_snapshot=snapshot)
        assert out["report_metadata"]["company_name"] == "快照事实源公司"
        assert out["report_metadata"]["report_period"] == "2025年半年度"
        assert out["report_metadata"]["analysis_id"] == "run-snapshot"
        assert out["risk_ledger"]["company_info"]["company_name"] == "快照事实源公司"
        assert len(out["artifact_manifest"]) == 2
        assert any(item["kind"] == "json" for item in out["artifact_manifest"])
        assert out["artifact_manifest"][0]["analysis_id"] == "run-snapshot"

    def test_metadata_includes_report_period_and_accounting_standard(self):
        ledger = _ledger()
        ledger["company_info"]["report_period"] = "2025年半年度"
        ledger["company_info"]["accounting_standard"] = "中国企业会计准则"
        out = main_mod.GraphService()._build_final_report_from_messages([_ai_message(ledger, "")])
        assert out["report_metadata"]["report_period"] == "2025年半年度"
        assert out["report_metadata"]["accounting_standard"] == "中国企业会计准则"


class TestSnapshotProvenance:
    def test_source_defaults_fill_fact_metric_and_evidence(self):
        report = {
            "company_info": {"run_id": "run-1", "report_period": "2025年半年度"},
            "source": {"document_name": "sample.pdf", "source_hash": "sha-1", "page_count": 3},
            "facts": [{"fact_id": "F-1", "field": "operating_cashflow", "value": 10,
                       "unit": "百万元", "period": "2024-06-30", "scope": "合并"}],
            "metric_results": [{"metric_id": "M-1", "value": 1, "unit": "倍",
                                "period": "2025年半年度", "scope": "合并"}],
            "evidence": [{"evidence_id": "E-1", "excerpt": "原文", "verified": True,
                           "status": "verified"}],
            "risk_details": [],
        }
        snap = build_final_snapshot(report, {})
        assert snap["facts"][0]["source_document"] == "sample.pdf"
        assert snap["facts"][0]["source_hash"] == "sha-1"
        assert snap["facts"][0]["period"] == "2025-01-01至2025-06-30"
        assert snap["metrics"][0]["source_document"] == "sample.pdf"
        assert snap["evidence"][0]["source_hash"] == "sha-1"

    def test_flow_facts_are_periods_and_derive_two_comparable_periods(self):
        source = {"document_name": "sample.pdf", "source_hash": "sha-1"}
        facts = []
        for field, value, period in (
            ("revenue_previous", 120, "2024-06-30"),
            ("revenue_current", 100, "2025-06-30"),
            ("net_profit_previous", 12, "2024-06-30"),
            ("net_profit_current", 10, "2025-06-30"),
        ):
            facts.append({"fact_id": f"F-{field}", "field": field, "value": value,
                          "unit": "人民币百万元", "period": period, "scope": "合并",
                          "source_status": "verified"})
        snap = build_final_snapshot({
            "company_info": {"report_period": "2025年半年度", "amount_unit": "人民币百万元"},
            "source": source,
            "facts": facts,
            "risk_details": [],
        }, {})
        normalized = {item["field"]: item for item in snap["facts"]}
        assert normalized["revenue_current"]["period_type"] == "flow_period"
        assert normalized["revenue_current"]["period"] == "2025-01-01至2025-06-30"
        assert normalized["revenue_previous"]["period"] == "2024-01-01至2024-06-30"
        assert snap["multi_year"]["status"] == "derived_comparable_periods"
        assert snap["multi_year"]["years_analyzed"] == [
            "2024-01-01至2024-06-30", "2025-01-01至2025-06-30"]
        assert snap["multi_year"]["indicators_by_year"]["2025-01-01至2025-06-30"]["revenue"] == 100

    def test_score_nested_records_receive_source_identity(self):
        snap = build_final_snapshot({
            "company_info": {"report_period": "2025年半年度"},
            "source": {"document_name": "sample.pdf", "source_hash": "sha-1"},
            "risk_details": [],
            "comprehensive_score": {
                "score": 4,
                "facts": [{"fact_id": "F-SCORE", "field": "score", "value": 4}],
                "metric_results": [{"metric_id": "score_total", "value": 4}],
                "evidence": [{"evidence_id": "E-SCORE", "excerpt": "量化评分4分"}],
            },
        }, {})
        for key in ("facts", "metric_results", "evidence"):
            assert snap["score"][key][0]["source_document"] == "sample.pdf"
            assert snap["score"][key][0]["source_hash"] == "sha-1"


class TestBatchIsolation:
    """旧批次隔离：新批次 manifest 必须挂当前 run_id，不得混入旧批次。"""

    def test_differs_from_old_batch(self):
        out = _build(accessible=True)
        for m in out["artifact_manifest"]:
            assert m["analysis_id"] == "run-abc1"
            assert m["analysis_id"] != "run-old1"
