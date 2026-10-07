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
import re
from langchain_core.tools import tool
from local_knowledge import get_knowledge_base

logger = logging.getLogger(__name__)

# 知识库数据集标识（当前本地模式未使用，保留用于未来扩展为远程知识库）
DATASET_NAME = "audit_regulations_kb"


def _extract_year(text: str):
    """从日期/年份/报告期文本中提取 4 位年份（int），无年份信息返回 None。"""
    match = re.search(r"(19|20)\d{2}", str(text or ""))
    return int(match.group(0)) if match else None


def _temporal_warnings(results, report_period: str) -> list:
    """E2 时间门控：比对语料施行年份与被检查报告期，晚于报告期的语料出警示。

    报告期未提供或语料无施行日期时不判定（不误报）；年份级比较取各自首个
    4 位年份，属筛查级因果提示而非法律意见。
    """
    period_year = _extract_year(report_period)
    if period_year is None:
        return []
    warnings = []
    seen = set()
    for r in results:
        effective = str(r.get("effective", "") or "")
        eff_year = _extract_year(effective)
        if eff_year is None or eff_year <= period_year:
            continue
        source = str(r.get("source", "") or "")
        if source in seen:
            continue
        seen.add(source)
        warnings.append(
            f"⚠️ 时点提示：来源「{source}」施行于 {effective}，晚于被检查报告期"
            f"（{report_period}）。不得援引其认定报告期行为的合规性或据此直接定级，"
            "仅可用于设计核查程序；如需引用须在报告中注明时点差异。")
    return warnings


@tool
def search_regulations(query: str, report_period: str = "") -> str:
    """检索审计法规知识库，获取与风险关键词相关的法规条文和处罚案例。

    本工具是 Agent 风险分析流程中的关键一环：Agent 在识别出某项风险后，
    须调用本工具为每条风险匹配具体的法规依据（regulatory_basis）和
    同类处罚案例参考（case_reference），确保结论有据可依。

    Args:
        query: 风险关键词字符串，建议使用具体风险描述以提高检索精度。
               示例："存贷双高货币资金"、"应收账款虚增收入"、
               "商誉减值"、"关联方资金占用"、"连续亏损持续经营"
        report_period: 被检查报告的报告期（如 "2023年度"、"2024-06-30"）。
               建议尽量传入：施行日期晚于报告期的语料（如 2026 年修订办法）
               会附带时点警示，提示不得用于认定报告期行为的合规性（时间先后因果）。

    Returns:
        格式化的检索结果文本，每条包含相关度得分、来源文件名、施行日期（如有）
        和正文内容；施行日期晚于报告期的来源附带时点警示。
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

        # E2 时间门控：施行日期晚于报告期的语料生成时点警示（置于结果之前，
        # 确保 LLM 在引用条文前先看到因果约束）
        warnings = _temporal_warnings(results, report_period)

        # 格式化每条检索结果为统一文本格式
        output_parts = []
        for i, r in enumerate(results):
            score = r.get("score", "N/A")      # 相关度得分（向量相似度或TF-IDF余弦相似度）
            content = r.get("content", "")     # 法规/案例正文内容
            source = r.get("source", "")        # 来源文件名（如 "审计准则1211号.txt"）
            effective = str(r.get("effective", "") or "")
            effective_part = f", 施行: {effective}" if effective else ""
            # 按编号输出，附带相关度和来源信息
            output_parts.append(f"【检索结果 {i+1}】(相关度: {score}, 来源: {source}{effective_part})\n{content}")

        # 用分隔线连接所有结果，便于 LLM 解析
        output = "\n\n---\n\n".join(output_parts)

        # 检索质量指标：输出 top-k 相关度分布，供评估和调试使用
        scores = [r.get("score", 0) for r in results if isinstance(r.get("score"), (int, float))]
        if scores:
            avg_score = sum(scores) / len(scores)
            score_dist = "/".join(f"{s:.2f}" for s in sorted(scores, reverse=True)[:5])
            output += f"\n\n---\n【检索质量】返回 {len(results)} 条结果，平均相关度: {avg_score:.3f}，Top-5 分布: {score_dist}"

        if warnings:
            output = "\n".join(warnings) + "\n\n---\n\n" + output

        logger.info(f"知识库检索完成，query='{query}'，返回 {len(results)} 条结果，平均相关度: {sum(scores)/len(scores):.3f}" if scores else f"知识库检索完成，query='{query}'，返回 {len(results)} 条结果")
        return output

    except Exception as e:
        logger.error(f"知识库检索异常: {e}")
        return f"知识库检索异常: {str(e)}"
