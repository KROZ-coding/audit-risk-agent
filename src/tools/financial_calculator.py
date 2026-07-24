"""财务指标计算工具 - 根据财务报表数据计算关键审计分析指标

本模块实现 16 项核心财务指标的计算与异常预警，覆盖：
- 盈利能力：毛利率、净利润同比变动
- 营运能力：应收账款周转、存货周转率
- 偿债能力：资产负债率、流动比率、速动比率
- 现金流质量：经营现金流/净利润比
- 特殊风险：存贷双高、连续亏损、商誉减值

所有指标均设置审计阈值，超过阈值自动生成风险预警。
"""
import json
import logging
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _safe_div(numerator, denominator, default=None):
    """安全除法运算，分母为零或任一操作数为 None 时返回默认值。

    Args:
        numerator: 被除数
        denominator: 除数
        default: 除数为零或操作数为 None 时的返回值

    Returns:
        除法结果（保留4位小数），或 default
    """
    if numerator is None or denominator is None or denominator == 0:
        return default
    return round(numerator / denominator, 4)


def _pct_change(current, previous, default=None):
    """计算同比变动百分比。

    公式：(本期值 - 上期值) / |上期值| × 100

    重要边界：当上期值为负（如上期亏损）时，同比百分比无良好定义
    （例如上期 -100、本期 +50 会算出 +150%，误导读者误以为“增长”，
    实则为扭亏为盈），故此时返回 default（None），由调用方改用定性描述。

    Args:
        current: 本期值
        previous: 上期值
        default: 无法计算时的返回值

    Returns:
        变动百分比（保留2位小数），或 default
    """
    if current is None or previous is None or previous == 0:
        return default
    # 上期为负：百分比无良好定义（扭亏/减亏场景），返回 default 避免输出误导性数字
    if previous < 0:
        return default
    return round((current - previous) / abs(previous) * 100, 2)


@tool
def calculate_financial_indicators(financial_data_json: str) -> str:
    """计算财务分析指标并识别异常。

    输入 financial_data_json 为 JSON 字符串，包含以下可选字段（均为数值型）：
    - revenue_current/previous: 本期/上期营业收入
    - net_profit_current/previous: 本期/上期净利润
    - operating_cashflow_current/previous: 本期/上期经营活动现金流净额
    - total_assets_current: 期末总资产
    - total_liabilities_current: 期末总负债
    - accounts_receivable_current/previous: 本期/上期应收账款
    - inventory_current/previous: 本期/上期存货
    - current_assets_current/current_liabilities_current: 流动资产/流动负债
    - cost_of_goods_current: 营业成本
    - net_assets_current: 净资产
    - goodwill_current: 商誉
    - other_receivables_current: 其他应收款
    - cash_and_equivalents_current: 货币资金
    - short_term_debt_current: 短期借款
    - interest_income_current/interest_expense_current: 利息收入/支出

    返回 JSON 包含：
    - indicators: 各指标计算结果
    - alerts: 超阈值的风险预警列表
    - alert_count: 预警总数
    """
    try:
        data = json.loads(financial_data_json)
    except json.JSONDecodeError as e:
        return f"错误：JSON 解析失败 - {str(e)}"

    # 初始化指标结果字典和预警列表
    results = {}
    alerts = []

    # ── 1. 营收同比变动率：检测收入异常增长或下滑 ──
    rev_c = data.get("revenue_current")
    rev_p = data.get("revenue_previous")
    if rev_c is not None and rev_p is not None:
        rev_change = _pct_change(rev_c, rev_p)
        results["revenue_yoy_change_pct"] = rev_change
        # 营收变动超过 30% 视为异常波动
        if rev_change is not None and abs(rev_change) > 30:
            alerts.append(f"营业收入同比变动 {rev_change}%，幅度较大，需关注合理性")

    # ── 2. 净利润同比变动率：检测盈利稳定性 ──
    np_c = data.get("net_profit_current")
    np_p = data.get("net_profit_previous")
    if np_c is not None and np_p is not None:
        np_change = _pct_change(np_c, np_p)
        if np_change is not None:
            results["net_profit_yoy_change_pct"] = np_change
            # 净利润波动超过 50% 需重点关注
            if abs(np_change) > 50:
                alerts.append(f"净利润同比变动 {np_change}%，波动显著")
        elif np_p < 0:
            # 上期亏损：同比百分比不适用，改用定性描述（扭亏/减亏/亏损扩大）
            if np_c > 0:
                desc = "扭亏为盈"
            elif np_c > np_p:
                desc = "亏损收窄（减亏）"
            else:
                desc = "亏损扩大"
            results["net_profit_yoy_change_desc"] = desc
            alerts.append(
                f"净利润由上期 {np_p} 变为本期 {np_c}，呈{desc}，"
                f"上期为负致同比百分比不适用，需关注盈利可持续性"
            )

    # ── 3. 毛利率：(营收-营业成本)/营收，衡量核心盈利能力 ──
    cog = data.get("cost_of_goods_current")
    if rev_c is not None and cog is not None:
        gross_margin = _safe_div(rev_c - cog, rev_c)
        if gross_margin is not None:
            results["gross_margin_pct"] = round(gross_margin * 100, 2)

    # ── 4. 经营现金流/净利润比：衡量利润含金量 ──
    ocf_c = data.get("operating_cashflow_current")
    if ocf_c is not None and np_c is not None:
        ocf_np_ratio = _safe_div(ocf_c, np_c)
        results["operating_cashflow_to_net_profit_ratio"] = ocf_np_ratio
        # 比值低于 0.5 表示利润缺乏现金流支撑，可能存在虚增利润
        if ocf_np_ratio is not None and ocf_np_ratio < 0.5 and np_c > 0:
            alerts.append(
                f"经营现金流/净利润比 = {ocf_np_ratio}，低于 0.5，"
                f"现金流与利润严重背离，存在利润质量风险"
            )
        # 营收增长但经营现金流为负，典型财务造假红旗信号
        if ocf_c is not None and ocf_c < 0 and np_c is not None and np_c > 0:
            alerts.append("营收增长但经营现金流为负，盈利质量存疑")

    # ── 5. 应收账款占营收比：评估回款风险和收入真实性 ──
    ar_c = data.get("accounts_receivable_current")
    if ar_c is not None and rev_c is not None:
        ar_rev_ratio = _safe_div(ar_c, rev_c)
        if ar_rev_ratio is not None:
            results["accounts_receivable_to_revenue_ratio"] = round(ar_rev_ratio * 100, 2)
            # 超过 30% 可能存在提前确认收入或虚构收入
            if ar_rev_ratio > 0.3:
                alerts.append(
                    f"应收账款占营收比 = {round(ar_rev_ratio * 100, 2)}%，超过 30%，需关注回款风险"
                )

    # ── 6. 应收增速 vs 营收增速：识别提前确认收入嫌疑 ──
    ar_p = data.get("accounts_receivable_previous")
    if ar_c is not None and ar_p is not None and rev_c is not None and rev_p is not None:
        ar_change = _pct_change(ar_c, ar_p)
        rev_change_val = _pct_change(rev_c, rev_p)
        results["accounts_receivable_yoy_change_pct"] = ar_change
        if ar_change is not None and rev_change_val is not None:
            # 应收增速超过营收增速 20 个百分点，审计准则重点关注信号
            if ar_change > rev_change_val + 20:
                alerts.append(
                    f"应收账款增速({ar_change}%)显著高于营收增速({rev_change_val}%)，"
                    f"可能存在提前确认收入或放宽信用政策"
                )

    # ── 7. 存货周转率：营业成本/平均存货，衡量存货管理效率 ──
    inv_c = data.get("inventory_current")
    inv_p = data.get("inventory_previous")
    if cog is not None and inv_c is not None:
        # 注：必须用 is None 判断而非 `inv_p or inv_c`——后者在上期存货合法为 0（如
        # 新成立/纯服务业无期初存货）时会错误地把 0 当作“未提供”而退化为 inv_c，导致均值失真
        prev_inv = inv_p if inv_p is not None else inv_c
        avg_inv = (inv_c + prev_inv) / 2  # 取期初期末平均值（上期缺失时退化为本期）
        inv_turnover = _safe_div(cog, avg_inv)
        results["inventory_turnover_ratio"] = inv_turnover
        if inv_turnover is not None and inv_p is not None and inv_p > 0:
            # 存货同比增长超过 50%，可能存在滞销或虚增存货（inv_p>0 防除零）
            if inv_c > inv_p * 1.5:
                alerts.append(
                    f"存货同比增长 {round((inv_c / inv_p - 1) * 100, 2)}%，"
                    f"存货激增需关注跌价风险"
                )

    # ── 8. 资产负债率：总负债/总资产，衡量财务杠杆水平 ──
    tl_c = data.get("total_liabilities_current")
    ta_c = data.get("total_assets_current")
    if tl_c is not None and ta_c is not None:
        debt_ratio = _safe_div(tl_c, ta_c)
        if debt_ratio is not None:
            results["debt_to_asset_ratio_pct"] = round(debt_ratio * 100, 2)
            # 超过 70% 为高杠杆，偿债压力较大
            if debt_ratio > 0.7:
                alerts.append(f"资产负债率 = {round(debt_ratio * 100, 2)}%，超过 70%，财务杠杆较高")

    # ── 9. 流动比率：流动资产/流动负债，衡量短期偿债能力 ──
    ca_c = data.get("current_assets_current")
    cl_c = data.get("current_liabilities_current")
    if ca_c is not None and cl_c is not None:
        current_ratio = _safe_div(ca_c, cl_c)
        results["current_ratio"] = current_ratio
        # 低于 1 表示流动资产不足以覆盖流动负债
        if current_ratio is not None and current_ratio < 1:
            alerts.append(f"流动比率 = {current_ratio}，低于 1，短期偿债能力不足")

    # ── 10. 速动比率：(流动资产-存货)/流动负债，更严格的流动性指标 ──
    if ca_c is not None and cl_c is not None and inv_c is not None:
        quick_ratio = _safe_div(ca_c - inv_c, cl_c)
        results["quick_ratio"] = quick_ratio
        if quick_ratio is not None and quick_ratio < 0.5:
            alerts.append(f"速动比率 = {quick_ratio}，低于 0.5，流动性风险较高")

    # ── 11. 商誉占净资产比：评估商誉减值风险敞口 ──
    gw = data.get("goodwill_current")
    na = data.get("net_assets_current")
    if gw is not None and na is not None and na > 0:
        gw_ratio = _safe_div(gw, na)
        if gw_ratio is not None:
            results["goodwill_to_net_assets_ratio_pct"] = round(gw_ratio * 100, 2)
            # 商誉占净资产超过 30%，减值可能对净资产造成重大冲击
            if gw_ratio > 0.3:
                alerts.append(
                    f"商誉占净资产比 = {round(gw_ratio * 100, 2)}%，超过 30%，"
                    f"存在较大减值风险"
                )

    # ── 12. 在建工程变动：关注资本化支出合理性 ──
    cip_c = data.get("construction_in_progress_current")
    cip_p = data.get("construction_in_progress_previous")
    if cip_c is not None and cip_p is not None:
        results["construction_in_progress_change_pct"] = _pct_change(cip_c, cip_p)

    # ── 13. 其他应收款/其他应付款占比：识别关联方资金占用 ──
    other_rec = data.get("other_receivables_current")
    other_pay = data.get("other_payables_current")
    if other_rec is not None and ta_c is not None:
        other_rec_ratio = _safe_div(other_rec, ta_c)
        # 其他应收款占总资产超过 5%，可能存在大股东或关联方资金占用
        if other_rec_ratio is not None and other_rec_ratio > 0.05:
            alerts.append(
                f"其他应收款占总资产比 = {round(other_rec_ratio * 100, 2)}%，"
                f"超过 5%，需关注资金占用风险"
            )
    if other_pay is not None and ta_c is not None:
        other_pay_ratio = _safe_div(other_pay, ta_c)
        results["other_payables_to_total_assets_ratio_pct"] = round(other_pay_ratio * 100, 2) if other_pay_ratio else None

    # ── 14. 存贷双高检测：货币资金高但利息支出远超利息收入，典型造假信号 ──
    cash = data.get("cash_and_equivalents_current")
    st_debt = data.get("short_term_debt_current")
    int_income = data.get("interest_income_current")
    int_expense = data.get("interest_expense_current")
    if cash is not None and st_debt is not None and int_income is not None and int_expense is not None:
        # 货币资金 > 短期借款 且 利息支出 > 2倍利息收入 → 存贷双高异常
        if cash > st_debt and int_expense > int_income * 2:
            alerts.append(
                f"货币资金({cash})高于短期借款({st_debt})，"
                f"但利息支出({int_expense})远大于利息收入({int_income})，"
                f"存在'存贷双高'异常，需核查资金真实性"
            )

    # ── 15. 连续亏损检测：连续两年净利润为负，触发持续经营疑虑 ──
    if np_c is not None and np_p is not None:
        if np_c < 0 and np_p < 0:
            alerts.append("连续两年净利润为负，存在持续经营风险")
        results["net_profit_current"] = np_c
        results["net_profit_previous"] = np_p

    # ── 16. 经营现金流连续为负：进一步验证持续经营能力 ──
    ocf_p = data.get("operating_cashflow_previous")
    if ocf_c is not None and ocf_p is not None:
        if ocf_c < 0 and ocf_p < 0:
            alerts.append("经营活动现金流量净额连续两年为负，持续经营风险较高")

    # 构建输出：指标字典 + 预警列表 + 预警计数
    output = {
        "indicators": results,
        "alerts": alerts,
        "alert_count": len(alerts)
    }

    return json.dumps(output, ensure_ascii=False, indent=2)
