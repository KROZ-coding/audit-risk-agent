"""示例图表样张生成脚本（合成数据版）。

为 samples/charts/ 生成三张示例图（财务雷达图 / 风险热力图 / 多年趋势图），
使用与 scripts/generate_sample_reports.py 一致的虚构公司「华信智造科技」，
全部数据为构造值，可安全入库；不读取任何真实年报数据。

用法: uv run python scripts/generate_sample_charts.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.visualizer import _generate_radar_chart, _generate_risk_heatmap, _generate_trend_chart

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_DIR = os.path.join(ROOT, "samples", "charts")

COMPANY = "华信智造科技股份有限公司"
INDUSTRY = "制造业"


def _radar_payload() -> str:
    """雷达图：指标值与 scripts/generate_sample_reports.py 的台账口径一致。"""
    return json.dumps({
        "company_info": {"company_name": COMPANY, "report_year": "2025", "industry": INDUSTRY},
        "calculated_indicators": {
            "gross_margin_pct": 21.5,
            "debt_to_asset_ratio_pct": 67.8,
            "accounts_receivable_to_revenue_ratio": 28.0,
            "inventory_turnover_ratio": 3.2,
            "current_ratio": 0.9,
        },
    }, ensure_ascii=False)


def _heatmap_payload() -> str:
    """热力图：7 条正式风险覆盖五大维度（与示例报告台账同源）。"""
    return json.dumps({
        "company_info": {"company_name": COMPANY, "report_year": "2025", "industry": INDUSTRY},
        "risk_details": [
            {"risk_id": "R001", "dimension": "财务错报风险", "level": "重大",
             "confidence": 0.85, "title": "扣非净利润连续三年为负，主业盈利能力存疑"},
            {"risk_id": "R002", "dimension": "财务错报风险", "level": "重要",
             "confidence": 0.65, "title": "应收账款增速持续高于营收增速，回款风险加剧"},
            {"risk_id": "R003", "dimension": "持续经营风险", "level": "重要",
             "confidence": 0.7, "title": "经营现金流持续紧张，流动比率低于1"},
            {"risk_id": "R004", "dimension": "财务错报风险", "level": "重要",
             "confidence": 0.6, "title": "商誉减值迹象：子公司业绩承诺未达标"},
            {"risk_id": "R005", "dimension": "信息披露合规风险", "level": "一般",
             "confidence": 0.5, "title": "部分关联交易披露要素不完整"},
            {"risk_id": "R006", "dimension": "关联交易风险", "level": "一般",
             "confidence": 0.5, "title": "关联方资金往来规模上升"},
            {"risk_id": "R007", "dimension": "监管处罚类高风险", "level": "一般",
             "confidence": 0.45, "title": "报告期内无监管处罚记录（未触发）"},
        ],
    }, ensure_ascii=False)


def _trend_payload() -> str:
    """趋势图：2022-2025 四年构造数据（营收下滑、扣非连亏、现金流紧张）。"""
    def year(y, revenue, net_profit, ocf, ar, inventory, ta, tl, cogs):
        return {"year": y, "revenue": revenue, "net_profit": net_profit,
                "operating_cashflow": ocf, "accounts_receivable": ar,
                "inventory": inventory, "total_assets": ta, "total_liabilities": tl,
                "cost_of_goods": cogs}

    return json.dumps({
        "company_name": COMPANY,
        "amount_unit": "元",
        "years": [
            year("2022", 5_800_000_000, 320_000_000, 280_000_000, 1_150_000_000,
                 640_000_000, 6_200_000_000, 3_900_000_000, 4_350_000_000),
            year("2023", 5_120_000_000, 90_000_000, 60_000_000, 1_420_000_000,
                 700_000_000, 6_350_000_000, 4_180_000_000, 3_940_000_000),
            year("2024", 4_690_000_000, -180_000_000, -120_000_000, 1_560_000_000,
                 720_000_000, 6_280_000_000, 4_290_000_000, 3_680_000_000),
            year("2025", 4_290_000_000, -290_000_000, -210_000_000, 1_610_000_000,
                 690_000_000, 6_150_000_000, 4_170_000_000, 3_370_000_000),
        ],
    }, ensure_ascii=False)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    jobs = [
        ("示例_财务雷达图.png", _generate_radar_chart, _radar_payload()),
        ("示例_风险热力图.png", _generate_risk_heatmap, _heatmap_payload()),
        ("示例_趋势图.png", _generate_trend_chart, _trend_payload()),
    ]
    for filename, fn, payload in jobs:
        out_path = os.path.join(OUT_DIR, filename)
        result = fn(payload, out_path)
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        print(f"{filename}: {size} bytes ({result})")


if __name__ == "__main__":
    main()
