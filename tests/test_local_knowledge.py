"""本地知识库 TF-IDF 回退路径测试

覆盖 src/local_knowledge.py 的降级逻辑：
- ChromaDB 在「初始化」阶段抛异常 → load() 捕获后构建 TF-IDF，search() 走回退分支
- ChromaDB 在「query」阶段抛异常 → search() 捕获后惰性构建 TF-IDF 并返回结果
- 对若干代表性法规/案例查询，断言回退结果非空且命中预期来源文件
- 若真实 ChromaDB 可用，则比对两条路径的 top-k 命中，确保回退结果可用
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from local_knowledge import LocalKnowledgeBase


# 代表性查询 -> 预期命中来源文件名中应出现的关键片段（任一即可）
# 说明：TF-IDF 回退使用「最长中文串」粗分词，故查询词需与语料中实际出现的
# 连续中文片段对齐；以下查询均已在两条路径上实测可稳定命中对应来源。
REPRESENTATIVE_QUERIES = [
    ("内幕交易", ["内幕交易"]),                       # 案例
    ("操纵证券市场 连续交易", ["操纵市场"]),           # 案例
    ("信息披露 重大事件", ["信息披露"]),               # 法规
    ("财务报表 资产负债表", ["财务报表列报"]),         # 法规
    ("现金流量表", ["财务报表列报"]),                 # 法规
    ("关联交易", ["年报内容", "信息披露"]),           # 法规
]


class _FakeCollectionQueryFails:
    """模拟 ChromaDB collection：初始化/写入正常，但 query 抛异常"""

    def __init__(self):
        self.metadata = {}

    def count(self):
        return 0

    def add(self, **kwargs):  # 写入正常，避免 load() 阶段失败
        return None

    def query(self, **kwargs):
        raise RuntimeError("模拟 ChromaDB query 运行时异常")


class _FakeClientQueryFails:
    """模拟 ChromaDB 客户端：初始化成功，产出 query 会失败的 collection"""

    def get_collection(self, name):
        # 强制走 create_collection 分支（视作缓存未命中）
        raise ValueError("collection 不存在")

    def create_collection(self, name, metadata=None):
        return _FakeCollectionQueryFails()

    def delete_collection(self, name):
        return None


def _fresh_kb():
    """构建独立实例，避免 get_knowledge_base 单例造成跨用例状态污染"""
    return LocalKnowledgeBase()


class TestChromaInitFailureFallback:
    """场景一：ChromaDB 初始化阶段抛异常 → TF-IDF 激活"""

    def test_init_failure_activates_tfidf(self, monkeypatch):
        kb = _fresh_kb()

        def _boom():
            raise RuntimeError("模拟 ChromaDB 初始化异常")

        monkeypatch.setattr(kb, "_get_chroma_client", _boom)

        results = kb.search("内幕交易 违法所得", top_k=5)

        # collection 被置空，说明确实走了 TF-IDF 回退
        assert kb._collection is None
        # 文档正常加载（回退不依赖 ChromaDB）
        assert kb.documents
        # 回退结果非空且结构完整
        assert results
        assert all({"content", "score", "source"} <= set(r) for r in results)

    def test_init_failure_hits_expected_sources(self, monkeypatch):
        kb = _fresh_kb()
        monkeypatch.setattr(
            kb, "_get_chroma_client",
            lambda: (_ for _ in ()).throw(RuntimeError("init fail")),
        )

        for query, expected_fragments in REPRESENTATIVE_QUERIES:
            results = kb.search(query, top_k=8)
            assert results, f"回退检索对『{query}』返回为空"
            sources = " ".join(r["source"] for r in results)
            assert any(frag in sources for frag in expected_fragments), (
                f"『{query}』的回退结果未命中预期来源 {expected_fragments}，"
                f"实际来源: {sources}"
            )


class TestChromaQueryFailureFallback:
    """场景二：ChromaDB 初始化成功但 query 抛异常 → TF-IDF 惰性激活"""

    def test_query_failure_falls_back_to_tfidf(self, monkeypatch):
        kb = _fresh_kb()
        monkeypatch.setattr(kb, "_get_chroma_client", lambda: _FakeClientQueryFails())

        # load() 成功建立（会失败的）collection，且文档已加载
        results = kb.search("操纵证券市场 连续交易", top_k=5)

        assert kb._collection is not None  # 初始化成功，非 None
        assert kb.documents
        # query 抛异常后惰性构建 TF-IDF 并返回非空结果
        assert results
        sources = " ".join(r["source"] for r in results)
        assert "操纵市场" in sources

    def test_query_failure_all_representative_queries(self, monkeypatch):
        kb = _fresh_kb()
        monkeypatch.setattr(kb, "_get_chroma_client", lambda: _FakeClientQueryFails())
        # 预热加载
        kb.search("信息披露", top_k=3)

        for query, expected_fragments in REPRESENTATIVE_QUERIES:
            results = kb.search(query, top_k=8)
            assert results, f"query 失败回退对『{query}』返回为空"
            sources = " ".join(r["source"] for r in results)
            assert any(frag in sources for frag in expected_fragments), (
                f"『{query}』的回退结果未命中预期来源 {expected_fragments}，"
                f"实际来源: {sources}"
            )


class TestFallbackResultQuality:
    """回退结果质量：分数有序、去重、阈值过滤"""

    def test_scores_sorted_desc_and_within_topk(self, monkeypatch):
        kb = _fresh_kb()
        monkeypatch.setattr(
            kb, "_get_chroma_client",
            lambda: (_ for _ in ()).throw(RuntimeError("init fail")),
        )
        results = kb.search("现金流量表", top_k=6)
        assert results
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert len(results) <= 6
        assert all(r["score"] >= 0.05 for r in results)


class TestTwoPathConsistency:
    """两条路径 top-k 命中比对（真实 ChromaDB 可用时执行，否则跳过）

    目的：确认在真实向量检索可用的前提下，TF-IDF 回退路径依然「可用」——
    对代表性查询返回非空且命中预期来源文件，从而在向量检索失效时可无缝顶替。
    注：默认 embedding 对中文语义匹配较弱，两路径 top-k 来源集合未必重合，
    故此处不强制断言来源交集，而是校验回退路径的可用性与精确命中。
    """

    def _load_chroma_kb(self):
        """尝试用真实 ChromaDB 加载；不可用时返回 None"""
        kb = _fresh_kb()
        try:
            kb.load()
        except Exception:
            return None
        if kb._collection is None:
            return None
        return kb

    def test_fallback_usable_versus_chroma(self):
        chroma_kb = self._load_chroma_kb()
        if chroma_kb is None:
            pytest.skip("真实 ChromaDB 不可用，跳过两路径比对")

        # TF-IDF 回退实例（强制关闭 collection）
        fallback_kb = _fresh_kb()
        fallback_kb.load()
        fallback_kb._collection = None

        overlap_count = 0
        for query, expected_fragments in REPRESENTATIVE_QUERIES:
            chroma_res = chroma_kb.search(query, top_k=5)
            fb_res = fallback_kb.search(query, top_k=5)

            # 向量路径应可运行并返回结果
            assert chroma_res, f"ChromaDB 路径对『{query}』返回为空"
            # 回退路径非空且命中预期来源，证明其可作为向量检索的替代
            assert fb_res, f"TF-IDF 路径对『{query}』返回为空"
            fb_sources = " ".join(r["source"] for r in fb_res)
            assert any(frag in fb_sources for frag in expected_fragments), (
                f"『{query}』回退结果未命中预期来源 {expected_fragments}，"
                f"实际: {fb_sources}"
            )

            if {r["source"] for r in chroma_res} & {r["source"] for r in fb_res}:
                overlap_count += 1

        # 两路径在部分查询上应存在来源交集，佐证回退方向与向量检索一致
        assert overlap_count >= 1, "两路径在所有代表性查询上均无来源交集"
