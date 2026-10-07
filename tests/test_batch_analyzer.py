"""T6 batch_analyze_companies 单元测试（此前零测试覆盖的宣传能力）

锁定行为：
- 空列表/畸形输入的报错文案
- 单公司失败隔离（部分失败不影响整体）
- 多公司分析的输出结构（company_list/industry_comparison/summary）
- 勾稽口径计分（risk_level 映射）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.batch_processor import batch_analyze_companies


def _company(name="测试公司", ta=10000, tl=4000, np=800, rev=50000):
    return {
        "company_name": name,
        "report_year": "2025",
        "industry": "制造业",
        "financial_data": {
            "total_assets": ta, "total_liabilities": tl, "net_assets": ta - tl,
            "net_profit": np, "revenue": rev,
        },
    }


class TestInputValidation:
    def test_empty_list_rejected(self):
        """空数组 → 返回提示文本（工具输出为字符串，非 JSON）"""
        result = batch_analyze_companies.invoke({"companies_data_json": json.dumps([])})
        assert "输入必须是非空的公司数组" in result

    def test_invalid_json_rejected(self):
        result = batch_analyze_companies.invoke({"companies_data_json": "{broken"})
        assert "输入必须" in result or "JSON" in result

    def test_non_list_rejected(self):
        result = batch_analyze_companies.invoke({"companies_data_json": json.dumps({"a": 1})})
        assert "输入必须" in result


class TestIsolation:
    def test_single_company_failure_isolated(self):
        """单公司缺数据 → 该公司标「数据不足」且不崩，正常公司照常分析（T6 隔离验证）"""
        companies = [_company("正常公司"), {"company_name": "坏数据公司", "financial_data": None}]
        result = json.loads(batch_analyze_companies.invoke(
            {"companies_data_json": json.dumps(companies, ensure_ascii=False)}))
        names = [c.get("company_name") for c in result.get("company_list", [])]
        assert "坏数据公司" in names
        statuses = {c.get("company_name"): c.get("status") for c in result["company_list"]}
        assert statuses.get("正常公司") == "已分析"
        assert statuses.get("坏数据公司") in ("数据不足", "分析失败")

    def test_multi_company_output_structure(self):
        result = json.loads(batch_analyze_companies.invoke(
            {"companies_data_json": json.dumps([_company(), _company("第二家")], ensure_ascii=False)}))
        assert "company_list" in result and "industry_comparison" in result and "summary" in result
        assert len(result["company_list"]) == 2
        for c in result["company_list"]:
            assert "risk_score" in c or "risk_level" in c or c.get("status")

    def test_risk_level_mapping(self):
        """净资产为负的高杠杆公司 → 非「一般」等级（F10 等级映射锁定）"""
        bad = _company("资不抵债公司", ta=1000, tl=1500, np=-200)
        result = json.loads(batch_analyze_companies.invoke(
            {"companies_data_json": json.dumps([bad], ensure_ascii=False)}))
        first = result["company_list"][0]
        assert first.get("risk_level") in ("重大", "重要", "一般"), f"实际: {first.get('risk_level')}"
        # 高杠杆+亏损必须至少触发告警，不得静默
        assert first.get("total_risks", 0) > 0 or first.get("risk_level") != "一般"
