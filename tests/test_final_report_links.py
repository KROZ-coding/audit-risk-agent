"""最终报告链接提取测试（兜底导出场景）

背景：导出改为全兜底后，PDF/Excel 链接由 _post_process 追加在 AI 正文中，
不产生 ToolMessage；旧版 _build_final_report_from_messages 只扫 ToolMessage，
导致前端文件下载卡与历史记录 files 字段为空（实测事故）。
本测试锁定：正文链接可被提取、两源去重、图表 ToolMessage 链接不受影响。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as main_mod
from main import GraphService


@pytest.fixture(autouse=True)
def _stub_artifact_access(monkeypatch):
    """本文件用假路径验证链接提取：放行产物存在性门禁，门禁本身另有独立用例。"""
    monkeypatch.setattr(main_mod, "_artifact_is_accessible", lambda path: True)


def _build(messages):
    return GraphService()._build_final_report_from_messages(messages)


class TestFinalReportLinks:
    """_build_final_report_from_messages 的双源链接提取与去重"""

    def test_fallback_links_in_ai_text_are_collected(self):
        """兜底导出场景：链接仅存在于 AI 正文，也必须进入 files 列表。"""
        messages = [
            {"type": "tool", "name": "calculate_comprehensive_score", "content": '{"score": 20}'},
            {"type": "ai", "content": (
                "分析完成。\n\n"
                "📎 PDF报告: PDF风险报告已生成，下载链接: /local_storage/reports/20260725_测试公司_2025_审计风险报告.pdf\n\n"
                "📊 Excel底稿: Excel审计底稿已生成，下载链接: /local_storage/reports/20260725_测试公司_2025_审计底稿.xlsx"
            )},
        ]
        report = _build(messages)
        paths = [f["path"] for f in report["files"]]
        assert "/local_storage/reports/20260725_测试公司_2025_审计风险报告.pdf" in paths
        assert "/local_storage/reports/20260725_测试公司_2025_审计底稿.xlsx" in paths
        assert all(f["tool"] == "fallback_export" for f in report["files"])

    def test_toolmessage_and_ai_text_deduplicated(self):
        """同一链接出现在 ToolMessage 与正文两处：只保留一份（ToolMessage 源优先）。"""
        pdf = "/local_storage/reports/x.pdf"
        png = "/local_storage/charts/y.png"
        messages = [
            {"type": "tool", "name": "export_pdf_report", "content": f"下载链接: {pdf}"},
            {"type": "tool", "name": "generate_risk_heatmap", "content": f'{{"download_url": "{png}"}}'},
            {"type": "ai", "content": f"报告见 {pdf} ，图表见 {png}"},
        ]
        report = _build(messages)
        assert [f["path"] for f in report["files"]] == [pdf]
        assert report["files"][0]["tool"] == "export_pdf_report"
        assert [i["path"] for i in report["images"]] == [png]

    def test_charts_from_toolmessage_still_work(self):
        """图表仍由 LLM 主动调用（ToolMessage 源）：提取行为不回归。"""
        messages = [
            {"type": "tool", "name": "generate_radar_chart",
             "content": '{"chart_type": "雷达图", "download_url": "/local_storage/charts/radar.png"}'},
            {"type": "ai", "content": "完成"},
        ]
        report = _build(messages)
        assert report["images"] == [{"tool": "generate_radar_chart", "path": "/local_storage/charts/radar.png"}]

    def test_inaccessible_paths_are_dropped(self, monkeypatch):
        """不存在的产物不得进入下载列表（避免前端给出失效链接）。"""
        monkeypatch.setattr(main_mod, "_artifact_is_accessible", lambda path: False)
        messages = [
            {"type": "ai", "content": "报告见 /local_storage/reports/missing.pdf"},
        ]
        report = _build(messages)
        assert report["files"] == [] and report["images"] == []
