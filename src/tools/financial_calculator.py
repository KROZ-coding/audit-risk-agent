"""确定性财务指标计算。

金额计算统一从原始字符串构建 :class:`~decimal.Decimal`，只在最终序列化时
转换为 JSON 数字。单位只接受输入中明确声明的口径，不再按典型财务结构
猜测或静默修正量级。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from langchain_core.tools import tool

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact

logger = logging.getLogger(__name__)

CALCULATION_VERSION = "2026-09-v5"
_D_ZERO = Decimal("0")
_D_HUNDRED = Decimal("100")


def _decimal(value):
    """从数字或字符串构建 Decimal；空值和布尔值视为缺失。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        text = str(value).strip().replace(",", "")
        parsed = Decimal(text) if text else None
        return parsed if parsed is not None and parsed.is_finite() else None
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
            "operating_profit", "income_tax_expense", "profit_before_tax",
            "capital_expenditure", "purchase_of_fixed_assets",
            "retained_earnings", "depreciation", "amortization", "working_capital_change",
            "fixed_assets", "intangible_assets", "investments",
            # 流动资产/流动负债同为金额字段：此前遗漏导致其单位缺失，且不随声明单位换算
            "current_assets", "current_liabilities",
            "operating_cost", "equity_total", "equity_parent", "gross_profit",
            "bad_debt_provision", "allowance_for_doubtful_accounts",
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


def _company_info(data: dict) -> dict:
    """company_info 子字典：LLM 常把公司名/期间/行业放在该节点下。"""
    value = data.get("company_info") if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def financial_period(data: dict) -> str:
    for source in (data, _company_info(data), _metadata(data)):
        if not isinstance(source, dict):
            continue
        for key in ("period", "report_period", "current_period", "report_year", "year"):
            if source.get(key) not in (None, ""):
                return str(source[key]).strip()
    return ""


def is_interim_period(period: str) -> bool:
    return bool(re.search(r"半年度?|上半年|下半年|中期|季度?|[Hh][12]|[Qq][1-4]|[1-9]个月|1\s*[-至~]\s*[369]\s*月", period))


def _period_kind(period: str) -> str:
    """Classify an explicitly supplied comparison period conservatively.

    Balance-sheet amounts are point-in-time values while revenue is a flow over a
    reporting period.  Only explicit, compatible labels permit a cross-signal
    comparison; unknown labels remain ``unknown`` rather than being guessed.
    """
    text = str(period or "").strip()
    if not text:
        return "unknown"
    if re.search(r"(?:12[-/]31|12月31日|年末|上年度期末|期初)", text):
        return "year_end"
    if re.search(r"(?:半年度?|上半年|下半年|中期|[Hh][12]|[Qq][1-4]|[1-9]个月)", text):
        return "interim"
    if re.search(r"(?:上年同期|同期|[1-9][-至~]?[1-9]?月)", text):
        return "same_period"
    return "unknown"


def _comparison_periods_aligned(left: str, right: str) -> bool | None:
    """Return alignment only when both period kinds are explicit and comparable."""
    left_kind, right_kind = _period_kind(left), _period_kind(right)
    if "unknown" in (left_kind, right_kind):
        return None
    return left_kind == right_kind


def _opening_period_matches(field: str, facts_by_key: dict, period: str) -> bool:
    half_year = re.search(r"(20\d{2})\s*年?\s*(?:半年度?|上半年|[Hh]1)", period)
    declared = str(facts_by_key.get(field, {}).get("period") or "")
    dated = re.search(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", declared)
    if not half_year or not dated:
        return True
    parts = tuple(int(value) for value in dated.groups())
    year = int(half_year.group(1))
    return parts in {(year - 1, 12, 31), (year, 1, 1)}


_CURRENT_ALIASES = {
    "revenue_current": ("revenue",),
    "net_profit_current": ("net_profit",),
    "net_profit_parent_current": ("net_profit_parent", "net_profit_attributable_to_parent"),
    "net_profit_parent_deducted_current": (
        "net_profit_parent_deducted", "deducted_net_profit_parent"),
    "net_profit_parent_previous": ("net_profit_parent_prior",),
    "net_profit_parent_deducted_previous": ("net_profit_parent_deducted_prior",),
    "operating_cashflow_current": ("operating_cashflow", "operating_cash_flow"),
    "cost_of_goods_current": ("operating_cost_current", "cost_of_goods", "operating_cost"),
    "cost_of_goods_previous": ("operating_cost_previous",),
    "net_assets_current": ("net_assets", "equity_total", "owners_equity"),
    "gross_profit_current": ("gross_profit",),
    "bad_debt_provision_current": ("bad_debt_provision", "allowance_for_doubtful_accounts"),
    "bad_debt_provision_same_period_previous": (
        "bad_debt_provision_previous", "allowance_for_doubtful_accounts_previous"),
    "accounts_receivable_gross_same_period_previous": (
        "accounts_receivable_gross_previous", "gross_accounts_receivable_previous"),
    "cash_and_equivalents_current": ("cash_and_equivalents", "monetary_funds", "cash"),
    "short_term_debt_current": ("short_term_debt", "short_term_loans", "short_term_borrowings"),
    "net_assets_previous": ("net_assets_prior", "equity_total_previous", "owners_equity_previous"),
    "total_assets_previous": ("total_assets_prior",),
    "current_assets_previous": ("current_assets_prior",),
    "operating_profit_current": ("operating_profit", "operating_income"),
    "income_tax_expense_current": ("income_tax_expense", "income_tax"),
    "profit_before_tax_current": ("profit_before_tax", "total_profit"),
    "weighted_average_shares_current": (
        "weighted_average_common_shares", "weighted_average_shares",
        "common_shares_weighted_average"),
    "common_shares_current": ("common_shares", "shares_outstanding", "total_common_shares"),
    "capital_expenditure_current": (
        "capital_expenditure", "capital_expenditures", "purchase_of_fixed_assets"),
    **{f"{field}_current": (field,) for field in (
        "total_assets", "total_liabilities", "current_assets", "current_liabilities",
        "accounts_receivable", "inventory", "goodwill", "other_receivables", "other_payables",
        "fixed_assets", "construction_in_progress",
        "interest_income", "interest_expense",
    )},
}


def _resolve_aliases(values: dict, facts: list[dict]) -> tuple[dict, dict, list[str]]:
    """Resolve declared aliases while preserving original fact IDs; conflicting values fail closed."""
    resolved = dict(values)
    by_key = {item["field"]: item for item in facts}
    conflicts = []
    for target, aliases in _CURRENT_ALIASES.items():
        present = [key for key in (target, *aliases) if key in values]
        if not present:
            continue
        if len({values[key] for key in present}) > 1:
            resolved.pop(target, None)
            conflicts.append(f"{target} 别名值冲突：{'、'.join(present)}，须核实后计算")
            continue
        source = present[0]
        resolved[target] = values[source]
        by_key[target] = by_key[source]
    return resolved, by_key, conflicts


def _fact_records(data: dict) -> tuple[dict, list[dict]]:
    meta = _metadata(data)
    unit = str(data.get("amount_unit") or meta.get("amount_unit") or "")
    currency = str(data.get("currency") or meta.get("currency") or "人民币")
    period = financial_period(data)
    scope = str(data.get("scope") or meta.get("scope") or "")
    document = str(meta.get("source_document") or data.get("source_document") or "")
    source_hash = str(meta.get("source_hash") or data.get("source_hash") or "")
    page = str(meta.get("page") or meta.get("page_number") or "")
    locator = str(meta.get("locator") or meta.get("table_locator") or "")
    excerpt = str(meta.get("excerpt") or "")
    method = str(meta.get("extraction_method") or "structured_input")
    raw_values = {}
    facts = []
    field_metadata = data.get("_field_metadata") or meta.get("fields") or {}
    if not isinstance(field_metadata, dict):
        field_metadata = {}
    for key, raw in data.items():
        if key.startswith("_") or key in {"metadata", "amount_unit", "currency", "period", "scope"}:
            continue
        parsed = _decimal(raw)
        if parsed is None:
            continue
        fact_id = f"F-{key}"
        raw_values[key] = parsed
        field_meta = field_metadata.get(key) or {}
        if not isinstance(field_meta, dict):
            field_meta = {}
        field_period = period
        if key.endswith("_previous"):
            field_period = str(data.get("previous_period") or meta.get("previous_period") or "上期（具体期间未提供）")
        facts.append(make_fact(
            key, raw, fact_id=fact_id,
            unit=(str(field_meta.get("unit")) if field_meta.get("unit") else "天" if key == "period_days" else
                  unit if _is_amount_field(key) else
                  "%" if key.endswith("_pct") else "次" if "turnover" in key else ""),
            currency=currency if _is_amount_field(key) else "",
            period=str(field_meta.get("period") or field_period),
            scope=str(field_meta.get("scope") or scope),
            source_document=str(field_meta.get("source_document") or document),
            source_hash=str(field_meta.get("source_hash") or source_hash),
            page=str(field_meta.get("page") or page),
            locator=str(field_meta.get("locator") or locator),
            excerpt=str(field_meta.get("excerpt") or excerpt),
            extraction_method=str(field_meta.get("extraction_method") or method),
        ).to_dict())
    return raw_values, facts


def _input_ref(key: str, values: dict, facts_by_key: dict) -> dict:
    value = values.get(key)
    fact = facts_by_key.get(key, {})
    return {
        "field": key,
        "source_field": fact.get("field", ""),
        "fact_id": fact.get("fact_id", ""),
        "raw_value": fact.get("raw_value", ""),
        "value": _number(value),
        "unit": fact.get("unit", "天" if key == "period_days" else ""),
        "period": fact.get("period", ""),
        "scope": fact.get("scope", ""),
        "source": fact.get("source", "input") if fact else "rule_default" if key == "period_days" else "missing",
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
        ("本期归母净利润", "net_profit_parent_current"),
        ("上期归母净利润", "net_profit_parent_previous"),
        ("本期扣非归母净利润", "net_profit_parent_deducted_current"),
        ("上期扣非归母净利润", "net_profit_parent_deducted_previous"),
        ("经营活动现金流净额", "operating_cashflow_current"),
        ("营业成本", "cost_of_goods_current"),
        ("应收账款期末余额", "accounts_receivable_current"),
        ("本期应收账款账面余额", "accounts_receivable_gross_current"),
        ("本期应收账款账面", "accounts_receivable_gross_current"),
        ("上年同期末应收账款账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上年同期应收账款账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上年同期末账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上年同期账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上年末应收账款账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上年末账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上期应收账款账面余额", "accounts_receivable_gross_same_period_previous"),
        ("上期账面余额", "accounts_receivable_gross_same_period_previous"),
        ("期初应收账款", "accounts_receivable_previous"),
        ("期末应收账款", "accounts_receivable_current"),
        ("本期应收", "accounts_receivable_current"),
        ("上期应收", "accounts_receivable_previous"),
        ("上年同期末应收", "accounts_receivable_same_period_previous"),
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
        ("期初总资产", "total_assets_previous"),
        ("期末总资产", "total_assets_current"),
        ("期初流动资产", "current_assets_previous"),
        ("期末流动资产", "current_assets_current"),
        ("期初净资产", "net_assets_previous"),
        ("期末净资产", "net_assets_current"),
        ("经营利润", "operating_profit_current"),
        ("营业利润", "operating_profit_current"),
        ("所得税费用", "income_tax_expense_current"),
        ("利润总额", "profit_before_tax_current"),
        ("利息支出", "interest_expense_current"),
        ("加权平均普通股股数", "weighted_average_shares_current"),
        ("加权平均股数", "weighted_average_shares_current"),
        ("期末普通股股数", "common_shares_current"),
        ("资本性支出", "capital_expenditure_current"),
        ("购建固定资产", "capital_expenditure_current"),
        ("本期在建工程", "construction_in_progress_current"),
        ("上期在建工程", "construction_in_progress_previous"),
        ("其他应收款", "other_receivables_current"),
        ("其他应付款", "other_payables_current"),
        ("现金及现金等价物", "cash_and_equivalents_current"),
        ("短期借款", "short_term_debt_current"),
        ("坏账准备", "bad_debt_provision_current"),
        ("应收账款账面余额", "accounts_receivable_gross_current"),
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
    elif str(item.get("source", "")) == "period_calendar":
        text += "（按已声明期间日历计算）"
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
    inputs = [facts_by_key.get(item.get("field"), {}) for item in metric.get("inputs", [])]
    def source_values(key):
        return "；".join(dict.fromkeys(str(item[key]) for item in inputs if item.get(key)))
    return Evidence(
        evidence_id=f"E-{metric['metric_id']}",
        source_type="local_calculation",
        source_document=source_values("source_document") or str(meta.get("source_document", "")),
        source_hash=source_values("source_hash") or str(meta.get("source_hash", "")),
        page=source_values("page") or str(meta.get("page", "")),
        locator=source_values("locator") or str(meta.get("locator", "")),
        excerpt=source_values("excerpt") or str(meta.get("excerpt", "")),
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
    与 ``_metadata``，以便结果直接追溯到原文。``_field_metadata`` 可逐字段登记
    period/page/locator；中期周转率的 ``*_previous`` 应为期初余额，上年同期末
    应收须另传 ``accounts_receivable_same_period_previous``。缺失值不会被当作零。
    """
    try:
        data = json.loads(financial_data_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"JSON解析失败: {exc}", "calculation_version": CALCULATION_VERSION}, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps({"error": "输入 JSON 顶层须为对象", "calculation_version": CALCULATION_VERSION}, ensure_ascii=False)

    values, facts = _fact_records(data)
    values, facts_by_key, input_conflicts = _resolve_aliases(values, facts)
    period = financial_period(data)
    scope = str(data.get("scope") or _metadata(data).get("scope") or "")
    metrics: list[dict] = []
    alerts: list[str] = []
    indicators: dict = {}

    def add(metric_id, name, formula, keys, value, **kwargs):
        only_if_any_input = kwargs.pop("_only_if_any_input", False)
        missing = [key for key in keys if values.get(key) is None]
        if only_if_any_input and len(missing) == len(keys):
            # 相关字段完全未披露时不生成无意义的未计算条目；部分字段存在时，
            # 仍保留指标并明确列出缺失输入。
            return None
        if value is None and missing:
            kwargs["status"] = "insufficient_data"
            kwargs["reason"] = "缺少有效输入：" + "、".join(missing)
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
    net_assets_c = values.get("net_assets_current")
    total_assets_p = values.get("total_assets_previous")
    current_assets_p = values.get("current_assets_previous")
    net_assets_p = values.get("net_assets_previous")
    ar_c, ar_p = values.get("accounts_receivable_current"), values.get("accounts_receivable_previous")
    inv_c, inv_p = values.get("inventory_current"), values.get("inventory_previous")
    cog = values.get("cost_of_goods_current")

    rev_change = _pct_change(rev_c, rev_p)
    add("revenue_yoy_change_pct", "营业收入同比增长率", "(本期营业收入-上期营业收入)/|上期营业收入|×100%",
        ("revenue_current", "revenue_previous"), rev_change, unit="%", threshold=30,
        threshold_source="内部筛查规则：绝对变动超过30%提示复核",
        status="calculated" if rev_change is not None else "not_comparable",
        reason="上期为零或负数时同比百分比不适用" if rev_change is None else "")
    if rev_change is not None and abs(rev_change) > Decimal("30"):
        alerts.append(f"营业收入同比增长率为 {_format(rev_change, 2, '%')}，幅度较大，需关注合理性")

    np_change = _pct_change(np_c, np_p)
    profit_label = f"净利润（{scope or '口径未标注'}）"
    add("net_profit_yoy_change_pct", f"{profit_label}同比变动率", "(本期净利润-上期净利润)/|上期净利润|×100%",
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

    for prefix, label in (("net_profit_parent", "归母净利润"),
                          ("net_profit_parent_deducted", "扣非归母净利润")):
        keys = (f"{prefix}_current", f"{prefix}_previous")
        if not any(key in values for key in keys):
            continue
        change = _pct_change(values.get(keys[0]), values.get(keys[1]))
        add(f"{prefix}_yoy_change_pct", f"{label}同比变动率",
            f"(本期{label}-上期{label})/|上期{label}|×100%", keys, change,
            unit="%", status="calculated" if change is not None else "not_comparable",
            reason="上期为零或负数时同比百分比不适用" if change is None else "")

    # 毛利率优先按「营业收入-营业成本」计算；营业成本缺失但原文已披露毛利额时，
    # 退化为「毛利额/营业收入」口径（实测：半年报附注给出毛利额而无单列营业成本，
    # 仅按成本口径会在演示中显示「未获取」）。两条口径在 reason 中显式区分。
    gross_profit_input = values.get("gross_profit_current")
    if rev_c is not None and cog is not None:
        gross_margin = _safe_div(rev_c - cog, rev_c)
        gross_margin_basis = "(营业收入-营业成本)/营业收入×100%"
        gross_margin_keys = ("revenue_current", "cost_of_goods_current")
        gross_margin_reason = "本指标按营业收入减营业成本计算；与主营业务利润/主营业务收入等其他披露定义不得直接混用"
    elif rev_c is not None and gross_profit_input is not None:
        gross_margin = _safe_div(gross_profit_input, rev_c)
        gross_margin_basis = "毛利额/营业收入×100%"
        gross_margin_keys = ("revenue_current", "gross_profit_current")
        gross_margin_reason = ("营业成本缺失，按原文披露的毛利额口径计算；"
                               "与营业收入减营业成本口径定义可能不同，不得直接混用")
    else:
        gross_margin = None
        gross_margin_basis = "(营业收入-营业成本)/营业收入×100%"
        gross_margin_keys = ("revenue_current", "cost_of_goods_current")
        gross_margin_reason = "缺少营业收入或营业成本"
    add("gross_margin_pct", "毛利率（营业收入与营业成本口径）", gross_margin_basis,
        gross_margin_keys, gross_margin * _D_HUNDRED if gross_margin is not None else None,
        unit="%", status="calculated" if gross_margin is not None else "insufficient_data",
        reason="营业收入缺失或为零" if (rev_c is None or rev_c == 0) else gross_margin_reason)

    # 盈利能力补充指标。所有指标只使用已解析的原始事实；没有任何相关输入时
    # 不生成空条目，只有部分输入时才保留“未获取”并列出缺失字段。
    operating_profit_c = values.get("operating_profit_current")
    net_assets_p = values.get("net_assets_previous")
    total_assets_p = values.get("total_assets_previous")
    current_assets_p = values.get("current_assets_previous")
    parent_np_c = values.get("net_profit_parent_current")
    deducted_np_c = values.get("net_profit_parent_deducted_current")
    weighted_shares = values.get("weighted_average_shares_current")

    sales_net_margin = _safe_div(np_c, rev_c)
    add("sales_net_margin_pct", "销售净利率", "净利润/营业收入×100%",
        ("net_profit_current", "revenue_current"),
        sales_net_margin * _D_HUNDRED if sales_net_margin is not None else None,
        unit="%", status="calculated" if sales_net_margin is not None else "not_comparable",
        reason="营业收入缺失或为零" if sales_net_margin is None else "",
        _only_if_any_input=True)

    operating_margin = _safe_div(operating_profit_c, rev_c)
    add("operating_margin_pct", "营业利润率", "营业利润/营业收入×100%",
        ("operating_profit_current", "revenue_current"),
        operating_margin * _D_HUNDRED if operating_margin is not None else None,
        unit="%", status="calculated" if operating_margin is not None else "not_comparable",
        reason="营业收入缺失或为零" if operating_margin is None else "",
        _only_if_any_input=True)

    avg_net_assets = ((net_assets_p + net_assets_c) / Decimal("2")
                      if net_assets_p is not None and net_assets_c is not None else None)
    roe = _safe_div(parent_np_c, avg_net_assets) if avg_net_assets not in (None, _D_ZERO) else None
    add("roe_weighted_pct", "净资产收益率（加权）",
        "归属于普通股股东的净利润/平均净资产×100%",
        ("net_profit_parent_current", "net_assets_previous", "net_assets_current"),
        roe * _D_HUNDRED if roe is not None else None, unit="%",
        status="calculated" if roe is not None else "not_comparable",
        reason="缺少归母净利润或两期净资产，无法计算平均净资产口径" if roe is None else "",
        _only_if_any_input=True)

    deducted_roe = (_safe_div(deducted_np_c, avg_net_assets)
                    if avg_net_assets not in (None, _D_ZERO) else None)
    add("roe_deducted_pct", "净资产收益率（扣非）",
        "扣非归属于普通股股东的净利润/平均净资产×100%",
        ("net_profit_parent_deducted_current", "net_assets_previous", "net_assets_current"),
        deducted_roe * _D_HUNDRED if deducted_roe is not None else None, unit="%",
        status="calculated" if deducted_roe is not None else "not_comparable",
        reason="缺少扣非归母净利润或两期净资产，无法计算平均净资产口径" if deducted_roe is None else "",
        _only_if_any_input=True)

    avg_total_assets = ((total_assets_p + ta_c) / Decimal("2")
                        if total_assets_p is not None and ta_c is not None else None)
    roa = _safe_div(np_c, avg_total_assets) if avg_total_assets not in (None, _D_ZERO) else None
    add("roa_pct", "总资产报酬率（ROA）", "净利润/平均总资产×100%",
        ("net_profit_current", "total_assets_previous", "total_assets_current"),
        roa * _D_HUNDRED if roa is not None else None, unit="%",
        status="calculated" if roa is not None else "not_comparable",
        reason="缺少净利润或两期总资产，无法计算平均总资产口径" if roa is None else "",
        _only_if_any_input=True)

    eps = _safe_div(parent_np_c, weighted_shares)
    add("eps_basic", "每股收益（EPS）", "归属于普通股股东的净利润/加权平均普通股股数",
        ("net_profit_parent_current", "weighted_average_shares_current"), eps, unit="元/股",
        status="calculated" if eps is not None else "not_comparable",
        reason="缺少归母净利润或加权平均普通股股数" if eps is None else "",
        _only_if_any_input=True)

    common_shares = values.get("common_shares_current")
    book_value_per_share = _safe_div(net_assets_c, common_shares)
    add("book_value_per_share", "每股净资产", "期末净资产/期末普通股股数",
        ("net_assets_current", "common_shares_current"), book_value_per_share,
        unit="元/股", status="calculated" if book_value_per_share is not None else "not_comparable",
        reason="缺少期末净资产或普通股股数" if book_value_per_share is None else "",
        _only_if_any_input=True)

    ocf_np = _safe_div(ocf_c, np_c)
    add("operating_cashflow_to_net_profit_ratio", f"经营现金流/{profit_label}", "经营活动现金流净额/净利润",
        ("operating_cashflow_current", "net_profit_current"), ocf_np, threshold=0.5,
        threshold_source="内部筛查规则：比值低于0.5提示利润质量复核",
        status="calculated" if ocf_np is not None else "not_comparable",
        reason="净利润缺失或为零" if ocf_np is None else "",
        _only_if_any_input=True)
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

    # 应收账款附注通常同时披露账面余额和坏账准备。账面余额的跨期变化是
    # 独立于净额/营收比的事实信号，必须在有明确原文输入时稳定进入告警，
    # 但仍保留经营现金流改善作为反向证据，不直接认定收入造假或回款恶化。
    ar_gross_c = values.get("accounts_receivable_gross_current")
    ar_gross_p = values.get("accounts_receivable_gross_same_period_previous")
    ar_gross_change = _pct_change(ar_gross_c, ar_gross_p)
    ar_gross_previous_period = facts_by_key.get("accounts_receivable_gross_same_period_previous", {}).get("period", "")
    revenue_previous_period = facts_by_key.get("revenue_previous", {}).get("period", "")
    gross_period_kind = _period_kind(ar_gross_previous_period)
    gross_period_label = {
        "year_end": "较上年末", "interim": "较上年同期", "same_period": "较上年同期",
    }.get(gross_period_kind, "跨期")
    gross_formula_period = {
        "year_end": "上年末", "interim": "上年同期", "same_period": "上年同期",
    }.get(gross_period_kind, "上期")
    add("accounts_receivable_gross_yoy_change_pct", f"应收账款账面余额{gross_period_label}变动率",
        f"(本期应收账款账面余额-{gross_formula_period}账面余额)/|{gross_formula_period}账面余额|×100%",
        ("accounts_receivable_gross_current", "accounts_receivable_gross_same_period_previous"),
        ar_gross_change, unit="%", status="calculated" if ar_gross_change is not None else "not_comparable",
        reason="缺少原文明确的应收账款账面余额跨期数据" if ar_gross_change is None else "")
    if ar_gross_change is not None:
        rev_change_for_alert = rev_change
        periods_aligned = _comparison_periods_aligned(ar_gross_previous_period, revenue_previous_period)
        growth_direction = "上升" if ar_gross_change > 0 else "下降" if ar_gross_change < 0 else "基本不变"
        revenue_direction = ("上升" if rev_change_for_alert > 0 else "下降" if rev_change_for_alert < 0 else "基本不变") \
            if rev_change_for_alert is not None else "未获取"
        # A point-in-time balance versus a different flow period cannot support a
        # numeric divergence claim.  Keep only the observable directions and gate
        # the stronger alert until both prior periods are explicitly aligned.
        if ar_gross_change >= Decimal("30") and periods_aligned is not True:
            if periods_aligned is False and rev_change_for_alert is not None and growth_direction != revenue_direction:
                alerts.append(
                    f"应收账款账面余额{gross_period_label}增长 {_format(ar_gross_change, 2, '%')}，"
                    f"与营业收入同比{_format(rev_change_for_alert, 2, '%')}方向相反；"
                    "两项比较期间不一致，口径待对齐，暂不作异常判断，"
                    "需补充同期间应收与营收数据后复核"
                )
            elif periods_aligned is None:
                alerts.append(
                    f"应收账款账面余额{gross_period_label}增长 {_format(ar_gross_change, 2, '%')}；"
                    "营业收入比较期间未完整标注，暂不作背离判断，需核实期间口径"
                )
        elif ar_gross_change >= Decimal("30") and (rev_change_for_alert is None
                                                    or ar_gross_change - rev_change_for_alert >= Decimal("20")):
            bad_debt_c = values.get("bad_debt_provision_current")
            bad_debt_p = values.get("bad_debt_provision_same_period_previous")
            bad_debt_note = ""
            bad_debt_change = _pct_change(bad_debt_c, bad_debt_p)
            if bad_debt_change is not None:
                bad_debt_note = f"，坏账准备较上年末变动 {_format(bad_debt_change, 2, '%')}"
            alerts.append(
                f"应收账款账面余额较上年末增长 {_format(ar_gross_change, 2, '%')}，"
                f"与营业收入同比变动 {_format(rev_change_for_alert, 2, '%') if rev_change_for_alert is not None else '未获取'}"
                f"方向相反，比较期间口径待对齐{bad_debt_note}；"
                "该项仅作为待核查线索，需补充同期间应收与营收数据后复核；"
                "经营现金流仍需结合跨期变化核查，不能据此排除回款风险"
            )

    # 不再把应收余额同比与营业收入同比直接作差，改为比较同口径应收/营收比。
    ar_rev_previous = _safe_div(ar_p, rev_p)
    ratio_change_pp = (ar_rev - ar_rev_previous) * _D_HUNDRED if ar_rev is not None and ar_rev_previous is not None else None
    comparison_reason = "缺少匹配期间的应收或营业收入，不能进行同口径比较"
    comparison_formula = "(本期应收/本期营收-上期应收/上期营收)×100个百分点"
    comparison_keys = ("accounts_receivable_current", "revenue_current", "accounts_receivable_previous", "revenue_previous")
    if is_interim_period(period):
        ar_rev_previous = _safe_div(values.get("accounts_receivable_same_period_previous"), rev_p)
        ratio_change_pp = (ar_rev - ar_rev_previous) * _D_HUNDRED if ar_rev is not None and ar_rev_previous is not None else None
        if ar_rev_previous is not None:
            comparison_formula = "(本期应收/本期营收-上年同期末应收/上期营收)×100个百分点"
            comparison_keys = ("accounts_receivable_current", "revenue_current", "accounts_receivable_same_period_previous", "revenue_previous")
        comparison_reason = "中期报表期初应收余额不能与上年同期营收配比；需补充上年同期期末应收余额，不能将期初期末变动视为同比"
    add("accounts_receivable_to_revenue_ratio_change_pp", "应收/营收比变动", comparison_formula,
        comparison_keys, ratio_change_pp,
        unit="个百分点", threshold=5, threshold_source="内部筛查规则：同口径应收/营收比上升超过5个百分点提示复核",
        status="calculated" if ratio_change_pp is not None else "not_comparable",
        reason=comparison_reason if ratio_change_pp is None else "")
    if ratio_change_pp is not None and ratio_change_pp > Decimal("5"):
        alerts.append(f"应收账款/营收比上升 {_format(ratio_change_pp, 2, '个百分点')}，需复核回款与收入确认")

    period_days = values.get("period_days")
    if period_days is None:
        half_year = re.search(r"(20\d{2})\s*年?\s*(?:半年度?|上半年|[Hh]1)", period)
        if half_year:
            year = int(half_year.group(1))
            period_days = Decimal((date(year, 7, 1) - date(year, 1, 1)).days)
            days_source = "根据已声明上半年期间按日历计算天数（非原始披露值）"
            facts_by_key["period_days"] = {
                "field": "period_days", "raw_value": str(period_days), "unit": "天",
                "period": period, "source": "period_calendar",
            }
        elif is_interim_period(period):
            period_days = None
            days_source = "中期报告缺少明确期间天数，不采用年度365天"
        else:
            period_days = Decimal("365")
            days_source = "年度默认365天（规则默认，非原始披露值）"
    elif period_days <= 0:
        period_days = None
        days_source = "期间天数必须为正数"
    else:
        days_source = "输入的期间天数"
    values["period_days"] = period_days
    avg_ar = (ar_c + ar_p) / Decimal("2") if ar_c is not None and ar_p is not None else None
    ar_opening_matches = _opening_period_matches("accounts_receivable_previous", facts_by_key, period)
    if not ar_opening_matches:
        avg_ar = None
    ar_turn = _safe_div(rev_c, avg_ar)
    add("accounts_receivable_turnover_ratio", "应收账款周转率", "营业收入/((期初应收账款+期末应收账款)/2)",
        ("revenue_current", "accounts_receivable_previous", "accounts_receivable_current"), ar_turn, unit="次",
        status="calculated" if ar_turn is not None else "insufficient_data",
        reason=("应收余额日期与本期期初不匹配，不能作为周转率平均余额" if not ar_opening_matches else
                "缺少匹配期间平均应收余额或营业收入") if ar_turn is None else "")
    ar_days = _safe_div(period_days, ar_turn)
    add("accounts_receivable_turnover_days", "应收账款周转天数", "期间天数/应收账款周转率", ("period_days",), ar_days, unit="天",
        status="calculated" if ar_days is not None else "insufficient_data", reason=days_source if ar_days is not None else "周转率无法计算")

    avg_inv = (inv_c + inv_p) / Decimal("2") if inv_c is not None and inv_p is not None else None
    inv_opening_matches = _opening_period_matches("inventory_previous", facts_by_key, period)
    if not inv_opening_matches:
        avg_inv = None
    inv_turn = _safe_div(cog, avg_inv)
    add("inventory_turnover_ratio", "存货周转率", "营业成本/((期初存货+期末存货)/2)",
        ("cost_of_goods_current", "inventory_previous", "inventory_current"), inv_turn, unit="次",
        status="calculated" if inv_turn is not None else "insufficient_data",
        reason=("存货余额日期与本期期初不匹配，不能作为周转率平均余额" if not inv_opening_matches else
                "缺少匹配期间平均存货余额或营业成本") if inv_turn is None else "")
    inv_days = _safe_div(period_days, inv_turn)
    add("inventory_turnover_days", "存货周转天数", "期间天数/存货周转率", ("period_days",), inv_days, unit="天",
        status="calculated" if inv_days is not None else "insufficient_data", reason=days_source if inv_days is not None else "周转率无法计算")
    if inv_c is not None and inv_p is not None and inv_p > 0 and inv_c > inv_p * Decimal("1.5"):
        alerts.append(f"存货较期初增长 {_format((inv_c / inv_p - 1) * _D_HUNDRED, 2, '%')}，存货激增需关注跌价风险")

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

    cash = values.get("cash_and_equivalents_current")
    cash_ratio = _safe_div(cash, cl_c)
    add("cash_ratio", "现金比率（短期偿债）", "现金及现金等价物/流动负债",
        ("cash_and_equivalents_current", "current_liabilities_current"), cash_ratio,
        unit="倍", threshold=0.2,
        threshold_source="内部筛查规则：现金比率低于0.2倍提示短期流动性复核",
        status="calculated" if cash_ratio is not None else "not_comparable",
        reason="流动负债缺失或为零" if cash_ratio is None else "",
        _only_if_any_input=True)

    equity_ratio = _safe_div(tl_c, net_assets_c)
    add("equity_ratio", "产权比率（长期偿债）", "总负债/所有者权益",
        ("total_liabilities_current", "net_assets_current"), equity_ratio,
        unit="倍", threshold=1,
        threshold_source="内部筛查规则：产权比率高于1倍提示长期偿债结构复核",
        status="calculated" if equity_ratio is not None else "not_comparable",
        reason="净资产缺失、为零或为负，比例不适用" if equity_ratio is None else "",
        _only_if_any_input=True)

    interest_expense_c = values.get("interest_expense_current")
    profit_before_tax_c = values.get("profit_before_tax_current")
    # F6 口径修复：主口径 = (利润总额+利息支出)/利息支出。EBIT 须加回利息支出
    # （与 knowledge_base/风险评分模型库 的 EBIT 定义一致）；旧主口径直接用营业
    # 利润——中国准则下营业利润已扣除财务费用（含利息支出）且混入投资收益等
    # 非经常项，系统性低估利息保障倍数。营业利润口径仅在利润总额缺失时作
    # 标注性 fallback。
    if profit_before_tax_c is not None and interest_expense_c is not None:
        ebit = profit_before_tax_c + interest_expense_c
        ebit_keys = ("profit_before_tax_current", "interest_expense_current")
        interest_formula = "(利润总额+利息支出)/利息支出"
    else:
        ebit = operating_profit_c
        ebit_keys = ("operating_profit_current", "interest_expense_current")
        interest_formula = "营业利润/利息支出（利润总额缺失，口径受限）"
    interest_coverage = _safe_div(ebit, interest_expense_c)
    add("interest_coverage_ratio", "利息保障倍数（长期偿债）", interest_formula,
        ebit_keys, interest_coverage, unit="倍", threshold=1.5,
        threshold_source="内部筛查规则：利息保障倍数低于1.5倍提示偿债能力复核",
        status="calculated" if interest_coverage is not None else "not_comparable",
        reason="缺少息税前利润或利息支出" if interest_coverage is None else "",
        _only_if_any_input=True)

    goodwill, net_assets = values.get("goodwill_current"), values.get("net_assets_current")
    goodwill_ratio = _safe_div(goodwill, net_assets) if net_assets is not None and net_assets > 0 else None
    add("goodwill_to_net_assets_ratio_pct", "商誉/净资产", "商誉/净资产×100%", ("goodwill_current", "net_assets_current"),
        goodwill_ratio * _D_HUNDRED if goodwill_ratio is not None else None, unit="%", threshold=30,
        threshold_source="内部筛查规则：商誉/净资产超过30%提示减值复核",
        status="calculated" if goodwill_ratio is not None else "not_comparable", reason="净资产缺失、为零或为负，比例不适用" if goodwill_ratio is None else "",
        _only_if_any_input=True)
    if goodwill_ratio is not None and goodwill_ratio > Decimal("0.3"):
        alerts.append(f"商誉占净资产比 = {_format(goodwill_ratio * _D_HUNDRED, 2, '%')}，超过 30%，需复核减值")

    fixed_c, fixed_p = values.get("fixed_assets_current"), values.get("fixed_assets_previous")
    fixed_avg = (fixed_c + fixed_p) / Decimal("2") if fixed_c is not None and fixed_p is not None else None
    if not _opening_period_matches("fixed_assets_previous", facts_by_key, period):
        fixed_avg = None
    fixed_turn = _safe_div(rev_c, fixed_avg)
    add("fixed_asset_turnover_ratio", "固定资产周转率", "营业收入/((期初固定资产+期末固定资产)/2)",
        ("revenue_current", "fixed_assets_previous", "fixed_assets_current"), fixed_turn, unit="次",
        status="calculated" if fixed_turn is not None else "insufficient_data", reason="缺少匹配期间平均固定资产余额" if fixed_turn is None else "",
        _only_if_any_input=True)

    avg_total_assets = ((total_assets_p + ta_c) / Decimal("2")
                        if total_assets_p is not None and ta_c is not None else None)
    total_asset_turn = _safe_div(rev_c, avg_total_assets)
    add("total_asset_turnover_ratio", "总资产周转率",
        "营业收入/((期初总资产+期末总资产)/2)",
        ("revenue_current", "total_assets_previous", "total_assets_current"),
        total_asset_turn, unit="次",
        status="calculated" if total_asset_turn is not None else "insufficient_data",
        reason="缺少匹配期间平均总资产" if total_asset_turn is None else "",
        _only_if_any_input=True)

    avg_current_assets = ((current_assets_p + ca_c) / Decimal("2")
                          if current_assets_p is not None and ca_c is not None else None)
    current_asset_turn = _safe_div(rev_c, avg_current_assets)
    add("current_asset_turnover_ratio", "流动资产周转率",
        "营业收入/((期初流动资产+期末流动资产)/2)",
        ("revenue_current", "current_assets_previous", "current_assets_current"),
        current_asset_turn, unit="次",
        status="calculated" if current_asset_turn is not None else "insufficient_data",
        reason="缺少匹配期间平均流动资产" if current_asset_turn is None else "",
        _only_if_any_input=True)

    cip_c, cip_p = values.get("construction_in_progress_current"), values.get("construction_in_progress_previous")
    cip_change = _pct_change(cip_c, cip_p)
    add("construction_in_progress_change_pct", "在建工程期初期末变动", "(本期在建工程-上期在建工程)/|上期在建工程|×100%",
        ("construction_in_progress_current", "construction_in_progress_previous"), cip_change, unit="%",
        status="calculated" if cip_change is not None else "not_comparable", reason="上期为零或负数时较上年末变动百分比不适用" if cip_change is None else "",
        _only_if_any_input=True)

    other_rec, other_pay = values.get("other_receivables_current"), values.get("other_payables_current")
    other_rec_ratio = _safe_div(other_rec, ta_c)
    add("other_receivables_to_total_assets_ratio_pct", "其他应收款/总资产", "其他应收款/总资产×100%",
        ("other_receivables_current", "total_assets_current"), other_rec_ratio * _D_HUNDRED if other_rec_ratio is not None else None,
        unit="%", threshold=5, threshold_source="内部筛查规则：其他应收款/总资产超过5%提示关联方资金往来复核",
        status="calculated" if other_rec_ratio is not None else "not_comparable", reason="总资产缺失或为零" if other_rec_ratio is None else "",
        _only_if_any_input=True)
    if other_rec_ratio is not None and other_rec_ratio > Decimal("0.05"):
        alerts.append(f"其他应收款占总资产比 = {_format(other_rec_ratio * _D_HUNDRED, 2, '%')}，超过 5%，需关注资金往来")
    other_pay_ratio = _safe_div(other_pay, ta_c)
    add("other_payables_to_total_assets_ratio_pct", "其他应付款/总资产", "其他应付款/总资产×100%",
        ("other_payables_current", "total_assets_current"), other_pay_ratio * _D_HUNDRED if other_pay_ratio is not None else None,
        unit="%", status="calculated" if other_pay_ratio is not None else "not_comparable", reason="总资产缺失或为零" if other_pay_ratio is None else "",
        _only_if_any_input=True)

    cash, short_debt = values.get("cash_and_equivalents_current"), values.get("short_term_debt_current")
    cash_debt_ratio = _safe_div(cash, short_debt)
    add("cash_to_short_term_debt_ratio", "现金/短期借款", "现金及现金等价物/短期借款",
        ("cash_and_equivalents_current", "short_term_debt_current"), cash_debt_ratio, unit="倍", threshold=1,
        threshold_source="内部筛查规则：现金/短期借款低于1倍提示短期资金安排复核",
        status="calculated" if cash_debt_ratio is not None else "not_comparable",
        reason="短期借款缺失或为零" if cash_debt_ratio is None else "",
        _only_if_any_input=True)

    ocf_liability_ratio = _safe_div(ocf_c, tl_c)
    add("operating_cashflow_to_total_liabilities_ratio", "经营现金流/总负债",
        "经营活动现金流净额/总负债", ("operating_cashflow_current", "total_liabilities_current"),
        ocf_liability_ratio, unit="倍", threshold=0.1,
        threshold_source="内部筛查规则：经营现金流/总负债低于0.1倍提示负债现金覆盖复核",
        status="calculated" if ocf_liability_ratio is not None else "not_comparable",
        reason="总负债缺失或为零" if ocf_liability_ratio is None else "",
        _only_if_any_input=True)

    ocf_assets_ratio = _safe_div(ocf_c, ta_c)
    add("operating_cashflow_to_total_assets_ratio", "经营现金流/总资产",
        "经营活动现金流净额/总资产×100%", ("operating_cashflow_current", "total_assets_current"),
        ocf_assets_ratio * _D_HUNDRED if ocf_assets_ratio is not None else None,
        unit="%", threshold=3,
        threshold_source="内部筛查规则：经营现金流/总资产低于3%提示现金创造能力复核",
        status="calculated" if ocf_assets_ratio is not None else "not_comparable",
        reason="总资产缺失或为零" if ocf_assets_ratio is None else "",
        _only_if_any_input=True)

    gross_ar, bad_debt = values.get("accounts_receivable_gross_current"), values.get("bad_debt_provision_current")
    bad_debt_ratio = _safe_div(bad_debt, gross_ar)
    add("bad_debt_provision_to_gross_receivables_ratio_pct", "坏账准备/应收账款账面余额",
        "坏账准备/应收账款账面余额×100%",
        ("bad_debt_provision_current", "accounts_receivable_gross_current"),
        bad_debt_ratio * _D_HUNDRED if bad_debt_ratio is not None else None, unit="%",
        status="calculated" if bad_debt_ratio is not None else "not_comparable",
        reason="应收账款账面余额缺失或为零" if bad_debt_ratio is None else "",
        _only_if_any_input=True)

    ocf_revenue_ratio = _safe_div(ocf_c, rev_c)
    add("operating_cashflow_to_revenue_ratio", "经营现金流/营业收入",
        "经营活动现金流净额/营业收入×100%",
        ("operating_cashflow_current", "revenue_current"),
        ocf_revenue_ratio * _D_HUNDRED if ocf_revenue_ratio is not None else None,
        unit="%", threshold=5,
        threshold_source="内部筛查规则：经营现金流/营业收入低于5%提示现金转化复核",
        status="calculated" if ocf_revenue_ratio is not None else "not_comparable",
        reason="营业收入缺失或为零" if ocf_revenue_ratio is None else "",
        _only_if_any_input=True)

    capex = values.get("capital_expenditure_current")
    free_cash_flow = ocf_c - capex if ocf_c is not None and capex is not None else None
    add("free_cash_flow", "自由现金流",
        "经营活动现金流净额-资本性支出（资本性支出按正数输入）",
        ("operating_cashflow_current", "capital_expenditure_current"), free_cash_flow,
        unit="金额单位同输入", status="calculated" if free_cash_flow is not None else "not_comparable",
        reason="缺少经营活动现金流净额或资本性支出" if free_cash_flow is None else "",
        _only_if_any_input=True)

    total_assets_growth = _pct_change(ta_c, total_assets_p)
    add("total_assets_growth_pct", "总资产增长率",
        "(本期总资产-上期总资产)/|上期总资产|×100%",
        ("total_assets_current", "total_assets_previous"), total_assets_growth, unit="%",
        status="calculated" if total_assets_growth is not None else "not_comparable",
        reason="上期总资产为零、为负或缺失" if total_assets_growth is None else "",
        _only_if_any_input=True)

    net_assets_growth = _pct_change(net_assets_c, net_assets_p)
    add("net_assets_growth_pct", "净资产增长率",
        "(本期净资产-上期净资产)/|上期净资产|×100%",
        ("net_assets_current", "net_assets_previous"), net_assets_growth, unit="%",
        status="calculated" if net_assets_growth is not None else "not_comparable",
        reason="上期净资产为零、为负或缺失" if net_assets_growth is None else "",
        _only_if_any_input=True)

    int_income, int_expense = values.get("interest_income_current"), values.get("interest_expense_current")
    # F7 存贷双高筛查加固：
    # ① 双侧量级门槛——货币资金与短期借款需各自达到总资产的 5%，"双高"才是
    #    有意义的异常信号；小额现金对照小额借款触发告警属常态噪声。
    # ② 覆盖口径声明：短期借款并非有息负债全口径（缺长期借款/应付债券/一年内
    #    到期非流动负债字段），「存长贷双高」形态本筛查覆盖不到，结果须结合
    #    人工复核；字段扩展后应改用有息负债合计。
    # ③ 输入不足时显式声明「未验证」，不再静默跳过（fail-loud）。
    _dual_high_inputs = {"cash_and_equivalents": cash, "short_term_debt": short_debt,
                         "interest_income": int_income, "interest_expense": int_expense}
    _ta_for_dual_high = values.get("total_assets_current")
    _missing_dual = [k for k, v in _dual_high_inputs.items() if v is None]
    if _missing_dual:
        indicators["deposit_loan_dual_high"] = (
            f"未验证：缺少 {('、'.join(_missing_dual))}，存贷双高筛查未执行")
    else:
        _scale_ok = (_ta_for_dual_high is None or _ta_for_dual_high <= 0
                     or (cash >= _ta_for_dual_high * Decimal("0.05")
                         and short_debt >= _ta_for_dual_high * Decimal("0.05")))
        indicators["deposit_loan_dual_high"] = (
            f"已筛查（口径：货币资金 vs 短期借款，非有息负债全口径）；"
            f"货币资金/总资产={_format(_safe_div(cash, _ta_for_dual_high) * _D_HUNDRED, 1, '%') if _ta_for_dual_high else 'N/A'}，"
            f"短期借款/总资产={_format(_safe_div(short_debt, _ta_for_dual_high) * _D_HUNDRED, 1, '%') if _ta_for_dual_high else 'N/A'}")
        if cash > short_debt and int_expense > int_income * 2 and _scale_ok:
            alerts.append(f"现金及现金等价物({_format(cash)})高于短期借款({_format(short_debt)})，但利息支出({_format(int_expense)})远大于利息收入({_format(int_income)})，存在存贷双高异常（口径：短期借款，未覆盖长期有息负债）")

    if np_c is not None and np_p is not None:
        indicators["net_profit_current"] = _number(np_c)
        indicators["net_profit_previous"] = _number(np_p)
        if np_c < 0 and np_p < 0:
            alerts.append(f"连续两个报告期间{profit_label}为负，存在持续经营风险信号")
    if ocf_c is not None and ocf_p is not None and ocf_c < 0 and ocf_p < 0:
        alerts.append("经营活动现金流量净额连续两个报告期间为负，存在持续经营风险信号")

    statement_items = {
        "balance_sheet": {key: _number(values[key]) for key in (
            "total_assets_current", "total_assets_previous", "total_liabilities_current", "net_assets_current", "net_assets_previous",
            "cash_and_equivalents_current",
            "accounts_receivable_current", "accounts_receivable_previous", "inventory_current", "inventory_previous",
            "current_assets_current", "current_assets_previous", "current_liabilities_current", "short_term_debt_current", "goodwill_current",
            "other_receivables_current", "fixed_assets_current", "intangible_assets_current",
            "accounts_receivable_gross_current", "accounts_receivable_gross_same_period_previous",
            "bad_debt_provision_current", "bad_debt_provision_same_period_previous",
        ) if key in values},
        "income_statement": {key: _number(values[key]) for key in (
            "revenue_current", "revenue_previous", "net_profit_current", "net_profit_previous", "net_profit_parent_current",
            "net_profit_parent_deducted_current", "cost_of_goods_current", "operating_profit_current",
            "profit_before_tax_current", "income_tax_expense_current", "weighted_average_shares_current",
            "common_shares_current",
        ) if key in values},
        "cashflow_statement": {key: _number(values[key]) for key in (
            "operating_cashflow_current", "operating_cashflow_previous", "interest_income_current", "interest_expense_current",
            "capital_expenditure_current",
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
        "input_warnings": input_conflicts,
        "statement_items": statement_items,
        "unit_check": {
            "status": "declared" if data.get("amount_unit") or _metadata(data).get("amount_unit") else "pending",
            "message": "金额单位来自输入声明" if data.get("amount_unit") or _metadata(data).get("amount_unit") else "未提供统一金额单位，结果不得跨单位比较",
        },
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
