"""知识库检索工具 - 从法规与案例知识库中检索相关条文和案例

本模块封装了基于 ChromaDB 向量语义检索的知识库能力，为审计风险分析提供法规依据。
知识库数据来源于 knowledge_base/ 目录下的 txt 文件，涵盖：
- 审计准则（如 1211 号重大错报风险识别、1231 号风险应对措施）
- 证监会法规（信息披露管理办法、年报内容格式准则等）
- 典型处罚案例（内幕交易、操纵市场、上市公司违规等）

底层使用 ChromaDB 向量数据库进行语义相似度匹配（embedding 向量化），
相比 TF-IDF 关键词匹配能更准确地理解法规条文的语义含义。
若 ChromaDB 不可用，自动回退到 TF-IDF 余弦相似度检索。

Agent 在识别每条风险后，须调用本工具检索法规条文和同类案例，
确保风险结论有据可依，避免无依据编造。
"""
import logging
from langchain_core.tools import tool
from local_knowledge import get_knowledge_base

logger = logging.getLogger(__name__)

# 知识库数据集标识（当前本地模式未使用，保留用于未来扩展为远程知识库）
DATASET_NAME = "audit_regulations_kb"


@tool
def search_regulations(query: str) -> str:
    """检索审计法规知识库，获取与风险关键词相关的法规条文和处罚案例。

    本工具是 Agent 风险分析流程中的关键一环：Agent 在识别出某项风险后，
    须调用本工具为每条风险匹配具体的法规依据（regulatory_basis）和
    同类处罚案例参考（case_reference），确保结论有据可依。

    检索机制：
    - 底层使用 ChromaDB 向量语义检索（embedding 向量化 + 余弦相似度）
    - 若 ChromaDB 不可用，自动回退到 TF-IDF 余弦相似度
    - 返回相关度最高的 top_k=8 条结果
    - 最低相关度阈值 min_score=0.05，过滤低质量匹配

    Args:
        query: 风险关键词字符串，建议使用具体风险描述以提高检索精度。
               示例："存贷双高货币资金"、"应收账款虚增收入"、
               "商誉减值"、"关联方资金占用"、"连续亏损持续经营"

    Returns:
        格式化的检索结果文本，每条结果包含相关度得分、来源文件名和正文内容。
        若未检索到结果，返回提示要求 Agent 标注「信息不足，需人工核查」。
        若检索过程异常，返回错误信息。
    """
    try:
        # 获取知识库单例对象（首次调用时自动加载 txt 文件并构建向量索引）
        kb = get_knowledge_base()
        # 执行检索：返回最多 8 条结果，过滤相关度低于 0.05 的噪声匹配
        results = kb.search(query=query, top_k=8, min_score=0.05)

        # 无结果时返回引导提示，明确要求 Agent 不要编造法规依据
        if not results:
            return "⚠️ 未检索到相关法规条文或案例。请在风险报告中将 regulatory_basis 和 case_reference 标注为「信息不足，需人工核查」，切勿编造法规依据。"

        # 格式化每条检索结果为统一文本格式
        output_parts = []
        for i, r in enumerate(results):
            score = r.get("score", "N/A")      # 相关度得分（向量相似度或TF-IDF余弦相似度）
            content = r.get("content", "")     # 法规/案例正文内容
            source = r.get("source", "")        # 来源文件名（如 "审计准则1211号.txt"）
            # 按编号输出，附带相关度和来源信息
            output_parts.append(f"【检索结果 {i+1}】(相关度: {score}, 来源: {source})\n{content}")

        # 用分隔线连接所有结果，便于 LLM 解析
        output = "\n\n---\n\n".join(output_parts)
        logger.info(f"知识库检索完成，query='{query}'，返回 {len(results)} 条结果")
        return output

    except Exception as e:
        logger.error(f"知识库检索异常: {e}")
        return f"知识库检索异常: {str(e)}"
