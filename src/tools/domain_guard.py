"""领域约束机制化校验（Domain Constraint Guard）

本模块为审计风险识别系统的两条关键领域约束提供机制化校验（断言 / 后置检查），
在 Agent 执行链与报告导出环节强制执行，防止 LLM 或后续代码回归破坏审计合规底线：

约束一：工具调用顺序 —— 必须"先校验后计算"
    在调用 calculate_financial_indicators（财务指标计算）之前，必须已调用
    validate_financial_data（财务数据勾稽校验）。审计准则要求先验证数据可靠性
    再据此计算指标，否则一切指标都可能建立在不可靠（不平衡/勾稽不成立）的数据之上。

约束二：导出内容必须包含 AI 免责声明
    导出的 PDF 风险报告 / Excel 审计底稿必须包含"AI 辅助生成"免责声明，
    否则可能被误认为正式注册会计师审计意见，违反执业规范与本系统定位。

每条约束提供两组接口：
- check_*(...) -> (ok: bool, message: str)：纯函数，返回布尔与说明，便于非阻断式集成与单测
- assert_*(...)：校验失败时抛出带"修复方向"的异常，用于强制拦截导出/执行链

所有失败信息均给出明确"修复方向"，方便调用方（人或 Agent）定位并纠正。
"""
import logging

logger = logging.getLogger(__name__)

# ── 约束一相关常量：工具名 ────────────────────────────────
VALIDATE_TOOL = "validate_financial_data"
CALCULATE_TOOL = "calculate_financial_indicators"
DISCLOSURE_TOOL = "check_disclosure_compliance"
SEARCH_TOOL = "search_regulations"
SCORE_TOOL = "calculate_comprehensive_score"
EXPORT_PDF_TOOL = "export_pdf_report"
EXPORT_EXCEL_TOOL = "export_excel_report"

# 完整声明链路的相对次序等级（rank）：
#   validate_financial_data → calculate_financial_indicators
#   → (check_disclosure_compliance ∥ search_regulations)  ← 二者无数据依赖，共享 rank 并行
#   → calculate_comprehensive_score → export_pdf_report + export_excel_report
# 设计说明：披露检查与法规检索之间不存在数据依赖（检索为风险找依据、披露检查独立
# 评分输入），强制先后只会误伤；而综合评分消费校验/指标/披露三模块结果、导出
# 需携带评分，因此 score 与 export 的后置次序保留。
PIPELINE_RANK = {
    VALIDATE_TOOL: 0,
    CALCULATE_TOOL: 1,
    DISCLOSURE_TOOL: 2,
    SEARCH_TOOL: 2,
    SCORE_TOOL: 3,
    EXPORT_PDF_TOOL: 4,
    EXPORT_EXCEL_TOOL: 4,
}

# 供错误信息展示的人类可读声明链路
PIPELINE_DESC = (
    "validate_financial_data → calculate_financial_indicators"
    " → check_disclosure_compliance ∥ search_regulations（并行）"
    " → calculate_comprehensive_score → export_pdf_report + export_excel_report"
)

# ── 约束二相关常量：免责声明标识 ──────────────────────────
# 免责声明的规范核心短语，PDF/Excel 导出内容中必须出现该标识
DISCLAIMER_MARKER = "AI 辅助生成"


class ToolCallOrderViolation(Exception):
    """工具调用顺序违反领域约束时抛出（先 validate 后 calculate）。"""


class DisclaimerMissingError(Exception):
    """导出内容缺失 AI 免责声明时抛出。"""


# ═══════════════════════════════════════════════════════════
# 约束一：工具调用顺序（先 validate 后 calculate）
# ═══════════════════════════════════════════════════════════

def check_tool_call_order(called_tools):
    """后置检查：验证工具调用顺序满足完整声明链路不变量。

    分两层校验：
    - 层一（强制前置）“先 validate 后 calculate”：
        * 若调用了 calculate_financial_indicators，则其首次调用之前必须已存在
          至少一次 validate_financial_data 调用；若从未 validate 或 validate 晚于
          首次 calculate，均判定为违反。
    - 层二（完整声明链路）相对次序：
        * 仅对实际调用到的链路工具做检查，任一后置步骤早于其前置步骤
          即判定为违反；未被调用的中间步骤不作要求（跳过某步骤不算顺序违反）。

    Args:
        called_tools: 按实际调用先后顺序排列的工具名称序列（可迭代）。

    Returns:
        (ok, message)：ok 为是否通过；message 为结论说明，失败时包含明确修复方向。
    """
    seq = list(called_tools)
    # 层一：先 validate 后 calculate（含 calculate 缺失前置 validate 的强制拦截）
    ok, message = _check_validate_before_calculate(seq)
    if not ok:
        return ok, message
    # 层二：完整声明链路的相对次序
    return _check_pipeline_order(seq)


def _check_validate_before_calculate(seq):
    """层一校验：calculate 必须以至少一次前置 validate 为前提。"""
    # 首次 calculate 的位置；未调用则无需校验
    first_calc = next((i for i, name in enumerate(seq) if name == CALCULATE_TOOL), None)
    if first_calc is None:
        return True, "未调用 calculate_financial_indicators，无需校验调用顺序"

    # 首次 validate 的位置
    first_val = next((i for i, name in enumerate(seq) if name == VALIDATE_TOOL), None)

    repair = (
        f"修复方向：请在调用 {CALCULATE_TOOL} 之前先调用 {VALIDATE_TOOL} 完成财务数据勾稽校验"
        f"（资产负债表平衡 / 现金流勾稽 / 未分配利润一致性），确认数据可靠后再计算财务指标。"
    )

    if first_val is None:
        return False, (
            f"违反领域约束「先校验后计算」：检测到已调用 {CALCULATE_TOOL}，"
            f"但全程未调用 {VALIDATE_TOOL}。{repair}"
        )
    if first_val > first_calc:
        return False, (
            f"违反领域约束「先校验后计算」：{VALIDATE_TOOL} 在 {CALCULATE_TOOL} 之后才被调用"
            f"（validate 位置 {first_val} 晚于首次 calculate 位置 {first_calc}）。{repair}"
        )
    return True, "工具调用顺序正确：validate_financial_data 先于 calculate_financial_indicators"


def _check_pipeline_order(seq):
    """层二校验：完整声明链路的相对次序（仅对实际调用到的链路工具生效）。

    规则：对任意两个链路工具 A、B，若声明链路要求 A 先于 B（rank(A) < rank(B)），
    且二者均被调用，则 A 的首次调用必须早于 B 的首次调用；否则判定违反声明顺序。
    共享 rank 的并行步骤（export_pdf_report / export_excel_report）之间无先后约束。
    """
    # 收集链路工具的首次调用位置
    first_idx = {}
    for i, name in enumerate(seq):
        if name in PIPELINE_RANK and name not in first_idx:
            first_idx[name] = i

    # 按实际调用先后排序，逐对比较 rank 是否非递减
    present = sorted(first_idx, key=lambda name: first_idx[name])
    for a_pos in range(len(present)):
        for b_pos in range(a_pos + 1, len(present)):
            earlier, later = present[a_pos], present[b_pos]
            if PIPELINE_RANK[earlier] > PIPELINE_RANK[later]:
                repair = (
                    f"修复方向：请严格按声明链路依次调用工具（{PIPELINE_DESC}），"
                    f"确保 {later} 在 {earlier} 之前完成。"
                )
                return False, (
                    f"违反领域约束「工具链声明顺序」：{earlier} 早于 {later} 被调用，"
                    f"但声明链路要求 {later} 先于 {earlier}。{repair}"
                )
    return True, "工具调用顺序正确：符合完整声明链路次序"


def assert_tool_call_order(called_tools):
    """断言版本（两层合并）：任一层违规均抛 ToolCallOrderViolation。

    注：Agent 运行时已改用分级门禁（assert_hard_order + check_soft_order），
    本函数保留给需要严格两层拦截的调用方（如 CI 完整链路回归校验）。

    Args:
        called_tools: 按调用先后顺序排列的工具名称序列。

    Raises:
        ToolCallOrderViolation: 当任一层顺序约束被违反时。
    """
    ok, message = check_tool_call_order(called_tools)
    if not ok:
        raise ToolCallOrderViolation(message)
    return message


# ── 分级门禁接口（Agent 运行时使用）──
# 硬约束：「先校验后计算」——审计底线，数据不可靠则一切指标无意义，
#         违反时 fail-closed 中断，绝不在不可靠数据上产出报告。
# 软约束：完整声明链路的其余相对次序（disclosure/search/score/export 之间）——
#         属推荐顺序而非数据依赖，LLM 偶发乱序不应炸掉整场分析（兜底导出也会
#         被连带中断导致零产出），降级为「可见警告」附在报告中供人工复核。

def assert_hard_order(called_tools):
    """硬约束断言：仅拦截「先校验后计算」违规（fail-closed）。

    Raises:
        ToolCallOrderViolation: calculate 无前置 validate 或次序颠倒时。
    """
    ok, message = _check_validate_before_calculate(list(called_tools))
    if not ok:
        raise ToolCallOrderViolation(message)
    return message


def check_soft_order(called_tools):
    """软约束检查：完整声明链路相对次序，返回 (ok, message) 不抛异常。

    调用方应在 ok=False 时将 message 以可见警告形式附入报告（降级可见原则），
    但不中断分析与导出。
    """
    return _check_pipeline_order(list(called_tools))


# ═══════════════════════════════════════════════════════════
# 约束二：导出内容必须包含 AI 免责声明
# ═══════════════════════════════════════════════════════════

def collect_flowable_texts(elements):
    """从 reportlab flowable 元素树中提取全部文本（不依赖 reportlab 类型）。

    通过鸭子类型读取：Paragraph 的 .text 属性，以及 KeepTogether 等容器的
    ._content 子元素列表，递归展开为纯文本列表，供免责声明校验使用。

    Args:
        elements: reportlab flowable 元素列表（Paragraph / Table / KeepTogether 等）。

    Returns:
        提取到的字符串列表。
    """
    texts = []
    stack = list(elements)
    while stack:
        e = stack.pop()
        text = getattr(e, "text", None)
        if isinstance(text, str):
            texts.append(text)
        content = getattr(e, "_content", None)
        if isinstance(content, (list, tuple)):
            stack.extend(content)
    return texts


def check_disclaimer_present(texts, marker=DISCLAIMER_MARKER):
    """后置检查：验证导出内容中包含 AI 免责声明标识。

    Args:
        texts: 待检查的文本片段列表（如 PDF 段落文本或 Excel 单元格文本）。
        marker: 免责声明核心标识，默认 DISCLAIMER_MARKER。

    Returns:
        (ok, message)：ok 为是否包含；失败时 message 含明确修复方向。
    """
    joined = "\n".join(t for t in texts if isinstance(t, str))
    if marker in joined:
        return True, f"导出内容已包含免责声明标识「{marker}」"
    return False, (
        f"违反领域约束「导出须含免责声明」：导出内容未找到免责声明标识「{marker}」。"
        f"修复方向：请在报告封面 / 页脚 / 整体评估处写入 AI_DISCLAIMER 免责声明常量，"
        f"明确告知读者本报告为 AI 辅助生成、可能存在偏差、不构成正式审计意见。"
    )


def assert_disclaimer_present(texts, doc_kind="报告", marker=DISCLAIMER_MARKER):
    """断言版本：导出内容缺失免责声明时抛出 DisclaimerMissingError。

    Args:
        texts: 待检查的文本片段列表。
        doc_kind: 文档类型描述（用于错误信息，如 "PDF风险报告"）。
        marker: 免责声明核心标识。

    Raises:
        DisclaimerMissingError: 当内容中不含免责声明标识时。
    """
    ok, message = check_disclaimer_present(texts, marker=marker)
    if not ok:
        raise DisclaimerMissingError(f"[{doc_kind}] {message}")
    return message
