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
#   validate_financial_data → calculate_financial_indicators → check_disclosure_compliance
#   → search_regulations → calculate_comprehensive_score → export_pdf_report + export_excel_report
# 其中 PDF / Excel 导出为并行步骤，共享同一 rank（彼此之间无先后约束）。
PIPELINE_RANK = {
    VALIDATE_TOOL: 0,
    CALCULATE_TOOL: 1,
    DISCLOSURE_TOOL: 2,
    SEARCH_TOOL: 3,
    SCORE_TOOL: 4,
    EXPORT_PDF_TOOL: 5,
    EXPORT_EXCEL_TOOL: 5,
}

# 供错误信息展示的人类可读声明链路
PIPELINE_DESC = (
    "validate_financial_data → calculate_financial_indicators → check_disclosure_compliance"
    " → search_regulations → calculate_comprehensive_score → export_pdf_report + export_excel_report"
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
    """后置检查：验证工具调用顺序满足"先校验后计算"。

    规则：
    - 若从未调用 calculate_financial_indicators，则无需校验，视为通过；
    - 若调用了 calculate_financial_indicators，则其首次调用之前必须已存在
      至少一次 validate_financial_data 调用；
    - 若 calculate 之前从未 validate，或 validate 出现在首次 calculate 之后，
      均判定为违反约束。

    Args:
        called_tools: 按实际调用先后顺序排列的工具名称序列（可迭代）。

    Returns:
        (ok, message)：ok 为是否通过；message 为结论说明，失败时包含明确修复方向。
    """
    seq = list(called_tools)

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


def assert_tool_call_order(called_tools):
    """断言版本：调用顺序违反约束时抛出 ToolCallOrderViolation。

    Args:
        called_tools: 按调用先后顺序排列的工具名称序列。

    Raises:
        ToolCallOrderViolation: 当 calculate 先于 validate 或缺失 validate 时。
    """
    ok, message = check_tool_call_order(called_tools)
    if not ok:
        raise ToolCallOrderViolation(message)
    return message


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
