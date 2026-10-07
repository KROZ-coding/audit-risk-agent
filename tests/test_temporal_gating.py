"""E2 知识库时间门控测试

锁定行为：
- LocalKnowledgeBase 加载 corpus_meta.json 并按来源 enrich 检索结果
- _temporal_warnings：施行年份晚于报告期 → 警示；无报告期/无年份/不晚于 → 不误报
- search_regulations 输出携带施行日期与警示文本（fake KB，不重建索引）
- corpus_meta.json 本身合法且覆盖法规类语料
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from local_knowledge import LocalKnowledgeBase
from tools.knowledge_search import _temporal_warnings, search_regulations

REPO = os.path.join(os.path.dirname(__file__), "..")


class TestCorpusMeta:
    def test_meta_file_loads_and_covers_regulations(self):
        kb = LocalKnowledgeBase(kb_dir=os.path.join(REPO, "knowledge_base"))
        meta = kb._meta_by_source
        assert meta, "corpus_meta.json 应能加载"
        assert "上市公司信息披露管理办法2025.txt" in meta
        assert meta["上市公司信息披露管理办法2025.txt"]["effective"] == "2025-07-01"
        assert "上交所纪律处分和监管措施办法2026.txt" in meta
        # 案例类语料无施行日期（null），不产出 effective 字段
        assert not kb._meta_for("cases_内幕交易案.txt").get("effective")

    def test_meta_for_enriches_entries(self):
        kb = LocalKnowledgeBase(kb_dir=os.path.join(REPO, "knowledge_base"))
        entry = kb._meta_for("证监会2025年执法情况.txt")
        assert entry.get("effective") == "2025"
        assert "执法情况综述" in entry.get("effective_note", "")


class TestTemporalWarnings:
    def test_later_effective_year_warns(self):
        results = [{"source": "上交所纪律处分和监管措施办法2026.txt", "effective": "2026"}]
        out = _temporal_warnings(results, "2023年度")
        assert len(out) == 1
        assert "时点提示" in out[0] and "2026" in out[0] and "2023" in out[0]

    def test_earlier_or_equal_year_no_warning(self):
        results = [
            {"source": "证券法2019修订关键条款.txt", "effective": "2020-03-01"},
            {"source": "某准则2023.txt", "effective": "2023-06-30"},
        ]
        assert _temporal_warnings(results, "2023年度") == []
        assert _temporal_warnings(results, "2025年度") == []

    def test_missing_period_or_date_no_warning(self):
        results = [{"source": "a.txt", "effective": "2026"}]
        assert _temporal_warnings(results, "") == []
        assert _temporal_warnings([{"source": "a.txt"}], "2023年度") == []
        assert _temporal_warnings([], "2023年度") == []

    def test_duplicate_sources_single_warning(self):
        results = [
            {"source": "x.txt", "effective": "2026"},
            {"source": "x.txt", "effective": "2026"},
        ]
        assert len(_temporal_warnings(results, "2023年度")) == 1


class TestSearchToolOutput:
    def _fake_kb(self, results):
        class _FakeKB:
            def search(self, query, top_k=8, min_score=0.05):
                return results
        return _FakeKB()

    def test_output_shows_effective_date_and_warning(self, monkeypatch):
        import tools.knowledge_search as ks
        results = [{"content": "条文正文", "score": 0.8,
                    "source": "上交所纪律处分和监管措施办法2026.txt", "effective": "2026"}]
        monkeypatch.setattr(ks, "get_knowledge_base", lambda: self._fake_kb(results))
        out = search_regulations.invoke({"query": "纪律处分", "report_period": "2023年度"})
        assert "施行: 2026" in out
        assert "时点提示" in out
        # 警示必须出现在条文引用之前
        assert out.index("时点提示") < out.index("条文正文")

    def test_output_without_period_has_dates_but_no_warning(self, monkeypatch):
        import tools.knowledge_search as ks
        results = [{"content": "条文正文", "score": 0.8,
                    "source": "上市公司信息披露管理办法2025.txt", "effective": "2025-07-01"}]
        monkeypatch.setattr(ks, "get_knowledge_base", lambda: self._fake_kb(results))
        out = search_regulations.invoke({"query": "信息披露"})
        assert "施行: 2025-07-01" in out
        assert "时点提示" not in out
