# -*- coding: utf-8 -*-
"""监管问询在线查询工具单元测试

覆盖新浪联想接口返回解析（公司名→股票代码）的纯逻辑（不联网，确定性）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json

import tools.regulatory_inquiry as ri
from tools.regulatory_inquiry import _parse_sina_suggest


class TestParseSinaSuggest:
    """_parse_sina_suggest：从新浪联想返回提取 6 位股票代码"""

    def test_extracts_sse_code(self):
        text = 'var suggestvalue="贵州茅台,11,600519,sh600519,贵州茅台,,贵州茅台,99,1,ESG,,";'
        assert _parse_sina_suggest(text) == "600519"

    def test_extracts_szse_code(self):
        text = 'var suggestvalue="比亚迪,12,002594,sz002594,比亚迪,,比亚迪,99,1,ESG,,";'
        assert _parse_sina_suggest(text) == "002594"

    def test_multiple_records_takes_first(self):
        text = 'var suggestvalue="宁德时代,11,300750,sz300750,宁德时代;宁德,11,300750,sz300750,宁德";'
        assert _parse_sina_suggest(text) == "300750"

    def test_empty_response_returns_none(self):
        assert _parse_sina_suggest('var suggestvalue="";') is None

    def test_garbage_returns_none(self):
        assert _parse_sina_suggest("not a suggest response") is None
        assert _parse_sina_suggest("") is None

    def test_non_six_digit_code_returns_none(self):
        text = 'var suggestvalue="x,11,ABC,shABC,x";'
        assert _parse_sina_suggest(text) is None


class TestDegradedSemantics:
    """degraded/no_records：区分「接口失败」与「查无记录」（mock 抓取，不联网）"""

    def test_no_records_not_degraded(self, monkeypatch):
        """接口正常但查无记录 → no_records=True, degraded=False（不误报网络失败）。"""
        monkeypatch.setattr(ri, "_cache", {})
        monkeypatch.setattr(ri, "_resolve_stock_code", lambda q: "600519")
        monkeypatch.setattr(ri, "_fetch_sse", lambda q, l: ([], True))
        out = json.loads(ri.search_regulatory_inquiries.invoke({"query": "贵州茅台"}))
        assert out["degraded"] is False
        assert out["no_records"] is True
        assert "无监管问询记录" in out["disclaimer"]
        assert "网络查询未成功" not in out["disclaimer"]

    def test_network_failure_is_degraded(self, monkeypatch):
        """接口异常 → degraded=True（真降级），提示人工查询入口。"""
        monkeypatch.setattr(ri, "_cache", {})
        monkeypatch.setattr(ri, "_resolve_stock_code", lambda q: "600519")
        monkeypatch.setattr(ri, "_fetch_sse", lambda q, l: ([], False))
        out = json.loads(ri.search_regulatory_inquiries.invoke({"query": "贵州茅台"}))
        assert out["degraded"] is True
        assert out["no_records"] is False
        assert "网络查询未成功" in out["disclaimer"]

    def test_found_records_not_degraded(self, monkeypatch):
        """查到记录 → degraded=False, no_records=False。"""
        monkeypatch.setattr(ri, "_cache", {})
        monkeypatch.setattr(ri, "_resolve_stock_code", lambda q: "600519")
        monkeypatch.setattr(ri, "_fetch_sse", lambda q, l: (
            [{"title": "问询函", "date": "2025", "inquiry_type": "监管问询", "url": ""}], True))
        out = json.loads(ri.search_regulatory_inquiries.invoke({"query": "贵州茅台"}))
        assert out["degraded"] is False
        assert out["no_records"] is False
        assert len(out["items"]) == 1
        assert "获取 1 条" in out["disclaimer"]
