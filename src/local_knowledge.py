"""本地知识库检索 - 基于 ChromaDB 向量语义检索

使用 ChromaDB 向量数据库替代原有的 TF-IDF 文本匹配，
通过 embedding 语义向量化实现更高质量的法规条文和案例检索。
知识库数据来源于 knowledge_base/ 目录下的 txt 文件。
"""
import os
import json
import logging
import threading
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


class LocalKnowledgeBase:
    """基于 ChromaDB 向量数据库的本地知识库"""

    def __init__(self, kb_dir: str = None):
        workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), ".."))
        self.kb_dir = kb_dir or os.path.join(workspace, "knowledge_base")
        self.documents: List[Dict] = []
        self._collection = None
        self._loaded = False
        self._load_lock = threading.Lock()

    def _get_chroma_client(self):
        """获取 ChromaDB 持久化客户端（延迟导入，避免启动时阻塞）"""
        import chromadb
        # 持久化目录放在项目根目录下的 .chroma_db 子目录
        workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), ".."))
        persist_dir = os.path.join(workspace, ".chroma_db")
        os.makedirs(persist_dir, exist_ok=True)
        return chromadb.PersistentClient(path=persist_dir)

    def load(self):
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if not os.path.exists(self.kb_dir):
                logger.warning(f"知识库目录不存在: {self.kb_dir}")
                self._loaded = True
                return

            # 读取所有 txt 文件并分块（优先按“第X条”分块，回退到按段落分块）
            chunks = []
            metadatas = []
            for fname in sorted(os.listdir(self.kb_dir)):
                if not fname.endswith(".txt"):
                    continue
                fpath = os.path.join(self.kb_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    # 策略：优先按法规条款分块（匹配“第X条”模式），提升检索精度
                    import re
                    article_splits = re.split(r'(?=第[一二三四五六七八九十百千\d]+条)', content)
                    if len(article_splits) > 3:
                        # 法规文本：按条款分块，每块保留条款号作为上下文
                        file_chunks = [c.strip() for c in article_splits if c.strip() and len(c.strip()) > 20]
                    else:
                        # 非法规文本（如案例分析）：按双换行分块
                        file_chunks = [c.strip() for c in content.split("\n\n") if c.strip()]
                    for i, chunk in enumerate(file_chunks):
                        chunks.append(chunk)
                        metadatas.append({"source": fname, "chunk_id": i})
                        # 保留原始文档列表用于兼容外部接口
                        self.documents.append({
                            "source": fname,
                            "chunk_id": i,
                            "content": chunk,
                        })
                except Exception as e:
                    logger.warning(f"加载知识库文件失败 {fname}: {e}")

            if not chunks:
                self._loaded = True
                return

            # 初始化 ChromaDB 并建立向量索引
            try:
                client = self._get_chroma_client()
                # 使用或创建 collection（如果已存在则复用缓存）
                collection_name = "audit_regulations"
                # 内容指纹：对全部分块内容 + 块数算 md5，感知「内容变化」而非仅「块数变化」
                # 解决原 count() 比较无法发现「原地修改法规内容但条款数不变」的脏缓存问题
                import hashlib
                fingerprint = hashlib.md5(
                    ("\x1f".join(chunks) + f"|n={len(chunks)}").encode("utf-8")
                ).hexdigest()
                try:
                    self._collection = client.get_collection(collection_name)
                    cached_fp = (self._collection.metadata or {}).get("content_fingerprint")
                    # 指纹不一致（内容或块数变化）或缺失则重建
                    if cached_fp != fingerprint:
                        client.delete_collection(collection_name)
                        raise ValueError("rebuild")
                    logger.info(f"ChromaDB 缓存命中（指纹一致），共 {self._collection.count()} 个文档块")
                except (ValueError, Exception):
                    # 创建新 collection 并写入文档（metadata 记录内容指纹用于下次校验）
                    self._collection = client.create_collection(
                        name=collection_name,
                        metadata={"hnsw:space": "cosine", "content_fingerprint": fingerprint},
                    )
                    # 批量写入文档（ChromaDB 自动使用默认 embedding 函数）
                    ids = [f"doc_{i}" for i in range(len(chunks))]
                    # 分批写入，每批最多 100 条，避免内存溢出
                    batch_size = 100
                    for start in range(0, len(chunks), batch_size):
                        end = min(start + batch_size, len(chunks))
                        self._collection.add(
                            documents=chunks[start:end],
                            metadatas=metadatas[start:end],
                            ids=ids[start:end],
                        )
                    logger.info(f"ChromaDB 向量索引构建完成，共 {len(chunks)} 个文档块")
            except Exception as e:
                logger.warning(f"ChromaDB 初始化失败，回退到 TF-IDF 模式: {e}")
                self._collection = None
                self._build_tfidf_fallback()

            self._loaded = True
            logger.info(f"本地知识库加载完成，共 {len(self.documents)} 个文档块")

    def _build_tfidf_fallback(self):
        """ChromaDB 不可用时的 TF-IDF 回退方案"""
        import math
        from collections import Counter
        import re

        self._idf = {}
        self._tfidf_vectors = []

        # 分词
        for doc in self.documents:
            doc["tokens"] = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}|\d+', doc["content"].lower())

        # 构建 IDF
        n = len(self.documents)
        if n == 0:
            return
        df = Counter()
        for doc in self.documents:
            for t in set(doc["tokens"]):
                df[t] += 1
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}

    def _tfidf_search_fallback(self, query: str, top_k: int, min_score: float) -> List[Dict]:
        """TF-IDF 回退检索"""
        import math
        import re
        from collections import Counter

        # ChromaDB 初始化成功但运行时 query 抛异常时，_build_tfidf_fallback 尚未执行，
        # 此处惰性构建 IDF 与分词索引，确保回退路径可用（否则会因缺少 _idf 抛 AttributeError）。
        if not getattr(self, "_idf", None):
            self._build_tfidf_fallback()

        q_tokens = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}|\d+', query.lower())
        tf = Counter(q_tokens)
        total = len(q_tokens) or 1
        q_vec = {t: (tf[t] / total) * self._idf.get(t, 1.0) for t in set(q_tokens)}

        scored = []
        for doc in self.documents:
            dtf = Counter(doc.get("tokens", []))
            dt = len(doc.get("tokens", [])) or 1
            d_vec = {t: (dtf[t] / dt) * self._idf.get(t, 1.0) for t in set(doc.get("tokens", []))}
            keys = set(q_vec) & set(d_vec)
            if not keys:
                continue
            dot = sum(q_vec[k] * d_vec[k] for k in keys)
            na = math.sqrt(sum(v * v for v in q_vec.values()))
            nb = math.sqrt(sum(v * v for v in d_vec.values()))
            sim = dot / (na * nb) if na and nb else 0.0
            if sim >= min_score:
                scored.append((sim, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, doc in scored[:top_k]:
            results.append({
                "content": doc["content"],
                "score": round(sim, 4),
                "source": doc["source"],
            })
        return results

    def search(self, query: str, top_k: int = 8, min_score: float = 0.05) -> List[Dict]:
        """语义检索：通过 ChromaDB 向量相似度匹配，返回最相关的法规条文和案例。

        Args:
            query: 检索查询文本
            top_k: 返回结果数量上限
            min_score: 最低相关度阈值（0~1），过滤低质量匹配

        Returns:
            检索结果列表，每条包含 content/score/source 字段
        """
        self.load()
        if not self.documents:
            return []

        # 优先使用 ChromaDB 向量检索
        if self._collection is not None:
            try:
                results = self._collection.query(
                    query_texts=[query],
                    n_results=min(top_k, len(self.documents)),
                )
                output = []
                if results and results["documents"] and results["documents"][0]:
                    docs = results["documents"][0]
                    dists = results["distances"][0] if results.get("distances") else [0] * len(docs)
                    metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)
                    for doc_text, dist, meta in zip(docs, dists, metas):
                        # ChromaDB cosine distance -> similarity score (1 - distance)
                        score = round(1.0 - dist, 4)
                        if score >= min_score:
                            output.append({
                                "content": doc_text,
                                "score": score,
                                "source": meta.get("source", "unknown"),
                            })
                return output[:top_k]
            except Exception as e:
                logger.warning(f"ChromaDB 检索失败，回退 TF-IDF: {e}")

        # 回退到 TF-IDF
        return self._tfidf_search_fallback(query, top_k, min_score)


_kb_instance: LocalKnowledgeBase = None


def get_knowledge_base() -> LocalKnowledgeBase:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = LocalKnowledgeBase()
    return _kb_instance
