# -*- coding: utf-8 -*-
"""报告身份确定性识别 + 原文表格回填的回归测试（演示缺陷防复发）。

数据脱敏约定：本地真实样本（PDF 路径、身份识别与逐字段回填预期值）存放于
``scripts/_local_case/*.json``（已列入 .gitignore，不入库）。未提供本地案例时
整个文件跳过——CI 上的合成链路由 tests/test_offline_case.py 覆盖。

覆盖三个实测缺陷：
1. 综合研判模式漏识别公司名/报告期 → 产物名「未知公司」、Altman/Beneish
   误报「缺少本期期间」；
2. 抽取窗口过窄导致营业成本/商誉等关键行未进入摘录；
3. 现金流量表行（附注引用形如「59(f)」）未被回填，OCF/NP 质量比缺口。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import GraphService
from tools.financial_calculator import (calculate_financial_indicators,
                                        detect_amount_unit, normalize_financial_units)
from tools.risk_models import calculate_risk_models
from utils.report_identity import apply_company_info_fallback, extract_report_identity
from core.report_publication import finalize_snapshot_publication
from core.report_snapshot import build_final_snapshot

_CASE_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "_local_case")
_CASE_FILES = sorted(
    os.path.join(_CASE_DIR, name)
    for name in (os.listdir(_CASE_DIR) if os.path.isdir(_CASE_DIR) else [])
    if name.endswith(".json")
)

pytestmark = pytest.mark.skipif(
    not _CASE_FILES, reason="未提供本地真实案例（scripts/_local_case/），跳过")

_CASE = {}
_PDF = ""
if _CASE_FILES:
    with open(_CASE_FILES[0], encoding="utf-8") as _f:
        _CASE = json.load(_f)
    _PDF = os.path.join(os.path.dirname(__file__), "..", _CASE.get("pdf_path", ""))


@pytest.fixture(scope="module")
def report_text():
    if not _PDF or not os.path.exists(_PDF):
        pytest.skip("本地样本 PDF 不存在")
    from tools.pdf_parser import parse_pdf_report
    return parse_pdf_report.invoke({"file_path": _PDF})


class TestReportIdentity:
    def test_cover_fields_are_deterministically_extracted(self, report_text):
        ident = extract_report_identity(report_text)
        for key, expected in _CASE.get("identity_expectations", {}).items():
            if key == "audit_opinion":
                assert expected in str(ident.get(key, "")), key
            else:
                assert ident.get(key) == expected, key

    def test_fallback_fills_only_empty_fields(self, report_text):
        company = _CASE["identity_expectations"]["company_name"]
        period = _CASE["identity_expectations"]["period"]
        filled = apply_company_info_fallback({}, report_text)
        assert filled["company_name"] == company
        assert filled["period"] == period
        # 已有非空值绝不覆盖
        kept = apply_company_info_fallback({"company_name": "模型给的名字"}, report_text)
        assert kept["company_name"] == "模型给的名字"

    def test_fallback_on_none_returns_dict(self, report_text):
        assert isinstance(apply_company_info_fallback(None, report_text), dict)

    def test_fallback_corrects_generic_model_period(self, report_text):
        period = _CASE["identity_expectations"]["period"]
        year = period[:4]
        filled = apply_company_info_fallback({
            "report_year": year,
            "report_period": year,
            "period": year,
        }, report_text)
        assert filled["report_year"] == year
        assert filled["report_period"] == period
        assert filled["period"] == period
        assert filled["accounting_standard"] == _CASE["identity_expectations"]["accounting_standard"]


class TestSnapshotSourceIdentity:
    def test_source_hash_is_available_at_snapshot_top_level_and_stable_after_manifest(self):
        snapshot = build_final_snapshot({
            "analysis_id": "run-source-hash",
            "company_info": {"company_name": "源文件公司", "report_period": "2025年度"},
            "source": {"document_name": "source.pdf", "source_hash": "sha-source"},
            "risk_details": [],
        }, {})
        assert snapshot["source_hash"] == "sha-source"
        assert snapshot["source"]["source_hash"] == "sha-source"

        published = finalize_snapshot_publication(snapshot, [{
            "artifact_id": "artifact-001", "status": "success", "path": "/tmp/report.pdf",
        }])
        assert published["source_hash"] == "sha-source"
        assert published["snapshot_id"] == snapshot["snapshot_id"]
        assert published["artifact_manifest"][0]["artifact_id"] == "artifact-001"


class TestExcerptCoverage:
    def test_key_report_rows_survive_excerpt(self, report_text):
        excerpt = GraphService._financial_extraction_excerpt(report_text)
        # 固定封面/重要提示页
        assert _CASE["identity_expectations"]["company_name"] in excerpt
        # 关键报表行都必须在摘录里，否则对应指标必然「未获取」
        for kw in ("营业成本", "商誉", "在建工程", "货币资金", "流动资产合计",
                   "流动负债合计", "未分配利润", "其他应付款", "利息费用",
                   "利息收入", "资产总计", "股东权益合计",
                   "经营活动产生的现金流量净额"):
            assert kw in excerpt, f"摘录缺失关键行: {kw}"
        assert len(excerpt) <= GraphService._EXCERPT_MAX_CHARS


class TestSourceBackfill:
    def test_backfills_core_statement_rows(self, report_text):
        data = GraphService._backfill_source_facts(report_text, {})
        for key, value in _CASE["backfill_expectations"].items():
            assert data.get(key) == value, f"{key}: got={data.get(key)} expect={value}"

    def test_backfill_records_field_provenance(self, report_text):
        data = GraphService._backfill_source_facts(report_text, {})
        meta = data.get("_field_metadata", {})
        for key in ("monetary_funds_current", "revenue_current",
                    "operating_cashflow_current", "retained_earnings_end"):
            assert key in meta, f"{key} 缺少来源元数据"
            assert meta[key].get("page"), f"{key} 缺少页码"
            assert meta[key].get("unit"), f"{key} 缺少单位"



class TestRiskModelInputBackfill:
    """回归：SG&A / 折旧摊销 / 毛利额必须从原表确定回填。

    实测缺陷：三项缺失会让 Beneish M-Score 永远 insufficient_data，
    演示时量化模型表格只剩 Z 一行有数。
    """

    def test_beneish_inputs_backfilled_from_source_tables(self, report_text):
        data = GraphService._backfill_source_facts(report_text, {})
        for key, value in _CASE["risk_model_inputs"].items():
            assert data[key] == value, f"{key}: got={data.get(key)} expect={value}"

class TestDeterministicChain:
    """回填数据跑通 校验/指标/模型，确认不再大面积未获取（演示底线）。"""

    def _prepare(self, report_text):
        data = GraphService._backfill_source_facts(report_text, {})
        data["amount_unit"] = detect_amount_unit(report_text) or data.get("amount_unit")
        return normalize_financial_units(data)

    def test_missing_optional_indicators_are_explicit(self, report_text):
        data = self._prepare(report_text)
        result = json.loads(calculate_financial_indicators.invoke(
            {"financial_data_json": json.dumps(data, ensure_ascii=False)}))
        gaps = [m for m in result["metric_results"]
                if m["status"] not in ("calculated", "not_comparable")]
        gap_ids = {m["metric_id"] for m in gaps}
        assert gap_ids <= {
            "roe_deducted_pct", "eps_basic", "book_value_per_share",
            "cash_ratio", "free_cash_flow",
        }
        assert all(m["value"] is None and m["substitution"].startswith("输入缺失")
                   for m in gaps)

    def test_interim_report_calculates_models_with_interim_caveat(self, report_text):
        """半年报也要出 Z/M 分值（演示表格不留空），但必须标注中期口径局限。"""
        data = self._prepare(report_text)
        result = json.loads(calculate_risk_models.invoke(
            {"financial_data_json": json.dumps(data, ensure_ascii=False)}))
        for key in ("altman_z_score", "beneish_m_score"):
            model = result["risk_models"][key]
            assert model["status"] == "calculated", (key, model.get("reason"))
            assert isinstance(model["score"], float), (key, model)
            assert model["interim_basis"] is True
            assert "中期" in model["interim_note"] and "年度" in model["interim_note"]
            assert "交叉印证" in model["interim_note"]
            assert "缺少" not in model["interim_note"]
        assert "缺少本期期间" not in json.dumps(result["risk_models"], ensure_ascii=False)
