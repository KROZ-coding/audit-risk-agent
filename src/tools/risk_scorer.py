"""综合风险评分工具

汇总财务指标分析、披露规范性检查、数据校验三大模块的结果，
计算 0-100 综合风险分数并映射为风险等级。

评分模型（加权）：
- 财务指标风险（权重 50%）：基于 alerts 数量和严重程度
- 披露合规风险（权重 30%）：基于 disclosure_checker 的 risk_score
- 数据校验风险（权重 20%）：基于 validate_financial_data 的 failed_checks

等级映射：
- 0-25: 低风险
- 26-50: 中等风险
- 51-75: 高风险
- 76-100: 极高风险
"""
import json
import logging
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 各模块权重配置 ──
WEIGHTS = {
    "financial": 0.50,   # 财务指标风险权重
    "disclosure": 0.30,  # 披露合规风险权重
    "validation": 0.20,  # 数据校验风险权重
}

# ── 风险等级映射 ──
RISK_LEVELS = [
    (25, "低风险", "low"),
    (50, "中等风险", "medium"),
    (75, "高风险", "high"),
    (100, "极高风险", "critical"),
]


def _get_level(score: float) -> tuple:
    """根据分数返回风险等级名称和标识。"""
    for threshold, name, key in RISK_LEVELS:
        if score <= threshold:
            return name, key
    return "极高风险", "critical"


def _calc_financial_risk(analysis_json: str) -> float:
    """从财务指标分析结果中计算财务风险分（0-100）。

    评分逻辑：
    - 每条 alert 贡献 8 分基础风险
    - 含"重大"/"严重"/"持续经营"关键词的 alert 额外 +5 分
    - 上限 100 分
    """
    try:
        data = json.loads(analysis_json) if isinstance(analysis_json, str) else analysis_json
    except (json.JSONDecodeError, TypeError):
        return 50.0  # 解析失败给中间值

    alerts = data.get("alerts", [])
    if not alerts:
        return 0.0

    score = 0.0
    severe_keywords = ["重大", "严重", "持续经营", "资不抵债", "存贷双高", "连续"]
    for alert in alerts:
        score += 8.0
        if any(kw in str(alert) for kw in severe_keywords):
            score += 5.0

    return min(100.0, score)


def _calc_disclosure_risk(disclosure_json: str) -> float:
    """从披露检查结果中获取披露风险分。"""
    try:
        data = json.loads(disclosure_json) if isinstance(disclosure_json, str) else disclosure_json
    except (json.JSONDecodeError, TypeError):
        return 30.0  # 解析失败给较低默认值

    return float(data.get("risk_score", 30.0))


def _calc_validation_risk(validation_json: str) -> float:
    """从数据校验结果中计算校验风险分。

    评分逻辑：
    - 每项未通过校验贡献 30 分
    - 上限 100 分
    """
    try:
        data = json.loads(validation_json) if isinstance(validation_json, str) else validation_json
    except (json.JSONDecodeError, TypeError):
        return 20.0

    dv = data.get("data_validation", data)
    failed = dv.get("failed_checks", 0)
    return min(100.0, failed * 30.0)


@tool
def calculate_comprehensive_score(
    financial_analysis_json: str = "{}",
    disclosure_check_json: str = "{}",
    validation_json: str = "{}"
) -> str:
    """计算综合审计风险评分（0-100），汇总三大模块分析结果。

    将财务指标分析、披露规范性检查、数据一致性校验三个维度的结果
    按 50%/30%/20% 权重加权，输出综合风险分数和等级。

    Args:
        financial_analysis_json: calculate_financial_indicators 的输出 JSON
        disclosure_check_json: check_disclosure_compliance 的输出 JSON
        validation_json: validate_financial_data 的输出 JSON

    Returns:
        JSON 字符串，包含：
        - score: 综合风险分（0-100，越高风险越大）
        - level: 风险等级（低风险/中等风险/高风险/极高风险）
        - level_key: 等级标识（low/medium/high/critical）
        - breakdown: 三维度分解分数
        - weights: 各维度权重
        - summary: 一句话风险总结
    """
    # 计算各维度风险分
    financial_risk = _calc_financial_risk(financial_analysis_json)
    disclosure_risk = _calc_disclosure_risk(disclosure_check_json)
    validation_risk = _calc_validation_risk(validation_json)

    # 加权计算综合分
    score = (
        financial_risk * WEIGHTS["financial"]
        + disclosure_risk * WEIGHTS["disclosure"]
        + validation_risk * WEIGHTS["validation"]
    )
    score = round(min(100, max(0, score)), 1)

    # 映射风险等级
    level, level_key = _get_level(score)

    # 生成一句话总结
    level_desc = {
        "low": "该公司年报整体风险较低，各项指标基本正常",
        "medium": "该公司年报存在一定风险信号，建议关注相关指标变动",
        "high": "该公司年报存在较多风险信号，建议重点核查并追加审计程序",
        "critical": "该公司年报存在重大风险信号，强烈建议全面深入审计",
    }
    summary = level_desc.get(level_key, "")

    result = {
        "score": score,
        "level": level,
        "level_key": level_key,
        "breakdown": {
            "financial": round(financial_risk, 1),
            "disclosure": round(disclosure_risk, 1),
            "validation": round(validation_risk, 1),
        },
        "weights": WEIGHTS,
        "summary": summary,
    }

    logger.info(f"综合风险评分：{score} 分（{level}）")
    return json.dumps(result, ensure_ascii=False, indent=2)
