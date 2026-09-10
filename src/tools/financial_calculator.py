"""确定性财务指标计算。

金额计算统一从原始字符串构建 :class:`~decimal.Decimal`，只在最终序列化时
转换为 JSON 数字。单位只接受输入中明确声明的口径，不再按典型财务结构
猜测或静默修正量级。
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from langchain_core.tools import tool

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact

logger = logging.getLogger(__name__)

CALCULATION_VERSION = "2026-09-v3"
_D_ZERO = Decimal("0")
_D_HUNDRED = Decimal("100")


def _decimal(value):
    """从数字或字符串构建 Decimal；空值和布尔值视为缺失。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        text = str(value).strip().replace(",", "")
        return Decimal(text) if text else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _number(value, places: int | None = None):
    """把 Decimal 转为 JSON 可序列化数字；places 只用于最终展示。"""
    if value is None:
        return None
    if places is not None:
        quant = Decimal("1") if places == 0 else Decimal("1") / (Decimal("10") ** places)
        value = value.quantize(quant, rounding=ROUND_HALF_UP)
    return float(value)


def _format(value, places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "未获取"
    quant = Decimal("1") / (Decimal("10") ** places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):.{places}f}{suffix}"


def _safe_div(numerator, denominator, default=None):
    """安全除法，不提前舍入；调用方在结果落盘时决定展示精度。"""
    n = _decimal(numerator)
    d = _decimal(denominator)
    if n is None or d is None or d == _D_ZERO:
        return default
    return n / d


def _pct_change(current, previous, default=None):
    """计算同比变化；零基期和负基期不返回伪造百分比。"""
    c = _decimal(current)
    p = _decimal(previous)
    if c is None or p is None or p == _D_ZERO or p < _D_ZERO:
        return default
    return (c - p) / abs(p) * _D_HUNDRED


_UNIT_PATTERNS = (
    re.compile(r"单位[：:]\s*(?:人民币)?\s*(万亿|百万元|万元|亿元|千元|元)"),
    re.compile(r"金额单位[：:]\s*(?:人民币)?\s*(万亿|百万元|万元|亿元|千元|元)"),
    re.compile(r"除特别注明外[^。]{0,40}?(万亿|百万元|万元|亿元|千元|元)"),
)
_UNIT_FACTOR = {
    "元": 1,
    "千元": 1000,
    "万元": 10000,
    "百万元": 1000000,
    "亿元": 100000000,
    "万亿": 1000000000000,
}


def detect_amount_unit(text: str):
    """从原文的明确单位声明中提取单位，无法识别时返回 None。"""
    for pattern in _UNIT_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


def extract_parent_net_profit(text: str):
    """按原文明确单位提取归母净利润，无法确认单位时返回 None。"""
    if not text:
        return None
    match = re.search(
        r"归属于母公司(?:股东)?(?:所有)?的?净利润[^，。；（）]{0,20}?"
        r"(\d[\d,]*\.?\d*)\s*(万亿|亿元|百万元|万元|千元|元)",
        text,
    )
    if match:
        value = _decimal(match.group(1))
        factor = _UNIT_FACTOR.get(match.group(2))
        return _number(value * factor) if value is not None and factor else None

    match = re.search(r"归属于母公司股东的净利润\s+([\d,]+\.?\d*)", text)
    if match:
        unit = detect_amount_unit(text)
        value = _decimal(match.group(1))
        factor = _UNIT_FACTOR.get(unit)
        return _number(value * factor) if value is not None and factor else None
    return None


def _is_amount_field(key: str) -> bool:
    if not isinstance(key, str) or key.endswith(("_pct", "_ratio", "_change_pct")):
        return False
    return any(
        key.startswith(prefix)
        for prefix in (
            "revenue", "net_profit", "dividends", "operating_cashflow", "total_assets",
            "total_liabilities", "net_assets", "accounts_receivable", "inventory",
            "cost_of_goods", "goodwill", "cash_and_equivalents", "monetary_funds",
            "short_term_debt", "short_term_loans", "other_receivables", "other_payables",
            "construction_in_progress", "interest_income", "interest_expense",
            "retained_earnings", "depreciation", "amortization", "working_capital_change",
            "fixed_assets", "intangible_assets", "investments",
            # 流动资产/流动负债同为金额字段：此前遗漏导致其单位缺失，且不随声明单位换算
            "current_assets", "current_liabilities",
        )
    )


def scale_amount_fields(data: dict, factor: float | int) -> dict:
    """按原文明确声明的单位整体换算金额字段。"""
    if not isinstance(data, dict) or not factor or factor == 1:
        return data
    multiplier = _decimal(factor)
    for key, value in list(data.items()):
        if _is_amount_field(key) and _decimal(value) is not None:
            data[key] = _number(_decimal(value) * multiplier)
    return data


def normalize_financial_units(data: dict) -> dict:
    """保留兼容入口，但不再依据财务结构猜测并修正金额单位。"""
    if isinstance(data, dict):
        data.setdefault("unit_normalization", {
            "status": "not_applied",
            "reason": "未发现明确的统一单位声明，不按典型财务结构猜测换算因子",
        })
    return data


def _metadata(data: dict) -> dict:
    value = data.get("_metadata") or data.get("metadata") or {}
    return value if isinstance(value, dict) else {}


def _fact_records(data: dict) -> tuple[dict, list[dict]]:
    meta = _metadata(data)
    unit = str(data.get("amount_unit") or meta.get("amount_unit") or "")
    currency = str(data.get("currency") or meta.get("currency") or "人民币")
    period = str(data.get("period") or meta.get("period") or "")
    scope = str(data.get("scope") or meta.get("scope") or "")
    document = str(meta.get("source_document") or data.get("source_document") or "")
    source_hash = str(meta.get("source_hash") or data.get("source_hash") or "")
    page = str(meta.get("page") or meta.get("page_number") or "")
    locator = str(meta.get("locator") or meta.get("table_locator") or "")
    excerpt = str(meta.get("excerpt") or "")
    method = str(meta.get("extraction_method") or "structured_input")
    raw_values = {}
    facts = []
    for key, raw in data.items():
        if key.startswith("_") or key in {"metadata", "amount_unit", "currency", "period", "scope"}:
            continue
        parsed = _decimal(raw)
        if parsed is None:
            continue
        fact_id = f"F-{key}"
        raw_values[key] = parsed
        facts.append(make_fact(
            key, raw, fact_id=fact_id,
            unit=("天" if key == "period_days" else
                  unit if _is_amount_field(key) else
                  "%" if key.endswith("_pct") else "次" if "turnover" in key else ""),
            currency=currency if _is_amount_field(key) else "",
            period=period,
            scope=scope,
            source_document=document,
            source_hash=source_hash,
            page=page,
            locator=locator,
            excerpt=excerpt,
            extraction_method=method,
        ).to_dict())
    return raw_values, facts


def _input_ref(key: str, values: dict, facts_by_key: dict) -> dict:
    value = values.get(key)
    fact = facts_by_key.get(key, {})
    return {
        "field": key,
        "fact_id": fact.get("fact_id", ""),
        "raw_value": fact.get("raw_value", ""),
        "value": _number(value),
        "unit": fact.get("unit", "天" if key == "period_days" else ""),
        "period": fact.get("period", ""),
        "scope": fact.get("scope", ""),
        "source": "input" if fact else "rule_default" if key == "period_days" else "missing",
    }


# ── 代入过程：公式术语 → 输入字段 ────────────────────────────────
# 公式用中文业务术语书写（如「上期营业收入」），此处给出术语到输入字段的映射，
# 以便把实际取值代回算式供人工复算。映射不改写公式原文，长术语优先匹配，
# 避免「本期营业收入」被「营业收入」抢先命中。
def _term_to_key_map() -> dict[str, str]:
    """术语→字段映射（覆盖各项公式中出现的中文写法）。"""
    pairs = [
        ("本期营业收入", "revenue_current"),
        ("上期营业收入", "revenue_previous"),
        ("营业收入", "revenue_current"),
        ("本期净利润", "net_profit_current"),
        ("上期净利润", "net_profit_previous"),
        ("净利润", "net_profit_current"),
        ("经营活动现金流净额", "operating_cashflow_current"),
        ("营业成本", "cost_of_goods_current"),
        ("应收账款期末余额", "accounts_receivable_current"),
        ("期初应收账款", "accounts_receivable_previous"),
        ("期末应收账款", "accounts_receivable_current"),
        ("本期应收", "accounts_receivable_current"),
        ("上期应收", "accounts_receivable_previous"),
        ("本期营收", "revenue_current"),
        ("上期营收", "revenue_previous"),
        ("期间天数", "period_days"),
        ("期初存货", "inventory_previous"),
        ("期末存货", "inventory_current"),
        ("存货", "inventory_current"),
        ("总负债", "total_liabilities_current"),
        ("总资产", "total_assets_current"),
        ("流动资产", "current_assets_current"),
        ("流动负债", "current_liabilities_current"),
        ("商誉", "goodwill_current"),
        ("净资产", "net_assets_current"),
        ("期初固定资产", "fixed_assets_previous"),
        ("期末固定资产", "fixed_assets_current"),
        ("本期在建工程", "construction_in_progress_current"),
        ("上期在建工程", "construction_in_progress_previous"),
        ("其他应收款", "other_receivables_current"),
        ("其他应付款", "other_payables_current"),
    ]
    return dict(pairs)


# 公式中以「指标名」引用的前序指标（如周转天数引用周转率），按指标名取值
_TERM_METRIC_NAMES: tuple[str, ...] = ("应收账款周转率", "存货周转率", "应收账款周转天数")


def _substitution_terms() -> tuple[str, ...]:
    """全部可代入术语，按长度降序（长术语优先，避免短词抢先命中）。"""
    return tuple(sorted(set(_term_to_key_map()) | set(_TERM_METRIC_NAMES),
                        key=len, reverse=True))


def _format_input(item: dict) -> str:
    """格式化单个输入值：数值 + 单位，规则默认值额外标注来源。"""
    unit = str(item.get("unit", "") or "")
    raw = item.get("value")
    try:
        text = f"{Decimal(str(raw)):,.2f}{unit}"
    except (InvalidOperation, TypeError, ValueError):
        text = f"{item.get('raw_value', '')}{unit}"
    if str(item.get("source", "")) == "rule_default":
        text += "（规则默认）"
    return text


def _build_substitution(formula, inputs, value, unit, *, reason: str = "",
                        metric_lookup: dict | None = None) -> str:
    """把输入值及单位代入公式，生成可复算的算式；缺输入时如实标注而非补齐。

    Args:
        formula: 公式原文（中文术语书写）
        inputs: ``MetricResult.inputs``（含 field/value/unit/source）
        value: 计算结果（None 表示未计算）
        unit: 结果单位
        reason: 未计算原因（结果为空时附在算式后）
        metric_lookup: 前序指标的「指标名 → 展示值」映射（供公式引用指标名的场景）
    """
    text = str(formula or "").strip()
    if not text:
        return ""
    by_field = {str(item.get("field", "")): item for item in (inputs or [])
                if isinstance(item, dict)}
    missing: list[str] = []
    term_key = _term_to_key_map()

    def _replace(match: re.Match) -> str:
        term = match.group(0)
        key = term_key.get(term)
        if key:
            item = by_field.get(key)
            if not item or item.get("value") is None:
                missing.append(term)
                return term
            return _format_input(item)
        resolved = (metric_lookup or {}).get(term)
        if resolved is None:
            missing.append(term)
            return term
        return str(resolved)

    pattern = re.compile("|".join(re.escape(term) for term in _substitution_terms()))
    expression = pattern.sub(_replace, text)
    if missing:
        return f"输入缺失（{'、'.join(dict.fromkeys(missing))}），不作代入"
    if value is None:
        return f"{expression} = 未计算" + (f"（{reason}）" if reason else "")
    return f"{expression} = {_format(value, 2, unit)}"


def _metric(metric_id: str, name: str, formula: str, values: dict, facts_by_key: dict,
            keys: tuple[str, ...], value, *, unit: str = "", threshold=None,
            threshold_source: str = "", status: str = "calculated", reason: str = "",
            period: str = "", scope: str = "",
            metric_lookup: dict | None = None) -> dict:
    refs = [_input_ref(key, values, facts_by_key) for key in keys if key in values]
    result = MetricResult(
        metric_id=metric_id,
        name=name,
        formula=formula,
        inputs=refs,
        period=period,
        scope=scope,
        unit=unit,
        value=_number(value),
        display_value=_format(value, 2, unit) if value is not None else "未获取",
        substitution=_build_substitution(formula, refs, value, unit, reason=reason,
                                         metric_lookup=metric_lookup),
        threshold=threshold,
        threshold_source=threshold_source,
        status=status,
        reason=reason,
        evidence_ids=[f"E-{metric_id}"],
    )
    return result.to_dict()


def _evidence(metric: dict, data: dict, facts_by_key: dict) -> dict:
    meta = _metadata(data)
    return Evidence(
        evidence_id=f"E-{metric['metric_id']}",
        source_type="local_calculation",
        source_document=str(meta.get("source_document", "")),
        source_hash=str(meta.get("source_hash", "")),
        page=str(meta.get("page", "")),
        locator=str(meta.get("locator", "")),
        excerpt=str(meta.get("excerpt", "")),
        fact_ids=[item.get("fact_id", "") for item in metric.get("inputs", [])
                  if item.get("fact_id")],
        metric_ids=[metric["metric_id"]],
        verified=metric.get("status") == "calculated",
        status="verified" if metric.get("status") == "calculated" else metric.get("status", "pending"),
    ).to_dict()


@tool
def calculate_financial_indicators(financial_data_json: str) -> str:
    """计算偿债、营运、盈利、成长及现金流/资产质量指标。

    金额字段必须统一单位；推荐同时提供 ``amount_unit``、``period``、``scope``
    与 ``_metadata``，以便结果直接追溯到原文。缺失值不会被当作零。
    """
    try:
        data = json.loads(financial_data_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"JSON解析失败: {exc}", "calculation_version": CALCULATION_VERSION}, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps({"error": "输入 JSON 顶层须为对象", "calculation_version": CALCULATION_VERSION}, ensure_ascii=False)

    values, facts = _fact_records(data)
    facts_by_key = {item["field"]: item for item in facts}
    period = str(data.get("period") or _metadata(data).get("period") or "")
    scope = str(data.get("scope") or _metadata(data).get("scope") or "")
    metrics: list[dict] = []
    alerts: list[str] = []
    indicators: dict = {}

    def add(metric_id, name, formula, keys, value, **kwargs):
        # 仅前序「已算出」的指标可用于代入；未算出的引用按输入缺失处理，不填占位值
        metric_lookup = {item["name"]: item["display_value"]
                         for item in metrics if item.get("value") is not None}
        metric = _metric(metric_id, name, formula, values, facts_by_key, keys, value,
                         period=period, scope=scope, metric_lookup=metric_lookup, **kwargs)
        metrics.append(metric)
        if value is not None:
            indicators[metric_id] = metric["value"]
        return value

    rev_c, rev_p = values.get("revenue_current"), values.get("revenue_previous")
    np_c, np_p = values.get("net_profit_current"), values.get("net_profit_previous")
    ocf_c, ocf_p = values.get("operating_cashflow_current"), values.get("operating_cashflow_previous")
    ta_c, tl_c = values.get("total_assets_current"), values.get("total_liabilities_current")
    ar_c, ar_p = values.get("accounts_receivable_current"), values.get("accounts_receivable_previous")
    inv_c, inv_p = values.get("inventory_current"), values.get("inventory_previous")
    cog = values.get("cost_of_goods_current")

    rev_change = _pct_change(rev_c, rev_p)
    add("revenue_yoy_change_pct", "营业收入同比变动率", "(本期营业收入-上期营业收入)/|上期营业收入|×100%",
        ("revenue_current", "revenue_previous"), rev_change, unit="%", threshold=30,
        threshold_source="内部筛查规则：绝对变动超过30%提示复核",
        status="calculated" if rev_change is not None else "not_comparable",
        reason="上期为零或负数时同比百分比不适用" if rev_change is None else "")
    if rev_change is not None and abs(rev_change) > Decimal("30"):
        alerts.append(f"营业收入同比变动 {_format(rev_change, 2, '%')}，幅度较大，需关注合理性")

    np_change = _pct_change(np_c, np_p)
    add("net_profit_yoy_change_pct", "净利润同比变动率", "(本期净利润-上期净利润)/|上期净利润|×100%",
        ("net_profit_current", "net_profit_previous"), np_change, unit="%", threshold=50,
        threshold_source="内部筛查规则：绝对变动超过50%提示复核",
        status="calculated" if np_change is not None else ("not_comparable" if np_p is not None else "insufficient_data"),
        reason="上期为零或负数时同比百分比不适用" if np_change is None else "")
    if np_change is None and np_p is not None:
        desc = "扭亏为盈" if np_c is not None and np_c > 0 else "亏损收窄（减亏）" if np_c is not None and np_c > np_p else "亏损扩大"
        indicators["net_profit_yoy_change_desc"] = desc
        alerts.append(f"净利润由上期 {_format(np_p)} 变为本期 {_format(np_c)}，呈{desc}，上期为负或基期不可比")
    elif np_change is not None and abs(np_change) > Decimal("50"):
        alerts.append(f"净利润同比变动 {_format(np_change, 2, '%')}，波动显著")

    gross_margin = _safe_div(rev_c - cog if rev_c is not None and cog is not None else None, rev_c)
    add("gross_margin_pct", "毛利率", "(营业收入-营业成本)/营业收入×100%",
        ("revenue_current", "cost_of_goods_current"), gross_margin * _D_HUNDRED if gross_margin is not None else None,
        unit="%", status="calculated" if gross_margin is not None else "insufficient_data",
        reason="营业收入缺失或为零" if gross_margin is None else "")

    ocf_np = _safe_div(ocf_c, np_c)
    add("operating_cashflow_to_net_profit_ratio", "经营现金流/净利润", "经营活动现金流净额/净利润",
        ("operating_cashflow_current", "net_profit_current"), ocf_np, threshold=0.5,
        threshold_source="内部筛查规则：比值低于0.5提示利润质量复核",
        status="calculated" if ocf_np is not None else "not_comparable",
        reason="净利润缺失或为零" if ocf_np is None else "")
    if ocf_np is not None and ocf_np < Decimal("0.5") and np_c is not None and np_c > 0:
        alerts.append(f"经营现金流/净利润比 = {_format(ocf_np, 4)}，低于 0.5，现金流与利润背离，需复核")
    if ocf_c is not None and ocf_c < 0 and np_c is not None and np_c > 0:
        alerts.append("净利润为正但经营现金流为负，盈利质量需复核")

    ar_rev = _safe_div(ar_c, rev_c)
    add("accounts_receivable_to_revenue_ratio", "应收账款/营业收入", "应收账款期末余额/营业收入×100%",
        ("accounts_receivable_current", "revenue_current"), ar_rev * _D_HUNDRED if ar_rev is not None else None,
        unit="%", threshold=30, threshold_source="内部筛查规则：应收/营收超过30%提示回款复核",
        status="calculated" if ar_rev is not None else "not_comparable",
        reason="营业收入缺失或为零" if ar_rev is None else "")
    if ar_rev is not None and ar_rev > Decimal("0.3"):
        alerts.append(f"应收账款占营收比 = {_format(ar_rev * _D_HUNDRED, 2, '%')}，超过 30%，需关注回款质量")

    # 不再把应收余额同比与营业收入同比直接作差，改为比较同口径应收/营收比。
    ar_rev_previous = _safe_div(ar_p, rev_p)
    ratio_change_pp = (ar_rev - ar_rev_previous) * _D_HUNDRED if ar_rev is not None and ar_rev_previous is not None else None
    add("accounts_receivable_to_revenue_ratio_change_pp", "应收/营收比变动", "(本期应收/本期营收-上期应收/上期营收)×100个百分点",
        ("accounts_receivable_current", "revenue_current", "accounts_receivable_previous", "revenue_previous"), ratio_change_pp,
        unit="个百分点", threshold=5, threshold_source="内部筛查规则：同口径应收/营收比上升超过5个百分点提示复核",
        status="calculated" if ratio_change_pp is not None else "not_comparable",
        reason="缺少匹配期间的应收或营业收入，不能进行同口径比较" if ratio_change_pp is None else "")
    if ratio_change_pp is not None and ratio_change_pp > Decimal("5"):
        alerts.append(f"应收账款/营收比上升 {_format(ratio_change_pp, 2, '个百分点')}，需复核回款与收入确认")

    period_days = values.get("period_days")
    if period_days is None:
        period_days = Decimal("365")
        days_source = "年度默认365天（规则默认，非原始披露值）"
    elif period_days <= 0:
        period_days = None
        days_source = "期间天数必须为正数"
    else:
        days_source = "输入的期间天数"
    values["period_days"] = period_days
    avg_ar = (ar_c + ar_p) / Decimal("2") if ar_c is not None and ar_p is not None else None
    ar_turn = _safe_div(rev_c, avg_ar)
    add("accounts_receivable_turnover_ratio", "应收账款周转率", "营业收入/((期初应收账款+期末应收账款)/2)",
        ("revenue_current", "accounts_receivable_previous", "accounts_receivable_current"), ar_turn, unit="次",
        status="calculated" if ar_turn is not None else "insufficient_data",
        reason="缺少匹配期间平均应收余额或营业收入" if ar_turn is None else "")
    ar_days = _safe_div(period_days, ar_turn)
    add("accounts_receivable_turnover_days", "应收账款周转天数", "期间天数/应收账款周转率", ("period_days",), ar_days, unit="天",
        status="calculated" if ar_days is not None else "insufficient_data", reason=days_source if ar_days is not None else "周转率无法计算")

    avg_inv = (inv_c + inv_p) / Decimal("2") if inv_c is not None and inv_p is not None else None
    inv_turn = _safe_div(cog, avg_inv)
    add("inventory_turnover_ratio", "存货周转率", "营业成本/((期初存货+期末存货)/2)",
        ("cost_of_goods_current", "inventory_previous", "inventory_current"), inv_turn, unit="次",
        status="calculated" if inv_turn is not None else "insufficient_data",
        reason="缺少匹配期间平均存货余额或营业成本" if inv_turn is None else "")
    inv_days = _safe_div(period_days, inv_turn)
    add("inventory_turnover_days", "存货周转天数", "期间天数/存货周转率", ("period_days",), inv_days, unit="天",
        status="calculated" if inv_days is not None else "insufficient_data", reason=days_source if inv_days is not None else "周转率无法计算")
    if inv_c is not None and inv_p is not None and inv_p > 0 and inv_c > inv_p * Decimal("1.5"):
        alerts.append(f"存货同比增长 {_format((inv_c / inv_p - 1) * _D_HUNDRED, 2, '%')}，存货激增需关注跌价风险")

    debt_ratio = _safe_div(tl_c, ta_c)
    add("debt_to_asset_ratio_pct", "资产负债率", "总负债/总资产×100%", ("total_liabilities_current", "total_assets_current"),
        debt_ratio * _D_HUNDRED if debt_ratio is not None else None, unit="%", threshold=70,
        threshold_source="内部筛查规则：资产负债率超过70%提示偿债复核",
        status="calculated" if debt_ratio is not None else "not_comparable", reason="总资产缺失或为零" if debt_ratio is None else "")
    if debt_ratio is not None and debt_ratio > Decimal("0.7"):
        alerts.append(f"资产负债率 = {_format(debt_ratio * _D_HUNDRED, 2, '%')}，超过 70%，财务杠杆较高")

    ca_c, cl_c = values.get("current_assets_current"), values.get("current_liabilities_current")
    current_ratio = _safe_div(ca_c, cl_c)
    add("current_ratio", "流动比率", "流动资产/流动负债", ("current_assets_current", "current_liabilities_current"), current_ratio,
        threshold=1, threshold_source="内部筛查规则：流动比率低于1提示短期偿债复核",
        status="calculated" if current_ratio is not None else "not_comparable", reason="流动负债缺失或为零" if current_ratio is None else "")
    if current_ratio is not None and current_ratio < 1:
        alerts.append(f"流动比率 = {_format(current_ratio, 4)}，低于 1，短期偿债能力需复核")

    quick_ratio = _safe_div(ca_c - inv_c if ca_c is not None and inv_c is not None else None, cl_c)
    add("quick_ratio", "速动比率", "(流动资产-存货)/流动负债", ("current_assets_current", "inventory_current", "current_liabilities_current"), quick_ratio,
        threshold=0.5, threshold_source="内部筛查规则：速动比率低于0.5提示流动性复核",
        status="calculated" if quick_ratio is not None else "not_comparable", reason="流动负债缺失或为零" if quick_ratio is None else "")
    if quick_ratio is not None and quick_ratio < Decimal("0.5"):
        alerts.append(f"速动比率 = {_format(quick_ratio, 4)}，低于 0.5，流动性风险需复核")

    goodwill, net_assets = values.get("goodwill_current"), values.get("net_assets_current")
    goodwill_ratio = _safe_div(goodwill, net_assets) if net_assets is not None and net_assets > 0 else None
    add("goodwill_to_net_assets_ratio_pct", "商誉/净资产", "商誉/净资产×100%", ("goodwill_current", "net_assets_current"),
        goodwill_ratio * _D_HUNDRED if goodwill_ratio is not None else None, unit="%", threshold=30,
        threshold_source="内部筛查规则：商誉/净资产超过30%提示减值复核",
        status="calculated" if goodwill_ratio is not None else "not_comparable", reason="净资产缺失、为零或为负，比例不适用" if goodwill_ratio is None else "")
    if goodwill_ratio is not None and goodwill_ratio > Decimal("0.3"):
        alerts.append(f"商誉占净资产比 = {_format(goodwill_ratio * _D_HUNDRED, 2, '%')}，超过 30%，需复核减值")

    fixed_c, fixed_p = values.get("fixed_assets_current"), values.get("fixed_assets_previous")
    fixed_avg = (fixed_c + fixed_p) / Decimal("2") if fixed_c is not None and fixed_p is not None else None
    fixed_turn = _safe_div(rev_c, fixed_avg)
    add("fixed_asset_turnover_ratio", "固定资产周转率", "营业收入/((期初固定资产+期末固定资产)/2)",
        ("revenue_current", "fixed_assets_previous", "fixed_assets_current"), fixed_turn, unit="次",
        status="calculated" if fixed_turn is not None else "insufficient_data", reason="缺少匹配期间平均固定资产余额" if fixed_turn is None else "")

    cip_c, cip_p = values.get("construction_in_progress_current"), values.get("construction_in_progress_previous")
    cip_change = _pct_change(cip_c, cip_p)
    add("construction_in_progress_change_pct", "在建工程同比变动", "(本期在建工程-上期在建工程)/|上期在建工程|×100%",
        ("construction_in_progress_current", "construction_in_progress_previous"), cip_change, unit="%",
        status="calculated" if cip_change is not None else "not_comparable", reason="上期为零或负数时同比百分比不适用" if cip_change is None else "")

    other_rec, other_pay = values.get("other_receivables_current"), values.get("other_payables_current")
    other_rec_ratio = _safe_div(other_rec, ta_c)
    add("other_receivables_to_total_assets_ratio_pct", "其他应收款/总资产", "其他应收款/总资产×100%",
        ("other_receivables_current", "total_assets_current"), other_rec_ratio * _D_HUNDRED if other_rec_ratio is not None else None,
        unit="%", threshold=5, threshold_source="内部筛查规则：其他应收款/总资产超过5%提示关联方资金往来复核",
        status="calculated" if other_rec_ratio is not None else "not_comparable", reason="总资产缺失或为零" if other_rec_ratio is None else "")
    if other_rec_ratio is not None and other_rec_ratio > Decimal("0.05"):
        alerts.append(f"其他应收款占总资产比 = {_format(other_rec_ratio * _D_HUNDRED, 2, '%')}，超过 5%，需关注资金往来")
    other_pay_ratio = _safe_div(other_pay, ta_c)
    add("other_payables_to_total_assets_ratio_pct", "其他应付款/总资产", "其他应付款/总资产×100%",
        ("other_payables_current", "total_assets_current"), other_pay_ratio * _D_HUNDRED if other_pay_ratio is not None else None,
        unit="%", status="calculated" if other_pay_ratio is not None else "not_comparable", reason="总资产缺失或为零" if other_pay_ratio is None else "")

    cash, short_debt = values.get("cash_and_equivalents_current"), values.get("short_term_debt_current")
    int_income, int_expense = values.get("interest_income_current"), values.get("interest_expense_current")
    if all(v is not None for v in (cash, short_debt, int_income, int_expense)) and cash > short_debt and int_expense > int_income * 2:
        alerts.append(f"货币资金({_format(cash)})高于短期借款({_format(short_debt)})，但利息支出({_format(int_expense)})远大于利息收入({_format(int_income)})，存在存贷双高异常")

    if np_c is not None and np_p is not None:
        indicators["net_profit_current"] = _number(np_c)
        indicators["net_profit_previous"] = _number(np_p)
        if np_c < 0 and np_p < 0:
            alerts.append("连续两年净利润为负，存在持续经营风险信号")
    if ocf_c is not None and ocf_p is not None and ocf_c < 0 and ocf_p < 0:
        alerts.append("经营活动现金流量净额连续两年为负，存在持续经营风险信号")

    statement_items = {
        "balance_sheet": {key: _number(values[key]) for key in (
            "total_assets_current", "total_liabilities_current", "net_assets_current", "cash_and_equivalents_current",
            "accounts_receivable_current", "accounts_receivable_previous", "inventory_current", "inventory_previous",
            "current_assets_current", "current_liabilities_current", "short_term_debt_current", "goodwill_current",
            "other_receivables_current", "fixed_assets_current", "intangible_assets_current",
        ) if key in values},
        "income_statement": {key: _number(values[key]) for key in (
            "revenue_current", "revenue_previous", "net_profit_current", "net_profit_previous", "cost_of_goods_current",
        ) if key in values},
        "cashflow_statement": {key: _number(values[key]) for key in (
            "operating_cashflow_current", "operating_cashflow_previous", "interest_income_current", "interest_expense_current",
        ) if key in values},
    }
    evidence = [_evidence(metric, data, facts_by_key) for metric in metrics]
    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "amount_unit": data.get("amount_unit") or _metadata(data).get("amount_unit") or "",
        "currency": data.get("currency") or _metadata(data).get("currency") or "人民币",
        "period": period,
        "scope": scope,
        "facts": facts,
        "metric_results": metrics,
        "evidence": evidence,
        "indicators": indicators,
        "alerts": alerts,
        "alert_count": len(alerts),
        "statement_items": statement_items,
        "unit_check": {
            "status": "declared" if data.get("amount_unit") or _metadata(data).get("amount_unit") else "pending",
            "message": "金额单位来自输入声明" if data.get("amount_unit") or _metadata(data).get("amount_unit") else "未提供统一金额单位，结果不得跨单位比较",
        },
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
