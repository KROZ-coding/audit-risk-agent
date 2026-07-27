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
import tempfile
from datetime import datetime

from utils.filename import resolve_company_year, sanitize_filename

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# AI 辅助生成免责声明，强制出现在封面和整体评估 Sheet 中
AI_DISCLAIMER = "【AI 辅助生成】本报告由大语言模型基于公开数据自动生成，可能存在幻觉或偏差，请务必结合人工专业判断进行复核。"


def _build_file_prefix(report: dict) -> str:
    """根据报告内容生成统一的文件名前缀：日期_公司名_年份。

    使用 sanitize_filename 清洗公司名和年份，移除 Windows 非法字符。

    Args:
        report: 包含 company_info 的风险台账字典

    Returns:
        格式为 "YYYYMMDD_公司名_年份" 的安全文件名前缀；
        若无年份信息则省略年份部分
    """
    ci = report.get("company_info", {})
    # 别名兼容：LLM 可能用 name/report_period 等键名，避免文件名变「未知公司_未知」
    raw_company, raw_year = resolve_company_year(ci)
    company = sanitize_filename(raw_company or "未知公司")
    year = sanitize_filename(raw_year) if raw_year else ""
    date_str = datetime.now().strftime("%Y%m%d")
    if year:
        return f"{date_str}_{company}_{year}"
    return f"{date_str}_{company}"


def _export_excel_impl(risk_report_json: str, output_path: str = None) -> str:
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
    for i, (label, value) in enumerate(info_rows, start=3):
        # A 列放标签（加粗），C 列放值（常规）
        ws_summary[f"A{i}"] = label
        ws_summary[f"A{i}"].font = Font(name="微软雅黑", size=10, bold=True)
        ws_summary[f"C{i}"] = value
        ws_summary[f"C{i}"].font = normal_font

    # ── AI 辅助生成声明行（红色加粗，合并 A~F 列）──
    disclaimer_row = len(info_rows) + 4
    ws_summary.merge_cells(f"A{disclaimer_row}:F{disclaimer_row}")
    ws_summary[f"A{disclaimer_row}"] = AI_DISCLAIMER
    ws_summary[f"A{disclaimer_row}"].font = Font(name="微软雅黑", size=9, bold=True, color="CC0000")
    ws_summary[f"A{disclaimer_row}"].alignment = Alignment(wrap_text=True, vertical="center")
    ws_summary.row_dimensions[disclaimer_row].height = 36

    # ── 风险统计摘要：显示风险总数和各级别数量 ──
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
        ws_summary[f"C{i}"] = value
        ws_summary[f"C{i}"].font = normal_font

    # ═══════════════════════════════════════════════════════
    # Sheet 2：风险明细表 —— 每条风险的 11 列详细信息
    # ═══════════════════════════════════════════════════════
    ws_detail = wb.create_sheet("风险明细")

    # 定义表头和各列宽度（共 11 列，新增推理思维链）
    headers = ["风险ID", "风险维度", "风险标题", "风险等级", "证据", "数据分析",
               "法规依据", "案例参考", "审计建议", "置信度", "推理思维链"]
    col_widths = [10, 15, 25, 10, 40, 40, 35, 35, 40, 10, 50]

    # 写入表头行（第 1 行），设置深蓝色背景 + 白色粗体
    for col_idx, (header, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws_detail.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
        ws_detail.column_dimensions[get_column_letter(col_idx)].width = width

    ws_detail.row_dimensions[1].height = 25

    # 逐行写入风险明细数据（从第 2 行开始）
    risk_details = report.get("risk_details", [])
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
            risk.get("risk_id", ""),        # 风险编号（如 R001）
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

        # 设置行高为 60，确保长文本内容可见
        ws_detail.row_dimensions[row_idx].height = 60

    # ═══════════════════════════════════════════════════════
    # Sheet 3：整体评估 —— 风险结论 + AI 声明 + 行业基准对比
    # ═══════════════════════════════════════════════════════
    ws_assessment = wb.create_sheet("整体评估")

    # 写入标题和整体风险评估结论文本
    ws_assessment.merge_cells("A1:B1")
    ws_assessment["A1"] = "整体风险评估结论"
    ws_assessment["A1"].font = Font(name="微软雅黑", size=12, bold=True)
    ws_assessment["A3"] = report.get("overall_assessment", "")
    ws_assessment["A3"].font = normal_font
    ws_assessment["A3"].alignment = wrap_alignment
    ws_assessment.column_dimensions["A"].width = 100
    ws_assessment.row_dimensions[3].height = 150

    # AI 辅助生成声明行（与风险总览 Sheet 保持一致）
    ws_assessment.merge_cells("A5:B5")
    ws_assessment["A5"] = AI_DISCLAIMER
    ws_assessment["A5"].font = Font(name="微软雅黑", size=9, bold=True, color="CC0000")
    ws_assessment["A5"].alignment = Alignment(wrap_text=True, vertical="center")
    ws_assessment.row_dimensions[5].height = 36

    # ── 行业基准对比（可选）：仅当报告数据中包含 industry_benchmark 时写入 ──
    if "industry_benchmark" in report:
        benchmark = report["industry_benchmark"]
        # 行业基准从第 8 行开始（第 5 行为声明，6~7 行留间隔）
        start_row = 8
        ws_assessment[f"A{start_row}"] = "行业基准对比"
        ws_assessment[f"A{start_row}"].font = Font(name="微软雅黑", size=12, bold=True)
        start_row += 1
        # 遍历基准数据（嵌套字典：大类 → 子项 → 值）
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
            ws_suggestions.row_dimensions[sug_row].height = 40
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
def export_excel_report(risk_report_json: str) -> str:
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
    return _export_excel_impl(risk_report_json)
