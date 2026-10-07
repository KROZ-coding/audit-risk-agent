"""F4/F5 行业基准匹配测试

锁定行为：
- 匹配特异性优先："医药制造业"命中医药生物（p2），不再错配通用制造业（p1）
- 关键词最长者优先；无命中返回 None（宁缺毋错）
- debt_to_asset_ratio 基准含 match_keywords/match_priority/source 字段（F4）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.benchmark_contract import match_industry_entry

REPO = os.path.join(os.path.dirname(__file__), "..")
INDUSTRIES = json.load(open(os.path.join(REPO, "assets", "industry_benchmarks.json"),
                            encoding="utf-8"))["industries"]


class TestIndustryMatching:
    def test_pharma_beats_generic_manufacturing(self):
        entry, name = match_industry_entry("医药制造业", INDUSTRIES)
        assert name == "医药生物", f"医药制造业被错配到 {name}"

    def test_generic_manufacturing_still_matches(self):
        entry, name = match_industry_entry("汽车制造业", INDUSTRIES)
        assert name == "制造业"

    def test_unknown_industry_returns_none(self):
        entry, name = match_industry_entry("禅修服务中心", INDUSTRIES)
        assert entry is None

    def test_bank_matches_finance(self):
        entry, name = match_industry_entry("商业银行", INDUSTRIES)
        assert name == "金融"

    def test_benchmark_entries_have_provenance_fields(self):
        for name, entry in INDUSTRIES.items():
            assert "match_keywords" in entry, f"{name} 缺 match_keywords"
            assert "match_priority" in entry, f"{name} 缺 match_priority"
            assert "source" in entry, f"{name} 缺来源声明（F4）"
            assert entry.get("statistical_year", "missing") is None, "统计年份未标注应显式为 null"
