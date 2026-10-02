# -*- coding: utf-8 -*-
"""报告身份确定性识别 + 原文表格回填的回归测试（演示缺陷防复发）。

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


_PDF = os.path.join(os.path.dirname(__file__), "..", "测试",
                    "中国石油：中国石油天然气股份有限公司2025 年半年度报告.pdf")


@pytest.fixture(scope="module")
def report_text():
    if not os.path.exists(_PDF):
        pytest.skip("真实样本 PDF 不在仓库中")
    from tools.pdf_parser import parse_pdf_report
    return parse_pdf_report.invoke({"file_path": _PDF})


class TestReportIdentity:
    def test_cover_fields_are_deterministically_extracted(self, report_text):
        ident = extract_report_identity(report_text)
        assert ident["company_name"] == "中国石油天然气股份有限公司"
        assert ident["stock_code"] == "601857"
        assert ident["period"] == ident["report_period"] == "2025年半年度"
        assert ident["industry"] == "能源"
        assert "未经审计" in ident["audit_opinion"]
        assert ident["accounting_standard"] == "中国企业会计准则"

    def test_fallback_fills_only_empty_fields(self, report_text):
        filled = apply_company_info_fallback({}, report_text)
        assert filled["company_name"] == "中国石油天然气股份有限公司"
        assert filled["period"] == "2025年半年度"
        # 已有非空值绝不覆盖
        kept = apply_company_info_fallback({"company_name": "模型给的名字"}, report_text)
        assert kept["company_name"] == "模型给的名字"

    def test_fallback_on_none_returns_dict(self, report_text):
        assert isinstance(apply_company_info_fallback(None, report_text), dict)

    def test_fallback_corrects_generic_model_period(self, report_text):
        filled = apply_company_info_fallback({
            "report_year": "2025",
            "report_period": "2025",
            "period": "2025",
        }, report_text)
        assert filled["report_year"] == "2025"
        assert filled["report_period"] == "2025年半年度"
        assert filled["period"] == "2025年半年度"
        assert filled["accounting_standard"] == "中国企业会计准则"


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
        assert "中国石油天然气股份有限公司" in excerpt
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
        expected = {
            "monetary_funds_current": 284493, "monetary_funds_previous": 216246,
            "current_assets_current": 710678, "current_assets_previous": 590844,
            "accounts_receivable_current": 119715, "accounts_receivable_previous": 71610,
            "inventory_current": 155724, "inventory_previous": 168338,
            "goodwill_current": 7424, "goodwill_previous": 7436,
            "other_payables_current": 72676, "other_payables_previous": 24198,
            "current_liabilities_current": 684270, "current_liabilities_previous": 637317,
            "total_liabilities_current": 1096490, "total_liabilities_previous": 1043144,
            "total_assets_current": 2849632, "total_assets_previous": 2753007,
            "net_assets_current": 1753142, "net_assets_previous": 1709863,
            "revenue_current": 1450099, "revenue_previous": 1554973,
            "cost_of_goods_current": 1147144, "cost_of_goods_previous": 1228848,
            "net_profit_current": 93666, "net_profit_previous": 99805,
            "net_profit_parent_current": 83993, "net_profit_parent_previous": 88802,
            "retained_earnings_end": 1020356, "retained_earnings_begin": 982234,
            "interest_expense_current": 9296, "interest_income_current": 3706,
            # 现金流量表行（附注引用 59(f)）
            "operating_cashflow_current": 227063, "operating_cashflow_previous": 218419,
        }
        for key, value in expected.items():
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
        # 销售费用 28,693 + 管理费用 32,274（合并利润表）
        assert data["sga_expense_current"] == 60967
        assert data["sga_expense_previous"] == 59918
        # 间接法调整表：折旧、折耗及摊销
        assert data["depreciation_current"] == 121348
        assert data["depreciation_previous"] == 116702
        # 毛利额＝营业收入-营业成本（两行同在合并利润表）
        assert data["gross_profit_current"] == 1450099 - 1147144
        assert data["gross_profit_previous"] == 1554973 - 1228848

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
