"""Excel审计底稿导出工具

将风险台账 JSON 数据导出为格式化的 Excel 审计底稿文件，包含三个工作表：
1. 风险总览：公司基本信息（名称/股票代码/行业等）+ AI 声明 + 风险统计摘要
2. 风险明细：每条风险的完整信息（10 列），风险等级用颜色标注（重大红/重要橙/一般蓝）
3. 整体评估：整体风险评估结论 + AI 声明 + 行业基准对比（可选）

生成的 Excel 文件自动上传到存储并返回可下载 URL。
"""
import os
import json
import uuid
import logging
import math
import tempfile
from datetime import datetime

from utils.filename import build_file_prefix as _build_file_prefix

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# AI 辅助生成免责声明，强制出现在封面和整体评估 Sheet 中
AI_DISCLAIMER = "【AI 辅助生成】本报告由大语言模型基于公开数据自动生成，可能存在幻觉或偏差，请务必结合人工专业判断进行复核。"

# 风险等级别名归一表：与 pdf_export 保持一致，LLM 非标准取值统一归为三档后统计
_LEVEL_ALIASES = {
    "重大": "重大", "高风险": "重大", "极高风险": "重大", "严重": "重大", "高": "重大",
    "重要": "重要", "中等风险": "重要", "中": "重要",
    "一般": "一般", "低风险": "一般", "轻微": "一般", "低": "一般",
}


def _level_counts(risks: list) -> dict:
    """统计风险列表等级分布（重大/重要/一般），非标准等级取值归一后计数。"""
    c = {"重大": 0, "重要": 0, "一般": 0}
    for r in risks or []:
        lv = str(r.get("level") or r.get("risk_level") or "").strip()
        lv = _LEVEL_ALIASES.get(lv)
        if lv:
            c[lv] += 1
    return c


def _reconcile_risk_summary(report: dict) -> None:
    """以 risk_details 为唯一事实源实时重算 risk_summary（总数/等级分布/维度分布）。

    背景：risk_summary 由 LLM 生成，仲裁回写与勾稽校验条目并入（V 系列）之后
    不会自动更新，导致底稿首页摘要与明细清单脱节（实测缺陷：摘要 7 条 vs
    明细 9 条）。导出前强制按明细动态聚合，摘要恒与明细一致。
    """
    rd = report.get("risk_details") or []
    rs = report.get("risk_summary")
    if not isinstance(rs, dict):
        rs = report["risk_summary"] = {}
    c = _level_counts(rd)
    rs["total_risks"] = len(rd)
    rs["major_risks"] = c["重大"]
    rs["important_risks"] = c["重要"]
    rs["general_risks"] = c["一般"]
    # 维度分布同步按明细重算（修复非五维度条目如市场风险/数据可靠性风险漏计；
    # 键经 _norm_dim 归一化，防止"财务错报"与"财务错报风险"分裂成两行）
    from tools.pdf_export import _norm_dim
    dim_counts = {}
    for r in rd:
        if isinstance(r, dict):
            d = _norm_dim(r.get("dimension", "") or "") or "未分类"
            dim_counts[d] = dim_counts.get(d, 0) + 1
    rs["risk_dimensions"] = dim_counts


def _display_width(text: str) -> int:
    """计算字符串的显示宽度：CJK 及全角字符计 2，其余计 1。

    Excel 列宽单位约等于 ASCII 字符数，而一个汉字视觉宽度约为两个 ASCII 字符，
    估算换行行数时需区分对待，否则纯中文内容会被低估行数导致行高偏矮。

    Args:
        text: 待计算宽度的字符串

    Returns:
        以 ASCII 字符为 1 个单位的显示宽度
    """
    width = 0
    for ch in text:
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF        # CJK 统一汉字
                or 0x3400 <= code <= 0x4DBF     # CJK 扩展 A
                or 0x3000 <= code <= 0x303F     # CJK 符号与标点
                or 0xFF00 <= code <= 0xFFEF     # 全角/半角形
                or 0x2E80 <= code <= 0x2EFF):   # CJK 部首补充
            width += 2
        else:
            width += 1
    return width


def _estimate_row_height(values, col_widths, line_h=15, min_h=30, max_h=400):
    """根据单元格内容与列宽估算自适应行高，避免长文本被固定行高裁剪。

    openpyxl 一旦设置显式行高，Excel 打开时不会再自动增高，因此需在写入时
    按内容估算。口径：按 _display_width 折算各列显示宽度，除以列宽（预留 8%
    边距）向上取整得行数，显式换行分段累加；取各列最大行数乘行高，再夹取到
    [min_h, max_h]，防止空行过矮或超长内容撑出畸形高行。

    Args:
        values: 该行各列的值列表（与 col_widths 逐一配对）
        col_widths: 各列宽度列表（openpyxl 列宽单位，约等于字符数）
        line_h: 单行行高（磅），默认 15
        min_h: 行高下限（磅）
        max_h: 行高上限（磅）

    Returns:
        估算出的行高（磅）
    """
    max_lines = 1
    for val, width in zip(values, col_widths):
        if val is None or width <= 0:
            continue
        text = val if isinstance(val, str) else str(val)
        if not text:
            continue
        effective = max(1.0, width * 0.92)   # 预留边距，防边界字符被挤到下一行
        total_lines = 0
        for segment in text.split("\n"):
            total_lines += max(1, math.ceil(_display_width(segment) / effective))
        max_lines = max(max_lines, total_lines)
    return max(min_h, min(max_h, max_lines * line_h))


def _export_excel_impl(risk_report_json: str, output_path: str = None,
                       validation_json: str = "", financial_indicators_json: str = "",
                       disclosure_check_json: str = "", audit_opinion_json: str = "") -> str:
    """将风险台账 JSON 导出为 Excel 审计底稿的核心实现。

    完整流程：
    1. 解析 JSON 输入（兼容字符串和字典两种输入格式）
    2. 创建 Sheet1「风险总览」：写入封面信息 → AI 声明 → 风险统计摘要
    3. 创建 Sheet2「风险明细」：写入 10 列表头 → 逐行填充风险数据 → 颜色标注等级
    4. 创建 Sheet3「整体评估」：写入结论 → AI 声明 → 行业基准对比（可选）
    5. 保存到本地临时目录并上传到存储

    Args:
        risk_report_json: 风险台账 JSON 字符串或已解析的字典，包含：
            - company_info: 公司基本信息
            - risk_summary: 风险统计摘要（总数/重大/重要/一般）
            - risk_details: 风险明细列表（每条含 risk_id/dimension/title/level/evidence 等）
            - overall_assessment: 整体风险评估结论（文本）
            - industry_benchmark: 行业基准对比（可选，嵌套字典）
        output_path: 可选的输出文件路径，缺省时使用系统临时目录

    Returns:
        成功时返回含下载链接的提示文本，失败时返回错误信息
    """
    # 第一步：解析输入 JSON，兼容字符串和已解析的字典
    try:
        report = json.loads(risk_report_json) if isinstance(risk_report_json, str) else risk_report_json
    except json.JSONDecodeError as e:
        return f"JSON解析失败: {e}"

    wb = Workbook()

    # ═══════════════════════════════════════════════════════
    # Sheet 1：风险总览 —— 封面信息 + AI 声明 + 风险统计摘要
    # ═══════════════════════════════════════════════════════
    ws_summary = wb.active
    ws_summary.title = "风险总览"

    # ── 定义全局样式：标题字体、表头填充、边框、对齐等 ──
    header_font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    title_font = Font(name="微软雅黑", size=14, bold=True)
    normal_font = Font(name="微软雅黑", size=10)
    # 四边细边框，用于风险明细表的每个单元格
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin")
    )
    # 自动换行 + 垂直居中，用于长文本内容单元格
    wrap_alignment = Alignment(wrap_text=True, vertical="center")

    # ── 风险等级颜色映射：重大(红)、重要(橙)、一般(蓝) ──
    level_colors = {
        "重大": PatternFill(start_color="FF0000", end_color="FF0000", fill_type="solid"),
        "重要": PatternFill(start_color="FF8C00", end_color="FF8C00", fill_type="solid"),
        "一般": PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid"),
    }
    # 风险等级对应的字体样式（重大/重要使用白色粗体，一般使用白色常规体）
    level_fonts = {
        "重大": Font(name="微软雅黑", size=10, bold=True, color="FFFFFF"),
        "重要": Font(name="微软雅黑", size=10, bold=True, color="FFFFFF"),
        "一般": Font(name="微软雅黑", size=10, color="FFFFFF"),
    }

    # ── 封面区域：标题行 + 公司基本信息表格 ──
    company_info = report.get("company_info", {})
    # 合并 A1:F1 作为封面标题行
    ws_summary.merge_cells("A1:F1")
    ws_summary["A1"] = "上市公司年报审计风险台账"
    ws_summary["A1"].font = title_font
    ws_summary["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws_summary.row_dimensions[1].height = 30

    # 封面信息行：从第 3 行开始，每行一个标签-值对
    info_rows = [
        ("公司名称", company_info.get("company_name", "未提供")),
        ("股票代码", company_info.get("stock_code", "未提供")),
        ("报告年度", company_info.get("report_year", "未提供")),
        ("行业分类", company_info.get("industry", "未提供")),
        ("审计意见", company_info.get("audit_opinion", "未提供")),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    # Q 补丁：构建版本戳——产物可追溯（旧实例/旧代码产物一眼可辨）
    from tools.pdf_export import _build_hash
    _run_id = str(company_info.get("run_id", "") or "")
    info_rows.append(("构建信息", f"构建 {_build_hash()} | 实例 {_run_id[:8] if _run_id else '—'}"))
    # 封面信息：A 列标签（加粗）、B 列值（常规），相邻排列消除原来的中间空列
    for i, (label, value) in enumerate(info_rows, start=3):
        ws_summary[f"A{i}"] = label
        ws_summary[f"A{i}"].font = Font(name="微软雅黑", size=10, bold=True)
        ws_summary[f"B{i}"] = value
        ws_summary[f"B{i}"].font = normal_font
    ws_summary.column_dimensions["A"].width = 14
    ws_summary.column_dimensions["B"].width = 40

    # ── AI 辅助生成声明行（红色加粗，合并 A~F 列）──
    disclaimer_row = len(info_rows) + 4
    ws_summary.merge_cells(f"A{disclaimer_row}:F{disclaimer_row}")
    ws_summary[f"A{disclaimer_row}"] = AI_DISCLAIMER
    ws_summary[f"A{disclaimer_row}"].font = Font(name="微软雅黑", size=9, bold=True, color="CC0000")
    ws_summary[f"A{disclaimer_row}"].alignment = Alignment(wrap_text=True, vertical="center")
    ws_summary.row_dimensions[disclaimer_row].height = 36

    # ── 风险统计摘要：显示风险总数和各级别数量 ──
    # P3: 摘要禁止读静态缓存——导出前按明细实时聚合（仲裁回写/勾稽校验条目并入后
    # LLM 的 risk_summary 已过时，直接读取会导致首页摘要与内页明细脱节，实测缺陷）
    _reconcile_risk_summary(report)
    risk_summary = report.get("risk_summary", {})
    # 摘要起始行 = 封面信息行数 + 6（预留声明行和间隔）
    summary_start = len(info_rows) + 6
    ws_summary.merge_cells(f"A{summary_start}:F{summary_start}")
    ws_summary[f"A{summary_start}"] = "风险摘要"
    ws_summary[f"A{summary_start}"].font = Font(name="微软雅黑", size=12, bold=True)

    # 摘要数据：总数 + 三个等级的风险计数
    summary_data = [
        ("风险总数", risk_summary.get("total_risks", 0)),
        ("重大风险", risk_summary.get("major_risks", 0)),
        ("重要风险", risk_summary.get("important_risks", 0)),
        ("一般风险", risk_summary.get("general_risks", 0)),
    ]
    for i, (label, value) in enumerate(summary_data, start=summary_start + 1):
        ws_summary[f"A{i}"] = label
        ws_summary[f"A{i}"].font = Font(name="微软雅黑", size=10, bold=True)
        ws_summary[f"B{i}"] = value
        ws_summary[f"B{i}"].font = normal_font

    # ═══════════════════════════════════════════════════════
    # Sheet 2：风险明细表 —— 每条风险的 11 列详细信息
    # ═══════════════════════════════════════════════════════
    ws_detail = wb.create_sheet("风险明细")

    # 定义表头和各列宽度（共 12 列：新增推理思维链 + 系统量化事实）
    headers = ["风险ID", "风险维度", "风险标题", "风险等级", "证据", "数据分析",
               "法规依据", "案例参考", "审计建议", "置信度", "推理思维链", "系统量化事实"]
    col_widths = [10, 15, 25, 10, 40, 40, 35, 35, 40, 10, 50, 50]

    # 写入表头行（第 1 行），设置深蓝色背景 + 白色粗体
    for col_idx, (header, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws_detail.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
        ws_detail.column_dimensions[get_column_letter(col_idx)].width = width

    ws_detail.row_dimensions[1].height = 25
    # 冻结表头：滚动查看长明细时首行表头始终可见
    ws_detail.freeze_panes = "A2"

    # 逐行写入风险明细数据（从第 2 行开始）
    risk_details = report.get("risk_details", [])
    # S4 事实层注入：复用 pdf_export 的确定性量化事实（工具 JSON 传入时）
    from tools.pdf_export import _system_facts_text
    # 50d：风险编号展示复用 pdf_export 的语义编号并列格式（主编号+semantic_id）
    from tools.pdf_export import _risk_id_label as _risk_id_cell

    def _facts_text(risk):
        try:
            return _system_facts_text(risk, validation_json, financial_indicators_json,
                                      disclosure_check_json, audit_opinion_json)
        except Exception:
            return ""

    for row_idx, risk in enumerate(risk_details, start=2):
        level = risk.get("level", "")
        # 按表头顺序组装 11 列数据（新增推理思维链列）
        # 将 reasoning_chain 列表序列化为可读文本
        chain = risk.get("reasoning_chain", [])
        if chain and isinstance(chain, list):
            chain_text = "; ".join(
                f"[{s.get('step','')}] {s.get('detail','')}"
                for s in chain if isinstance(s, dict) and s.get("step")
            )
        else:
            chain_text = ""
        row_data = [
            _risk_id_cell(risk),            # 风险编号（50d：主编号+语义编号并列）
            risk.get("dimension", ""),       # 风险维度（五大维度之一）
            risk.get("title", ""),           # 风险标题
            level,                           # 风险等级（重大/重要/一般）
            risk.get("evidence", ""),        # 年报原文证据
            risk.get("data_analysis", ""),   # 异常数据分析
            risk.get("regulatory_basis", ""), # 法规依据
            risk.get("case_reference", ""),  # 案例参考
            risk.get("audit_suggestion", ""), # 审计核查建议
            risk.get("confidence", 0),       # 置信度（0~1 的浮点数）
            chain_text,                      # 推理思维链
            _facts_text(risk),               # 系统量化事实（确定性计算，供核对）
        ]
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws_detail.cell(row=row_idx, column=col_idx, value=value)
            cell.font = normal_font
            cell.alignment = wrap_alignment
            cell.border = thin_border

            # 第 4 列（风险等级）：根据等级填充颜色背景 + 白色字体
            if col_idx == 4 and level in level_colors:
                cell.fill = level_colors[level]
                cell.font = level_fonts[level]
                cell.alignment = Alignment(horizontal="center", vertical="center")

            # 第 10 列（置信度）：格式化为两位小数
            if col_idx == 10 and isinstance(value, (int, float)):
                cell.number_format = "0.00"

        # 行高自适应：按各列内容与列宽估算，避免固定行高裁剪长文本
        ws_detail.row_dimensions[row_idx].height = _estimate_row_height(row_data, col_widths)

    # ═══════════════════════════════════════════════════════
    # Sheet 3：整体评估 —— 风险结论 + AI 声明 + 行业基准对比
    # ═══════════════════════════════════════════════════════
    ws_assessment = wb.create_sheet("整体评估")

    # 写入标题和整体风险评估结论文本
    ws_assessment.merge_cells("A1:B1")
    ws_assessment["A1"] = "整体风险评估结论"
    ws_assessment["A1"].font = Font(name="微软雅黑", size=12, bold=True)
    overall_text = report.get("overall_assessment", "")
    # L 补丁：评分快照替换（吞全 span），杜绝整体评估与封面双分矛盾
    from tools.pdf_export import _apply_score_snapshot
    overall_text = _apply_score_snapshot(overall_text, report)
    # 系统排除提示：正文由 LLM 基于原始分析生成，可能引用已被系统复核排除的条目
    # （如归母口径复核移入备查录的未分配利润勾稽条目），与最终清单口径冲突（实测缺陷）
    _excluded = report.get("excluded_items", []) or []
    if _excluded:
        _ids = "、".join(str(r.get("risk_id", "")) for r in _excluded
                         if isinstance(r, dict) and r.get("risk_id"))
        overall_text = (str(overall_text) + "\n\n【系统复核提示】正文结论由 LLM 基于原始分析生成，"
                        f"可能引用已被系统复核排除的条目（{_ids}，详见风险总览备查说明）；"
                        "风险清单与统计以最终明细为准。")
    ws_assessment["A3"] = overall_text
    ws_assessment["A3"].font = normal_font
    ws_assessment["A3"].alignment = wrap_alignment
    ws_assessment.column_dimensions["A"].width = 100
    ws_assessment.column_dimensions["B"].width = 40
    # 行高自适应：整体结论通常为大段文本，按内容估算防裁剪（下限/上限放宽）
    ws_assessment.row_dimensions[3].height = _estimate_row_height(
        [overall_text], [100], min_h=150, max_h=600)

    # 注记区（50b/50c）：信披一致性 + 风险等级底线 + 跨期穿透三条确定性注记，
    # 动态行号依次写入，AI 声明与行业基准行号随之下移。
    _note_row = 4
    _notes = [
        ("【披露合规一致性提示】", report.get("disclosure_consistency_note", "") or ""),
        ("【风险等级底线提示】", report.get("level_floor_note", "") or ""),
        ("【跨期穿透提示】", report.get("cashflow_penetration_note", "") or ""),
    ]
    for _label, _text in _notes:
        if not _text:
            continue
        ws_assessment.merge_cells(f"A{_note_row}:B{_note_row}")
        ws_assessment[f"A{_note_row}"] = f"{_label}{_text}"
        ws_assessment[f"A{_note_row}"].font = Font(name="微软雅黑", size=9, bold=True, color="CC6600")
        ws_assessment[f"A{_note_row}"].alignment = wrap_alignment
        ws_assessment.row_dimensions[_note_row].height = 28
        _note_row += 1

    # AI 辅助生成声明行（与风险总览 Sheet 保持一致）
    ws_assessment.merge_cells(f"A{_note_row}:B{_note_row}")
    ws_assessment[f"A{_note_row}"] = AI_DISCLAIMER
    ws_assessment[f"A{_note_row}"].font = Font(name="微软雅黑", size=9, bold=True, color="CC0000")
    ws_assessment[f"A{_note_row}"].alignment = Alignment(wrap_text=True, vertical="center")
    ws_assessment.row_dimensions[_note_row].height = 36
    _note_row += 1

    # ── 行业基准对比（可选）：仅当报告数据中包含 industry_benchmark 时写入 ──
    if "industry_benchmark" in report:
        benchmark = report["industry_benchmark"]
        # 行业基准从注记/声明行之后开始（留一行间隔）
        start_row = _note_row + 1
        ws_assessment[f"A{start_row}"] = "行业基准对比"
        ws_assessment[f"A{start_row}"].font = Font(name="微软雅黑", size=12, bold=True)
        start_row += 1
        # 遍历基准数据（嵌套字典：大类 → 子项 → 值；标量/列表直接写为字符串）
        for key, val in benchmark.items():
            if isinstance(val, dict):
                # 写入大类标题（加粗）
                ws_assessment[f"A{start_row}"] = key
                ws_assessment[f"A{start_row}"].font = Font(name="微软雅黑", size=10, bold=True)
                start_row += 1
                # 逐行写入子项数据
                for k, v in val.items():
                    ws_assessment[f"A{start_row}"] = f"  {k}"
                    ws_assessment[f"B{start_row}"] = str(v)
                    ws_assessment[f"A{start_row}"].font = normal_font
                    ws_assessment[f"B{start_row}"].font = normal_font
                    start_row += 1
            else:
                # 非字典值（标量/列表）：键值同行写入，避免被静默跳过
                ws_assessment[f"A{start_row}"] = key
                ws_assessment[f"B{start_row}"] = str(val)
                ws_assessment[f"A{start_row}"].font = Font(name="微软雅黑", size=10, bold=True)
                ws_assessment[f"B{start_row}"].font = normal_font
                start_row += 1

    # ── 保存文件并上传到存储 ──
    # 生成统一文件名前缀
    prefix = _build_file_prefix(report)

    # ═══════════════════════════════════════════════════════
    # Sheet 4：审计建议汇总 —— 将所有风险的 audit_suggestion 集中展示
    # ═══════════════════════════════════════════════════════
    ws_suggestions = wb.create_sheet("审计建议汇总")
    # 表头：风险ID + 风险标题 + 等级 + 审计建议
    sug_headers = ["风险ID", "风险标题", "风险等级", "审计核查建议"]
    sug_widths = [10, 30, 10, 80]
    for col_idx, (header, width) in enumerate(zip(sug_headers, sug_widths), start=1):
        cell = ws_suggestions.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
        ws_suggestions.column_dimensions[get_column_letter(col_idx)].width = width
    # 冻结表头：滚动查看审计建议时首行表头始终可见
    ws_suggestions.freeze_panes = "A2"
    # 逐行写入每条风险的审计建议
    sug_row = 2
    for risk in risk_details:
        suggestion = risk.get("audit_suggestion", "")
        if suggestion:
            ws_suggestions.cell(row=sug_row, column=1, value=risk.get("risk_id", "")).font = normal_font
            ws_suggestions.cell(row=sug_row, column=2, value=risk.get("title", "")).font = normal_font
            lv = risk.get("level", "")
            lv_cell = ws_suggestions.cell(row=sug_row, column=3, value=lv)
            lv_cell.font = normal_font
            if lv in level_colors:
                lv_cell.fill = level_colors[lv]
                lv_cell.font = level_fonts[lv]
            sug_cell = ws_suggestions.cell(row=sug_row, column=4, value=suggestion)
            sug_cell.font = normal_font
            sug_cell.alignment = wrap_alignment
            for c in range(1, 5):
                ws_suggestions.cell(row=sug_row, column=c).border = thin_border
            # 行高自适应：审计建议列（80 宽）内容通常较长，按内容估算防裁剪
            ws_suggestions.row_dimensions[sug_row].height = _estimate_row_height(
                [risk.get("risk_id", ""), risk.get("title", ""), lv, suggestion],
                sug_widths, min_h=40)
            sug_row += 1

    # 若未指定输出路径，使用系统临时目录
    if not output_path:
        output_path = os.path.join(tempfile.gettempdir(), f"{prefix}_审计底稿.xlsx")

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # ── 领域约束后置校验：导出内容必须包含 AI 免责声明 ──
    # 在落盘/上传前拦截：遍历所有工作表单元格文本，若声明缺失则不产出文件，
    # 直接返回带修复方向的错误，避免生成看似正式审计意见的无声明底稿。
    from tools.domain_guard import assert_disclaimer_present, DisclaimerMissingError
    cell_texts = [
        v
        for ws in wb.worksheets
        for row in ws.iter_rows(values_only=True)
        for v in row
        if isinstance(v, str)
    ]
    try:
        assert_disclaimer_present(cell_texts, doc_kind="Excel审计底稿")
    except DisclaimerMissingError as e:
        logger.error(f"Excel导出被拦截: {e}")
        return f"导出被拦截：{e}"

    wb.save(output_path)

    # 上传到本地存储，返回 HTTP 可访问 URL
    from local_storage import upload_file_to_storage
    download_url = upload_file_to_storage(
        local_path=output_path,
        file_name=f"reports/{prefix}_审计底稿.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # 根据 URL 格式返回不同格式的提示信息
    if download_url.startswith("/") or download_url.startswith("http") or download_url.startswith("file://"):
        return f"Excel审计底稿已生成，下载链接: {download_url}"
    return f"Excel审计底稿已生成(本地): {output_path}"


@tool
def export_excel_report(risk_report_json: str, validation_json: str = "",
                        financial_indicators_json: str = "",
                        disclosure_check_json: str = "",
                        audit_opinion_json: str = "") -> str:
    """将风险台账 JSON 数据导出为 Excel 审计底稿文件，上传到对象存储并返回可下载 URL。

    生成的 Excel 包含三个工作表：
    1. 风险总览：公司基本信息、AI 辅助生成声明和风险统计摘要
    2. 风险明细：每条风险的详细信息，风险等级用颜色标注（重大红色/重要橙色/一般蓝色）
    3. 整体评估：整体风险评估结论、AI 辅助生成声明和行业基准对比（可选）

    Args:
        risk_report_json: 风险台账的 JSON 字符串，包含以下字段：
            - company_info: 公司基本信息（名称、股票代码、行业等）
            - risk_summary: 风险统计（total_risks/major_risks/important_risks/general_risks）
            - risk_details: 风险明细列表（每条含 risk_id/dimension/title/level/evidence 等）
            - overall_assessment: 整体风险评估结论文本
            - industry_benchmark: 行业基准对比（可选）

    Returns:
        含下载链接的提示文本，或错误信息
    """
    return _export_excel_impl(risk_report_json, validation_json=validation_json,
                              financial_indicators_json=financial_indicators_json,
                              disclosure_check_json=disclosure_check_json,
                              audit_opinion_json=audit_opinion_json)
