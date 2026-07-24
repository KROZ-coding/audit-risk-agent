"""PDF风险报告导出工具 - 将风险台账JSON导出为格式化PDF

本模块使用 reportlab 库生成专业格式的审计风险评估报告 PDF 文件，包含以下章节：
1. 封面页：公司名称、股票代码、报告年度、行业分类、审计意见 + AI 声明
2. 风险总览：风险统计摘要 + 五维度风险分布表
3. 风险清单摘要：所有风险的 ID/维度/标题/等级 一览表
4. 风险明细：每条风险的详细信息（证据/数据分析/法规/案例/建议/趋势分析）
5. 整体评估结论 + 行业基准对比（可选）

每页底部自动添加 AI 辅助生成声明水印（灰色小字）。
"""
import os
import json
import uuid
import logging
import tempfile
from datetime import datetime

from utils.filename import sanitize_filename

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# AI 辅助生成免责声明，出现在封面页和每页页脚水印中
AI_DISCLAIMER = "【AI 辅助生成】本报告由大语言模型基于公开数据自动生成，可能存在幻觉或偏差，请务必结合人工专业判断进行复核。"

# ── 字体 & 样式 ────────────────────────────────────────────

def _register_chinese_font():
    """注册中文字体，按优先级依次尝试：

    1. 项目 assets 目录下的文泉驿微米黑 (wqy-microhei.ttc)
    2. Linux 系统字体目录下的文泉驿正黑/微米黑
    3. reportlab 内置的 STSong-Light CID 字体（回退方案）
    4. Helvetica（最终兜底，中文将显示异常）

    Returns:
        成功注册的字体名称字符串，供 ParagraphStyle 和 TableStyle 引用
    """
    # 从环境变量或默认路径获取工作目录
    workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), "..", ".."))
    # 按优先级依次尝试注册 TrueType 字体文件
    for fp in [
        os.path.join(workspace, "assets", "wqy-microhei.ttc"),
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("ChineseFont", fp))
                return "ChineseFont"
            except Exception as e:
                logger.warning(f"字体注册失败 {fp}: {e}")
                continue
    # TrueType 全部失败，尝试 reportlab 内置 CID 字体
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception as e:
        logger.warning(f"CID字体注册失败: {e}")
    # 最终兜底：使用 Helvetica（中文将无法正确显示）
    logger.warning("所有中文字体注册失败，PDF中文可能显示异常")
    return "Helvetica"


def _build_styles(font_name):
    """构建报告所需的全部 Paragraph 样式集合。

    包含 6 种预定义样式：
    - title: 封面标题（22pt 居中）
    - h1: 一级标题（16pt 深蓝色）
    - h2: 二级标题（13pt 深蓝色）
    - body: 正文（10pt 两端对齐）
    - small: 辅助文字（9pt 用于标签和元信息）
    - center: 居中正文（11pt）

    Args:
        font_name: 已注册的中文字体名称

    Returns:
        样式字典，键为样式名，值为 ParagraphStyle 对象
    """
    s = getSampleStyleSheet()
    return {
        'title': ParagraphStyle("CNTitle", parent=s["Title"], fontName=font_name,
                                fontSize=22, leading=28, alignment=TA_CENTER, spaceAfter=20),
        'h1': ParagraphStyle("CNH1", parent=s["Heading1"], fontName=font_name,
                             fontSize=16, leading=22, spaceBefore=15, spaceAfter=10,
                             textColor=HexColor("#2F5496")),
        'h2': ParagraphStyle("CNH2", parent=s["Heading2"], fontName=font_name,
                             fontSize=13, leading=18, spaceBefore=10, spaceAfter=8,
                             textColor=HexColor("#2F5496")),
        'body': ParagraphStyle("CNBody", parent=s["Normal"], fontName=font_name,
                               fontSize=10, leading=15, alignment=TA_JUSTIFY, spaceAfter=6),
        'small': ParagraphStyle("CNSmall", parent=s["Normal"], fontName=font_name,
                                fontSize=9, leading=13, spaceAfter=4),
        'center': ParagraphStyle("CNCenter", parent=s["Normal"], fontName=font_name,
                                 fontSize=11, leading=16, alignment=TA_CENTER, spaceAfter=8),
    }


def _styled_table(data, col_widths, font_name, font_size=10, header_bg=HexColor("#2F5496"),
                  level_cols=None, level_colors_map=None, right_align_cols=None):
    """构建带统一样式的表格。

    自动应用以下样式：全局字体/字号、灰色网格线、垂直居中、内边距。
    可选：深蓝色表头背景+白色文字、风险等级列的颜色标注、右对齐列。

    Args:
        data: 二维列表，第一行为表头
        col_widths: 各列宽度列表
        font_name: 字体名称
        font_size: 字号（默认 10pt）
        header_bg: 表头背景色（默认深蓝 #2F5496），设为 None 则不渲染表头样式
        level_cols: 风险等级列数据，格式 [(col_idx, row_data_list)]
        level_colors_map: 风险等级 → 颜色 映射表
        right_align_cols: 需要右对齐的列，格式 [(col_idx, _)]

    Returns:
        已完成样式设置的 Table 对象
    """
    t = Table(data, colWidths=col_widths)
    # 基础样式：字体、字号、网格线、垂直居中、内边距
    cmds = [
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    # 表头样式：深蓝色背景 + 白色文字
    if header_bg:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ]
    # 右对齐列
    for col, _ in (right_align_cols or []):
        cmds.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    # 风险等级列：根据等级值（重大/重要/一般）设置文字颜色
    if level_cols and level_colors_map:
        for col_idx, rows in level_cols:
            for ri, val in enumerate(rows, start=1):
                if val in level_colors_map:
                    cmds.append(("TEXTCOLOR", (col_idx, ri), (col_idx, ri), level_colors_map[val]))
    t.setStyle(TableStyle(cmds))
    return t


# ── 核心导出逻辑 ──────────────────────────────────────────

def _build_file_prefix(report: dict) -> str:
    """根据报告内容生成统一的文件名前缀：日期_公司名_年份。

    使用 sanitize_filename 清洗公司名和年份，移除 Windows 非法字符。

    Args:
        report: 包含 company_info 的风险台账字典

    Returns:
        格式为 "YYYYMMDD_公司名_年份" 的安全文件名前缀
    """
    ci = report.get("company_info", {})
    company = sanitize_filename(ci.get("company_name", "未知公司"))
    year = sanitize_filename(ci.get("report_year", ""))
    date_str = datetime.now().strftime("%Y%m%d")
    if year:
        return f"{date_str}_{company}_{year}"
    return f"{date_str}_{company}"


def _export_pdf_impl(risk_report_json: str, output_path: str = None) -> str:
    """将风险台账 JSON 导出为格式化 PDF 风险报告的核心实现。

    报告结构（共 5 大章节）：
    1. 封面页：标题 + 公司信息表 + AI 辅助生成声明
    2. 风险总览：统计摘要文本 + 五维度风险分布表 + 风险清单摘要表
    3. 风险明细：逐条展示风险的证据/数据分析/法规/案例/建议/趋势分析
    4. 整体评估结论：完整的风险评估结论文本
    5. 行业基准对比（可选）：与行业基准的横向比较

    每页底部自动渲染 AI 辅助生成声明水印（灰色 7pt 小字）。

    Args:
        risk_report_json: 风险台账 JSON 字符串或已解析的字典，包含：
            - company_info: 公司基本信息
            - risk_summary: 风险统计（total/major/important/general_risks + risk_dimensions）
            - risk_details: 风险明细列表
            - overall_assessment: 整体评估结论文本
            - industry_benchmark: 行业基准对比（可选）
        output_path: 可选的输出文件路径，缺省时使用系统临时目录

    Returns:
        成功时返回含下载链接的提示文本，失败时返回错误信息
    """
    # 第一步：解析输入 JSON，兼容字符串和已解析字典
    try:
        report = json.loads(risk_report_json) if isinstance(risk_report_json, str) else risk_report_json
    except json.JSONDecodeError as e:
        return f"JSON解析失败: {e}"

    # 注册中文字体并构建样式集合
    font = _register_chinese_font()
    st = _build_styles(font)
    elements = []   # PDF 文档元素列表（Paragraph / Table / Spacer / PageBreak 等）

    # 提取报告三大核心数据块
    ci = report.get("company_info", {})     # 公司基本信息
    rs = report.get("risk_summary", {})     # 风险统计摘要
    rd = report.get("risk_details", [])     # 风险明细列表

    # 风险等级颜色映射：重大(红)、重要(橙)、一般(蓝)
    level_colors = {"重大": HexColor("#FF0000"), "重要": HexColor("#FF8C00"), "一般": HexColor("#4472C4")}

    # ═══════════════════════════════════════════════════════
    # 章节1：封面页 —— 标题 + 公司信息表 + AI 声明
    # ═══════════════════════════════════════════════════════
    elements += [Spacer(1, 80), Paragraph("上市公司年报", st['title']),
                 Paragraph("审计风险评估报告", st['title']), Spacer(1, 40)]
    # 构建封面信息表（6 行 × 2 列：标签 + 值）
    cover = [
        ["公司名称", ci.get("company_name", "未提供")],
        ["股票代码", ci.get("stock_code", "未提供")],
        ["报告年度", ci.get("report_year", "未提供")],
        ["行业分类", ci.get("industry", "未提供")],
        ["审计意见", ci.get("audit_opinion", "未提供")],
        ["报告日期", datetime.now().strftime("%Y年%m月%d日")],
    ]
    ct = Table(cover, colWidths=[120, 250])
    ct.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, 0), (0, -1), HexColor("#2F5496")),  # 标签列深蓝色
        ("ALIGN", (0, 0), (0, -1), "RIGHT"),     # 标签列右对齐
        ("ALIGN", (1, 0), (1, -1), "LEFT"),      # 值列左对齐
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, HexColor("#CCCCCC")),  # 行间分割线
    ]))
    elements += [ct, Spacer(1, 30)]

    # 封面底部 AI 辅助生成声明（红色 9pt 居中）
    disclaimer_style = ParagraphStyle("Disclaimer", parent=st['small'], fontName=font,
                                       fontSize=9, leading=14, alignment=TA_CENTER,
                                       textColor=HexColor("#CC0000"), spaceAfter=6)
    elements += [
        Spacer(1, 20),
        Paragraph(AI_DISCLAIMER, disclaimer_style),
        PageBreak()   # 封面页结束，强制分页
    ]

    # ═══════════════════════════════════════════════════════
    # 目录页 —— 报告章节导航
    # ═══════════════════════════════════════════════════════
    elements += [Paragraph("目  录", st['title']), Spacer(1, 30)]
    toc_items = [
        ("一、风险总览", "风险统计摘要、五维度分布、风险清单"),
        ("二、风险明细", f"共 {len(rd)} 条风险的详细分析"),
        ("三、整体风险评估结论", "综合评估意见与审计建议"),
    ]
    if "industry_benchmark" in report:
        toc_items.append(("四、行业基准对比", "与行业平均水平的横向比较"))
    toc_data = [["章节", "内容说明"]]
    for title, desc in toc_items:
        toc_data.append([title, desc])
    elements += [_styled_table(toc_data, [180, 250], font, font_size=11), PageBreak()]

    # ═══════════════════════════════════════════════════════
    # 章节2：风险总览 —— 统计摘要 + 五维度分布表 + 清单摘要
    # ═══════════════════════════════════════════════════════
    elements += [Paragraph("一、风险总览", st['h1']), Spacer(1, 10)]
    # 风险统计摘要文本（总数 + 各级别数量，重大/重要/一般用颜色区分）
    summary = (
        f"本次审计风险识别共发现 <b>{rs.get('total_risks', 0)}</b> 项风险，"
        f"其中重大风险 <b><font color='red'>{rs.get('major_risks', 0)}</font></b> 项，"
        f"重要风险 <b><font color='#FF8C00'>{rs.get('important_risks', 0)}</font></b> 项，"
        f"一般风险 <b><font color='#4472C4'>{rs.get('general_risks', 0)}</font></b> 项。"
    )
    elements += [Paragraph(summary, st['body']), Spacer(1, 10)]

    # 五维度风险分布表（兼容 LLM 返回字典/列表/整数等多种格式）
    dim_map = {"financial_misstatement": "财务错报风险", "related_party": "关联交易风险",
               "disclosure_compliance": "信息披露合规风险", "going_concern": "持续经营风险",
               "regulatory_penalty": "监管处罚类高风险"}
    dims = rs.get("risk_dimensions", {})
    dim_data = [["风险维度", "风险数量"]]
    for k, v in dims.items():
        # 兼容大模型返回的各种数据格式：数字直接用、字典取 count 字段、列表取长度
        if isinstance(v, (int, float)):
            count = v
        elif isinstance(v, dict):
            count = v.get("count", v.get("数量", len(v)))
        elif isinstance(v, list):
            count = len(v)
        else:
            count = 0

        if count > 0:
            dim_data.append([dim_map.get(k, k), str(count)])
    # 仅当有维度数据时才渲染分布表
    if len(dim_data) > 1:
        elements += [_styled_table(dim_data, [200, 100], font, font_size=10), Spacer(1, 15)]

    # 风险清单摘要表（ID + 维度 + 标题 + 等级，等级列按颜色标注）
    elements += [Paragraph("风险清单摘要", st['h2'])]
    rows = [["风险ID", "维度", "标题", "等级"]]
    levels_col = []
    for r in rd:
        rows.append([r.get("risk_id", ""), r.get("dimension", ""), r.get("title", ""), r.get("level", "")])
        levels_col.append(r.get("level", ""))
    elements += [_styled_table(rows, [50, 90, 180, 50], font, font_size=9,
                               level_cols=[(3, levels_col)], level_colors_map=level_colors),
                 PageBreak()]

    # ═══════════════════════════════════════════════════════
    # 章节3：风险明细 —— 逐条展示每条风险的完整信息
    # ═══════════════════════════════════════════════════════
    elements += [Paragraph("二、风险明细", st['h1']), Spacer(1, 10)]
    for risk in rd:
        lv = risk.get("level", "")
        lc = level_colors.get(lv, black)   # 等级对应颜色，默认黑色
        # 每条风险的头部信息：编号+标题 + 维度/等级/置信度
        re = [
            Paragraph(f"{risk.get('risk_id','')} {risk.get('title','')}", st['h2']),
            Paragraph(f"<b>风险维度：</b>{risk.get('dimension','')}  |  <b>风险等级：</b>"
                      f"<font color='{lc.hexval()}'>{lv}</font>  |  <b>置信度：</b>{risk.get('confidence',0)}",
                      st['small']), Spacer(1, 5),
        ]
        # 按固定顺序渲染风险的 5 个详细字段（仅在字段非空时显示）
        for label, key in [("年报原文证据", "evidence"), ("异常数据分析", "data_analysis"),
                            ("法规依据", "regulatory_basis"), ("案例参考", "case_reference"),
                            ("审计核查建议", "audit_suggestion")]:
            v = risk.get(key, "")
            if v:
                re += [Paragraph(f"<b>{label}：</b>", st['small']), Paragraph(v, st['body']), Spacer(1, 3)]
        # 渲染思维链（reasoning_chain）：结构化展示推理过程
        chain = risk.get("reasoning_chain", [])
        if chain and isinstance(chain, list):
            re += [Paragraph("<b>推理思维链：</b>", st['small']), Spacer(1, 2)]
            for step_item in chain:
                if isinstance(step_item, dict):
                    step_label = step_item.get("step", "")
                    step_detail = step_item.get("detail", "")
                    if step_label and step_detail:
                        re += [Paragraph(f"  [{step_label}] {step_detail}", st['body'])]
            re += [Spacer(1, 3)]
        # 趋势分析（可选字段）
        if risk.get("trend_analysis"):
            re += [Paragraph("<b>趋势分析：</b>", st['small']),
                   Paragraph(str(risk["trend_analysis"]), st['body']), Spacer(1, 3)]
        # KeepTogether 确保单条风险不会被分页截断
        elements.append(KeepTogether(re + [Spacer(1, 10)]))

    # ═══════════════════════════════════════════════════════
    # 章节4：整体评估结论
    # ═══════════════════════════════════════════════════════
    elements += [PageBreak(), Paragraph("三、整体风险评估结论", st['h1']), Spacer(1, 10),
                 Paragraph(report.get("overall_assessment", ""), st['body'])]

    # ═══════════════════════════════════════════════════════
    # 章节5：行业基准对比（可选）
    # ═══════════════════════════════════════════════════════
    if "industry_benchmark" in report:
        elements += [Spacer(1, 15), Paragraph("四、行业基准对比", st['h1'])]
        # 遍历基准数据（嵌套字典：大类 → 子项 → 值）
        for k, v in report["industry_benchmark"].items():
            if isinstance(v, dict):
                elements.append(Paragraph(f"<b>{k}</b>", st['body']))
                for sk, sv in v.items():
                    elements.append(Paragraph(f"  {sk}: {sv}", st['small']))

    # ═══════════════════════════════════════════════════════
    # 免责声明页 —— 独立章节，明确 AI 生成性质与使用限制
    # ═══════════════════════════════════════════════════════
    elements += [PageBreak(), Paragraph("免责声明", st['h1']), Spacer(1, 15)]
    disclaimer_paragraphs = [
        "本报告由人工智能大语言模型基于公开数据自动生成，属于 AI 辅助分析工具的输出结果，"
        "不构成任何注册会计师审计意见、鉴证结论或投资建议。",
        "AI 模型可能存在幻觉（Hallucination）或推理偏差，报告中的风险判定、法规引用及数据分析"
        "均需经具备专业资质的审计人员复核确认后方可作为决策依据。",
        "本报告所引用的数据来源包括：巨潮资讯网上市公司公开年报、中国证监会近五年行政处罚决定书、"
        "上海/深圳证券交易所问询函、中国审计准则及企业会计准则电子版。所有数据均为合法公开信息。",
        "使用者不得将本报告用于对特定上市公司或个人作出未经核实的负面评价，亦不得将其作为"
        "证券买卖、信贷审批等商业决策的唯一依据。",
        "如需正式审计意见，请委托具备证券期货相关业务资格的会计师事务所执行独立审计程序。",
    ]
    for para in disclaimer_paragraphs:
        elements += [Paragraph(para, st['body']), Spacer(1, 8)]

    # ── 领域约束后置校验：导出内容必须包含 AI 免责声明 ──
    # 在真正构建/落盘 PDF 前拦截：若模板被误改导致声明缺失，则不产出文件，
    # 直接返回带修复方向的错误，避免生成看似正式审计意见的无声明报告。
    from tools.domain_guard import collect_flowable_texts, assert_disclaimer_present, DisclaimerMissingError
    try:
        assert_disclaimer_present(collect_flowable_texts(elements), doc_kind="PDF风险报告")
    except DisclaimerMissingError as e:
        logger.error(f"PDF导出被拦截: {e}")
        return f"导出被拦截：{e}"

    # ── 构建 PDF 文件 ──
    prefix = _build_file_prefix(report)
    # 输出路径：使用指定路径或系统临时目录
    out = output_path or os.path.join(tempfile.gettempdir(), f"{prefix}_审计风险报告.pdf")
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2.5*cm, bottomMargin=2.5*cm)

    # 页脚水印回调：每页底部居中渲染灰色 AI 声明小字 + 页码
    def _add_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(font, 7)                          # 7pt 灰色小字
        canvas.setFillColor(HexColor("#999999"))
        canvas.drawCentredString(A4[0] / 2, 1.2 * cm, AI_DISCLAIMER)
        # 页码：右下角显示“第 X 页”
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"第 {doc_obj.page} 页")
        canvas.restoreState()

    # 构建 PDF，首页和后续页均应用页脚水印
    doc.build(elements, onFirstPage=_add_footer, onLaterPages=_add_footer)

    # 上传到本地存储，返回 HTTP 可访问 URL
    from local_storage import upload_file_to_storage
    url = upload_file_to_storage(out, f"reports/{prefix}_审计风险报告.pdf", "application/pdf")
    # 根据 URL 格式返回不同格式的提示信息
    if url.startswith("/") or url.startswith("http") or url.startswith("file://"):
        return f"PDF风险报告已生成，下载链接: {url}"
    return f"PDF风险报告已生成(本地): {out}"


@tool
def export_pdf_report(risk_report_json: str) -> str:
    """将风险台账 JSON 导出为 PDF 风险报告，上传到存储并返回下载链接。

    报告包含五个章节：
    1. 封面页（公司信息表 + AI 辅助生成声明）
    2. 风险总览（统计摘要 + 五维度分布表 + 清单摘要）
    3. 风险明细（每条风险的证据/数据分析/法规/案例/建议）
    4. 整体评估结论
    5. 行业基准对比（可选）

    每页底部自动渲染 AI 辅助生成声明水印。

    Args:
        risk_report_json: 风险台账 JSON 字符串，包含 company_info、risk_summary、
            risk_details、overall_assessment 等字段

    Returns:
        含下载链接的提示文本，或错误信息
    """
    return _export_pdf_impl(risk_report_json)
