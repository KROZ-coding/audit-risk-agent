"""Build the one authoritative data snapshot used by every report adapter.

The agent still exposes its historical top-level fields for compatibility.  This
module is deliberately independent from the exporters so a chart or a document
cannot silently invent a second risk count, period or evidence decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.result_contract import (
    DATA_VERSION,
    RULE_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    SOURCE_STATUS_DEMO_PLACEHOLDER,
    SOURCE_STATUS_INCOMPLETE,
    SOURCE_STATUS_LABELS,
    SOURCE_STATUS_UNVERIFIED,
    SOURCE_STATUS_VERIFIED,
    derive_task_status,
    score_status_eligible,
)


_LEVEL_ALIASES = {
    "重大": "重大", "高风险": "重大", "极高风险": "重大", "严重": "重大", "高": "重大",
    "重要": "重要", "中等风险": "重要", "中": "重要",
    "一般": "一般", "低风险": "一般", "轻微": "一般", "低": "一般",
}

_DIMENSION_ALIASES = {
    "财务错报": "financial_misstatement", "财务错报风险": "financial_misstatement",
    "财务": "financial_misstatement", "financial": "financial_misstatement",
    "关联交易": "related_party", "关联交易风险": "related_party", "related_party": "related_party",
    "信披合规": "disclosure_compliance", "信息披露合规": "disclosure_compliance",
    "信息披露合规风险": "disclosure_compliance", "disclosure_compliance": "disclosure_compliance",
    "持续经营": "going_concern", "持续经营风险": "going_concern", "going_concern": "going_concern",
    "监管处罚": "regulatory_penalty", "监管处罚类高风险": "regulatory_penalty",
    "regulatory_penalty": "regulatory_penalty", "数据可靠性风险": "data_reliability",
    "data_reliability": "data_reliability",
}

# 内部维度键用于评分、拆分和历史兼容；公共标签只在展示适配层使用，避免把
# 尚未核实的会计结论直接写进网页、PDF 或 Excel 的标题。
_PUBLIC_DIMENSION_LABELS = {
    "financial_misstatement": "财务风险",
    "related_party": "关联方交易与资金往来",
    "disclosure_compliance": "信息披露与合规",
    "going_concern": "持续经营与偿债",
    "regulatory_penalty": "监管问询与处罚",
    "data_reliability": "数据勾稽与可靠性",
    "市场风险": "行业与市场环境",
    "经营风险": "经营与行业环境",
    "经营与财务": "经营与财务传导",
    "资产质量": "资产质量",
}

_PUBLIC_TITLE_REPLACEMENTS = (
    ("财务报表错报风险", "财务风险"),
    ("财务错报风险", "财务风险"),
    ("财务错报", "财务风险"),
    ("关联交易风险", "关联方交易与资金往来"),
    ("信息披露合规风险", "信息披露与合规"),
    ("持续经营风险", "持续经营与偿债"),
    ("监管处罚类高风险", "监管问询与处罚"),
    ("监管处罚风险", "监管问询与处罚"),
    ("数据可靠性风险", "数据勾稽与可靠性"),
    ("市场风险", "行业与市场环境"),
    ("经营风险", "经营与行业环境"),
    ("数据异常", "数据勾稽异常"),
)

_FLOW_FACT_PREFIXES = (
    "revenue", "net_profit", "gross_profit", "operating_profit", "ebit",
    "operating_cashflow", "cost_of_goods", "operating_cost", "sga_expense",
    "interest_income", "interest_expense", "depreciation", "amortization",
    "dividends",
)


def _is_flow_fact(field: str) -> bool:
    field = str(field or "")
    return any(field == prefix or field.startswith(prefix + "_") for prefix in _FLOW_FACT_PREFIXES)


def _jsonish(value: Any) -> Any:
    """Parse tool strings without turning malformed output into an empty success."""
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    if isinstance(value, (Decimal, datetime, date)):
        return str(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"raw_text": value}
    return copy.deepcopy(value)


def normalize_tool_results(tool_results: dict) -> dict:
    """Return a deep-copied mapping with JSON tool results parsed to dictionaries."""
    source = tool_results or {}
    if isinstance(source, dict) and isinstance(source.get("entries"), list):
        normalized: dict[str, Any] = {}
        for entry in source["entries"]:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            normalized[str(entry["name"])] = _jsonish(entry.get("content", entry.get("result", {})))
        if normalized:
            return normalized
    if isinstance(source, list):
        normalized = {}
        for entry in source:
            if isinstance(entry, dict) and entry.get("name"):
                normalized[str(entry["name"])] = _jsonish(entry.get("content", entry.get("result", {})))
        return normalized
    return {str(key): _jsonish(value) for key, value in source.items()} if isinstance(source, dict) else {}


def _records(values: Any) -> list[dict]:
    if not isinstance(values, list):
        return []
    return [copy.deepcopy(item) for item in values if isinstance(item, dict)]


def _merge_records(*groups: list[dict], id_key: str) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        for item in group:
            key = str(item.get(id_key) or "")
            if not key:
                continue
            if key not in merged:
                merged[key] = copy.deepcopy(item)
                order.append(key)
                continue
            # Existing non-empty/verified values win.  Tool adapters may only
            # enrich fields that were absent from the LLM record.
            for field, value in item.items():
                old = merged[key].get(field)
                if old in (None, "", [], {}):
                    merged[key][field] = copy.deepcopy(value)
    return [merged[key] for key in order]


def _valid_status(value: Any) -> str | None:
    value = str(value or "").strip()
    return value if value in SOURCE_STATUS_LABELS else None


def _as_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\s]+", value)
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if item))


def _source_ref(item: dict) -> dict:
    ref = item.get("source_ref")
    if isinstance(ref, dict):
        ref = copy.deepcopy(ref)
    else:
        ref = {}
    for key, aliases in {
        "document": ("source_document", "document"),
        "page": ("page", "source_page"),
        "locator": ("locator",),
        "evidence_ids": ("evidence_ids", "evidence_id"),
    }.items():
        if key in ref and ref[key] not in (None, "", []):
            continue
        for alias in aliases:
            if item.get(alias) not in (None, "", []):
                ref[key] = _as_ids(item[alias]) if key == "evidence_ids" else item[alias]
                break
    return ref


def _infer_period_type(item: dict) -> str:
    field = str(item.get("field") or item.get("metric_id") or "")
    # 收入、利润、成本和现金流即使期间以期末日期表示，仍是期间流量，不能因
    # 字段带 _current/_previous 或日期形态而误标为时点余额。
    if _is_flow_fact(field):
        return "flow_period"
    existing = str(item.get("period_type") or "").strip()
    if existing:
        return existing
    period = str(item.get("period") or item.get("point_in_time") or "")
    if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", period) or any(
        token in field for token in ("_current", "_previous", "_end", "_begin")
    ):
        return "point_in_time"
    if period:
        return "flow_period"
    return ""


def _normalize_value_record(item: dict, *, kind: str) -> dict:
    result = copy.deepcopy(item)
    status = _valid_status(result.get("source_status"))
    if not status:
        # `status=calculated` is an execution status, not a source claim. A
        # calculated value becomes verified only when its source contract is
        # complete; otherwise it remains visible but cannot enter scoring.
        has_value = result.get("value") is not None
        has_context = bool(result.get("unit") and result.get("period") and result.get("scope"))
        has_location = bool(result.get("source_document") or result.get("page") or result.get("locator")
                            or result.get("source_ref"))
        has_evidence = bool(_as_ids(result.get("evidence_ids")))
        status = SOURCE_STATUS_VERIFIED if has_value and has_context and (has_location or has_evidence) else (
            SOURCE_STATUS_INCOMPLETE if has_value else SOURCE_STATUS_UNVERIFIED)
    result["source_status"] = status
    result.setdefault("source_note", SOURCE_STATUS_LABELS[status])
    result["source_ref"] = _source_ref(result)
    result["period_type"] = _infer_period_type(result)
    result.setdefault("comparability", "comparable" if result.get("period") else "unknown")
    result["display_eligible"] = result.get("display_eligible", True)
    result["score_eligible"] = bool(result.get("score_eligible", score_status_eligible(status))) and score_status_eligible(status)
    if result.get("value") is not None:
        result.setdefault("display_value", _display_value(result.get("value"), result.get("unit", "")))
    return result


def _display_value(value: Any, unit: str = "") -> str:
    if value is None:
        return "未获取"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "未获取"
    if isinstance(value, (int, float, Decimal)):
        return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
    return str(value)


def _evidence_usable(item: dict) -> bool:
    return bool(item.get("verified") is True and str(item.get("status", "verified")) == "verified"
                and str(item.get("excerpt") or "").strip()
                and (item.get("source_document") or item.get("page") or item.get("locator")
                     or item.get("fact_ids") or item.get("metric_ids")))


def bind_evidence_to_risks(report: dict) -> dict:
    """Attach only catalogued evidence IDs and assign a source status to each risk."""
    result = copy.deepcopy(report or {})
    catalog = {
        str(item.get("evidence_id")): item
        for item in _records(result.get("evidence"))
        if item.get("evidence_id")
    }
    # _records() deliberately returns deep copies. Keep the normalized list and
    # write it back; otherwise fields added below disappear from the result.
    risks = _records(result.get("risk_details"))
    for risk in risks:
        ids = _as_ids(risk.get("evidence_ids") or risk.get("evidence_id"))
        if not ids:
            # Tool-generated source IDs follow the stable risk prefix. This is
            # intentionally conservative: no text-only match is promoted.
            rid = str(risk.get("risk_id") or "")
            ids = [key for key in catalog if rid and rid in key]
        ids = [key for key in ids if key in catalog]
        risk["evidence_ids"] = list(dict.fromkeys(ids))
        usable = bool(ids) and all(_evidence_usable(catalog[key]) for key in ids)
        status = SOURCE_STATUS_VERIFIED if usable else (
            SOURCE_STATUS_INCOMPLETE if ids or str(risk.get("evidence") or "").strip() else SOURCE_STATUS_UNVERIFIED)
        risk["source_status"] = status
        risk["source_note"] = SOURCE_STATUS_LABELS[status]
        risk["source_ref"] = {
            "evidence_ids": risk["evidence_ids"],
            "pages": list(dict.fromkeys(str(catalog[key].get("page")) for key in ids if catalog[key].get("page"))),
            "locators": list(dict.fromkeys(str(catalog[key].get("locator")) for key in ids if catalog[key].get("locator"))),
        }
        risk["display_eligible"] = True
        risk["score_eligible"] = usable
        risk["formal_status"] = "accepted" if risk.get("formal_status") == "accepted" and usable else "unaccepted"
        if risk["formal_status"] != "accepted":
            risk.setdefault("pending_reason", "缺少可回指且已核验的证据" if not usable else "待人工复核")
    result["risk_details"] = risks
    return result


def _normalize_dimension(value: Any) -> str:
    text = str(value or "").strip()
    return _DIMENSION_ALIASES.get(text, _DIMENSION_ALIASES.get(text.lower(), text.lower()))


def _public_dimension_label(dimension: Any) -> str:
    normalized = _normalize_dimension(dimension)
    return _PUBLIC_DIMENSION_LABELS.get(normalized, str(dimension or "").strip() or "未分类")


def _public_risk_title(title: Any) -> str:
    """仅清理标题中的结论性旧标签，不改写证据、分析和法规依据。"""
    text = str(title or "").strip()
    for source, target in _PUBLIC_TITLE_REPLACEMENTS:
        text = text.replace(source, target)
    return text


def _normalize_public_risk_fields(item: dict, *, pending: bool) -> dict:
    """Attach the public pending/formal fields used by every artifact renderer."""
    result = item
    result["display_status"] = "待复核提示" if pending else "正式采信风险"
    result["display_level"] = (
        f"{result.get('level', '一般')}（暂定关注）" if pending
        else str(result.get("level") or "一般")
    )
    if pending:
        result.setdefault("verification_status", "待复核")
        result.setdefault("pending_reason", "待补充证据或人工复核")
        result.setdefault("next_procedure", result.get("audit_suggestion") or "补充证据并完成人工复核")
    result["evidence_refs"] = _as_ids(result.get("evidence_ids") or result.get("evidence_id"))
    result.setdefault("public_title", result.get("display_title") or result.get("title") or "未命名事项")
    return result


def _normalize_level(value: Any) -> str:
    return _LEVEL_ALIASES.get(str(value or "").strip(), "一般")


def _dedupe_risks(groups: list[list[dict]]) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("risk_id") or item.get("semantic_id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            output.append(copy.deepcopy(item))
    return output


def build_risk_layers(report: dict) -> dict:
    """Split visible risks into formal, pending and excluded layers."""
    source = copy.deepcopy(report or {})
    candidates = _dedupe_risks([
        _records(source.get("risk_details")),
        _records(source.get("accepted_risk_details")),
        _records(source.get("pending_items")),
    ])
    excluded = _dedupe_risks([_records(source.get("excluded_items")), _records(source.get("risks", {}).get("excluded") if isinstance(source.get("risks"), dict) else [])])
    formal: list[dict] = []
    pending: list[dict] = []
    for item in candidates:
        item["dimension"] = _normalize_dimension(item.get("dimension"))
        item["display_dimension"] = _public_dimension_label(item["dimension"])
        item["display_title"] = _public_risk_title(item.get("title"))
        item["level"] = _normalize_level(item.get("level") or item.get("suggested_level"))
        evidence_ok = item.get("source_status") == SOURCE_STATUS_VERIFIED or (
            item.get("formal_status") == "accepted" and item.get("score_eligible") is not False)
        if item.get("formal_status") == "accepted" and evidence_ok:
            item["formal_status"] = "accepted"
            item["score_eligible"] = True
            formal.append(item)
        else:
            item["formal_status"] = "unaccepted"
            item["score_eligible"] = False
            item.setdefault("pending_reason", "待补证据或人工复核")
            pending.append(item)
    for item in formal:
        _normalize_public_risk_fields(item, pending=False)
    for item in pending:
        _normalize_public_risk_fields(item, pending=True)
    for item in excluded:
        item["dimension"] = _normalize_dimension(item.get("dimension"))
        item["display_dimension"] = _public_dimension_label(item["dimension"])
        item["display_title"] = _public_risk_title(item.get("title"))
        item["formal_status"] = "excluded"
        item["score_eligible"] = False
        _normalize_public_risk_fields(item, pending=False)
    return {"all": formal + pending + excluded, "formal": formal, "pending": pending, "excluded": excluded}


def _level_counts(items: list[dict]) -> dict:
    counts = {"total": 0, "major": 0, "important": 0, "general": 0}
    for item in items:
        if not isinstance(item, dict):
            continue
        counts["total"] += 1
        level = _normalize_level(item.get("level") or item.get("suggested_level"))
        counts[{"重大": "major", "重要": "important", "一般": "general"}[level]] += 1
    return counts


def build_risk_summary(risk_layers: dict) -> dict:
    formal = _records(risk_layers.get("formal"))
    pending = _records(risk_layers.get("pending"))
    excluded = _records(risk_layers.get("excluded"))
    def _dimension_counts(items: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            dimension = _normalize_dimension(item.get("dimension")) or "unclassified"
            counts[dimension] = counts.get(dimension, 0) + 1
        return counts

    formal_by_dimension = _dimension_counts(formal)
    pending_by_dimension = _dimension_counts(pending)
    identified_by_dimension = _dimension_counts(formal + pending + excluded)
    # ``risk_dimensions`` is a historical public field consumed by PDF/Excel.
    # Keep it formal-only so a pending prompt cannot inflate the formal risk
    # distribution.  The all-layer counts remain available explicitly for the
    # web and machine-readable consumers.
    return {
        "all_identified": _level_counts(formal + pending + excluded),
        "formal": _level_counts(formal),
        "pending": _level_counts(pending),
        "by_dimension": identified_by_dimension,
        "formal_by_dimension": formal_by_dimension,
        "pending_by_dimension": pending_by_dimension,
        # Compatibility with the old summary contract.
        "total_risks": len(formal),
        "major_risks": _level_counts(formal)["major"],
        "important_risks": _level_counts(formal)["important"],
        "general_risks": _level_counts(formal)["general"],
        "pending_risks": len(pending),
        "risk_dimensions": formal_by_dimension,
    }


def _tool_payload(tool_results: dict, names: tuple[str, ...]) -> dict:
    for name in names:
        value = tool_results.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _source_defaults(records: list[dict], source: dict) -> list[dict]:
    """为事实、指标和证据补齐本批次源文件元数据，不覆盖字段级来源。"""
    source = source if isinstance(source, dict) else {}
    document = str(source.get("document_name") or "").strip()
    source_hash = str(source.get("source_hash") or "").strip()
    output = []
    for item in records:
        value = copy.deepcopy(item)
        if document and not str(value.get("source_document") or "").strip():
            value["source_document"] = document
        if source_hash and not str(value.get("source_hash") or "").strip():
            value["source_hash"] = source_hash
        output.append(value)
    return output


def _report_flow_period(company: dict) -> str:
    """把报告身份转换为流量事实可用的明确期间。"""
    period = str((company or {}).get("report_period") or "")
    match = re.search(r"(20\d{2})年(?:半年度?|上半年|中期)", period)
    if match:
        year = match.group(1)
        return f"{year}-01-01至{year}-06-30"
    match = re.search(r"(20\d{2})年度", period)
    if match:
        year = match.group(1)
        return f"{year}-01-01至{year}-12-31"
    return ""


def _correct_known_current_flow_periods(facts: list[dict], company: dict) -> list[dict]:
    """把半年/年度流量事实的截止日规范为起止期间。"""
    flow_period = _report_flow_period(company)
    if not flow_period:
        return facts
    current_year = flow_period[:4]
    is_half_year = flow_period.endswith("06-30")

    def period_for_year(year: str) -> str:
        return f"{year}-01-01至{year}-{'06-30' if is_half_year else '12-31'}"

    output = []
    for item in facts:
        value = copy.deepcopy(item)
        field = str(value.get("field") or "")
        if _is_flow_fact(field):
            raw_period = str(value.get("period") or "")
            match = re.search(r"(20\d{2})", raw_period)
            # 只有明确的 previous 字段才采用上期年份；无后缀的通用事实代表
            # 当前值，历史上模型曾把它误抄为上期日期，必须由报告身份纠正。
            year = (
                match.group(1) if field.endswith("_previous") and match
                else str(int(current_year) - 1) if field.endswith("_previous")
                else current_year
            )
            value["period"] = period_for_year(year)
            value["period_type"] = "flow_period"
        output.append(value)
    return output


def _derive_comparable_periods(facts: list[dict], company: dict) -> dict:
    """从同一报告的上年同期比较列派生两期流量趋势，不混入时点余额。"""
    by_field = {str(item.get("field")): item for item in facts if isinstance(item, dict)}
    specs = {
        "revenue": ("revenue_previous", "revenue_current"),
        "net_profit": ("net_profit_previous", "net_profit_current"),
        "operating_cashflow": ("operating_cashflow_previous", "operating_cashflow_current"),
        "cost_of_goods": ("cost_of_goods_previous", "cost_of_goods_current"),
    }
    pairs = {}
    periods = set()
    units = set()
    scopes = set()
    for output_key, (previous_key, current_key) in specs.items():
        previous, current = by_field.get(previous_key), by_field.get(current_key)
        if not previous or not current or previous.get("value") is None or current.get("value") is None:
            continue
        if previous.get("period_type") != "flow_period" or current.get("period_type") != "flow_period":
            continue
        if previous.get("scope") and current.get("scope") and previous.get("scope") != current.get("scope"):
            continue
        pairs[output_key] = (previous, current)
        periods.update((str(previous.get("period") or ""), str(current.get("period") or "")))
        units.update(str(item.get("unit") or "") for item in (previous, current) if item.get("unit"))
        scopes.update(str(item.get("scope") or "") for item in (previous, current) if item.get("scope"))
    if not pairs or len(periods) != 2:
        return {}
    ordered_periods = sorted(periods)
    rows = {period: {} for period in ordered_periods}
    for output_key, records in pairs.items():
        for record in records:
            rows[str(record.get("period"))][output_key] = record.get("value")
    amount_unit = next(iter(units)) if len(units) == 1 else ""
    unit_state = "verified" if len(units) == 1 else "conflicting"
    note = (
        "由同一报告内上年同期比较列派生，仅表示两个可比半年/年度期间的变化，"
        "不代表已取得连续多年数据。"
    )
    return {
        "status": "derived_comparable_periods",
        "source": "verified_financial_facts",
        "source_status": "verified" if unit_state == "verified" and len(scopes) <= 1 else "incomplete",
        "years_analyzed": ordered_periods,
        "periods_analyzed": ordered_periods,
        "year_count": len(ordered_periods),
        "amount_unit": amount_unit,
        "amount_unit_state": unit_state,
        "amount_unit_note": note if unit_state == "verified" else note + " 金额单位存在冲突，需人工复核。",
        "scope": next(iter(scopes)) if len(scopes) == 1 else "",
        "indicators_by_year": rows,
        "note": note,
    }


def _metrics_for_radar(snapshot: dict) -> list[dict]:
    specs = [
        ("毛利率(%)", ("gross_margin_pct", "gross_margin")),
        ("资产负债率(%)", ("debt_to_asset_ratio_pct", "debt_to_asset_ratio")),
        ("应收占营收比(%)", ("accounts_receivable_to_revenue_ratio", "ar_to_revenue_ratio")),
        ("存货周转率(次)", ("inventory_turnover_ratio", "inventory_turnover")),
        ("流动比率(倍)", ("current_ratio", "current_ratio")),
    ]
    by_id = {str(m.get("metric_id")): m for m in _records(snapshot.get("metrics"))}
    benchmark = snapshot.get("industry_benchmark") if isinstance(snapshot.get("industry_benchmark"), dict) else {}
    values = benchmark.get("benchmarks", benchmark) if isinstance(benchmark, dict) else {}
    output = []
    for label, aliases in specs:
        metric = next((by_id[key] for key in aliases if key in by_id), {})
        actual = metric.get("value") if isinstance(metric, dict) else None
        bench = None
        entry = next((values.get(key) for key in aliases[1:] if isinstance(values, dict) and key in values), None)
        if isinstance(entry, dict):
            bench = entry.get("average") if entry.get("verified") is True or entry.get("comparability_reviewed") is True else entry.get("presentation_value")
        elif isinstance(entry, (int, float)):
            bench = entry
        output.append({
            "label": label, "metric_id": metric.get("metric_id", aliases[0]),
            "actual": actual, "benchmark": bench,
            "source_status": metric.get("source_status", SOURCE_STATUS_UNVERIFIED),
            "comparability": metric.get("comparability", "unknown"),
            "unit": metric.get("unit", ""), "period": metric.get("period", ""),
        })
    return output


def build_visualization_payload(snapshot: dict, kind: str) -> dict:
    """Create explicit chart payloads; chart code must not choose risk layers itself."""
    kind = str(kind or "").lower()
    if kind in {"heatmap", "risk_heatmap", "风险热力图"}:
        dimensions = ["financial_misstatement", "related_party", "disclosure_compliance", "going_concern", "regulatory_penalty"]
        levels = ["重大", "重要", "一般"]
        payload = {"formal_matrix": [[0] * 3 for _ in dimensions], "pending_matrix": [[0] * 3 for _ in dimensions],
                   "dimensions": dimensions, "levels": levels, "unknown_mapping": [],
                   "legend": {"formal": "正式采信风险", "pending": "待复核提示"},
                   "note": "正式风险与待复核提示分层展示；待复核提示不计入正式评分和正式风险总数。"}
        index = {value: i for i, value in enumerate(dimensions)}
        for layer_name, key in (("formal", "formal_matrix"), ("pending", "pending_matrix")):
            for risk in (snapshot.get("risks", {}).get(layer_name) or []):
                dim = _normalize_dimension(risk.get("dimension"))
                level = _normalize_level(risk.get("level") or risk.get("suggested_level"))
                if dim not in index:
                    payload["unknown_mapping"].append({"risk_id": risk.get("risk_id"), "reason": "维度未纳入五维热力图"})
                    continue
                payload[key][index[dim]][levels.index(level)] += 1
        return payload
    if kind in {"radar", "radar_chart", "财务雷达图"}:
        metrics = _metrics_for_radar(snapshot)
        return {"metrics": metrics, "has_actual": any(m.get("actual") is not None for m in metrics),
                "has_verified_benchmark": any(m.get("benchmark") is not None for m in metrics),
                "note": "无可核验行业基准时，仅展示公司实际值；内部参考值来源不完整，仅供演示。"}
    if kind in {"trend", "trend_chart", "趋势图", "趋势折线图"}:
        multi_year = snapshot.get("multi_year") if isinstance(snapshot.get("multi_year"), dict) else {}
        years = multi_year.get("years") or multi_year.get("years_analyzed") or []
        indicators = multi_year.get("indicators_by_year") or {}
        if not years and isinstance(multi_year.get("data"), list):
            years = [item.get("year") for item in multi_year["data"] if isinstance(item, dict)]
            indicators = {str(item.get("year")): item for item in multi_year["data"] if isinstance(item, dict)}
        rows = []
        for year in years:
            row = indicators.get(year, indicators.get(str(year), {})) if isinstance(indicators, dict) else {}
            row = row if isinstance(row, dict) else {}
            rows.append({"year": year, "revenue": row.get("revenue"), "net_profit": row.get("net_profit"),
                         "operating_cashflow": row.get("operating_cashflow"),
                         "accounts_receivable": row.get("accounts_receivable_end", row.get("accounts_receivable")),
                         "inventory": row.get("inventory_end", row.get("inventory")),
                         "total_assets": row.get("total_assets"), "total_liabilities": row.get("total_liabilities"),
                         "cost_of_goods": row.get("cost_of_goods")})
        return {"company_name": (snapshot.get("company") or {}).get("company_name", "未提供"),
                "amount_unit": (snapshot.get("company") or {}).get("amount_unit", ""),
                "amount_unit_state": multi_year.get("amount_unit_state", "missing"),
                "amount_unit_note": multi_year.get("amount_unit_note", ""), "years": rows}
    return {}


def snapshot_digest(snapshot: dict) -> str:
    """Stable digest used for a cross-process snapshot ID."""
    value = copy.deepcopy(snapshot or {})
    value.pop("snapshot_id", None)
    value.pop("artifact_manifest", None)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_final_snapshot(report: dict, tool_results: dict) -> dict:
    """Build a deep-copied, fully layered snapshot after all review mutations."""
    source_report = copy.deepcopy(report or {})
    normalized_tools = normalize_tool_results(tool_results)
    fin = _tool_payload(normalized_tools, ("calculate_financial_indicators",))
    validation = _tool_payload(normalized_tools, ("validate_financial_data",))
    disclosure = _tool_payload(normalized_tools, ("check_disclosure_compliance",))
    multi_year = _tool_payload(normalized_tools, ("compare_multi_year", "multi_year_comparison"))
    score = source_report.get("comprehensive_score") or source_report.get("comprehensive_score_snapshot") or _tool_payload(normalized_tools, ("calculate_comprehensive_score",))
    company = copy.deepcopy(source_report.get("company_info") or {})
    source = copy.deepcopy(source_report.get("source") or {})
    source_hash = str(source.get("source_hash") or company.get("source_file_sha256") or "")
    source.update({"document_name": source.get("document_name") or company.get("source_document") or company.get("file_name") or "",
                   "source_hash": source_hash, "page_count": source.get("page_count") or company.get("page_count")})
    company.setdefault("report_period", company.get("report_year", ""))
    company.setdefault("accounting_standard", company.get("accounting_standard", ""))
    company.setdefault("scope", source_report.get("scope") or fin.get("scope", ""))
    company.setdefault("amount_unit", source_report.get("amount_unit") or fin.get("amount_unit", ""))

    # Tool results are the authoritative calculation layer.  Put them first so
    # an older LLM compatibility field cannot overwrite a newly calculated
    # value while still allowing the compatibility record to enrich missing
    # labels or source metadata.
    facts = _merge_records(_records(fin.get("facts")), _records(source_report.get("facts")),
                           _records(validation.get("facts")), id_key="fact_id")
    metrics = _merge_records(_records(fin.get("metric_results")),
                             _records(source_report.get("metrics") or source_report.get("metric_results")),
                             _records(validation.get("metric_results")), id_key="metric_id")
    evidence = _merge_records(_records(fin.get("evidence")), _records(source_report.get("evidence")),
                              _records(validation.get("evidence")), _records(disclosure.get("evidence")), id_key="evidence_id")
    facts = _correct_known_current_flow_periods(_source_defaults(facts, source), company)
    metrics = _source_defaults(metrics, source)
    evidence = _source_defaults(evidence, source)
    facts = [_normalize_value_record(item, kind="fact") for item in facts]
    metrics = [_normalize_value_record(item, kind="metric") for item in metrics]
    multi_year_snapshot = copy.deepcopy(source_report.get("multi_year") or multi_year)
    if not multi_year_snapshot:
        multi_year_snapshot = _derive_comparable_periods(facts, company)
    score = copy.deepcopy(score) if isinstance(score, dict) else {}
    for key in ("facts", "metric_results", "evidence"):
        if isinstance(score.get(key), list):
            score[key] = _source_defaults(_records(score[key]), source)
    normalized_report = source_report
    normalized_report["facts"] = facts
    normalized_report["evidence"] = evidence
    normalized_report["risk_details"] = _dedupe_risks([_records(source_report.get("risk_details")), _records(source_report.get("accepted_risk_details")), _records(source_report.get("pending_items"))])
    normalized_report = bind_evidence_to_risks(normalized_report)
    layers = build_risk_layers(normalized_report)
    normalized_report["risk_details"] = layers["formal"] + layers["pending"]
    # Keep the full pending layer in the final snapshot and expose one stable
    # public shape so web, PDF, Excel and machine-readable exports cannot drift.
    normalized_report["pending_items"] = layers["pending"]
    normalized_report["accepted_risk_details"] = layers["formal"]
    risk_summary = build_risk_summary(layers)
    risk_models = source_report.get("risk_models") or _tool_payload(normalized_tools, ("calculate_risk_models",))
    analysis_id = str(source_report.get("analysis_id") or company.get("run_id") or "").strip()
    if not analysis_id:
        analysis_id = uuid.uuid4().hex
        company["run_id"] = analysis_id
    rendering = {
        key: copy.deepcopy(source_report[key])
        for key in (
            "overall_assessment", "review_conclusion", "system_conclusion_md",
            "risk_index_md", "score_review_note", "disclosure_consistency_note",
            "level_floor_note", "cashflow_penetration_note",
            "pending_verification_note", "c1_review", "semantic_review",
            "arbiter_verdict", "arbiter_incomplete", "report_invalidated",
            "analysis_module",
        )
        if key in source_report
    }
    financial_payload = (
        copy.deepcopy(fin)
        if isinstance(fin, dict) and fin and "error" not in fin
        else copy.deepcopy(source_report.get("financial") or {})
    )
    if isinstance(source_report.get("financial"), dict):
        # Preserve non-standard compatibility fields, but keep tool-produced
        # facts/metrics/evidence as the single source for exported indicators.
        for key, value in source_report["financial"].items():
            financial_payload.setdefault(key, copy.deepcopy(value))
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        # 顶层保留源文件哈希，便于 API、历史和产物门禁直接读取；source
        # 仍保留完整来源描述，两个字段由同一变量写入，避免口径漂移。
        "source_hash": source_hash,
        "data_version": str(source_report.get("data_version") or DATA_VERSION),
        "rule_version": str(source_report.get("rule_version") or RULE_VERSION),
        "source": source,
        "company": company,
        "facts": facts,
        "metrics": metrics,
        "evidence": evidence,
        "risks": layers,
        "risk_summary": risk_summary,
        "validation": source_report.get("validation") or source_report.get("data_validation") or validation,
        "disclosure": source_report.get("disclosure") or disclosure,
        "multi_year": multi_year_snapshot,
        # 保留财务工具的原始结构，供三份 PDF 和 Excel 从同一快照读取
        # indicators / statement_items / alerts 等非风险章节数据。
        "financial": financial_payload,
        "risk_models": risk_models,
        "score": score,
        "data_quality": {
            "verified_facts": sum(1 for item in facts if item.get("source_status") == SOURCE_STATUS_VERIFIED),
            "incomplete_facts": sum(1 for item in facts if item.get("source_status") != SOURCE_STATUS_VERIFIED),
            "verified_metrics": sum(1 for item in metrics if item.get("source_status") == SOURCE_STATUS_VERIFIED),
            "incomplete_metrics": sum(1 for item in metrics if item.get("source_status") != SOURCE_STATUS_VERIFIED),
            "pending_risks": len(layers["pending"]),
            "excluded_items": len(layers["excluded"]),
            "presentation_mode": str(source_report.get("presentation_mode") or "strict"),
        },
        "industry_benchmark": copy.deepcopy(source_report.get("industry_benchmark") or {}),
        "artifact_expectations": copy.deepcopy(source_report.get("artifact_expectations") or []),
        "artifact_manifest": copy.deepcopy(source_report.get("artifact_manifest") or []),
        "review_gate": copy.deepcopy(source_report.get("review_gate") or {}),
        # 最终展示字段也由快照携带，避免从 AI 正文或临时台账重新推断。
        "rendering": rendering,
    }
    snapshot["snapshot_id"] = "snap-" + snapshot_digest(snapshot)[:24]
    return copy.deepcopy(snapshot)


def snapshot_as_legacy_payload(snapshot: dict, base: dict | None = None) -> dict:
    """Expose one snapshot through the historical exporter/tool field names."""
    snap = copy.deepcopy(snapshot or {})
    result = copy.deepcopy(base or {})
    layers = snap.get("risks") if isinstance(snap.get("risks"), dict) else {}
    formal = _records(layers.get("formal"))
    pending = _records(layers.get("pending"))
    excluded = _records(layers.get("excluded"))
    company = copy.deepcopy(snap.get("company") or {})
    metrics = _records(snap.get("metrics"))
    rendering = snap.get("rendering") if isinstance(snap.get("rendering"), dict) else {}
    result.update({
        "report_snapshot": snap,
        "snapshot_id": snap.get("snapshot_id", ""),
        "schema_version": snap.get("schema_version", SNAPSHOT_SCHEMA_VERSION),
        "analysis_id": snap.get("analysis_id", ""),
        "data_version": snap.get("data_version", DATA_VERSION),
        "rule_version": snap.get("rule_version", RULE_VERSION),
        "company_info": company,
        "facts": _records(snap.get("facts")),
        "evidence": _records(snap.get("evidence")),
        "metric_results": metrics,
        "metrics": metrics,
        "risk_details": formal + pending,
        "accepted_risk_details": formal,
        "pending_items": pending,
        "excluded_items": excluded,
        "risk_summary": copy.deepcopy(snap.get("risk_summary") or {}),
        "data_validation": copy.deepcopy(snap.get("validation") or {}),
        "validation": copy.deepcopy(snap.get("validation") or {}),
        "disclosure": copy.deepcopy(snap.get("disclosure") or {}),
        "multi_year": copy.deepcopy(snap.get("multi_year") or {}),
        "financial": copy.deepcopy(snap.get("financial") or {}),
        "financial_indicators": copy.deepcopy(snap.get("financial") or {}),
        "industry_benchmark": copy.deepcopy(snap.get("industry_benchmark") or {}),
        "comprehensive_score": copy.deepcopy(snap.get("score") or {}),
        "comprehensive_score_snapshot": copy.deepcopy(snap.get("score") or {}),
        "artifact_expectations": copy.deepcopy(snap.get("artifact_expectations") or []),
        "artifact_manifest": copy.deepcopy(snap.get("artifact_manifest") or []),
        "review_gate": copy.deepcopy(snap.get("review_gate") or {"status": "not_run"}),
        "task_status": derive_task_status(snap.get("artifact_manifest") or []),
        "calculated_indicators": {
            str(item.get("metric_id")): item.get("value")
            for item in metrics if item.get("metric_id") and item.get("value") is not None
        },
    })
    # 兼容导出器/网页仍读取的最终展示字段，值来自快照而非 LLM 正文。
    result.update(copy.deepcopy(rendering))
    result["presentation_mode"] = str(
        (snap.get("data_quality") or {}).get("presentation_mode") or "strict")
    quality = snap.get("data_quality") if isinstance(snap.get("data_quality"), dict) else {}
    if result["presentation_mode"] in {"demo", "demo_placeholder"}:
        result["data_status"] = SOURCE_STATUS_DEMO_PLACEHOLDER
    elif (quality.get("pending_risks") or quality.get("incomplete_facts")
          or quality.get("incomplete_metrics")):
        result["data_status"] = SOURCE_STATUS_INCOMPLETE
    else:
        result["data_status"] = SOURCE_STATUS_VERIFIED
    result["visualization_payloads"] = {
        "heatmap": build_visualization_payload(snap, "heatmap"),
        "radar": build_visualization_payload(snap, "radar"),
        "trend": build_visualization_payload(snap, "trend"),
    }
    return result
