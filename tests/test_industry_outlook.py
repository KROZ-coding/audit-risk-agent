"""行业风向标工具单元测试

覆盖 industry_outlook 的核心场景：
- 在线抓取成功 → data_mode=online
- 在线全失败 → 降级离线知识库（data_mode=offline_kb）且不抛异常
- TTL 缓存命中不重复抓取
- 温度计 1-10 clamp
- 壁钟预算硬截断（慢源不拖垮整体）
- RSS 安全加固（拒绝 DTD/实体声明）

全部用例 monkeypatch 网络/知识库层，不发真实请求、不加载真实语料。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tools.industry_outlook as io_mod
from tools.industry_outlook import industry_outlook, _parse_rss, _score_sentiment


def _fake_events(n=3, keyword="测试"):
    return [{"title": f"{keyword}行业新闻{i}", "date": "2026-07-31", "source": "假源", "url": ""}
            for i in range(n)]


class TestIndustryOutlook:
    """行业风向标工具测试集（industry 名称各用例唯一，避免模块级缓存串扰）"""

    def _invoke(self, industry, company=""):
        raw = industry_outlook.invoke({"industry": industry, "company_name": company})
        return json.loads(raw)

    def test_online_mode(self, monkeypatch):
        """任一源抓取成功 → data_mode=online，事件带极性标注"""
        monkeypatch.setattr(io_mod, "_fetch_online_news", lambda kw: _fake_events(3, kw))
        result = self._invoke("在线测试业A")
        assert result["data_mode"] == "online"
        assert len(result["events"]) == 3
        assert all(ev["polarity"] in ("利好", "利空", "中性") for ev in result["events"])
        assert result["disclaimer"]

    def test_offline_fallback_no_crash(self, monkeypatch):
        """在线全失败 → 降级离线知识库，data_mode=offline_kb 且不抛异常"""
        monkeypatch.setattr(io_mod, "_fetch_online_news", lambda kw: [])
        monkeypatch.setattr(io_mod, "_search_offline_kb", lambda ind: _fake_events(2, ind))
        result = self._invoke("离线测试业B")
        assert result["data_mode"] == "offline_kb"
        assert len(result["events"]) == 2

    def test_cache_hit_skips_refetch(self, monkeypatch):
        """同行业二次调用命中 TTL 缓存，不重复抓取"""
        calls = {"n": 0}

        def counting_fetch(kw):
            calls["n"] += 1
            return _fake_events(1, kw)

        monkeypatch.setattr(io_mod, "_fetch_online_news", counting_fetch)
        self._invoke("缓存测试业C")
        self._invoke("缓存测试业C")
        assert calls["n"] == 1

    def test_empty_industry_returns_error(self):
        """行业名为空：返回带 error 与 disclaimer 的引导 JSON，不崩溃"""
        result = self._invoke("  ")
        assert "error" in result
        assert result["disclaimer"]

    # ── 温度计规则 ──

    def test_thermometer_clamps_high_and_low(self):
        """大量利好 → 封顶 10；大量利空 → 保底 1"""
        pos = [{"title": f"行业增长利好{i}"} for i in range(20)]
        neg = [{"title": f"行业下滑利空{i}"} for i in range(20)]
        t_hi, _ = _score_sentiment(pos)
        t_lo, _ = _score_sentiment(neg)
        assert t_hi == 10.0
        assert t_lo == 1.0

    def test_thermometer_neutral_baseline(self):
        """无事件时温度计为中性基准 5 分"""
        t, tagged = _score_sentiment([])
        assert t == 5.0
        assert tagged == []

    # ── 壁钟预算 ──

    def test_wall_budget_truncates_slow_sources(self, monkeypatch):
        """慢源超壁钟预算被硬截断：总耗时接近预算而非源耗时"""
        monkeypatch.setattr(io_mod, "_WALL_BUDGET", 1.0)
        monkeypatch.setattr(io_mod, "_fetch_one_source",
                            lambda n, u, kw, fn=None: (time.sleep(5), [])[1])
        start = time.monotonic()
        events = io_mod._fetch_online_news("慢源测试")
        elapsed = time.monotonic() - start
        assert events == []
        assert elapsed < 3.0, f"壁钟预算未生效，耗时 {elapsed:.1f}s"

    # ── RSS 解析安全加固 ──

    def test_rss_rejects_dtd_and_entities(self):
        """含 DTD/实体声明的 XML 应被拒绝解析（防实体膨胀攻击）"""
        evil = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>x</title></item></channel></rss>'
        assert _parse_rss(evil, "恶意源", "") == []

    def test_rss_parses_normal_feed_with_keyword_filter(self):
        """正常 RSS 按关键词过滤标题；不相关条目被剔除"""
        xml = ('<rss><channel>'
               '<item><title>锂电行业扩产提速</title><pubDate>Thu, 31 Jul 2026</pubDate><link>http://a</link></item>'
               '<item><title>白酒消费回暖</title><pubDate>Thu, 31 Jul 2026</pubDate><link>http://b</link></item>'
               '</channel></rss>')
        events = _parse_rss(xml, "测试源", "锂电")
        assert len(events) == 1
        assert "锂电" in events[0]["title"]
