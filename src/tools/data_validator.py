"""财务数据一致性校验工具

本模块实现三大财务数据勾稽校验，用于在审计分析前验证输入数据的可靠性：
1. 资产负债表平衡校验：总资产 ≈ 总负债 + 净资产（2% 容差）
2. 现金流勾稽校验：经营活动现金流 ≈ 净利润 + 折旧摊销 - 营运资本变动（15% 容差）
3. 净利润与未分配利润变动一致性校验：未分配利润变动 ≈ 净利润 - 分红（5% 容差）

每项校验均输出 passed 状态：True（通过）、False（未通过）、None（数据不足跳过）。
未通过的校验项将自动生成为"数据可靠性风险"条目，纳入后续风险评估。
"""
import json
import math
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _safe_float(val, default=0.0):
    """安全的浮点数转换，遇到无法转换的值时返回默认值而非抛出异常。

    用于处理 LLM 或用户输入中可能出现的 None、空字符串、非数值类型等脏数据。

    Args:
        val: 待转换的值，可以是任意类型
        default: 转换失败时的返回值，默认为 0.0

    Returns:
        转换后的 float 值，或 default
    """
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _validate_balance_sheet(data: dict) -> dict:
    """校验1：资产负债表平衡校验（总资产 = 总负债 + 所有者权益）。

    审计准则要求资产负债表必须满足 "资产 = 负债 + 所有者权益" 的恒等式。
    若差额占比超过 2% 容差阈值，则判定为不通过，提示数据可靠性风险。

    处理逻辑：
    1. 提取总资产、总负债、净资产（兼容 net_assets / owners_equity 两种字段名）
    2. 若净资产未提供，则通过 总资产 - 总负债 反算
    3. 计算差额绝对值和差额占比
    4. 以 2% 为阈值判断是否通过

    Args:
        data: 财务数据字典，需包含 total_assets、total_liabilities 等字段

    Returns:
        校验结果字典，含 check（校验项名称）、passed（是否通过）、
        difference（差额）、message（结论描述）等
    """
    # 提取三大基本会计要素，使用 _safe_float 防御脏数据
    total_assets = _safe_float(data.get("total_assets"))
    total_liabilities = _safe_float(data.get("total_liabilities"))
    # 净资产字段兼容两种命名：net_assets 或 owners_equity，都无则反算
    net_assets = _safe_float(data.get("net_assets") or data.get("owners_equity") or (total_assets - total_liabilities))
    # 验证恒等式：总资产 = 总负债 + 净资产（即所有者权益）
    equity = total_liabilities + net_assets

    # 若资产或权益均为 0，说明数据缺失，跳过校验
    if total_assets == 0 or equity == 0:
        return {"check": "资产负债表平衡", "passed": None, "message": "数据不足，无法校验"}

    # 计算差额绝对值和占比（取两者绝对值的较大者作分母，避免除零）
    diff = abs(total_assets - equity)
    ratio = diff / max(abs(total_assets), abs(equity))
    # 审计容差阈值：2%（四舍五入误差、汇率折算等合理偏差范围）
    threshold = 0.02

    passed = ratio <= threshold
    return {
        "check": "资产负债表平衡",
        "formula": "总资产 = 总负债 + 净资产",
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_assets": net_assets,
        "liabilities_plus_equity": equity,
        "difference": round(diff, 2),
        "difference_pct": f"{ratio * 100:.2f}%",
        "threshold": f"{threshold * 100:.0f}%",
        "passed": passed,
        "message": "资产负债表平衡" if passed else f"资产负债表不平，差额{diff:.2f}（{ratio*100:.2f}%），存在数据可靠性风险"
    }


def _validate_cashflow_reconciliation(data: dict) -> dict:
    """校验2：现金流勾稽校验（净利润 + 非现金费用 ≈ 经营活动现金流）。

    基于现金流量表间接法原理：经营活动现金流 ≈ 净利润 + 折旧摊销 - 营运资本变动。
    若企业净利润高但经营现金流低，可能存在利润虚增或应收账款异常。

    处理逻辑（分两种场景）：
    - 完整数据场景：提供折旧摊销和营运资本变动，用完整勾稽公式校验（15% 容差）
    - 简化场景：未提供折旧/营运资本数据，仅比较净利润与经营现金流的方向一致性（2 倍容差）

    Args:
        data: 财务数据字典，需包含 net_profit、operating_cashflow 等字段

    Returns:
        校验结果字典，含 passed 状态、差额、message 等
    """
    # 提取现金流勾稽所需的五个关键数据
    net_profit = _safe_float(data.get("net_profit"))
    operating_cashflow = _safe_float(data.get("operating_cashflow"))
    depreciation = _safe_float(data.get("depreciation", 0))
    amortization = _safe_float(data.get("amortization", 0))
    working_capital_change = _safe_float(data.get("working_capital_change", 0))

    # ── 简化勾稽场景：未提供折旧和营运资本数据时 ──
    # 此时无法执行完整间接法，仅判断现金流与净利润的方向和量级一致性
    if depreciation == 0 and working_capital_change == 0:
        # 两者均为零时无法校验
        if net_profit == 0 and operating_cashflow == 0:
            return {"check": "现金流勾稽", "passed": None, "message": "数据不足，无法校验"}
        # 计算差异绝对值和差异比率
        diff = abs(operating_cashflow - net_profit)
        ratio = diff / max(abs(net_profit), abs(operating_cashflow), 1)
        # 差异超过净利润绝对值 2 倍视为异常（宽松阈值，因缺少非现金费用调整项）
        passed = ratio <= 2.0
        return {
            "check": "现金流勾稽（简化）",
            "formula": "经营现金流 ≈ 净利润 + 折旧摊销 - 营运资本变动",
            "net_profit": net_profit,
            "operating_cashflow": operating_cashflow,
            "difference": round(diff, 2),
            "difference_ratio": f"{ratio:.2f}",
            "note": "未提供折旧摊销和营运资本变动数据，仅校验方向一致性",
            "passed": passed,
            "message": "净利润与经营现金流方向基本一致" if passed else f"净利润({net_profit})与经营现金流({operating_cashflow})差异过大，存在利润质量风险"
        }

    # ── 完整勾稽场景：使用间接法公式 ──
    # 估算经营现金流 = 净利润 + 折旧 + 摊销 - 营运资本变动
    estimated_cashflow = net_profit + depreciation + amortization - working_capital_change
    # 计算实际值与估算值的差额
    diff = abs(operating_cashflow - estimated_cashflow)
    ratio = diff / max(abs(operating_cashflow), abs(estimated_cashflow), 1)
    # 审计容差阈值：15%（间接法为估算，容差高于资产负债表平衡校验）
    threshold = 0.15

    passed = ratio <= threshold
    return {
        "check": "现金流勾稽",
        "formula": "经营现金流 ≈ 净利润 + 折旧摊销 - 营运资本变动",
        "net_profit": net_profit,
        "depreciation": depreciation,
        "amortization": amortization,
        "working_capital_change": working_capital_change,
        "estimated_cashflow": round(estimated_cashflow, 2),
        "actual_cashflow": operating_cashflow,
        "difference": round(diff, 2),
        "difference_pct": f"{ratio * 100:.2f}%",
        "threshold": f"{threshold * 100:.0f}%",
        "passed": passed,
        "message": "现金流勾稽关系成立" if passed else f"现金流勾稽不成立，差额{diff:.2f}（{ratio*100:.2f}%），存在数据可靠性风险"
    }


def _validate_retained_earnings(data: dict) -> dict:
    """校验3：净利润与未分配利润变动一致性校验。

    根据会计准则，未分配利润的期末-期初变动应等于净利润减去分红。
    若两者不一致，可能意味着存在未入账的利润分配或前期差错更正。

    处理逻辑：
    1. 提取净利润、期初/期末未分配利润、分红金额
    2. 计算预期变动（净利润 - 分红）和实际变动（期末 - 期初）
    3. 若两者均接近零（绝对值 < 1），跳过校验避免小数值误差
    4. 计算差额占比，以 5% 为阈值判断是否通过

    Args:
        data: 财务数据字典，需包含 net_profit、retained_earnings_begin/end 等字段

    Returns:
        校验结果字典，含 passed 状态、差额、message 等
    """
    # 提取校验所需数据
    net_profit = _safe_float(data.get("net_profit"))
    retained_earnings_end = _safe_float(data.get("retained_earnings_end", 0))
    retained_earnings_begin = _safe_float(data.get("retained_earnings_begin", 0))
    dividends = _safe_float(data.get("dividends", 0))

    # 期初期末均为零说明未提供该数据，跳过校验
    if retained_earnings_end == 0 and retained_earnings_begin == 0:
        return {"check": "未分配利润一致性", "passed": None, "message": "未提供未分配利润数据，无法校验"}

    # 预期变动 = 净利润 - 分红（理论上的未分配利润增减额）
    expected_change = net_profit - dividends
    # 实际变动 = 期末未分配利润 - 期初未分配利润
    actual_change = retained_earnings_end - retained_earnings_begin

    # 两者均接近零时跳过，避免微小数值导致误报
    if abs(expected_change) < 1 and abs(actual_change) < 1:
        return {"check": "未分配利润一致性", "passed": True, "message": "数据量级过小，跳过校验"}

    # 计算差额绝对值和占比
    diff = abs(actual_change - expected_change)
    ratio = diff / max(abs(expected_change), abs(actual_change), 1)
    # 审计容差阈值：5%（低于现金流勾稽，因利润分配关系较为精确）
    threshold = 0.05

    passed = ratio <= threshold
    return {
        "check": "未分配利润一致性",
        "formula": "期末未分配利润 - 期初未分配利润 ≈ 净利润 - 分红",
        "retained_earnings_begin": retained_earnings_begin,
        "retained_earnings_end": retained_earnings_end,
        "actual_change": round(actual_change, 2),
        "net_profit": net_profit,
        "dividends": dividends,
        "expected_change": round(expected_change, 2),
        "difference": round(diff, 2),
        "difference_pct": f"{ratio * 100:.2f}%",
        "threshold": f"{threshold * 100:.0f}%",
        "passed": passed,
        "message": "未分配利润变动与净利润一致" if passed else f"未分配利润变动({actual_change:.2f})与净利润-分红({expected_change:.2f})不一致，差额{diff:.2f}，存在数据可靠性风险"
    }


@tool
def validate_financial_data(financial_data_json: str) -> str:
    """校验财务数据的一致性，作为审计分析的前置数据质量检查。

    依次执行三项勾稽校验：
    1. 资产负债表平衡校验（资产 = 负债 + 所有者权益）
    2. 现金流勾稽校验（经营现金流 ≈ 净利润 + 折旧摊销 - 营运资本变动）
    3. 净利润与未分配利润变动一致性校验

    输入 JSON 格式示例：
    {"total_assets":100, "total_liabilities":60, "net_assets":40,
     "net_profit":8, "operating_cashflow":-2,
     "depreciation":0, "amortization":0, "working_capital_change":0,
     "retained_earnings_begin":0, "retained_earnings_end":0, "dividends":0}

    所有金额单位需统一（如万元），未提供的字段可用 0 或缺省。

    返回 JSON 包含：
    - data_validation.all_checks: 每项校验的详细结果
    - data_validation.validation_result: 总体验证结论（通过/未通过）
    - data_validation.risks: 未通过校验项自动生成的数据可靠性风险条目

    Args:
        financial_data_json: 财务数据的 JSON 字符串
    """
    # 解析输入 JSON，失败时直接返回错误信息
    try:
        data = json.loads(financial_data_json)
    except json.JSONDecodeError as e:
        return f"JSON解析失败: {e}"

    # 收集所有校验结果和未通过的校验项
    results = []
    failed_checks = []

    # 校验1：资产负债表平衡
    r1 = _validate_balance_sheet(data)
    results.append(r1)
    if r1.get("passed") is False:
        failed_checks.append(r1)

    # 校验2：现金流勾稽
    r2 = _validate_cashflow_reconciliation(data)
    results.append(r2)
    if r2.get("passed") is False:
        failed_checks.append(r2)

    # 校验3：未分配利润一致性
    r3 = _validate_retained_earnings(data)
    results.append(r3)
    if r3.get("passed") is False:
        failed_checks.append(r3)

    # 构建输出：汇总三项校验的通过/失败/跳过统计
    output = {
        "data_validation": {
            "all_checks": results,
            "total_checks": len(results),
            "passed_checks": sum(1 for r in results if r.get("passed") is True),
            "failed_checks": len(failed_checks),
            "skipped_checks": sum(1 for r in results if r.get("passed") is None),
            "validation_result": "通过" if not failed_checks else "未通过"
        }
    }

    # 将未通过的校验项自动转化为"数据可靠性风险"条目
    # 风险等级统一标记为"重要"，由后续 LLM 分析决定是否升级
    if failed_checks:
        output["data_validation"]["risks"] = [
            {
                "risk_id": f"V{i+1:03d}",
                "dimension": "数据可靠性风险",
                "title": f["message"],
                "level": "重要",
                "evidence": f"校验项：{f['check']}，差异：{f.get('difference', 'N/A')}",
                "audit_suggestion": "核实财务数据来源，检查是否存在编制错误或调整未入账"
            }
            for i, f in enumerate(failed_checks)
        ]

    return json.dumps(output, ensure_ascii=False, indent=2)
