"""多年财务数据对比分析工具的单元测试

覆盖 compare_multi_year 的核心场景：
- 数据不足2年的防御提示
- 非法 JSON 的错误提示
- 毛利率连续下滑的趋势预警
- 两种输入格式（years 数组 / 年度字典）兼容
- 同比变动与趋势结构输出
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.multi_year_comparison import compare_multi_year


class TestMultiYearComparison:
    """多年财务数据对比分析工具测试集"""

    def _invoke(self, data):
        """辅助方法：传入 dict/JSON 字符串，返回原始字符串结果"""
        payload = data if isinstance(data, str) else json.dumps(data)
        return compare_multi_year.invoke({"multi_year_data_json": payload})

    def test_insufficient_years(self):
        """仅提供1个年度时应返回提示而非分析结果"""
        result = self._invoke({"years": [{"year": "2023", "revenue": 100}]})
        assert "至少需要提供2个年度" in result

    def test_invalid_json(self):
        """非法 JSON 字符串应返回解析失败提示"""
        result = self._invoke("{not valid json")
        assert "JSON解析失败" in result

    def test_gross_margin_decline_alert(self):
        """毛利率连续三年下滑应触发趋势风险预警"""
        data = {"years": [
            {"year": "2022", "revenue": 10000, "cost_of_goods": 5000},
            {"year": "2023", "revenue": 10000, "cost_of_goods": 6000},
            {"year": "2024", "revenue": 10000, "cost_of_goods": 7000},
        ]}
        result = json.loads(self._invoke(data))
        assert result["years_analyzed"] == ["2022", "2023", "2024"]
        assert any("毛利率连续" in a for a in result["trend_alerts"])

    def test_dict_format_supported(self):
        """顶层年度字典格式应被正确解析并排序"""
        data = {
            "2023": {"revenue": 10000, "cost_of_goods": 6000},
            "2022": {"revenue": 10000, "cost_of_goods": 5000},
        }
        result = json.loads(self._invoke(data))
        assert result["years_analyzed"] == ["2022", "2023"]
        assert "2022-2023" in result["yoy_changes"]

    def test_output_structure(self):
        """输出应包含各年度指标、同比变动、趋势与预警计数字段"""
        data = {"years": [
            {"year": "2022", "revenue": 8000, "cost_of_goods": 4000, "total_assets": 20000,
             "total_liabilities": 10000},
            {"year": "2023", "revenue": 9000, "cost_of_goods": 4500, "total_assets": 22000,
             "total_liabilities": 12000},
        ]}
        result = json.loads(self._invoke(data))
        assert "indicators_by_year" in result
        assert "2022" in result["indicators_by_year"]
        assert result["indicators_by_year"]["2022"]["gross_margin"] == 50.0
        assert result["alert_count"] == len(result["trend_alerts"])
