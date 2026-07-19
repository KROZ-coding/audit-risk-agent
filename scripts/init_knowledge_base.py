"""初始化知识库 - 将法规文件导入本地知识库（TF-IDF 检索）"""
import os
import sys
import glob
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 使用本地兼容层，无需 coze_coding_dev_sdk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from local_knowledge import LocalKnowledgeBase


def init_knowledge_base():
    workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), ".."))
    kb_dir = os.path.join(workspace, "knowledge_base")

    if not os.path.exists(kb_dir):
        logger.error(f"知识库目录不存在: {kb_dir}")
        sys.exit(1)

    txt_files = sorted(glob.glob(os.path.join(kb_dir, "*.txt")))
    if not txt_files:
        logger.error("未找到任何知识库文件")
        sys.exit(1)

    logger.info(f"找到 {len(txt_files)} 个知识库文件")

    kb = LocalKnowledgeBase(kb_dir=kb_dir)
    kb.load()

    logger.info(f"知识库初始化完成，共加载 {len(kb.documents)} 个文档块")

    # 简单验证检索是否可用
    test_query = "审计准则 重大错报风险"
    results = kb.search(test_query, top_k=3)
    if results:
        logger.info(f"验证检索成功，查询 '{test_query}' 返回 {len(results)} 条结果:")
        for r in results:
            logger.info(f"  [{r['score']:.4f}] {r['source']}: {r['content'][:80]}...")
    else:
        logger.warning("验证检索未返回结果，请检查知识库文件内容")


if __name__ == "__main__":
    init_knowledge_base()
