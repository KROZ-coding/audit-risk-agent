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
import json
import os
import logging
from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _load_weights() -> dict:
    """F3 权重外置：默认 50/30/20 为内部筛查权重（无经验校准依据，透明声明），
    可用环境变量覆盖（RISK_WEIGHT_FINANCIAL / RISK_WEIGHT_DISCLOSURE /
    RISK_WEIGHT_VALIDATION），三者和按比例归一化到 1。"""
    defaults = {"financial": 0.50, "disclosure": 0.30, "validation": 0.20}
    try:
        for key in defaults:
            raw = os.getenv(f"RISK_WEIGHT_{key.upper()}")
            if raw:
                defaults[key] = max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError) as e:
        logger.warning(f"评分权重环境变量解析失败，使用默认权重: {e}")
    total = sum(defaults.values())
    if total <= 0:
        return {"financial": 0.50, "disclosure": 0.30, "validation": 0.20}
    return {k: v / total for k, v in defaults.items()}


# ── 各模块权重配置（F3：可经环境变量覆盖，输出中随评分透出实际生效值）──
WEIGHTS = _load_weights()

# ── 风险等级映射 ──
RISK_LEVELS = [
    (25, "低风险", "low"),
    (50, "中等风险", "medium"),
    (75, "高风险", "high"),
    (100, "极高风险", "critical"),
]

_SCORE_INPUT_SOURCES = {
    "financial": "calculate_financial_indicators",
    "disclosure": "check_disclosure_compliance",
    "validation": "validate_financial_data",
}


def _score_records(risks: dict, total_score=None, total_status="calculated",
                   total_reason="") -> tuple[list[dict], list[dict], list[dict]]:
    """把评分输入和总分包装为统一的事实、指标、证据记录。

    评分器消费的是其他确定性工具的结构化结果，因此这里记录来源工具和
    评分状态，不把缺失维度伪装成零分，也不把评分器的派生值冒充年报原始事实。
    """
    facts = []
    metrics = []
    evidence = []
    input_fact_ids = []
    input_metric_ids = []
    for key in ("financial", "disclosure", "validation"):
        value = risks.get(key)
        source = _SCORE_INPUT_SOURCES[key]
        fact_id = f"F-SCORE-{key.upper()}"
        metric_id = f"score_dimension_{key}"
        evidence_id = f"E-SCORE-{key.upper()}"
        value_status = "calculated" if isinstance(value, (int, float)) and value == value else "insufficient_data"
        value_reason = "" if value_status == "calculated" else f"{source}未提供可用评分输入"
        fact = make_fact(
            f"{key}_risk_score", value, fact_id=fact_id, unit="分",
            source_document=source, extraction_method="deterministic_score",
        ).to_dict()
        fact["status"] = value_status
        facts.append(fact)
        metrics.append(MetricResult(
            metric_id=metric_id,
            name={"financial": "财务指标风险分", "disclosure": "披露合规风险分",
                  "validation": "数据校验风险分"}[key],
            formula="来源工具结果→确定性评分规则",
            inputs=[{"field": f"{key}_risk_score", "fact_id": fact_id,
                      "raw_value": fact.get("raw_value", ""), "value": value,
                      "unit": "分", "source": source}],
            unit="分", value=value, display_value="未获取" if value is None else str(round(value, 1)),
            status=value_status, reason=value_reason, evidence_ids=[evidence_id],
        ).to_dict())
        evidence.append(Evidence(
            evidence_id=evidence_id, source_type="deterministic_score_input",
            source_document=source, excerpt=(f"{source}输出的评分输入："
                                             f"{'未获取' if value is None else round(value, 1)}分"),
            fact_ids=[fact_id], metric_ids=[metric_id],
            verified=value_status == "calculated", status=value_status,
        ).to_dict())
        input_fact_ids.append(fact_id)
        input_metric_ids.append(metric_id)

    total_fact_id = "F-SCORE-TOTAL"
    total_metric_id = "score_total"
    total_evidence_id = "E-SCORE-TOTAL"
    total_fact = make_fact(
        "comprehensive_risk_score", total_score, fact_id=total_fact_id, unit="分",
        source_document="calculate_comprehensive_score",
        extraction_method="deterministic_score",
    ).to_dict()
    total_fact["status"] = total_status
    facts.append(total_fact)
    metrics.append(MetricResult(
        metric_id=total_metric_id, name="综合风险评分",
        formula="各可用维度按有效权重归一化加权 + 适用规则抬升",
        inputs=[{"field": "dimension_scores", "fact_id": fact_id, "metric_id": metric_id}
                for fact_id, metric_id in zip(input_fact_ids, input_metric_ids)],
        unit="分", value=total_score,
        display_value="未获取" if total_score is None else str(round(total_score, 1)),
        status=total_status, reason=total_reason, evidence_ids=[total_evidence_id],
    ).to_dict())
    evidence.append(Evidence(
        evidence_id=total_evidence_id, source_type="deterministic_score",
        source_document="calculate_comprehensive_score",
        excerpt=(f"综合评分：{'未获取' if total_score is None else round(total_score, 1)}分"),
        fact_ids=input_fact_ids + [total_fact_id], metric_ids=input_metric_ids + [total_metric_id],
        verified=total_score is not None and total_status == "calculated",
        status=total_status,
    ).to_dict())
    return facts, metrics, evidence


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

    N 补丁：解析失败/含 error/非 dict → 返回 None（维度未获取，不参与计分），
    禁止捏造默认分（实测缺陷：披露工具失败仍计 30 分）。
    """
    try:
        data = json.loads(analysis_json) if isinstance(analysis_json, str) else analysis_json
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    # 不加此守卫时 float.get 会抛 AttributeError 被 ToolNode 上抛，炸掉整条分析流
    if not isinstance(data, dict):
        return None
    if "alerts" not in data:
        return None  # N 补丁：无 alerts 字段说明指标工具未产出预警结构 → 维度未获取

    alerts = data.get("alerts", [])
    if not isinstance(alerts, (list, tuple)):
        return 50.0  # alerts 字段畸形（如 LLM 传了数字）：视同不可解析
    if not alerts:
        # F3 可评分性门控：零告警 ≠ 低风险，前提是指标充分。提取失败/字段极稀时
        # （可计算的数值指标与报表科目都几乎没有），财务维度判「未获取」返回
        # None（不参与计分）——防止"数据越缺分越低"的反向敏感：把提取失败的
        # 年报评成 0 分低风险，比高估更隐蔽。
        _numeric_indicators = sum(1 for v in (data.get("indicators") or {}).values()
                                  if isinstance(v, (int, float)))
        _statement_items = sum(1 for section in (data.get("statement_items") or {}).values()
                               if isinstance(section, dict)
                               for v in section.values() if v is not None)
        if _numeric_indicators < 5 and _statement_items < 5:
            logger.info("财务维度不可评分：零告警且指标/科目数据稀疏"
                        f"（数值指标 {_numeric_indicators}、科目项 {_statement_items}）")
            return None
        return 0.0

    score = 0.0
    severe_keywords = ["重大", "严重", "持续经营", "资不抵债", "存贷双高", "连续"]
    for alert in alerts:
        score += 8.0
        if any(kw in str(alert) for kw in severe_keywords):
            score += 5.0

    return min(100.0, score)


def _calc_disclosure_risk(disclosure_json: str) -> float:
    """从披露检查结果中获取披露风险分。

    N 补丁：解析失败/含 error/非 dict → 返回 None（维度未获取，不参与计分），
    禁止返回默认 30（实测缺陷：披露工具失败仍计 30 分、依据不存在却加权）。
    """
    try:
        data = json.loads(disclosure_json) if isinstance(disclosure_json, str) else disclosure_json
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "error" in data:
        return None

    # risk_score 字段也可能是非标量（如嵌套对象），float() 失败时同样降级
    try:
        if "risk_score" not in data:
            return None
        return float(data["risk_score"])
    except (TypeError, ValueError):
        return None


def _calc_validation_risk(validation_json: str) -> float:
    """从数据校验结果中计算校验风险分。

    评分逻辑：
    - 每项未通过校验贡献 30 分
    - 上限 100 分
    """
    try:
        data = json.loads(validation_json) if isinstance(validation_json, str) else validation_json
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None  # N 补丁：非 dict 入参 → 维度未获取（防标量/列表入参炸流）

    dv = data.get("data_validation", data)
    # data_validation 字段本身也可能是标量（LLM 写成 "data_validation": 0），同样守卫
    if not isinstance(dv, dict):
        return None
    if "failed_checks" not in dv:
        return None  # N 补丁：无 failed_checks 说明校验未执行 → 维度未获取
    failed = dv.get("failed_checks", 0)
    try:
        failed = float(failed)
    except (TypeError, ValueError):
        return None  # failed_checks 畸形（如字符串/对象）：视同不可解析
    return min(100.0, failed * 30.0)


def _calc_model_escalation(risk_models_json: str, financial_analysis_json: str = "") -> tuple:
    """根据量化模型（Z-Score / M-Score）结果计算风险分抬升幅度与说明。

    设计考量：两个经典模型是独立于本系统自研指标的第三方证据，因此不改变
    原三维度权重结构，而是作为"加成项"抬升风险分，保证：
    1. 向后兼容——不传该参数时行为与原来完全一致；
    2. 可追溯——抬升原因会写进输出，不是黑箱调整。

    抬升幅度与 knowledge_base/风险评分模型库.txt 的影响规则对应：
    双重信号并存 +20（对应风险等级不低于重大）；单一信号 +10（不低于重要）。

    基本面豁免：Z-Score 对低杠杆（资产负债率<55%）且现金流充沛（经营现金流
    为正或流动比率≥1）的样本判别力失真（如能源央企被误判入困境区），此类
    样本不再因 Z 困境区抬分——避免"提示模型失真却照常加分"的自相矛盾（实测缺陷）。

    Returns:
        (抬升分值, 说明列表)
    """
    try:
        data = json.loads(risk_models_json) if isinstance(risk_models_json, str) else (risk_models_json or {})
    except (json.JSONDecodeError, TypeError):
        return 0.0, []
    if not isinstance(data, dict):
        return 0.0, []  # 标量/列表入参：下一行 (data or {}).get 会在 truthy 标量上炸
    rm = (data or {}).get("risk_models", data) or {}
    if not isinstance(rm, dict):
        return 0.0, []

    z = rm.get("altman_z_score") or {}
    m = rm.get("beneish_m_score") or {}
    # 嵌套字段可能是标量（LLM 直接传分值而非结构体，实测触发过），不守卫则 z.get 炸
    if not isinstance(z, dict):
        z = {}
    if not isinstance(m, dict):
        m = {}
    z_distress = bool(z.get("available")) and z.get("zone") == "财务困境区"
    m_suspect = bool(m.get("available")) and str(m.get("judgement", "")).startswith("存在")

    # 基本面健康判定：Z 困境区样本若财务基本面健康，豁免 Z 抬升（M 仍独立判定）
    z_waived = False
    z_waive_note = ""
    if z_distress and financial_analysis_json:
        try:
            fd = json.loads(financial_analysis_json) if isinstance(financial_analysis_json, str) \
                else (financial_analysis_json or {})
        except (json.JSONDecodeError, TypeError):
            fd = {}
        ind = fd.get("indicators", {}) if isinstance(fd, dict) else {}
        try:
            debt = float(ind.get("debt_to_asset_ratio_pct"))
        except (TypeError, ValueError):
            debt = None
        try:
            cr = float(ind.get("current_ratio"))
        except (TypeError, ValueError):
            cr = None
        try:
            ocf = float(ind.get("operating_cashflow_current"))
        except (TypeError, ValueError):
            ocf = None
        low_leverage = debt is not None and debt < 55
        positive_cashflow = ocf is not None and ocf > 0
        sound_liquidity = cr is not None and cr >= 1
        if (low_leverage and positive_cashflow) or (low_leverage and sound_liquidity):
            z_waived = True
            z_waive_note = (f"（基本面豁免：资产负债率 {debt:.1f}%<55%"
                            + (f"、流动比率 {cr:.2f}≥1" if cr is not None else "")
                            + "，Z 模型对低杠杆/现金流充沛样本判别力失真，不抬升）")

    notes = []
    if z_distress and m_suspect and not z_waived:
        notes.append(
            f"Altman Z-Score={z.get('score')} 落入财务困境区且 Beneish M-Score={m.get('score')} "
            "超过阈值，财务困境与盈余操纵双重信号并存，风险分抬升 20"
        )
        return 20.0, notes
    if z_distress and not z_waived:
        notes.append(f"Altman Z-Score={z.get('score')}（{z.get('variant_name')}）落入财务困境区，"
                     "存在财务困境风险嫌疑，风险分抬升 10")
        return 10.0, notes
    if z_distress and z_waived:
        notes.append(f"Altman Z-Score={z.get('score')} 落入财务困境区，但因财务基本面健康豁免抬升"
                     + z_waive_note)
    if m_suspect:
        notes.append(f"Beneish M-Score={m.get('score')}（{m.get('model')}）超过阈值，"
                     "存在盈余操纵风险嫌疑，风险分抬升 10")
        return 10.0, notes
    return 0.0, notes


def _calc_opinion_escalation(audit_opinion_json: str) -> tuple:
    """根据审计意见类型计算风险分抬升幅度与说明。

    规则与 knowledge_base/审计意见类型库.txt 一致：
    保留/否定/无法表示意见 +25（综合等级不低于重大）；
    带强调事项段的无保留意见本身不直接抬升（不构成否决规则），
    仅当强调事项为持续经营重大不确定性时 +15。

    Returns:
        (抬升分值, 说明列表)
    """
    try:
        data = json.loads(audit_opinion_json) if isinstance(audit_opinion_json, str) else (audit_opinion_json or {})
    except (json.JSONDecodeError, TypeError):
        return 0.0, []
    if not isinstance(data, dict):
        return 0.0, []

    op = data.get("audit_opinion") or {}
    gc = data.get("going_concern") or {}
    # 同 _calc_model_escalation：嵌套字段可能为标量，守卫后再 .get
    if not isinstance(op, dict):
        op = {}
    if not isinstance(gc, dict):
        gc = {}
    op_type = str(op.get("opinion_type", ""))
    notes = []

    if op_type in ("保留意见", "否定意见", "无法表示意见"):
        notes.append(f"审计意见为{op_type}（非标准意见，年报数据可信度影响程度"
                     f"{op.get('credibility_impact', '高')}），风险分抬升 25")
        return 25.0, notes
    if gc.get("flagged"):
        notes.append("审计报告提示与持续经营相关的重大不确定性，按裁定规则上调至重大关注，"
                     "风险分抬升 15")
        return 15.0, notes
    return 0.0, []


@tool
def calculate_comprehensive_score(
    financial_analysis_json: str = "{}",
    disclosure_check_json: str = "{}",
    validation_json: str = "{}",
    risk_models_json: str = "{}",
    audit_opinion_json: str = "{}"
) -> str:
    """计算综合风险评分（0-100，分越高风险越大），汇总各模块分析结果。

    基础分由三维度加权得出（财务 50% / 披露 30% / 校验 20%）；若传入量化模型
    与审计意见结果，则按知识库规则进一步抬升风险分并输出抬升理由（可追溯）。
    后两个参数为可选，不传时行为与原来完全一致。

    Args:
        financial_analysis_json: calculate_financial_indicators 的输出 JSON
        disclosure_check_json: check_disclosure_compliance 的输出 JSON
        validation_json: validate_financial_data 的输出 JSON
        risk_models_json: calculate_risk_models 的输出 JSON（可选，Z/M-Score）
        audit_opinion_json: identify_audit_opinion 的输出 JSON（可选）

    Returns:
        JSON 字符串，包含：
        - score: 综合风险分（0-100，越高风险越大）
        - level / level_key: 风险等级与标识
        - breakdown: 三维度分解分数
        - weights: 各维度权重
        - base_score / escalation: 基础分与抬升分
        - escalation_reasons: 抬升理由列表（可追溯）
        - summary: 一句话风险总结
    """
    # 防御纵深：各维度函数已有类型守卫，此处再包一层，确保任何意外异常
    # 都降级为带 score/level 字段的可见错误 JSON，而不是炸掉整条分析流
    # （下游 _post_process 与前端评分卡都依赖 score/level/level_key 字段解析）
    try:
        # N 补丁：各维度解析失败/无数据 → None（维度未获取，不参与计分、不捏造默认分）
        risks = {
            "financial": _calc_financial_risk(financial_analysis_json),
            "disclosure": _calc_disclosure_risk(disclosure_check_json),
            "validation": _calc_validation_risk(validation_json),
        }
        available = {k: v for k, v in risks.items()
                     if isinstance(v, (int, float)) and v == v}  # 排除 None/NaN

        # 全维度未获取：禁止进入等级映射（把"无数据"说成"低风险"比旧 Bug 更隐蔽）
        if not available:
            facts, metrics, evidence = _score_records(
                risks, total_score=None, total_status="insufficient_data",
                total_reason="三个评分维度均未获取")
            return json.dumps({
                "result_schema_version": RESULT_SCHEMA_VERSION,
                "rule_version": RULE_VERSION,
                "calculation_version": "2026-09-v3",
                "status": "insufficient_data",
                "score": None,
                "level": "未获取/无法判定",
                "level_key": "unavailable",
                "breakdown": {k: "未获取" for k in risks},
                "weights": dict(WEIGHTS),
                "base_score": None,
                "escalation": 0,
                "escalation_reasons": [],
                "summary": "三个评分维度数据均未获取，无法计算综合风险评分，请人工复核",
                "facts": facts,
                "metric_results": metrics,
                "evidence": evidence,
            }, ensure_ascii=False)

        # 部分维度未获取：剩余维度权重按比例重归一化（如仅财务+校验：0.5/0.7、0.2/0.7）
        weight_sum = sum(WEIGHTS[k] for k in available)
        base_score = sum(v * WEIGHTS[k] / weight_sum for k, v in available.items())

        # 50d：维度未获取时的归一化说明（评分透明化——实测 18:37 版披露维度未获取，
        # 15.0 分系权重重归一化计算却未在评分卡说明，被误读为「AI 编造评分」）
        _DIM_CN = {"financial": "财务指标", "disclosure": "披露合规", "validation": "数据校验"}
        renorm_notes = []
        missing = [k for k in WEIGHTS if k not in available]
        if missing:
            renorm = "、".join(f"{_DIM_CN.get(k, k)} {WEIGHTS[k] / weight_sum:.2f}"
                                for k in available)
            renorm_notes.append(
                f"{'、'.join(_DIM_CN.get(k, k) for k in missing)}维度未获取，"
                f"总分已按剩余维度权重归一化计算（{renorm}），"
                f"未获取维度不参与计分")

        # 量化模型与审计意见带来的风险抬升（可选输入，不传则为 0）
        # 模型抬升传入财务指标：Z-Score 困境判定需基本面门控（低杠杆+现金流
        # 充沛样本豁免），避免能源央企被机械抬分（实测缺陷）
        model_up, model_notes = _calc_model_escalation(risk_models_json, financial_analysis_json)
        opinion_up, opinion_notes = _calc_opinion_escalation(audit_opinion_json)
        escalation = model_up + opinion_up
        reasons = model_notes + opinion_notes

        score = round(min(100, max(0, base_score + escalation)), 1)

        # 映射风险等级
        level, level_key = _get_level(score)

        # 生成一句话总结
        level_desc = {
            "low": "现有可用输入的量化评分处于低风险区间，不代表已排除重大错报或其他风险",
            "medium": "现有可用输入的量化评分处于中等风险区间，建议关注相关指标变动",
            "high": "现有可用输入的量化评分处于高风险区间，建议重点核查并追加审计程序",
            "critical": "现有可用输入的量化评分处于极高风险区间，建议扩大审计核查范围",
        }
        summary = level_desc.get(level_key, "")
        if missing:
            summary = (summary + "（" + "、".join(_DIM_CN.get(k, k) for k in missing)
                       + "维度未获取，已按剩余维度权重归一化）") if summary else summary

        result = {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "rule_version": RULE_VERSION,
            "calculation_version": "2026-09-v3",
            "status": "partially_calculated" if missing else "calculated",
            "score": score,
            "level": level,
            "level_key": level_key,
            "breakdown": {
                # N 补丁：未获取维度以字符串"未获取"标注，不再输出不可追溯的默认分
                k: (round(v, 1) if isinstance(v, (int, float)) else "未获取")
                for k, v in risks.items()
            },
            # 50d：weights 输出归一化权重（未获取维度置 0）——与 base_score 口径一致，
            # 否则评分卡权重列显示原始权重（0.3）与「已按剩余维度权重归一化」同屏矛盾
            "weights": {k: (WEIGHTS[k] / weight_sum if k in available else 0.0)
                        for k in WEIGHTS},
            "base_score": round(base_score, 1),
            "escalation": round(escalation, 1),
            "escalation_reasons": reasons,
            # 50d：归一化/模型/意见说明统一入 notes（评分卡渲染，评分口径可追溯）
            "notes": renorm_notes + reasons,
            "summary": summary,
        }
        facts, metrics, evidence = _score_records(
            risks, total_score=score, total_status="calculated")
        result.update({"facts": facts, "metric_results": metrics, "evidence": evidence})

        logger.info(f"综合风险评分：{score} 分（{level}）"
                    + (f"，含模型/意见抬升 {escalation}" if escalation else ""))
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001 - 降级可见：失败时保持不可判定，不填中间值
        logger.warning(f"综合评分计算异常，降级输出不可判定结果: {e}")
        facts, metrics, evidence = _score_records(
            {"financial": None, "disclosure": None, "validation": None},
            total_score=None, total_status="failed", total_reason=str(e))
        return json.dumps({
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "rule_version": RULE_VERSION,
            "calculation_version": "2026-09-v3",
            "status": "failed",
            "score": None,
            "level": "未获取/无法判定",
            "level_key": "unavailable",
            "error": f"综合评分计算发生异常：{e}",
            "summary": "综合评分计算发生异常（详见 error 字段），无法判定风险等级，请人工复核。",
            "facts": facts,
            "metric_results": metrics,
            "evidence": evidence,
        }, ensure_ascii=False, indent=2)
