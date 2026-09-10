# -*- coding: utf-8 -*-
"""五年时序支持与字段别名容错测试（P4）

背景：架构定稿要求「扩充 5 年时序数据」，且固定输出骨架要求年份×指标时序表格
与 echarts 图表块。本测试锁定：
- compare_multi_year 输出 echarts-ready 的 timeseries 结构（xAxis/series）
- 缺年度以 null 表示（区分「未披露」与「真值为 0」）
- 字段别名容错：项目规范名 operating_cashflow 必须能被量化模型取到，
  否则 TATA（M-Score 权重最高因子）永远缺失、模型永远被迫降级（真实事故）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.multi_year_comparison import compare_multi_year
from tools.risk_models import calculate_risk_models


def _year_row(y, i):
    return {
        "year": str(y), "revenue": 100 + i * 10, "net_profit": 10 + i,
        "total_assets": 500 + i * 20, "total_liabilities": 200 + i * 10,
        "accounts_receivable": 30 + i * 5, "operating_cashflow": 12 + i,
        "gross_profit": 30 + i * 2, "current_assets": 150,
        "current_liabilities": 80, "inventory": 40, "cost_of_sales": 70,
    }


def _multi(years):
    data = {"years": [_year_row(y, i) for i, y in enumerate(years)]}
    return json.loads(compare_multi_year.invoke({"multi_year_data_json": json.dumps(data)}))


class TestFiveYearTimeseries:

    def test_six_years_analyzed(self):
        r = _multi([2019, 2020, 2021, 2022, 2023, 2024])
        assert r["year_count"] == 6
        assert r["years_analyzed"] == ["2019", "2020", "2021", "2022", "2023", "2024"]

    def test_timeseries_matches_echarts_schema(self):
        """timeseries 需可直接搬进 ```echarts 块：xAxis 为年份、series 每项含 name+data。"""
        ts = _multi([2020, 2021, 2022, 2023, 2024])["timeseries"]
        assert ts["xAxis"] == ["2020", "2021", "2022", "2023", "2024"]
        assert ts["series"] and all("name" in s and "data" in s for s in ts["series"])
        # 每条 series 的数据点数必须与年份数一致，否则前端图表会错位
        for s in ts["series"]:
            assert len(s["data"]) == len(ts["xAxis"])

    def test_timeseries_carries_real_values(self):
        ts = _multi([2020, 2021, 2022])["timeseries"]
        rev = next(s for s in ts["series"] if s["name"] == "营业收入")
        assert rev["data"] == [100, 110, 120]

    def test_operating_cashflow_not_silently_zero(self):
        """经营现金流必须取到真实值——曾因字段名不一致而静默为 0，导致误报趋势预警。"""
        ts = _multi([2020, 2021, 2022])["timeseries"]
        ocf = next(s for s in ts["series"] if "现金流" in s["name"])
        assert ocf["data"] == [12, 13, 14]

    def test_two_years_still_works(self):
        """年数不足 5 年时不应报错（真实年报常只披露 2-3 年可比数据）。"""
        r = _multi([2023, 2024])
        assert r["year_count"] == 2 and r["timeseries"]["xAxis"] == ["2023", "2024"]


class TestFieldAliasTolerance:
    """量化模型的字段别名容错（P4 修复的真实缺陷）"""

    _BASE = dict(
        industry="制造业", period="2025", total_assets=1000, total_assets_previous=900,
        current_assets=300, current_liabilities=400, total_liabilities=800,
        current_assets_previous=250,
        total_liabilities_previous=650, net_assets=200, retained_earnings_end=-50,
        pretax_profit=-60, interest_expense=20, revenue_current=500,
        revenue_previous=300, gross_profit=40, gross_profit_previous=60,
        accounts_receivable_current=250, accounts_receivable_previous=100,
        fixed_assets=200, fixed_assets_previous=210, depreciation=10,
        depreciation_previous=21, sga_expense=60, sga_expense_previous=40,
        net_profit_current=-60,
    )

    def _models(self, extra):
        data = dict(self._BASE, **extra)
        out = calculate_risk_models.invoke({"financial_data_json": json.dumps(data)})
        return json.loads(out)["risk_models"]

    def test_canonical_name_with_current_suffix(self):
        """项目规范名（operating_cashflow_current）应能算出八变量完整模型。"""
        m = self._models({"operating_cashflow_current": -150})["beneish_m_score"]
        assert m["model"] == "八变量完整模型"
        assert m["factors"]["TATA"] != "缺失"

    def test_canonical_name_without_suffix(self):
        m = self._models({"operating_cashflow": -150})["beneish_m_score"]
        assert m["model"] == "八变量完整模型"

    def test_legacy_underscore_variant(self):
        """兼容旧写法 operating_cash_flow，避免历史数据/其它来源失效。"""
        m = self._models({"operating_cash_flow": -150})["beneish_m_score"]
        assert m["model"] == "八变量完整模型"

    def test_degrades_only_when_truly_missing(self):
        """确实无现金流数据时才降级——降级本身是正确行为，不能因字段名而误触发。"""
        m = self._models({})["beneish_m_score"]
        assert m.get("model") == "五变量简化模型"

    def test_retained_earnings_alias(self):
        """留存收益支持 retained_earnings 与 retained_earnings_end 两种写法。"""
        z1 = self._models({"operating_cashflow": -150})["altman_z_score"]
        data = dict(self._BASE)
        data.pop("retained_earnings_end")
        data["retained_earnings"] = -50
        z2 = json.loads(calculate_risk_models.invoke(
            {"financial_data_json": json.dumps(data)}))["risk_models"]["altman_z_score"]
        assert z1["score"] == z2["score"]

    def test_accounts_receivable_alias(self):
        """应收账款支持 accounts_receivable 与 _current 两种写法。"""
        data = dict(self._BASE, operating_cashflow=-150)
        data["accounts_receivable"] = data.pop("accounts_receivable_current")
        m = json.loads(calculate_risk_models.invoke(
            {"financial_data_json": json.dumps(data)}))["risk_models"]["beneish_m_score"]
        assert "DSRI 应收账款指数" not in m.get("missing_factors", [])
