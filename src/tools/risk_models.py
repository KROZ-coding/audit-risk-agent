"""量化风险预警模型工具（Altman Z-Score / Beneish M-Score）

本模块实现两个国际公认的经典财务预警模型，为综合研判提供可量化、可复现的
第三方模型证据（区别于本系统自研的加权评分）：

1. Altman Z-Score —— 财务困境（破产）预警
   三个变体按行业与数据可得性自动选择：
   - 原始 Z-Score：上市制造业（需股权市值）
   - Z'-Score：制造业但无市值数据（改用股东权益账面价值）
   - Z''-Score：非制造业与新兴市场（剔除周转率因子 X5）

2. Beneish M-Score —— 盈余操纵预警
   八变量完整模型，TATA（权重最高）缺失时仅在五个简化模型因子完整的情况下
   降级；任何必要因子缺失都不以中性值代入。

公式、判定阈值与降级策略均与 knowledge_base/风险评分模型库.txt 一致，
便于报告引用时溯源。所有结论仅表示"风险嫌疑"，不构成定性认定。
"""
import json
import logging

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 非制造业行业关键词：命中则采用 Z''-Score 变体（剔除资产周转率因子）
_NON_MANUFACTURING = (
    "互联网", "信息技术", "软件", "服务", "金融", "银行", "证券", "保险",
    "房地产", "零售", "商贸", "传媒", "文化", "教育", "医疗服务", "物流", "旅游",
)
# Z-Score 判定区间：(安全区下限, 困境区上限)
_Z_ZONES = {
    "original": (2.99, 1.81),
    "private": (2.90, 1.23),
    "emerging": (2.60, 1.10),
}
# M-Score 判定阈值：超过即存在盈余操纵嫌疑
_M8_THRESHOLD = -1.78
_M5_THRESHOLD = -2.22


def _metadata(d: dict) -> dict:
    value = d.get("_metadata") or d.get("metadata") or {}
    return value if isinstance(value, dict) else {}


def _model_facts(d: dict) -> tuple[list[dict], dict[str, dict]]:
    """登记模型原始输入；模型因子本身是计算结果，不能替代原始事实。"""
    meta = _metadata(d)
    unit = str(d.get("amount_unit") or meta.get("amount_unit") or "")
    currency = str(d.get("currency") or meta.get("currency") or "人民币")
    period = _context_value(d, "period", "report_period", "current_period", "year", "report_year")
    scope = str(d.get("scope") or meta.get("scope") or "")
    source_document = str(meta.get("source_document") or d.get("source_document") or "")
    source_hash = str(meta.get("source_hash") or d.get("source_hash") or "")
    page = str(meta.get("page") or meta.get("page_number") or d.get("page") or "")
    locator = str(meta.get("locator") or meta.get("table_locator") or d.get("locator") or "")
    excerpt = str(meta.get("excerpt") or d.get("excerpt") or "")
    facts = []
    by_field = {}
    for field, raw in d.items():
        if str(field).startswith("_") or field in {"metadata", "amount_unit", "currency", "scope"}:
            continue
        value = _num(raw)
        if value is None:
            continue
        fact = make_fact(
            field, raw, fact_id=f"F-RM-{field}",
            unit=("分" if field == "market_cap" else unit), currency=currency,
            period=period, scope=scope, source_document=source_document,
            source_hash=source_hash, page=page, locator=locator, excerpt=excerpt,
            extraction_method=str(meta.get("extraction_method") or "structured_input"),
        ).to_dict()
        facts.append(fact)
        by_field[field] = fact
    return facts, by_field


def _model_records(model_key: str, result: dict, d: dict, facts_by_field: dict[str, dict]) -> tuple[dict, dict, dict]:
    """把模型分值、输入和证据统一包装为结构化结果。"""
    metric_id = f"risk_model_{model_key}"
    evidence_id = f"E-{metric_id}"
    model_fields = {
        "altman_z_score": (
            "total_assets", "total_assets_current", "current_assets", "current_assets_current",
            "current_liabilities", "current_liabilities_current", "retained_earnings",
            "retained_earnings_end", "ebit", "pretax_profit", "total_profit", "interest_expense",
            "total_liabilities", "total_liabilities_current", "net_assets", "owners_equity",
            "revenue", "revenue_current", "market_cap",
        ),
        "beneish_m_score": (
            "total_assets", "total_assets_current", "total_assets_previous", "revenue", "revenue_current",
            "revenue_previous", "accounts_receivable", "accounts_receivable_current",
            "accounts_receivable_previous", "gross_profit", "gross_profit_current", "gross_profit_previous",
            "current_assets", "current_assets_current", "current_assets_previous", "fixed_assets",
            "fixed_assets_current", "fixed_assets_previous", "depreciation", "depreciation_current",
            "depreciation_previous", "sga_expense", "sga_expense_current", "sga_expense_previous",
            "net_profit", "net_profit_current", "operating_cashflow", "operating_cashflow_current",
            "operating_cash_flow", "total_liabilities", "total_liabilities_current",
            "total_liabilities_previous",
        ),
    }[model_key]
    inputs = []
    seen = set()
    for field in model_fields:
        fact = facts_by_field.get(field)
        if not fact or field in seen:
            continue
        seen.add(field)
        inputs.append({
            "field": field, "fact_id": fact["fact_id"], "raw_value": fact["raw_value"],
            "value": fact.get("value"), "unit": fact.get("unit", ""),
            "period": fact.get("period", ""), "scope": fact.get("scope", ""),
        })
    status = str(result.get("status", "insufficient_data"))
    metric = MetricResult(
        metric_id=metric_id,
        name="Altman Z-Score" if model_key == "altman_z_score" else "Beneish M-Score",
        formula=str(result.get("formula") or "模型适用性与必要因子检查"),
        inputs=inputs,
        period=_context_value(d, "period", "report_period", "current_period", "year", "report_year"),
        scope=str(d.get("scope") or _metadata(d).get("scope") or ""),
        unit="分",
        value=result.get("score"),
        display_value="未获取" if result.get("score") is None else str(result.get("score")),
        threshold=result.get("thresholds", result.get("threshold")),
        threshold_source="knowledge_base/风险评分模型库.txt",
        status="calculated" if result.get("available") else status,
        reason=str(result.get("reason", "") or ""),
        evidence_ids=[evidence_id],
    ).to_dict()
    evidence = Evidence(
        evidence_id=evidence_id,
        source_type="local_risk_model",
        source_document=str(_metadata(d).get("source_document") or d.get("source_document") or ""),
        source_hash=str(_metadata(d).get("source_hash") or d.get("source_hash") or ""),
        page=str(_metadata(d).get("page") or _metadata(d).get("page_number") or d.get("page") or ""),
        locator=str(_metadata(d).get("locator") or _metadata(d).get("table_locator") or d.get("locator") or ""),
        excerpt=str(_metadata(d).get("excerpt") or d.get("excerpt") or ""),
        fact_ids=[item["fact_id"] for item in inputs],
        metric_ids=[metric_id],
        verified=bool(result.get("available") and inputs),
        status="verified" if result.get("available") and inputs else status,
    ).to_dict()
    result = dict(result)
    result["metric_id"] = metric_id
    result["evidence_ids"] = [evidence_id]
    return result, metric, evidence


def _num(val, default=None):
    """安全数值转换：无法转换时返回 default（默认 None 表示字段缺失）。

    区别于其他工具的 _safe_float 默认返回 0.0 —— 本模块必须区分"值为 0"
    与"字段缺失"，因为缺失需要走降级策略，而 0 会导致除零或错误结论。
    """
    if val is None or val == "":
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f


def _ratio(numerator, denominator):
    """安全比率：分子或分母缺失、分母为 0 时返回 None（标记为不可计算）。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _pick(d: dict, *keys):
    """按优先顺序取第一个有效字段（多别名容错）。

    项目内存在两套字段习惯：提取提示词与多数工具用 operating_cashflow（无下划线）
    且带 _current/_previous 后缀；部分场景又使用无后缀形式。若只认单一名称，
    字段会静默读不到 → 因子缺失 → 模型被迫降级，结论失真且难以察觉。
    """
    for k in keys:
        v = _num(d.get(k))
        if v is not None:
            return v
    return None


def _context_value(d: dict, *keys):
    """读取行业/期间等上下文，不把缺失上下文伪装成默认值。"""
    meta = d.get("_metadata") or d.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return str(value).strip()
        value = meta.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _context_gate(d: dict, *, require_industry: bool = False) -> list[str]:
    """返回模型适用性所需但未提供的上下文项。"""
    missing = []
    if require_industry and not _context_value(d, "industry", "industry_name"):
        missing.append("行业")
    if not _context_value(d, "period", "report_period", "current_period", "year", "report_year"):
        missing.append("本期期间")
    return missing


def _pick_z_variant(industry: str, market_cap, equity) -> str:
    """选择 Z-Score 变体：非制造业→emerging；有市值→original；否则→private。"""
    ind = str(industry or "")
    if any(k in ind for k in _NON_MANUFACTURING):
        return "emerging"
    if market_cap is not None and market_cap > 0:
        return "original"
    return "private"


def _zone_of(z, variant: str) -> str:
    """按变体阈值判定 Z 值所处区间。"""
    safe, distress = _Z_ZONES[variant]
    if z > safe:
        return "财务安全区"
    if z < distress:
        return "财务困境区"
    return "灰色预警区"


def _calc_altman(d: dict) -> dict:
    """计算 Altman Z-Score，返回分值、变体、区间与逐项因子明细。"""
    context_missing = _context_gate(d, require_industry=True)
    if context_missing:
        return {
            "available": False,
            "status": "not_applicable",
            "reason": f"缺少{'、'.join(context_missing)}，无法确认 Z-Score 适用条件",
            "missing_context": context_missing,
        }

    total_assets = _pick(d, "total_assets", "total_assets_current")
    if total_assets is None or total_assets <= 0:
        return {"available": False, "status": "insufficient_data", "reason": "缺少有效总资产数据，无法计算 Z-Score"}

    current_assets = _pick(d, "current_assets", "current_assets_current")
    current_liabilities = _pick(d, "current_liabilities", "current_liabilities_current")
    retained_earnings = _pick(d, "retained_earnings", "retained_earnings_end")
    ebit = _pick(d, "ebit")
    total_liabilities = _pick(d, "total_liabilities", "total_liabilities_current")
    revenue = _pick(d, "revenue_current", "revenue")
    market_cap = _pick(d, "market_cap")
    equity = _pick(d, "net_assets", "owners_equity")
    variant = _pick_z_variant(_context_value(d, "industry", "industry_name"), market_cap, equity)
    variant_names = {
        "original": "原始 Z-Score（上市制造业）",
        "private": "Z'-Score（制造业，无市值数据）",
        "emerging": "Z''-Score（非制造业/新兴市场）",
    }
    variant_name = variant_names[variant]

    # EBIT 缺失时只有在利润总额和利息费用均有原始事实时才使用近似口径。
    if ebit is None:
        pretax = _pick(d, "pretax_profit", "total_profit")
        interest = _pick(d, "interest_expense")
        if pretax is not None and interest is not None:
            ebit = pretax + interest

    missing = []
    # X1 营运资金/总资产
    if current_assets is not None and current_liabilities is not None:
        x1 = (current_assets - current_liabilities) / total_assets
    else:
        x1 = None
        missing.append("X1 营运资金（缺流动资产或流动负债）")
    # X2 留存收益/总资产
    if retained_earnings is not None:
        x2 = retained_earnings / total_assets
    else:
        x2 = None
        missing.append("X2 留存收益")
    # X3 EBIT/总资产
    if ebit is not None:
        x3 = ebit / total_assets
    else:
        x3 = None
        missing.append("X3 息税前利润")
    # X5 营业收入/总资产仅属于原始/私营制造业变体，Z'' 不使用该因子。
    if revenue is not None:
        x5 = revenue / total_assets
    else:
        x5 = None
        if variant != "emerging":
            missing.append("X5 营业收入")

    # X4：original 用股权市值/总负债；其余变体用股东权益账面价值/总负债
    if variant == "original":
        x4 = _ratio(market_cap, total_liabilities)
    else:
        x4 = _ratio(equity, total_liabilities)
    if x4 is None:
        missing.append("X4 权益/总负债（缺市值或净资产或总负债为0）")
    if missing:
        return {
            "available": False,
            "status": "insufficient_data",
            "variant": variant,
            "variant_name": variant_name,
            "reason": f"缺少必要因子（{len(missing)} 项），Z-Score 不适用，不输出分值",
            "missing_factors": missing,
        }

    if variant == "original":
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        formula = "Z = 1.2×X1 + 1.4×X2 + 3.3×X3 + 0.6×X4 + 1.0×X5"
        variant_name = "原始 Z-Score（上市制造业）"
    elif variant == "private":
        z = 0.717 * x1 + 0.847 * x2 + 3.107 * x3 + 0.420 * x4 + 0.998 * x5
        formula = "Z' = 0.717×X1 + 0.847×X2 + 3.107×X3 + 0.420×X4' + 0.998×X5"
        variant_name = "Z'-Score（制造业，无市值数据）"
    else:
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
        formula = "Z'' = 6.56×X1 + 3.26×X2 + 6.72×X3 + 1.05×X4'"
        variant_name = "Z''-Score（非制造业/新兴市场）"

    safe, distress = _Z_ZONES[variant]

    return {
        "available": True,
        "status": "calculated",
        "variant": variant,
        "variant_name": variant_name,
        "formula": formula,
        "score": round(z, 4),
        "zone": _zone_of(z, variant),
        "thresholds": {"安全区下限": safe, "困境区上限": distress},
        "factors": {
            "X1_营运资金占比": round(x1, 4), "X2_留存收益占比": round(x2, 4),
            "X3_EBIT占比": round(x3, 4), "X4_权益负债比": round(x4, 4),
            "X5_资产周转率": round(x5, 4) if variant != "emerging" else "不适用",
        },
        "missing_factors": [],
        "confidence": "高",
        "note": "Z-Score 低仅表示财务困境概率上升，须表述为存在财务困境风险嫌疑，不等于必然破产。",
    }


def _calc_beneish(d: dict) -> dict:
    """计算 Beneish M-Score（八变量，必要时降级为五变量）。"""
    context_missing = _context_gate(d, require_industry=True)
    if context_missing:
        return {
            "available": False,
            "status": "not_applicable",
            "reason": f"缺少{'、'.join(context_missing)}，无法确认 M-Score 适用期间与行业口径",
            "missing_context": context_missing,
        }

    ta_c = _pick(d, "total_assets", "total_assets_current")
    rev_c = _pick(d, "revenue_current", "revenue")
    rev_p = _pick(d, "revenue_previous")
    if ta_c is None or ta_c <= 0 or rev_c is None or rev_c <= 0 or rev_p is None or rev_p <= 0:
        return {"available": False, "status": "insufficient_data",
                "reason": "本期总资产及本期/上期营业收入必须为有效正数，无法计算 M-Score"}

    ta_p = _pick(d, "total_assets_previous")
    ar_c = _pick(d, "accounts_receivable_current", "accounts_receivable")
    ar_p = _pick(d, "accounts_receivable_previous")
    gm_c = _ratio(_pick(d, "gross_profit", "gross_profit_current"), rev_c)
    gm_p = _ratio(_pick(d, "gross_profit_previous"), rev_p)
    ca_c = _pick(d, "current_assets", "current_assets_current")
    ca_p = _pick(d, "current_assets_previous")
    fa_c = _pick(d, "fixed_assets", "fixed_assets_current")
    fa_p = _pick(d, "fixed_assets_previous")
    dep_c = _pick(d, "depreciation", "depreciation_current")
    dep_p = _pick(d, "depreciation_previous")
    sga_c = _pick(d, "sga_expense", "sga_expense_current")
    sga_p = _pick(d, "sga_expense_previous")
    ni_c = _pick(d, "net_profit_current", "net_profit")
    # 关键：项目规范名为 operating_cashflow（无下划线），提取提示词也用它；
    # 此处必须兼容多种写法，否则 TATA（权重最高）永远缺失、M-Score 永远降级。
    ocf_c = _pick(d, "operating_cashflow_current", "operating_cashflow", "operating_cash_flow")
    tl_c = _pick(d, "total_liabilities", "total_liabilities_current")
    tl_p = _pick(d, "total_liabilities_previous")

    missing = []

    def factor(value, name):
        """登记缺失因子；缺失因子不以中性值代入。"""
        if value is None:
            missing.append(name)
        return value

    # DSRI 应收账款指数
    dsri = factor(_ratio(_ratio(ar_c, rev_c), _ratio(ar_p, rev_p)), "DSRI 应收账款指数")
    # GMI 毛利率指数
    gmi = factor(_ratio(gm_p, gm_c), "GMI 毛利率指数")
    # AQI 资产质量指数
    aqi_c = None if (ca_c is None or fa_c is None) else 1 - (ca_c + fa_c) / ta_c
    aqi_p = None if (ca_p is None or fa_p is None or not ta_p) else 1 - (ca_p + fa_p) / ta_p
    aqi = factor(_ratio(aqi_c, aqi_p), "AQI 资产质量指数")
    # SGI 营收增长指数
    sgi = factor(_ratio(rev_c, rev_p), "SGI 营收增长指数")
    # DEPI 折旧率指数（折旧率 = 折旧 /（折旧 + 固定资产原值））
    dr_c = None if (dep_c is None or fa_c is None) else _ratio(dep_c, dep_c + fa_c)
    dr_p = None if (dep_p is None or fa_p is None) else _ratio(dep_p, dep_p + fa_p)
    depi = factor(_ratio(dr_p, dr_c), "DEPI 折旧率指数")
    # SGAI 销售管理费用指数
    sgai = factor(_ratio(_ratio(sga_c, rev_c), _ratio(sga_p, rev_p)), "SGAI 费用率指数")
    # LVGI 财务杠杆指数
    lvgi = factor(_ratio(_ratio(tl_c, ta_c), _ratio(tl_p, ta_p)), "LVGI 杠杆指数")
    # TATA 总应计利润占比（权重最高，缺失时仅允许完整五变量模型）
    tata_available = ni_c is not None and ocf_c is not None
    tata = (ni_c - ocf_c) / ta_c if tata_available else None
    if not tata_available:
        missing.append("TATA 总应计利润（缺净利润或经营现金流）")

    # 五变量模型只允许在自身五个因子完整时使用；不再用 1.0 填充任何缺失因子。
    five_factors = (dsri, gmi, aqi, sgi, depi)
    five_missing = [name for name, value in zip(
        ("DSRI 应收账款指数", "GMI 毛利率指数", "AQI 资产质量指数", "SGI 营收增长指数", "DEPI 折旧率指数"),
        five_factors) if value is None]
    use_five = not tata_available
    if five_missing:
        return {
            "available": False,
            "status": "insufficient_data",
            "reason": f"五变量模型缺少必要因子（{len(five_missing)} 项），不输出 M-Score",
            "missing_factors": list(dict.fromkeys(missing + five_missing)),
        }
    if use_five:
        m = (-6.065 + 0.823 * dsri + 0.906 * gmi + 0.593 * aqi
             + 0.717 * sgi + 0.107 * depi)
        model = "五变量简化模型"
        formula = "M5 = -6.065 + 0.823×DSRI + 0.906×GMI + 0.593×AQI + 0.717×SGI + 0.107×DEPI"
        threshold = _M5_THRESHOLD
        confidence = "中" if not tata_available else "高"
    else:
        full_missing = [name for name, value in (
            ("SGAI 费用率指数", sgai), ("LVGI 杠杆指数", lvgi)
        ) if value is None]
        if full_missing:
            return {
                "available": False,
                "status": "insufficient_data",
                "reason": f"八变量模型缺少必要因子（{len(full_missing)} 项），不输出 M-Score",
                "missing_factors": list(dict.fromkeys(missing + full_missing)),
            }
        m = (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
             + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
        model = "八变量完整模型"
        formula = ("M = -4.84 + 0.920×DSRI + 0.528×GMI + 0.404×AQI + 0.892×SGI "
                   "+ 0.115×DEPI - 0.172×SGAI + 4.679×TATA - 0.327×LVGI")
        threshold = _M8_THRESHOLD
        confidence = "高"

    suspected = m > threshold
    return {
        "available": True,
        "status": "calculated",
        "model": model,
        "formula": formula,
        "score": round(m, 4),
        "threshold": threshold,
        "judgement": "存在盈余操纵风险嫌疑" if suspected else "未发现明显盈余操纵特征",
        "high_suspicion": bool(m > -1.00),
        "factors": {
            "DSRI": round(dsri, 4), "GMI": round(gmi, 4), "AQI": round(aqi, 4),
            "SGI": round(sgi, 4), "DEPI": round(depi, 4),
            "SGAI": round(sgai, 4) if not use_five else "未使用",
            "TATA": round(tata, 4) if tata is not None else "缺失",
            "LVGI": round(lvgi, 4) if not use_five else "未使用",
        },
        "missing_factors": list(dict.fromkeys(missing)),
        "confidence": confidence,
        "note": "M-Score 超阈值仅表示财务特征与历史操纵样本相似，不能作为造假证据，"
                "须结合审计意见与关联交易情况进一步核查。",
    }


def _combine(z: dict, m: dict) -> dict:
    """交叉解读：Z 低 + M 高是最高风险的信号组合。"""
    z_distress = z.get("available") and z.get("zone") == "财务困境区"
    m_suspect = m.get("available") and m.get("judgement", "").startswith("存在")
    if z_distress and m_suspect:
        return {
            "signal": "双重信号并存",
            "level_floor": "重大",
            "interpretation": "财务困境与盈余操纵双重信号并存，指向经营恶化且可能通过"
                              "会计手段掩饰，属最高风险的信号组合，建议全面核查。",
        }
    if z_distress:
        return {"signal": "财务困境单一信号", "level_floor": "重要",
                "interpretation": "Altman Z-Score 落入财务困境区，存在财务困境风险嫌疑，"
                                  "建议结合经营现金流趋势核查持续经营能力。"}
    if m_suspect:
        return {"signal": "盈余操纵单一信号", "level_floor": "重要",
                "interpretation": "Beneish M-Score 超过阈值，存在盈余操纵风险嫌疑，"
                                  "建议核查收入确认与应计项目。"}
    return {"signal": "未触发模型预警", "level_floor": "一般",
            "interpretation": "两项量化模型均未触发预警，但不排除模型未覆盖的风险，"
                              "仍须结合勾稽校验与披露信息综合判断。"}


@tool
def calculate_risk_models(financial_data_json: str) -> str:
    """计算 Altman Z-Score（财务困境预警）与 Beneish M-Score（盈余操纵预警）两个经典量化模型。

    用于为风险研判提供国际公认的第三方量化证据。模型先检查行业/期间适用条件，
    再按行业选择变体；必要因子缺失时返回不可计算，不以零或中性值补齐。

    Args:
        financial_data_json: 财务数据 JSON 字符串。可用字段（缺失会触发降级，不报错）：
            总资产 total_assets、上期总资产 total_assets_previous、
            流动资产 current_assets（及 _previous）、流动负债 current_liabilities、
            总负债 total_liabilities（及 _previous）、净资产 net_assets、
            留存收益 retained_earnings、息税前利润 ebit（或 pretax_profit + interest_expense）、
            营业收入 revenue_current / revenue_previous、毛利 gross_profit（及 _previous）、
            应收账款 accounts_receivable（及 _previous）、固定资产 fixed_assets（及 _previous）、
            折旧 depreciation（及 _previous）、销售管理费用 sga_expense（及 _previous）、
            净利润 net_profit_current、经营活动现金流 operating_cashflow（或 _current 后缀）、
            股权市值 market_cap、行业 industry

    Returns:
        JSON 字符串，含 altman_z_score、beneish_m_score、cross_interpretation 三部分；
        每部分含分值、判定区间/阈值、逐项因子、缺失因子与置信度。
    """
    try:
        data = json.loads(financial_data_json) if isinstance(financial_data_json, str) else financial_data_json
        if not isinstance(data, dict):
            raise ValueError("财务数据须为 JSON 对象")
    except Exception as e:  # noqa: BLE001 - 入参格式错误返回可读提示而非抛栈
        return json.dumps({"error": f"财务数据解析失败: {e}"}, ensure_ascii=False)

    facts, facts_by_field = _model_facts(data)
    z, z_metric, z_evidence = _model_records("altman_z_score", _calc_altman(data), data, facts_by_field)
    m, m_metric, m_evidence = _model_records("beneish_m_score", _calc_beneish(data), data, facts_by_field)
    cross = _combine(z, m)
    risk_findings = []
    if z.get("available") and z.get("zone") == "财务困境区":
        risk_findings.append({
            "risk_id": "RM-Z-001", "dimension": "going_concern", "title": "Altman Z-Score落入财务困境区",
            "level": "重要", "status": "candidate", "source": "local_risk_model",
            "metric_ids": [z_metric["metric_id"]], "evidence_ids": [z_evidence["evidence_id"]],
            "reason": "模型信号仅表示财务困境风险嫌疑，不等同于破产或错报结论。",
        })
    if m.get("available") and str(m.get("judgement", "")).startswith("存在"):
        risk_findings.append({
            "risk_id": "RM-M-001", "dimension": "financial_misstatement", "title": "Beneish M-Score超过阈值",
            "level": "重要", "status": "candidate", "source": "local_risk_model",
            "metric_ids": [m_metric["metric_id"]], "evidence_ids": [m_evidence["evidence_id"]],
            "reason": "模型信号仅表示与历史操纵样本相似，不能作为造假证据。",
        })
    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "calculation_version": "2026-09-v4",
        "status": "calculated" if z.get("available") or m.get("available") else "partially_calculated",
        "industry": _context_value(data, "industry", "industry_name"),
        "period": _context_value(data, "period", "report_period", "current_period", "year", "report_year"),
        "risk_models": {
            "altman_z_score": z,
            "beneish_m_score": m,
            "cross_interpretation": cross,
            "reference": "公式与阈值依据 knowledge_base/风险评分模型库.txt；"
                         "模型对金融业与房地产业适用性较弱，引用时须说明局限。",
        },
        "facts": facts,
        "metric_results": [z_metric, m_metric],
        "evidence": [z_evidence, m_evidence],
        "risk_findings": risk_findings,
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
