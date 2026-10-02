"""多年财务数据对比分析工具

支持对多个年度的财务数据进行跨年对比分析，识别趋势性风险。

核心功能：
1. 从多年数据中计算 15 项关键财务指标（毛利率、资产负债率、流动比率等）
2. 计算相邻年度间的同比变动百分比
3. 基于多年趋势判定每项指标的方向（持续上升/持续下降/波动）
4. 针对 6 类高风险趋势自动生成预警（毛利率连降、应收连升、现金流连负等）

输出包含各年度指标明细、同比变动、趋势判定和趋势风险预警。
"""
import json
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

COMPARISON_VERSION = "2026-09-v4"


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip().replace(",", "")
        return Decimal(text) if text else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _number(value, places=None):
    if value is None:
        return None
    if places is not None:
        quant = Decimal("1") / (Decimal("10") ** places)
        value = value.quantize(quant, rounding=ROUND_HALF_UP)
    return float(value)


def _read(data: dict, *keys):
    for key in keys:
        value = _decimal(data.get(key))
        if value is not None:
            return value, key
    return None, ""


def _text(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _year_sort_key(year):
    text = str(year)
    match = re.match(r"^(\d{4})(?:H([12])|Q([1-4]))?$", text)
    if not match:
        return (1, text)
    suffix = int(match.group(2) or match.group(3) or 0)
    return (0, int(match.group(1)), suffix)


def _adjacent_period(previous, current):
    """仅对明确相邻的年度允许用上期末余额作为本期期初余额。"""
    prev = str(previous)
    curr = str(current)
    prev_match = re.match(r"^(\d{4})$", prev)
    curr_match = re.match(r"^(\d{4})$", curr)
    return bool(prev_match and curr_match and int(curr_match.group(1)) == int(prev_match.group(1)) + 1)


def _balance_average(year_data, previous_data, end_keys, begin_keys, year, previous_year):
    end, end_key = _read(year_data, *end_keys)
    begin, begin_key = _read(year_data, *begin_keys)
    source = ""
    if begin is not None:
        source = f"{begin_key}+{end_key}"
    elif previous_data and _adjacent_period(previous_year, year):
        begin, previous_key = _read(previous_data, *end_keys)
        if begin is not None:
            begin_key = f"{previous_year}.{previous_key}"
            source = f"{begin_key}+{end_key}"
    if begin is None or end is None:
        return None, "缺少匹配期间的期初/上期末与期末余额"
    average = (begin + end) / Decimal("2")
    if average <= 0:
        return None, "期初期末平均余额必须为正数"
    return average, source


def _id_part(value):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value))


def _legacy_compare_multi_year_impl(financial_data_json: str) -> str:
    """对多个年度的财务数据进行跨年对比分析的核心实现。

    处理流程：
    1. 解析输入 JSON，兼容两种输入格式（years 数组 / 年度字典）
    2. 按年度排序后，逐一计算每年度 15 项财务指标
    3. 计算相邻年度间的同比变动百分比
    4. 对每项指标进行多年趋势判定（3年+使用持续上升/下降/波动，2年使用同比上升/下降/持平）
    5. 针对 6 类高风险趋势自动生成预警文本

    Args:
        financial_data_json: 多年财务数据 JSON 字符串或字典，支持两种格式：
            格式1（推荐）: {"years": [{"year": "2022", "revenue": 50000, ...}, ...]}
            格式2: {"2022": {"revenue": 50000, ...}, "2023": {...}, ...}

    Returns:
        JSON 字符串，包含：
        - years_analyzed: 分析的年度列表
        - indicators_by_year: 各年度 15 项指标明细
        - yoy_changes: 相邻年度同比变动百分比
        - trends: 各项指标的趋势判定
        - trend_alerts: 趋势性风险预警列表
        - alert_count: 预警总数
    """
    return _compare_multi_year_impl(financial_data_json)



def _compare_multi_year_impl(financial_data_json: str) -> str:
    """对多个年度的财务数据进行严格口径的跨年对比分析。"""
    try:
        data = json.loads(financial_data_json) if isinstance(financial_data_json, str) else financial_data_json
    except (TypeError, json.JSONDecodeError) as exc:
        return f"JSON解析失败: {exc}"
    if not isinstance(data, dict):
        return "输入数据必须是 JSON 对象。"

    years_data = []
    if isinstance(data.get("years"), list):
        for values in data["years"]:
            if isinstance(values, dict) and values.get("year") not in (None, ""):
                years_data.append(dict(values))
    else:
        for year, values in data.items():
            if not str(year).startswith("_") and isinstance(values, dict):
                years_data.append({**values, "year": year})
    years_data.sort(key=lambda item: _year_sort_key(item.get("year", "")))
    if len(years_data) < 2:
        return "至少需要提供2个年度的财务数据才能进行多年对比分析。"

    metric_names = {
        "revenue": "营业收入",
        "net_profit": "净利润",
        "operating_cashflow": "经营活动现金流量净额",
        "gross_margin": "毛利率(%)",
        "ar_to_revenue_ratio": "应收账款占营收比(%)",
        "accounts_receivable_turnover": "应收账款周转率",
        "accounts_receivable_turnover_days": "应收账款周转天数",
        "inventory_turnover": "存货周转率",
        "inventory_turnover_days": "存货周转天数",
        "fixed_asset_turnover": "固定资产周转率",
        "debt_to_asset_ratio": "资产负债率(%)",
        "current_ratio": "流动比率",
        "quick_ratio": "速动比率",
        "cashflow_to_profit_ratio": "经营现金流/净利润比",
        "goodwill_to_net_assets": "商誉占净资产比(%)",
    }
    metric_units = {
        "gross_margin": "%", "ar_to_revenue_ratio": "%",
        "accounts_receivable_turnover": "次", "accounts_receivable_turnover_days": "天",
        "inventory_turnover": "次", "inventory_turnover_days": "天",
        "fixed_asset_turnover": "次", "debt_to_asset_ratio": "%",
        "current_ratio": "倍", "quick_ratio": "倍",
        "cashflow_to_profit_ratio": "倍", "goodwill_to_net_assets": "%",
    }
    raw_specs = {
        "revenue": ("revenue", "revenue_current"),
        "net_profit": ("net_profit", "net_profit_current"),
        "operating_cashflow": ("operating_cashflow", "operating_cashflow_current", "operating_cash_flow"),
        "total_assets": ("total_assets", "total_assets_current"),
        "total_liabilities": ("total_liabilities", "total_liabilities_current"),
        "accounts_receivable_end": ("accounts_receivable", "accounts_receivable_current"),
        "inventory_end": ("inventory", "inventory_current"),
        "cost_of_goods": ("cost_of_goods", "cost_of_sales"),
        "current_assets": ("current_assets", "current_assets_current"),
        "current_liabilities": ("current_liabilities", "current_liabilities_current"),
        "net_assets": ("net_assets", "net_assets_current", "owners_equity"),
        "goodwill": ("goodwill", "goodwill_current"),
        "fixed_assets_end": ("fixed_assets", "fixed_assets_current"),
        "credit_sales": ("credit_sales",),
        # 期间天数不是金额，单独登记以便周转天数的计算依据可追溯。
        "period_days": ("period_days",),
    }
    begin_specs = {
        "accounts_receivable_begin": ("accounts_receivable_begin", "accounts_receivable_previous"),
        "inventory_begin": ("inventory_begin", "inventory_previous"),
        "fixed_assets_begin": ("fixed_assets_begin", "fixed_assets_previous"),
    }

    indicators_by_year = {}
    indicator_values_by_year = {}
    metric_status_by_year = {}
    facts = []
    fact_records = {}
    fact_index = {}
    metric_results = []
    evidence = []
    observed_amount_units = []
    amount_unit_by_year = {}

    def metadata_text(row, *keys):
        meta = row.get("_metadata") or row.get("metadata") or {}
        return _text(row, *keys) or (_text(meta, *keys) if isinstance(meta, dict) else "")

    for index, row in enumerate(years_data):
        year = str(row.get("year"))
        year_id = _id_part(year)
        previous_row = years_data[index - 1] if index else None
        previous_year = str(previous_row.get("year")) if previous_row else ""
        period = metadata_text(row, "period", "report_period") or year
        amount_unit = metadata_text(row, "amount_unit", "unit") or _text(data, "amount_unit", "unit")
        amount_unit_by_year[year] = amount_unit or None
        if amount_unit:
            observed_amount_units.append(amount_unit)
        currency = metadata_text(row, "currency") or _text(data, "currency") or "人民币"
        scope = metadata_text(row, "scope") or _text(data, "scope")
        source_document = metadata_text(row, "source_document") or _text(data, "source_document")
        source_hash = metadata_text(row, "source_hash") or _text(data, "source_hash")
        page = metadata_text(row, "page", "page_number") or _text(data, "page", "page_number")
        locator = metadata_text(row, "locator", "table_locator") or _text(data, "locator", "table_locator")
        excerpt = metadata_text(row, "excerpt") or _text(data, "excerpt")
        extraction_method = metadata_text(row, "extraction_method") or "structured_input"

        values = {}
        raw_sources = {}
        for field, keys in raw_specs.items():
            value, source_key = _read(row, *keys)
            values[field] = value
            if source_key:
                raw_sources[field] = (source_key, row.get(source_key))
        for field, keys in begin_specs.items():
            value, source_key = _read(row, *keys)
            values[field] = value
            if source_key:
                raw_sources[field] = (source_key, row.get(source_key))

        # 只有明确相邻年度才允许借用上一年度期末作为本期期初。
        for field, end_field in (
            ("accounts_receivable_begin", "accounts_receivable_end"),
            ("inventory_begin", "inventory_end"),
            ("fixed_assets_begin", "fixed_assets_end"),
        ):
            if values[field] is None and previous_row and _adjacent_period(previous_year, year):
                previous_value, _ = _read(previous_row, *raw_specs[end_field])
                if previous_value is not None:
                    values[field] = previous_value
                    previous_fact_id = fact_index.get((previous_year, end_field))
                    if previous_fact_id:
                        fact_index[(year, field)] = previous_fact_id

        for field, value in values.items():
            if value is None or (year, field) in fact_index:
                continue
            source_key, raw_value = raw_sources.get(field, (field, value))
            fact_id = f"F-MY-{year_id}-{field}"
            fact_index[(year, field)] = fact_id
            fact_unit = "天" if field == "period_days" else amount_unit
            fact_currency = "" if field == "period_days" else currency
            fact_period = previous_year if (
                field in {"accounts_receivable_begin", "inventory_begin", "fixed_assets_begin"}
                and str(source_key).endswith("_previous")
                and previous_year
            ) else period
            fact = make_fact(
                field, raw_value, fact_id=fact_id,
                unit=fact_unit, currency=fact_currency, period=fact_period, scope=scope,
                source_document=source_document, source_hash=source_hash,
                page=page, locator=locator, excerpt=excerpt,
                extraction_method=extraction_method,
            ).to_dict()
            facts.append(fact)
            fact_records[fact_id] = fact

        metric_values = {}
        metric_status = {}
        entries = []

        def add_metric(key, value, formula, *, status="calculated", reason="", fields=()):
            metric_values[key] = value
            metric_status[key] = {"status": status, "reason": reason}
            entries.append((key, formula, value, status, reason, tuple(fields)))

        revenue = values["revenue"]
        net_profit = values["net_profit"]
        operating_cashflow = values["operating_cashflow"]
        total_assets = values["total_assets"]
        total_liabilities = values["total_liabilities"]
        ar_end = values["accounts_receivable_end"]
        inventory_end = values["inventory_end"]
        cost_of_goods = values["cost_of_goods"]
        current_assets = values["current_assets"]
        current_liabilities = values["current_liabilities"]
        net_assets = values["net_assets"]
        goodwill = values["goodwill"]

        add_metric("revenue", revenue, "原始披露值", status="calculated" if revenue is not None else "insufficient_data", reason="" if revenue is not None else "缺少营业收入", fields=("revenue",))
        add_metric("net_profit", net_profit, "原始披露值", status="calculated" if net_profit is not None else "insufficient_data", reason="" if net_profit is not None else "缺少净利润", fields=("net_profit",))
        add_metric("operating_cashflow", operating_cashflow, "原始披露值", status="calculated" if operating_cashflow is not None else "insufficient_data", reason="" if operating_cashflow is not None else "缺少经营活动现金流量净额", fields=("operating_cashflow",))

        if revenue is None or cost_of_goods is None:
            add_metric("gross_margin", None, "(营业收入-营业成本)/营业收入×100%", status="insufficient_data", reason="缺少营业收入或营业成本", fields=("revenue", "cost_of_goods"))
        elif revenue <= 0:
            add_metric("gross_margin", None, "(营业收入-营业成本)/营业收入×100%", status="not_comparable", reason="营业收入必须为正数", fields=("revenue", "cost_of_goods"))
        else:
            add_metric("gross_margin", (revenue - cost_of_goods) / revenue * 100, "(营业收入-营业成本)/营业收入×100%", fields=("revenue", "cost_of_goods"))

        if revenue is None or ar_end is None:
            add_metric("ar_to_revenue_ratio", None, "应收账款期末余额/营业收入×100%", status="insufficient_data", reason="缺少应收账款期末余额或营业收入", fields=("accounts_receivable_end", "revenue"))
        elif revenue <= 0:
            add_metric("ar_to_revenue_ratio", None, "应收账款期末余额/营业收入×100%", status="not_comparable", reason="营业收入必须为正数", fields=("accounts_receivable_end", "revenue"))
        else:
            add_metric("ar_to_revenue_ratio", ar_end / revenue * 100, "应收账款期末余额/营业收入×100%", fields=("accounts_receivable_end", "revenue"))

        def average_balance(end_field, begin_field):
            end = values[end_field]
            begin = values[begin_field]
            if end is None or begin is None:
                return None, "缺少匹配期间的期初/上期末与期末余额"
            average = (begin + end) / Decimal("2")
            return (average, "") if average > 0 else (None, "期初期末平均余额必须为正数")

        ar_avg, ar_reason = average_balance("accounts_receivable_end", "accounts_receivable_begin")
        inv_avg, inv_reason = average_balance("inventory_end", "inventory_begin")
        fixed_avg, fixed_reason = average_balance("fixed_assets_end", "fixed_assets_begin")
        credit_sales = values["credit_sales"] if values["credit_sales"] is not None else revenue
        sales_reason = "使用营业收入作为赊销收入近似" if values["credit_sales"] is None else "使用披露的赊销收入"
        if ar_avg is None or credit_sales is None or credit_sales <= 0:
            add_metric("accounts_receivable_turnover", None, "赊销收入/应收账款平均余额", status="insufficient_data", reason=ar_reason if ar_avg is None else "缺少有效赊销收入", fields=("accounts_receivable_begin", "accounts_receivable_end", "credit_sales", "revenue"))
        else:
            add_metric("accounts_receivable_turnover", credit_sales / ar_avg, "赊销收入/应收账款平均余额", reason=sales_reason, fields=("accounts_receivable_begin", "accounts_receivable_end", "credit_sales", "revenue"))

        period_days = values.get("period_days")
        period_days_fact_id = fact_index.get((year, "period_days"), "")
        if period_days is None and re.fullmatch(r"\d{4}", year):
            period_days = Decimal("365")
            period_days_reason = "年度默认365天（规则默认，非原始披露值）"
        elif period_days is not None and period_days <= 0:
            period_days = None
            period_days_reason = "期间天数必须为正数"
        elif period_days is None:
            period_days_reason = "非完整年度且未提供期间天数"
        else:
            period_days_reason = "使用输入的期间天数"
        ar_turnover = metric_values.get("accounts_receivable_turnover")
        if period_days is None or ar_turnover is None or ar_turnover <= 0:
            add_metric("accounts_receivable_turnover_days", None, "期间天数/应收账款周转率", status="insufficient_data", reason=period_days_reason if period_days is None else "周转率必须为正数", fields=("period_days", "accounts_receivable_begin", "accounts_receivable_end", "credit_sales", "revenue"))
        else:
            add_metric("accounts_receivable_turnover_days", period_days / ar_turnover, "期间天数/应收账款周转率", reason=period_days_reason, fields=("period_days", "accounts_receivable_begin", "accounts_receivable_end", "credit_sales", "revenue"))

        if inv_avg is None or cost_of_goods is None or cost_of_goods < 0:
            add_metric("inventory_turnover", None, "营业成本/存货平均余额", status="insufficient_data", reason=inv_reason if inv_avg is None else "缺少有效营业成本", fields=("inventory_begin", "inventory_end", "cost_of_goods"))
        else:
            add_metric("inventory_turnover", cost_of_goods / inv_avg, "营业成本/存货平均余额", fields=("inventory_begin", "inventory_end", "cost_of_goods"))
        inv_turnover = metric_values.get("inventory_turnover")
        if period_days is None or inv_turnover is None or inv_turnover <= 0:
            add_metric("inventory_turnover_days", None, "期间天数/存货周转率", status="insufficient_data", reason=period_days_reason if period_days is None else "周转率必须为正数", fields=("period_days", "inventory_begin", "inventory_end", "cost_of_goods"))
        else:
            add_metric("inventory_turnover_days", period_days / inv_turnover, "期间天数/存货周转率", reason=period_days_reason, fields=("period_days", "inventory_begin", "inventory_end", "cost_of_goods"))

        if fixed_avg is None or revenue is None or revenue <= 0:
            add_metric("fixed_asset_turnover", None, "营业收入/固定资产平均余额", status="insufficient_data", reason=fixed_reason if fixed_avg is None else "缺少有效营业收入", fields=("fixed_assets_begin", "fixed_assets_end", "revenue"))
        else:
            add_metric("fixed_asset_turnover", revenue / fixed_avg, "营业收入/固定资产平均余额", fields=("fixed_assets_begin", "fixed_assets_end", "revenue"))

        if total_assets is None or total_assets <= 0 or total_liabilities is None or total_liabilities < 0:
            add_metric("debt_to_asset_ratio", None, "总负债/总资产×100%", status="insufficient_data", reason="缺少有效总资产或总负债", fields=("total_assets", "total_liabilities"))
        else:
            add_metric("debt_to_asset_ratio", total_liabilities / total_assets * 100, "总负债/总资产×100%", fields=("total_assets", "total_liabilities"))

        if current_assets is None or current_liabilities is None or current_liabilities <= 0:
            add_metric("current_ratio", None, "流动资产/流动负债", status="insufficient_data", reason="缺少流动资产/流动负债或流动负债非正", fields=("current_assets", "current_liabilities"))
        else:
            add_metric("current_ratio", current_assets / current_liabilities, "流动资产/流动负债", fields=("current_assets", "current_liabilities"))
        if current_assets is None or inventory_end is None or current_liabilities is None or current_liabilities <= 0:
            add_metric("quick_ratio", None, "(流动资产-存货)/流动负债", status="insufficient_data", reason="缺少流动资产、存货或流动负债非正", fields=("current_assets", "inventory_end", "current_liabilities"))
        else:
            add_metric("quick_ratio", (current_assets - inventory_end) / current_liabilities, "(流动资产-存货)/流动负债", fields=("current_assets", "inventory_end", "current_liabilities"))

        if net_profit is None or operating_cashflow is None or net_profit == 0:
            add_metric("cashflow_to_profit_ratio", None, "经营活动现金流量净额/净利润", status="not_comparable", reason="缺少净利润/经营现金流或净利润为零", fields=("operating_cashflow", "net_profit"))
        else:
            add_metric("cashflow_to_profit_ratio", operating_cashflow / net_profit, "经营活动现金流量净额/净利润", fields=("operating_cashflow", "net_profit"))

        if goodwill is None or net_assets is None or net_assets <= 0:
            add_metric("goodwill_to_net_assets", None, "商誉/净资产×100%", status="insufficient_data", reason="缺少商誉/净资产或净资产非正；不反算净资产", fields=("goodwill", "net_assets"))
        else:
            add_metric("goodwill_to_net_assets", goodwill / net_assets * 100, "商誉/净资产×100%", fields=("goodwill", "net_assets"))

        indicator_values_by_year[year] = metric_values
        indicators_by_year[year] = {key: _number(value, 2) for key, value in metric_values.items()}
        # 同时保留趋势图所需的原始余额/成本字段；这些不是衍生指标，不参与趋势预警。
        indicators_by_year[year].update({
            "accounts_receivable_end": _number(ar_end, 2),
            "inventory_end": _number(inventory_end, 2),
            "fixed_assets_end": _number(values["fixed_assets_end"], 2),
            "total_assets": _number(total_assets, 2),
            "total_liabilities": _number(total_liabilities, 2),
            "cost_of_goods": _number(cost_of_goods, 2),
        })
        metric_status_by_year[year] = metric_status

        for key, formula, value, status, reason, fields in entries:
            metric_id = f"MY-{year_id}-{key}"
            refs = []
            for field in fields:
                fact_id = fact_index.get((year, field))
                if fact_id:
                    fact = fact_records.get(fact_id, {})
                    refs.append({
                        "field": field, "fact_id": fact_id, "raw_value": fact.get("raw_value", ""),
                        "value": fact.get("value"), "unit": fact.get("unit", ""),
                        "period": fact.get("period", period), "scope": fact.get("scope", scope),
                    })
                    continue
                # 年度指标允许使用明确规则默认的 365 天，但不能伪装成原始事实。
                if field == "period_days" and period_days is not None:
                    refs.append({
                        "field": field, "fact_id": "", "raw_value": str(_number(period_days)),
                        "value": _number(period_days), "unit": "天", "period": period,
                        "scope": scope, "source": "rule_default" if not period_days_fact_id else "input",
                    })
            evidence_id = f"E-{metric_id}"
            metric_results.append(MetricResult(
                metric_id=metric_id, name=metric_names[key], formula=formula, inputs=refs,
                period=period, scope=scope, unit=metric_units.get(key, amount_unit),
                value=_number(value), display_value="未获取" if value is None else str(_number(value, 2)),
                status=status, reason=reason, evidence_ids=[evidence_id],
            ).to_dict())
            evidence.append(Evidence(
                evidence_id=evidence_id, source_type="multi_year_input",
                source_document=source_document, source_hash=source_hash, page=page,
                locator=locator, excerpt=excerpt,
                fact_ids=[ref["fact_id"] for ref in refs], metric_ids=[metric_id],
                verified=bool(refs) and value is not None,
                status="verified" if bool(refs) and value is not None else "insufficient_data",
            ).to_dict())

    years = list(indicators_by_year)
    yoy_changes = {}
    for index in range(1, len(years)):
        previous_year, current_year = years[index - 1], years[index]
        period_key = f"{previous_year}-{current_year}"
        yoy_changes[period_key] = {}
        for metric in metric_names:
            previous_value = indicator_values_by_year[previous_year].get(metric)
            current_value = indicator_values_by_year[current_year].get(metric)
            change_key = f"{metric}_change_pct"
            status_key = f"{metric}_change_status"
            reason_key = f"{metric}_change_reason"
            if previous_value is None or current_value is None:
                yoy_changes[period_key][change_key] = None
                yoy_changes[period_key][status_key] = "insufficient_data"
                yoy_changes[period_key][reason_key] = "本期或上期指标缺失/不可计算"
            elif previous_value <= 0:
                yoy_changes[period_key][change_key] = None
                yoy_changes[period_key][status_key] = "not_comparable"
                yoy_changes[period_key][reason_key] = "上期为零或负数，不输出伪同比百分比"
            else:
                yoy_changes[period_key][change_key] = _number((current_value - previous_value) / previous_value * 100, 2)
                yoy_changes[period_key][status_key] = "calculated"
                yoy_changes[period_key][reason_key] = "以上期正数为基期计算"

    trends = {}
    trend_alerts = []
    for metric, name in metric_names.items():
        values = [indicator_values_by_year[year].get(metric) for year in years]
        trend_info = {"values": {year: _number(value, 2) for year, value in zip(years, values)}}
        if not any(value is not None for value in values):
            trend_info.update({"trend": "未披露", "status": "insufficient_data"})
        elif any(value is None for value in values):
            trend_info.update({"trend": "数据不完整", "status": "insufficient_data", "reason": "缺失年度不跨年拼接趋势"})
        elif len(values) == 2:
            trend = "同比上升" if values[1] > values[0] else "同比下降" if values[1] < values[0] else "持平"
            trend_info.update({"trend": trend, "status": "calculated"})
        else:
            increases = sum(values[i] > values[i - 1] for i in range(1, len(values)))
            decreases = sum(values[i] < values[i - 1] for i in range(1, len(values)))
            trend = "持续下降" if decreases == len(values) - 1 else "持续上升" if increases == len(values) - 1 else "波动"
            trend_info.update({"trend": trend, "status": "calculated"})
        trends[name] = trend_info

        if any(value is None for value in values) or len(values) < 2:
            continue
        if metric == "gross_margin" and all(values[i] < values[i - 1] for i in range(1, len(values))):
            trend_alerts.append(f"【趋势风险】毛利率连续{len(values) - 1}年下滑，需关注盈利能力持续恶化")
        elif metric == "ar_to_revenue_ratio" and all(values[i] > values[i - 1] for i in range(1, len(values))) and values[-1] > 20:
            trend_alerts.append(f"【趋势风险】应收账款占营收比连续{len(values) - 1}年上升且超过20%，回款风险加剧")
        elif metric == "operating_cashflow" and all(value < 0 for value in values[-2:]):
            trend_alerts.append("【趋势风险】经营活动现金流量净额连续2年为负，盈利质量严重存疑")
        elif metric == "current_ratio" and all(value < 1 for value in values) and all(values[i] < values[i - 1] for i in range(1, len(values))):
            trend_alerts.append("【趋势风险】流动比率持续低于1且持续恶化，短期偿债能力严重不足")
        elif metric == "debt_to_asset_ratio" and all(values[i] > values[i - 1] for i in range(1, len(values))) and values[-1] > 70:
            trend_alerts.append("【趋势风险】资产负债率连续上升且超过70%，财务杠杆持续加大")
        elif metric == "inventory_turnover" and all(values[i] < values[i - 1] for i in range(1, len(values))):
            trend_alerts.append(f"【趋势风险】存货周转率连续{len(values) - 1}年下降，存货积压风险加大")

    timeseries = {"xAxis": years, "series": []}
    for metric_key, name in metric_names.items():
        series_data = [indicators_by_year.get(year, {}).get(metric_key) for year in years]
        if any(value is not None for value in series_data):
            timeseries["series"].append({
                "name": name, "data": series_data, "unit": metric_units.get(metric_key, "")
            })

    has_incomplete = any(
        item.get("status") != "calculated"
        for year_status in metric_status_by_year.values()
        for item in year_status.values()
    )
    distinct_units = sorted(set(observed_amount_units))
    missing_unit_years = [
        year for year, unit in amount_unit_by_year.items() if not unit
    ]
    if not distinct_units:
        amount_unit_state = "missing"
        amount_unit_note = "所有年度均缺少明确金额单位，跨年度金额趋势只能按原始数值展示，不能确认可比性"
    elif len(distinct_units) > 1:
        amount_unit_state = "conflicting"
        amount_unit_note = (
            "不同年度存在多个金额单位（"
            + "、".join(distinct_units)
            + "），金额类跨年度比较已标记为单位冲突，需先统一口径"
        )
    elif missing_unit_years:
        amount_unit_state = "incomplete"
        amount_unit_note = (
            f"金额单位仅在部分年度明确，缺失年度：{'、'.join(missing_unit_years)}；"
            "金额类跨年度比较需人工复核可比性"
        )
    else:
        amount_unit_state = "verified"
        amount_unit_note = "所有年度金额单位一致，金额类跨年度比较具备单位口径"

    result = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "calculation_version": COMPARISON_VERSION,
        "rule_version": RULE_VERSION,
        "status": "partially_calculated" if has_incomplete else "calculated",
        "years_analyzed": years,
        "year_count": len(years),
        "amount_unit": (observed_amount_units[0] if observed_amount_units and
                         len(set(observed_amount_units)) == 1 else ""),
        "amount_unit_status": (
            "verified" if observed_amount_units and len(set(observed_amount_units)) == 1
            else "missing_or_conflicting"
        ),
        # amount_unit_status 保留兼容旧调用；以下字段给网页、报告和 Excel 提供
        # 可区分“缺失、部分缺失、冲突、已核验”的明确状态。
        "amount_unit_state": amount_unit_state,
        "amount_unit_by_year": amount_unit_by_year,
        "amount_unit_conflicts": distinct_units if len(distinct_units) > 1 else [],
        "amount_unit_missing_years": missing_unit_years,
        "amount_unit_note": amount_unit_note,
        "indicators_by_year": indicators_by_year,
        "metric_status_by_year": metric_status_by_year,
        "yoy_changes": yoy_changes,
        "trends": trends,
        "timeseries": timeseries,
        "trend_alerts": trend_alerts,
        "alert_count": len(trend_alerts),
        "facts": facts,
        "metric_results": metric_results,
        "evidence": evidence,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool
def compare_multi_year(multi_year_data_json: str) -> str:
    """多年财务数据跨年对比分析，识别趋势性风险。

    对多个年度的财务数据计算 15 项关键指标，分析同比变动和多年趋势方向，
    并针对 6 类高风险趋势模式（毛利率连降、应收连升、现金流连负、
    流动比率持续恶化、负债率连升超 70%、存货周转连降）自动生成预警。

    Args:
        multi_year_data_json: 多年财务数据 JSON 字符串，支持两种格式：
            格式1: {"years": [{"year": "2022", "revenue": 50000, ...}, ...]}
            格式2: {"2022": {"revenue": 50000, ...}, "2023": {...}, ...}

    Returns:
        JSON 字符串，包含：
        - years_analyzed / year_count：分析年度与年数（建议覆盖最近 5-6 年）
        - indicators_by_year：各年度 15 项指标明细
        - yoy_changes / trends：同比变动与多年趋势判定
        - timeseries：{"xAxis": [年份...], "series": [{"name": 指标名, "data": [...]}]}
          可直接用于 echarts 图表块与年份×指标时序表格；缺年度为 null（未披露）
        - trend_alerts / alert_count：趋势性风险预警
    """
    return _compare_multi_year_impl(multi_year_data_json)
