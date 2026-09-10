"""智能投资参考卡工具（C 端轻量工具）

基于综合风险评分与财务指标分析结果，为普通投资者生成结构化的
「投资参考卡」：四档参考位、投资亮点/风险警示 TOP3、关注检查清单、
风险承受度画像匹配。

合规定位（重要）：本工具是「风险提示」而非荐股——
- 四档参考位（关注/中性/谨慎/回避）表示风险关注优先级，不构成任何投资建议；
- 输出全程禁止出现「买入」「卖出」等证券投资咨询措辞（有测试断言锁定）；
- 输出强制携带免责声明字段（disclaimer），前端渲染须展示。

参考位映射（由综合风险分 0-100 反向推导，规则可追溯）：
- 0-25 低风险   → 关注（风险面干净，可纳入进一步研究范围）
- 26-50 中等风险 → 中性（存在一定风险信号，保持观察）
- 51-75 高风险   → 谨慎（风险信号较多，深入核查前不宜下结论）
- 76-100 极高风险 → 回避（重大风险信号密集，风险提示级别最高）
"""
import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 四档参考位映射表（上限, 名称, 标识, 命中规则说明）──
ADVICE_TIERS = [
    (25, "关注", "watch", "综合风险分 0-25（低风险）：风险面较干净，可纳入进一步研究范围"),
    (50, "中性", "neutral", "综合风险分 26-50（中等风险）：存在一定风险信号，建议保持观察"),
    (75, "谨慎", "caution", "综合风险分 51-75（高风险）：风险信号较多，深入核查前不宜下结论"),
    (100, "回避", "avoid", "综合风险分 76-100（极高风险）：重大风险信号密集，风险提示级别最高"),
]

# ── 风险承受度画像 × 参考位适配表 ──
# 语义为「该风险等级与此类风险承受度的匹配关系」，不是操作建议
PROFILE_MATRIX = {
    "watch":   {"保守型": "适配：风险指标处于低位，符合低波动偏好",
                "稳健型": "适配：基本面风险面干净，可作研究备选",
                "激进型": "适配：低风险标的，超额收益弹性需另行评估"},
    "neutral": {"保守型": "部分适配：存在风险信号，需结合自身承受度评估",
                "稳健型": "适配：风险中等，建议跟踪风险信号变动",
                "激进型": "适配：风险中等，波动容忍度内"},
    "caution": {"保守型": "不适配：风险信号较多，超出低风险偏好范围",
                "稳健型": "部分适配：仅在完成深入核查后再评估",
                "激进型": "部分适配：高风险高波动，须严格控制敞口"},
    "avoid":   {"保守型": "不适配：重大风险信号密集",
                "稳健型": "不适配：风险提示级别最高",
                "激进型": "不适配：不确定性超出常规风险管理范围"},
}

# ── 强制免责声明（与 domain_guard.DISCLAIMER_MARKER「AI 辅助生成」措辞一致）──
DISCLAIMER = (
    "本参考卡由 AI 辅助生成，仅为基于年报公开信息的风险提示参考，"
    "不构成任何投资建议或证券投资咨询意见；市场有风险，决策需独立判断并自担风险。"
)

# ── 亮点提取规则表（指标键, 判断函数, 亮点文案模板）──
# 判断函数入参为指标值（已确保 float），返回 True 表示命中亮点
_HIGHLIGHT_RULES = [
    ("gross_margin_pct", lambda v: v >= 30,
     "毛利率 {v}%，盈利空间较厚"),
    ("operating_cashflow_to_net_profit_ratio", lambda v: v >= 1.0,
     "经营现金流/净利润 = {v}，利润现金含量充足"),
    ("current_ratio", lambda v: v >= 2.0,
     "流动比率 {v}，短期偿债能力较强"),
    ("debt_to_asset_ratio_pct", lambda v: v <= 40,
     "资产负债率 {v}%，杠杆水平稳健"),
    ("revenue_yoy_change_pct", lambda v: v >= 10,
     "营收同比增长 {v}%，成长性较好"),
    ("net_profit_yoy_change_pct", lambda v: v >= 10,
     "净利润同比增长 {v}%，盈利趋势向好"),
]

# ── 关注检查清单：基础项 + 按风险维度追加项 ──
_CHECKLIST_BASE = [
    "核对最新一期定期报告与业绩预告是否有重大变动",
    "查询公司及董监高近期是否有监管处罚或问询函",
]
_CHECKLIST_BY_DIM = {
    "financial": "复核财务预警指标明细（现金流、应收、存货、商誉）的最新变动",
    "disclosure": "核查信息披露完整性与前后期口径一致性",
    "validation": "关注财务数据勾稽差异项是否在后续报告中更正",
}


def _load_dict(raw) -> dict:
    """JSON 字符串安全解析为 dict：失败或非 dict 一律返回空 dict。

    照抄 risk_scorer 的类型守卫模式——LLM 从会话摘要重构入参时
    可能传标量/列表（已实证的事故形态），不守卫则 .get 抛异常炸流。
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _map_tier(score: float) -> tuple:
    """按综合风险分映射四档参考位，返回 (名称, 标识, 命中规则说明)。"""
    for threshold, name, key, rule in ADVICE_TIERS:
        if score <= threshold:
            return name, key, rule
    return "回避", "avoid", ADVICE_TIERS[-1][3]


def _pick_highlights(indicators_data: dict) -> list:
    """从财务指标中按规则表提取投资亮点 TOP3（每条带指标依据）。"""
    indicators = indicators_data.get("indicators")
    if not isinstance(indicators, dict):
        return []

    # 亏损企业的 OCF/NP 比值会因负负得正误命中「现金含量充足」（实测触发过：
    # -1.2/-0.8=1.5），净利润非正时该亮点无意义，直接跳过。
    # 双信号判负：LLM 重构入参时可能丢字段，除 net_profit_current 外，
    # 同比变动 < -100% 在上期为正时数学上等价于本期为负（上期为负时
    # calculator 走定性描述分支不产生该百分比），两者任一命中即判负
    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    np_current = _to_float(indicators.get("net_profit_current"))
    np_yoy = _to_float(indicators.get("net_profit_yoy_change_pct"))
    np_negative = ((np_current is not None and np_current <= 0)
                   or (np_yoy is not None and np_yoy < -100))

    highlights = []
    for key, hit, template in _HIGHLIGHT_RULES:
        if key == "operating_cashflow_to_net_profit_ratio" and np_negative:
            continue
        val = indicators.get(key)
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if hit(val):
            highlights.append({
                "point": template.format(v=round(val, 2)),
                "reason": f"指标 {key}={round(val, 2)} 命中亮点规则",
            })
        if len(highlights) >= 3:
            break
    return highlights


def _pick_warnings(indicators_data: dict, score_data: dict, reg_alerts=None) -> list:
    """提取风险警示 TOP3：优先财务预警 alerts，次取联网监管问询，再补评分抬升理由。"""
    warnings = []

    alerts = indicators_data.get("alerts")
    if isinstance(alerts, (list, tuple)):
        for alert in alerts[:3]:
            warnings.append({
                "point": str(alert),
                "reason": "来自财务指标预警（calculate_financial_indicators）",
            })

    # 联网监管问询/处罚记录（search_regulatory_inquiries 证据）
    for title in (reg_alerts or []):
        if len(warnings) >= 3:
            break
        warnings.append({
            "point": str(title),
            "reason": "来自监管问询在线查询（search_regulatory_inquiries）",
        })

    reasons = score_data.get("escalation_reasons")
    if isinstance(reasons, (list, tuple)):
        for r in reasons:
            if len(warnings) >= 3:
                break
            warnings.append({
                "point": str(r),
                "reason": "来自综合评分的模型/审计意见抬升项",
            })
    return warnings[:3]


def _build_checklist(score_data: dict) -> list:
    """基础检查项 + 按三维度分解分数追加的针对性检查项（≥40 分才追加）。"""
    checklist = list(_CHECKLIST_BASE)
    breakdown = score_data.get("breakdown")
    if isinstance(breakdown, dict):
        for dim, item in _CHECKLIST_BY_DIM.items():
            try:
                dim_score = float(breakdown.get(dim, 0))
            except (TypeError, ValueError):
                continue
            if dim_score >= 40:
                checklist.append(item)
    return checklist


def _match_profiles(tier_key: str) -> dict:
    """按参考位返回三类风险承受度画像的匹配说明。"""
    return PROFILE_MATRIX.get(tier_key, PROFILE_MATRIX["neutral"])


def _extract_reg_alerts(reg_data: dict) -> list:
    """从 search_regulatory_inquiries 输出提取监管问询/处罚标题 TOP3（联网证据）。

    类型守卫照抄 _load_dict 模式：items 可能缺失/非列表/元素非字典，逐一容错。
    """
    if not isinstance(reg_data, dict):
        return []
    items = reg_data.get("items")
    if not isinstance(items, list):
        return []
    alerts = []
    for it in items:
        if isinstance(it, dict) and it.get("title"):
            alerts.append(str(it["title"]))
        if len(alerts) >= 3:
            break
    return alerts


def _extract_industry_sentiment(outlook_data: dict):
    """从 industry_outlook 输出提取景气度温度计与展望（联网证据，不参与财务评分）。"""
    if not isinstance(outlook_data, dict):
        return None
    thermometer = outlook_data.get("thermometer")
    if thermometer is None:
        return None
    return {
        "thermometer": thermometer,
        "outlook_3_6m": outlook_data.get("outlook_3_6m", ""),
        "data_mode": outlook_data.get("data_mode", ""),
    }


@tool
def investment_advisor(
    comprehensive_score_json: str = "{}",
    financial_indicators_json: str = "{}",
    regulatory_inquiry_json: str = "{}",
    industry_outlook_json: str = "{}"
) -> str:
    """生成面向普通投资者的「投资参考卡」（风险提示定位，非投资建议）。

    基于综合风险评分与财务指标分析结果，输出四档参考位（关注/中性/谨慎/回避）、
    投资亮点 TOP3、风险警示 TOP3、关注检查清单与风险承受度画像匹配。
    参考位由综合风险分区间规则映射（可追溯），不构成证券投资咨询意见，
    输出禁止出现「买入」「卖出」等措辞，且强制携带免责声明。

    Args:
        comprehensive_score_json: calculate_comprehensive_score 的输出 JSON
        financial_indicators_json: calculate_financial_indicators 的输出 JSON
        regulatory_inquiry_json: 可选，search_regulatory_inquiries 的输出 JSON；
            含监管问询/处罚记录时参考位下限抬升至「谨慎」（仅给公司名也能出非中性卡）
        industry_outlook_json: 可选，industry_outlook 的输出 JSON；
            提取景气度温度计与展望作为参考依据（不参与财务评分）

    Returns:
        JSON 字符串，包含：
        - tier / tier_key: 参考位名称与标识（watch/neutral/caution/avoid）
        - tier_rule: 参考位命中的区间规则（可追溯）
        - risk_score: 所依据的综合风险分
        - highlights: 投资亮点 TOP3（每条带指标依据）
        - warnings: 风险警示 TOP3（每条带来源说明）
        - checklist: 关注检查清单
        - investor_profiles: 保守/稳健/激进三类画像的匹配说明
        - disclaimer: 免责声明（前端渲染必须展示）
    """
    # 防御纵深：入参解析层已有类型守卫，此处再包一层，任何意外异常
    # 都降级为带 tier/disclaimer 字段的可见错误 JSON，不炸掉分析流
    try:
        score_data = _load_dict(comprehensive_score_json)
        indicators_data = _load_dict(financial_indicators_json)
        reg_data = _load_dict(regulatory_inquiry_json)
        outlook_data = _load_dict(industry_outlook_json)

        try:
            score = float(score_data.get("score", 50.0))
        except (TypeError, ValueError):
            score = 50.0
        score = min(100.0, max(0.0, score))

        tier, tier_key, tier_rule = _map_tier(score)

        # 联网监管证据：监管问询/处罚记录把参考位下限抬升至「谨慎」（可追溯），
        # 使「仅给公司名」场景也能产出有依据的非中性参考卡
        reg_alerts = _extract_reg_alerts(reg_data)
        if reg_alerts and tier_key in ("watch", "neutral"):
            tier, tier_key, tier_rule = (
                "谨慎", "caution",
                "监管问询在线查询发现该公司存在监管问询/处罚记录，参考位下限抬升至谨慎")

        # 联网行业新闻景气度（作为参考依据，不参与财务评分）
        industry_sentiment = _extract_industry_sentiment(outlook_data)

        result = {
            "tier": tier,
            "tier_key": tier_key,
            "tier_rule": tier_rule,
            "risk_score": round(score, 1),
            "risk_level": score_data.get("level", ""),
            "highlights": _pick_highlights(indicators_data),
            "warnings": _pick_warnings(indicators_data, score_data, reg_alerts),
            "checklist": _build_checklist(score_data),
            "investor_profiles": _match_profiles(tier_key),
            "regulatory_alerts": reg_alerts,
            "industry_sentiment": industry_sentiment,
            "disclaimer": DISCLAIMER,
        }

        logger.info(f"投资参考卡生成：参考位={tier}（风险分 {score}）")
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001 - 降级可见：保留 tier/disclaimer 关键字段
        logger.warning(f"投资参考卡生成异常，降级输出: {e}")
        return json.dumps({
            "tier": "中性",
            "tier_key": "neutral",
            "error": f"投资参考卡生成发生异常：{e}",
            "disclaimer": DISCLAIMER,
        }, ensure_ascii=False, indent=2)
