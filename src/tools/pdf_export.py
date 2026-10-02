"""PDF风险报告导出工具 - 将风险台账JSON导出为格式化PDF（专业版）

默认拆分导出：一次生成 3 份独立 PDF 报告（老师反馈：拆分更清晰、每份配封皮目录）——
1. 财务健康诊断报告：公司简介 + 财务指标与审计关注分析（含指标对比图/雷达图）+ 财务维度风险明细
2. 合规与信息披露报告：公司简介 + 披露规范性检查结果 + 合规标准对照 + 合规维度风险明细
3. 综合汇总报告：公司简介 + 综合评分解读（仪表盘）+ 风险总览 + 交叉验证/风险传导链
   + 全部风险明细 + 整体结论 + 行业基准对比 + 风险评估方法论

每份 PDF 均含：封面页（色带封皮）、动态目录页、AI 辅助生成声明、三段式页脚
（报告名/机密标注/页码）与免责声明页。风险明细采用「阿拉伯序号.R编号｜结论式标题
→ (1)-(5) 字段编号 → 多行内容 (1)(2) 子编号」的三级编号结构。

内容丰富化采用代码模板驱动：指标判读/评分解读/方法论均为确定性模板生成，
不依赖 LLM 二次输出，分析耗时不受影响。

兼容模式：显式传入 output_path 时按旧结构生成单份汇总 PDF（测试与旧调用路径不受影响）。
"""
import os
import re
import copy
import json
import math
import uuid
import logging
import tempfile
from datetime import datetime

from utils.filename import build_file_prefix as _build_file_prefix

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether,
    Image as RLImage, HRFlowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from langchain_core.tools import tool

from tools.visualizer import _generate_risk_heatmap, _generate_radar_chart, _setup_chinese_font
from tools.risk_scorer import RISK_LEVELS, WEIGHTS
from core.result_contract import risk_level_label
from core.benchmark_contract import sourced_benchmark_value

logger = logging.getLogger(__name__)

# AI 辅助生成免责声明，出现在封面页与免责声明页
AI_DISCLAIMER = ("【AI 辅助生成】本报告为AI辅助分析工具输出，不构成注册会计师审计意见、鉴证结论或投资建议。"
                 "系统采信仅表示风险线索通过系统检查，不等同于确认审计结论或违规；待复核提示及暂定等级须经人工复核。")

# 中文数字序号（章节编号：一、二、三…），覆盖到二十；超出时回退阿拉伯数字
_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
           "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]

# ── 专业配色体系（借鉴专业审计/法律报告 PDF 规范） ─────────
PRIMARY = HexColor("#1F3864")       # 主色：深藏蓝（封面色带/表头/标题）
ACCENT = HexColor("#2F5496")        # 辅色：标题下划线/标签
ZEBRA = HexColor("#F2F5FA")         # 表格斑马纹底色
LINE_GRAY = HexColor("#D9D9D9")     # 分隔线灰
TEXT_MUTED = HexColor("#595959")    # 弱化文字
GOOD_GREEN = HexColor("#388E3C")    # 正向/低风险
WARN_AMBER = HexColor("#F9A825")    # 关注/中等
WARN_ORANGE = HexColor("#E8710A")   # 偏高/高
BAD_RED = HexColor("#D32F2F")       # 负向/极高
TEXT_RED = "#C0392B"                # 判读警示文字（Paragraph 内联用）

# 风险等级颜色映射：重大(红)、重要(橙)、一般(蓝)——语义保持不变（历史测试依赖）
# 图表等级配色（计划口径）：重大红色、重要黄色、一般蓝色，且一律保留文字标识，
# 不以颜色作为唯一信号。纯黄在白底文字上对比度不足，故文字色另设深黄变体：
# 填充/图例用标准黄，正文文字用深黄，两处均为黄色系，不退回橙色。
LEVEL_COLORS = {"重大": HexColor("#FF0000"), "重要": HexColor("#FFD400"),
                "一般": HexColor("#4472C4")}
LEVEL_TEXT_COLORS = {"重大": HexColor("#FF0000"), "重要": HexColor("#B8860B"),
                     "一般": HexColor("#4472C4")}

# 综合评分等级标识 → 颜色（方法论/评分解读用）
SCORE_LEVEL_COLORS = {"low": GOOD_GREEN, "medium": WARN_AMBER, "high": WARN_ORANGE, "critical": BAD_RED}

# 风险维度公共标签。内部键仍按历史协议保留，报告只显示可核查的审计关注方向。
DIM_CN = {"financial_misstatement": "财务风险",
          "related_party": "关联方交易与资金往来",
          "disclosure_compliance": "信息披露与合规",
          "going_concern": "持续经营与偿债",
          "regulatory_penalty": "监管问询与处罚",
          "data_reliability": "数据勾稽与可靠性",
          "市场风险": "行业与市场环境",
          "经营风险": "经营与行业环境",
          "经营与财务": "经营与财务传导",
          "资产质量": "资产质量"}

# 拆分报告维度归属：财务健康报告 ← 财务风险/持续经营/数据可靠性；合规报告 ← 信披合规/监管处罚/关联交易
# （中英文双写兼容：台账维度可能未经 _normalize_dims 归一化，如测试直接调用场景）
# 数据可靠性风险（勾稽差异）属财务数据问题，归入财务健康诊断报告（P3：消除子报告丢包）
FINANCIAL_DIMS = {"financial_misstatement", "财务错报风险", "going_concern", "持续经营风险",
                  "数据可靠性风险", "data_reliability"}
COMPLIANCE_DIMS = {"disclosure_compliance", "信息披露合规风险", "regulatory_penalty",
                   "监管处罚类高风险", "related_party", "关联交易风险"}

# 风险维度别名归一表：LLM 可能输出简称（config 系统提示词「五大风险维度」表用语，
# 如"财务错报""信披合规"）、全称（带"风险"后缀）或英文标识符，统一归一为规范英文键，
# 保证 _split_risks 按维度过滤拆分报告时不会因措辞差异静默丢弃风险（历史 bug 根因）。
DIM_ALIASES = {
    # 简称（SP「五大风险维度」表用语，LLM 实际输出最常见）
    "财务错报": "financial_misstatement",
    "关联交易": "related_party",
    "信披合规": "disclosure_compliance",
    "持续经营": "going_concern",
    "监管处罚": "regulatory_penalty",
    # 全称（带「风险」后缀）
    "财务错报风险": "financial_misstatement",
    "关联交易风险": "related_party",
    "信息披露合规风险": "disclosure_compliance",
    "持续经营风险": "going_concern",
    "监管处罚类高风险": "regulatory_penalty",
    # 其他常见变体
    "信息披露合规": "disclosure_compliance",
    "监管处罚风险": "regulatory_penalty",
    # 一级词简写（实测：LLM 输出"财务"而非"财务错报"，缺归一会导致
    # R005/R006 类条目既不进财务报告也不进合规报告，仅在综合汇总可见——丢包）
    "财务": "financial_misstatement",
    "财务风险": "financial_misstatement",
    "信披": "disclosure_compliance",
    "信息披露": "disclosure_compliance",
    # 资产质量类（实测 v28：仲裁新增 R005 维度"资产质量"——坏账准备计提充分性属
    # 财务错报/资产质量判断，归入财务错报维度，与"数据可靠性风险"归财务同构）
    "资产质量": "financial_misstatement",
    "资产质量风险": "financial_misstatement",
    # 经营持续类（50c 实测：仲裁新增 R005 维度"经营与财务"——油气资产减值、油价
    # 下行盈利承压类，归入持续经营维度，否则子集完备性告警仅综合汇总可见）
    "经营与财务": "going_concern",
    # 经营风险类（50d 实测：仲裁新增 R007 维度"经营风险"——油价下行对全产业链
    # 收入结构的系统性影响，归入持续经营维度）
    "经营风险": "going_concern",
    # 财务报告类（50d 真实链路实测：LLM 输出"财务报告"维度——跨维度风险传导链
    # 待穿透核查类条目，归入财务错报维度）
    "财务报告": "financial_misstatement",
    # 英文标识符（幂等，保证大小写变体也能归一）
    "financial_misstatement": "financial_misstatement",
    "related_party": "related_party",
    "disclosure_compliance": "disclosure_compliance",
    "going_concern": "going_concern",
    "regulatory_penalty": "regulatory_penalty",
    "数据可靠性": "data_reliability",
    "数据可靠性风险": "data_reliability",
    "data_reliability": "data_reliability",
}

# 风险等级别名归一表：LLM 可能输出「高/中/低」「高风险」等非标准取值或 risk_level 键名，
# 统一归一为三档（重大/重要/一般）后参与统计，避免非标准取值被静默漏计为 0。
LEVEL_ALIASES = {
    "重大": "重大", "高风险": "重大", "极高风险": "重大", "严重": "重大", "高": "重大",
    "重要": "重要", "中等风险": "重要", "中": "重要",
    "一般": "一般", "低风险": "一般", "轻微": "一般", "低": "一般",
}


def _norm_dim(dim) -> str:
    """风险维度归一：任意变体（简称/全称/英文大小写）→ 规范英文键；无法识别时原样返回。"""
    d = str(dim or "").strip()
    return DIM_ALIASES.get(d, DIM_ALIASES.get(d.lower(), d))


def _risk_display_dimension(risk: dict) -> str:
    if not isinstance(risk, dict):
        return "未分类"
    value = risk.get("display_dimension")
    return str(value or DIM_CN.get(_norm_dim(risk.get("dimension", "")),
                                  risk.get("dimension", "") or "未分类"))


def _risk_display_title(risk: dict) -> str:
    """读取快照公共标题，并兼容旧报告中的结论性维度词。"""
    if not isinstance(risk, dict):
        return ""
    title = str(risk.get("display_title") or risk.get("title") or "")
    for source, target in (
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
    ):
        title = title.replace(source, target)
    return title

# calculate_financial_indicators 指标键 → 中文名（未收录的键原样展示）
_INDICATOR_CN = {
    "revenue_yoy_change_pct": "营业收入同比增长率(%)",
    "net_profit_yoy_change_pct": "净利润（合并）同比变动(%)",
    "net_profit_yoy_change_desc": "净利润（合并）同比变动说明",
    "gross_margin_pct": "毛利率(%)",
    "sales_net_margin_pct": "销售净利率(%)",
    "operating_margin_pct": "营业利润率(%)",
    "roe_weighted_pct": "净资产收益率（加权）(%)",
    "roe_deducted_pct": "净资产收益率（扣非）(%)",
    "roa_pct": "总资产报酬率（ROA）(%)",
    "eps_basic": "每股收益（EPS）(元/股)",
    "book_value_per_share": "每股净资产(元/股)",
    "operating_cashflow_to_net_profit_ratio": "经营现金流/净利润",
    "net_profit_parent_yoy_change_pct": "归母净利润同比变动(%)",
    "net_profit_parent_deducted_yoy_change_pct": "扣非归母净利润同比变动(%)",
    "accounts_receivable_to_revenue_ratio": "应收账款/营收(%)",
    "accounts_receivable_yoy_change_pct": "应收账款同比变动(%)",
    "accounts_receivable_gross_yoy_change_pct": "应收账款账面余额跨期变动(%)",
    "accounts_receivable_to_revenue_ratio_change_pp": "应收/营收比变动(百分点)",
    "accounts_receivable_turnover_ratio": "应收账款周转率(次)",
    "accounts_receivable_turnover_days": "应收账款周转天数(天)",
    "inventory_turnover_ratio": "存货周转率(次)",
    "inventory_turnover_days": "存货周转天数(天)",
    "fixed_asset_turnover_ratio": "固定资产周转率(次)",
    "debt_to_asset_ratio_pct": "资产负债率(%)",
    "current_ratio": "流动比率",
    "quick_ratio": "速动比率",
    "cash_ratio": "现金比率（短期偿债）(倍)",
    "equity_ratio": "产权比率（长期偿债）(倍)",
    "interest_coverage_ratio": "利息保障倍数（长期偿债）(倍)",
    "cash_to_short_term_debt_ratio": "现金/短期借款(倍)",
    "operating_cashflow_to_total_liabilities_ratio": "经营现金流/总负债(倍)",
    "operating_cashflow_to_total_assets_ratio": "经营现金流/总资产(%)",
    "operating_cashflow_to_revenue_ratio": "经营现金流/营业收入(%)",
    "free_cash_flow": "自由现金流",
    "bad_debt_provision_to_gross_receivables_ratio_pct": "坏账准备/应收账款账面余额(%)",
    "goodwill_to_net_assets_ratio_pct": "商誉/净资产(%)",
    "construction_in_progress_change_pct": "在建工程较上年末变动(%)",
    "total_asset_turnover_ratio": "总资产周转率(次)",
    "current_asset_turnover_ratio": "流动资产周转率(次)",
    "total_assets_growth_pct": "总资产增长率(%)",
    "net_assets_growth_pct": "净资产增长率(%)",
    "other_receivables_to_total_assets_ratio_pct": "其他应收款/总资产(%)",
    "other_payables_to_total_assets_ratio_pct": "其他应付款/总资产(%)",
    "net_profit_current": "本期净利润（合并）",
    "net_profit_previous": "上期净利润（合并）",
}

# 指标键 → 行业基准键（assets/industry_benchmarks.json 的 benchmarks 字段名）
_BENCH_KEY_MAP = {
    "gross_margin_pct": "gross_margin",
    "debt_to_asset_ratio_pct": "debt_to_asset_ratio",
    "accounts_receivable_to_revenue_ratio": "ar_to_revenue_ratio",
    "inventory_turnover_ratio": "inventory_turnover",
    "current_ratio": "current_ratio",
    "quick_ratio": "quick_ratio",
    "operating_cashflow_to_net_profit_ratio": "cashflow_to_profit_ratio",
    "goodwill_to_net_assets_ratio_pct": "goodwill_to_net_assets",
}

# 财务指标与审计关注分析分组：五项核心能力 + 资产质量与审计关注。
_DIM_GROUPS = [
    ("盈利能力", ["gross_margin_pct", "sales_net_margin_pct", "operating_margin_pct",
                  "roe_weighted_pct", "roe_deducted_pct", "roa_pct", "eps_basic", "book_value_per_share",
                  ]),
    ("营运能力", ["inventory_turnover_ratio", "accounts_receivable_turnover_ratio",
                  "accounts_receivable_turnover_days", "fixed_asset_turnover_ratio",
                  "current_asset_turnover_ratio", "total_asset_turnover_ratio",
                  "accounts_receivable_to_revenue_ratio", "accounts_receivable_yoy_change_pct"]),
    ("偿债能力", ["debt_to_asset_ratio_pct", "current_ratio", "quick_ratio", "cash_ratio",
                  "equity_ratio", "interest_coverage_ratio", "cash_to_short_term_debt_ratio"]),
    ("成长能力", ["revenue_yoy_change_pct", "net_profit_yoy_change_pct", "total_assets_growth_pct",
                  "net_assets_growth_pct"]),
    ("现金流质量", ["operating_cashflow_to_net_profit_ratio", "operating_cashflow_to_total_liabilities_ratio",
                  "operating_cashflow_to_total_assets_ratio", "operating_cashflow_to_revenue_ratio",
                  "free_cash_flow"]),
    ("资产质量与审计关注", ["goodwill_to_net_assets_ratio_pct", "bad_debt_provision_to_gross_receivables_ratio_pct",
                         "accounts_receivable_gross_yoy_change_pct", "accounts_receivable_to_revenue_ratio_change_pp",
                         "other_receivables_to_total_assets_ratio_pct", "construction_in_progress_change_pct",
                        "other_payables_to_total_assets_ratio_pct", "net_profit_current",
                        "net_profit_previous"]),
]

# 指标对比柱状图分组：百分比类 vs 倍数类（单位不同，分面板展示）
_PCT_KEYS = ("gross_margin_pct", "sales_net_margin_pct", "operating_margin_pct",
             "roe_weighted_pct", "roe_deducted_pct", "roa_pct", "total_assets_growth_pct",
             "net_assets_growth_pct", "operating_cashflow_to_revenue_ratio", "debt_to_asset_ratio_pct",
             "operating_cashflow_to_total_assets_ratio",
             "accounts_receivable_to_revenue_ratio", "accounts_receivable_gross_yoy_change_pct",
             "accounts_receivable_to_revenue_ratio_change_pp", "goodwill_to_net_assets_ratio_pct",
             "bad_debt_provision_to_gross_receivables_ratio_pct", "other_receivables_to_total_assets_ratio_pct")

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
                             fontSize=16, leading=22, spaceBefore=15, spaceAfter=4,
                             textColor=PRIMARY),
        'h2': ParagraphStyle("CNH2", parent=s["Heading2"], fontName=font_name,
                             fontSize=13, leading=18, spaceBefore=10, spaceAfter=8,
                             textColor=ACCENT),
        'body': ParagraphStyle("CNBody", parent=s["Normal"], fontName=font_name,
                               fontSize=10, leading=16, alignment=TA_JUSTIFY, spaceAfter=6,
                               textColor=HexColor("#262626")),
        'small': ParagraphStyle("CNSmall", parent=s["Normal"], fontName=font_name,
                                fontSize=9, leading=13, spaceAfter=4, textColor=TEXT_MUTED),
        'center': ParagraphStyle("CNCenter", parent=s["Normal"], fontName=font_name,
                                 fontSize=11, leading=16, alignment=TA_CENTER, spaceAfter=8),
        'caption': ParagraphStyle("CNCaption", parent=s["Normal"], fontName=font_name,
                                  fontSize=9, leading=13, alignment=TA_CENTER,
                                  spaceBefore=2, spaceAfter=8, textColor=TEXT_MUTED),
    }


def _styled_table(data, col_widths, font_name, font_size=10, header_bg=None,
                  level_cols=None, level_colors_map=None, right_align_cols=None, zebra=True):
    """构建带统一专业样式的表格。

    自动应用：全局字体/字号、浅灰网格线、垂直居中、内边距、斑马纹交替底色（可关）。
    默认深藏蓝表头背景+白色文字+表头下加粗分隔线。

    Args:
        data: 二维列表，第一行为表头
        col_widths: 各列宽度列表
        font_name: 字体名称
        font_size: 字号（默认 10pt）
        header_bg: 表头背景色（默认主色 PRIMARY），设为 None 则不渲染表头样式
        level_cols: 风险等级列数据，格式 [(col_idx, row_data_list)]
        level_colors_map: 风险等级 → 颜色 映射表
        right_align_cols: 需要右对齐的列，格式 [(col_idx, _)]
        zebra: 是否启用斑马纹（默认启用）

    Returns:
        已完成样式设置的 Table 对象
    """
    if header_bg is None:
        header_bg = PRIMARY
    t = Table(data, colWidths=col_widths, repeatRows=1)
    cmds = [
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if header_bg:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, PRIMARY),
        ]
    # 斑马纹：数据行交替底色，提升长表格可读性
    if zebra and len(data) > 2:
        cmds.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ZEBRA]))
    for col, _ in (right_align_cols or []):
        cmds.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    if level_cols and level_colors_map:
        for col_idx, rows in level_cols:
            for ri, val in enumerate(rows, start=1):
                if val in level_colors_map:
                    cmds.append(("TEXTCOLOR", (col_idx, ri), (col_idx, ri), level_colors_map[val]))
    t.setStyle(TableStyle(cmds))
    return t


def _esc(text) -> str:
    """XML 转义：将动态文本中的 & < > 转义为 reportlab Paragraph 安全的实体。

    必须按 & → < → > 的顺序替换（& 最先，否则会二次转义已生成的实体）。
    非字符串输入先 str() 转换，避免 LLM 传入数字/字典时拼接出意外内容。
    """
    s = text if isinstance(text, str) else str(text)
    # 公共报告不得暴露内部 JSON/实现字段。旧会话可能仍携带英文键名，
    # 因此在统一文本出口兼容清洗，而不是只依赖新快照的结构化字段。
    for machine, public in {
        "risk_chain_analysis": "风险传导链分析",
        "judgment_1": "第一次复核",
        "judgment_2": "第二次复核",
        "overall_status": "总体状态",
        "conditions_aligned": "条件是否对齐",
        "evidence_ids_1": "第一轮证据编号",
        "evidence_ids_2": "第二轮证据编号",
        "evidence_ids": "证据编号",
        "evidence": "证据",
        "overall_assessment": "综合评估结论",
        "reasoning_chain": "审计判断依据",
        "audit_suggestion": "建议执行程序",
        "risk_details": "风险明细",
        "semantic_review": "结构化复核摘要",
        "review_conclusion": "复核结论",
        "check_disclosure_compliance": "信息披露合规核查",
        "identify_audit_opinion": "审计意见识别",
    }.items():
        s = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(machine)}(?![A-Za-z0-9_])", public, s)
    s = re.sub(r"(?:check_disclosure_compliance|信息披露合规核查)\s+issues",
               "信息披露核查结果", s)
    # 公共报告使用审计关注方向，不把内部结论性标签直接呈现给评委或管理层。
    for internal, public in (
        ("财务报表错报风险", "财务风险"),
        ("财务错报风险", "财务风险"),
        ("财务错报", "财务风险"),
        ("待核查事项", "待复核提示"),
        ("待核实事项", "待复核提示"),
        ("待核查", "待复核"),
        ("待核实", "待复核"),
    ):
        s = s.replace(internal, public)
    # 客户可见 PDF 只保留可回查的中文来源提示，不暴露底稿内部证据键。
    # Excel 证据索引仍保留原编号，供审计底稿精确关联。
    s = re.sub(r"\bE-SOURCE-[A-Za-z0-9_-]+-P(\d+)\b", r"源报告物理页\1", s)
    s = re.sub(r"\bE-SYSTEM-[A-Za-z0-9_-]+\b", "系统计算证据", s)
    s = re.sub(r"\bE-RISK-MODEL-[A-Za-z0-9_-]+\b", "量化模型证据", s)
    s = re.sub(r"\bE-[A-Za-z0-9_-]+\b", "可追溯计算证据", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _public_evidence_reference(value) -> str:
    """Return a human-readable evidence reference for table cells."""
    return _esc(value)


def _para_text(text) -> str:
    """段落文本预处理：先 XML 转义，再把换行符转为 <br/> 以保留多行结构。"""
    return _esc(text).replace("\n", "<br/>")


def _cell_style(font_name, font_size, align=TA_LEFT):
    """构建表格单元格专用 ParagraphStyle：无段后间距、紧凑行距。"""
    return ParagraphStyle("CellStyle", fontName=font_name, fontSize=font_size,
                          leading=font_size * 1.2, alignment=align, spaceAfter=0)


def _numbered_lines(text):
    """将多行内容拆为有效行列表，供逐行加 (1)(2)… 子编号。

    仅当有效行数 ≥2 时返回行列表（单行内容无需编号，由调用方按普通段落渲染）；
    空内容或单行返回 None。
    """
    s = text if isinstance(text, str) else str(text)
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
    return lines if len(lines) >= 2 else None


_DISPLAY_UNIT_FACTORS = (
    ("万亿", 1e12), ("百万元", 1e6), ("千万元", 1e7),
    ("亿元", 1e8), ("万元", 1e4), ("千元", 1e3), ("元", 1.0),
)


def _amount_unit_factor(unit):
    """Return the yuan multiplier for an explicitly declared source unit."""
    text = str(unit or "").replace("人民币", "")
    for label, factor in _DISPLAY_UNIT_FACTORS:
        if label in text:
            return factor
    return None


def _fmt_num(v, no_scale=False, input_unit=None):
    """数值展示格式化：按声明单位换算后自动显示万元/亿元。

    no_scale=True 时不做万/亿自动换算（用于数据原始单位未知的场景，如多年对比
    时序表——自动换算会把 185000（万元口径）误显为 18.50 万元，产生量级误解）。
    input_unit 仅接受显式单位；未声明时保留原有数值，避免猜测量级。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    if input_unit and not no_scale:
        factor = _amount_unit_factor(input_unit)
        if factor is not None:
            x *= factor
    if not no_scale:
        if abs(x) >= 1e8:
            return f"{x / 1e8:,.2f} 亿元"
        if abs(x) >= 1e4:
            return f"{x / 1e4:,.2f} 万元"
    return f"{x:,.2f}"


def _embed_chart(gen_fn, payload, width_cm: float = 14.0):
    """图表生成统一包装：gen_fn(payload, 临时路径) 生成 PNG → reportlab Image 元素。

    所有内嵌图表（雷达图/热力图/仪表盘/柱状图/环形图）均走此入口，
    便于测试统一 monkeypatch；生成失败时静默降级返回 None，不中断 PDF 生成。

    Args:
        gen_fn: 图表生成函数，签名 (payload, output_path) → 路径
        payload: 传给生成函数的载荷（JSON 字符串或字典序列化结果）
        width_cm: 嵌入宽度（厘米），高度按图片原始比例缩放

    Returns:
        RLImage 流元素，或 None（生成失败）
    """
    try:
        tmp = os.path.join(tempfile.gettempdir(), f"pdfembed_{uuid.uuid4().hex}.png")
        gen_fn(payload, tmp)
        iw, ih = ImageReader(tmp).getSize()
        if iw <= 0 or ih <= 0:
            return None
        w = width_cm * cm
        return RLImage(tmp, width=w, height=w * ih / iw)
    except Exception as e:
        logger.warning(f"PDF内嵌图表生成失败（跳过）: {e}")
        return None


def _safe_json(obj):
    """宽松解析：str 尝试 json.loads，dict 原样返回，失败返回 {}。"""
    if isinstance(obj, dict):
        return obj
    if not obj:
        return {}
    try:
        parsed = json.loads(obj)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _benchmark_basis_note(has_verified_benchmark: bool) -> str:
    """Return a source-qualified benchmark note for human-facing reports.

    The bundled benchmark file is also used for screening thresholds, but its
    values must not be described as peer-company averages unless provenance,
    period, sample and comparability have all been reviewed.
    """
    if has_verified_benchmark:
        return ("行业基准已取得来源、统计期间、样本范围及可比性核验记录；"
                "仅在相应指标有核验基准时进行横向比较。")
    return ("本次未取得经来源、统计期间、样本和可比性核验的行业基准；"
            "系统内部筛查值仅作筛查参考，不代表行业均值或中位数。")


def _has_verified_benchmark_provenance(value) -> bool:
    """Recursively detect an explicitly qualified benchmark provenance block."""
    if isinstance(value, dict):
        source = value.get("source_metadata") or value.get("provenance")
        if isinstance(source, dict):
            if (source.get("verified") is True
                    and all(source.get(k) for k in ("source", "period", "sample", "scope"))
                    and source.get("comparability_reviewed") is True):
                return True
        return any(_has_verified_benchmark_provenance(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_verified_benchmark_provenance(v) for v in value)
    return False


def _structured_review_objects(text: str) -> list[tuple[int, int, dict]]:
    """Return machine-review JSON spans and parsed objects from arbitrary prose."""
    if not text:
        return []
    decoder = json.JSONDecoder()
    found = []
    pos = 0
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except (TypeError, ValueError):
            pos = start + 1
            continue
        if isinstance(value, dict) and any(
                key in value for key in (
                    "judgment_1", "judgment_2", "overall_status", "adjustments",
                    "overall_assessment", "reasoning_chain", "audit_suggestion",
                    "risk_details", "semantic_review")):
            found.append((start, start + end, value))
        pos = start + max(end, 1)
    return found


def _strip_structured_review_json(text: str) -> str:
    """Remove machine review JSON from prose while retaining surrounding reasoning.

    ``review_conclusion`` is an audit trail and may contain nested JSON emitted by
    the debate model.  A JSON decoder is used instead of a regex so nested
    ``checks``/``adjustments`` arrays are removed reliably.
    """
    if not text:
        return text
    spans = _structured_review_objects(text)
    for start, end, _value in reversed(spans):
        text = text[:start] + "（结构化复核摘要见上表）" + text[end:]
    # Clean up a label left immediately before a removed JSON object.
    text = re.sub(r"【(?:裁定)?JSON】\s*（结构化复核摘要见上表）", "", text)
    text = re.sub(r"```(?:json)?\s*```", "", text, flags=re.IGNORECASE)
    # Debate prose sometimes names the JSON keys without emitting a JSON object.
    # Translate those implementation labels as well so the PDF remains readable.
    for key, label in {
        "judgment_1": "第一次复核",
        "judgment_2": "第二次复核",
        "overall_status": "总体状态",
        "conditions_aligned": "条件是否对齐",
        "evidence_ids_1": "第一轮证据引用",
        "evidence_ids_2": "第二轮证据引用",
        "overall_assessment": "综合评估结论",
        "reasoning_chain": "审计判断依据",
        "audit_suggestion": "建议执行程序",
        "risk_details": "风险明细",
        "semantic_review": "结构化复核摘要",
        "check_disclosure_compliance": "信息披露合规核查",
    }.items():
        text = text.replace(key, label)
    text = re.sub(r"(?:check_disclosure_compliance|信息披露合规核查)\s+issues",
                  "信息披露核查结果", text)
    return text.strip()


def _review_actionable_text(text: str) -> str:
    """从复核留痕中保留可执行程序，屏蔽辩论原文和 Markdown 标记。"""
    cleaned = _strip_structured_review_json(str(text or ""))
    if not cleaned:
        return ""
    lines = []
    for raw_line in re.split(r"[\r\n]+", cleaned):
        line = re.sub(r"^\s*(?:#{1,6}|[-*])\s*", "", raw_line).strip()
        line = re.sub(r"\*+", "", line)
        line = re.sub(r"^【[^】]+】\s*", "", line).strip()
        if not line or line in {"（结构化复核摘要见上表）", "复核意见："}:
            continue
        if any(key in line for key in (
                "judgment_1", "judgment_2", "overall_status", "conditions_aligned",
                "evidence_ids", "risk_chain_analysis", "overall_assessment",
                "reasoning_chain", "audit_suggestion", "risk_details",
                "semantic_review", "check_disclosure_compliance")):
            continue
        if any(key in line for key in ("建议", "待执行", "核查", "复核", "函证", "抽查", "取得", "扩大")):
            lines.append(line)
    # 相同程序在三方辩论中经常重复，只保留首次出现的短句。
    result = []
    seen = set()
    for line in lines:
        line = re.sub(r"\s+", " ", line)
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return "；".join(result)[:1800]


# ── 行业基准加载与确定性指标判读 ──────────────────────────

# 行业基准注释缓存（小尾巴：判读注释钉入基准库配置，所有渲染口统一带出）
_BENCH_NOTES = {}


def _load_benchmarks(industry: str) -> dict:
    """按行业名加载基准均值表，返回 {基准键: average}；无匹配返回 {}。"""
    if not industry:
        return {}
    workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        with open(os.path.join(workspace, "assets", "industry_benchmarks.json"), "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in (raw.get("industries", {}) or {}).items():
            if not isinstance(v, dict):
                continue
            if str(industry) in k or k in str(industry):
                bms = v.get("benchmarks", {}) or {}
                out = {}
                for bk, spec in bms.items():
                    value = sourced_benchmark_value(spec, v)
                    if value is not None:
                        out[bk] = value
                # 缓存行业级判读注释（如毛利率贸易摊薄说明），供所有渲染口统一带出
                _BENCH_NOTES[industry] = str(v.get("notes", {}).get("gross_margin", "") or "")
                return out
    except Exception as e:
        logger.warning(f"行业基准加载失败（{industry}）: {e}")
    return {}


def _interpret_indicator(key, vnum, bench, context=None, industry=None):
    """确定性阈值判读：按审计审慎措辞给出专业判读文本。

    Args:
        key: 指标键名
        vnum: 指标数值（float 或 None）
        bench: 行业基准均值（float 或 None）
        context: 指标全量字典（可选）——供跨指标联动判读：应收增速 vs 营收增速、
            净利润正负对 OCF/NP 判读的影响（修复单指标判读与联动预警矛盾）
        industry: 行业名（可选）——供业务结构相关判读软化：能源全产业链综合
            毛利率受低毛利贸易业务摊薄，不应按纯开采板块基准直接定性（实测缺陷）

    Returns:
        (判读文本, 是否负面信号)；无法判读时返回 ("—", None)
    """
    if vnum is None:
        return ("—", None)
    v = float(vnum)
    if key == "gross_margin_pct":
        if bench is not None and v < bench * 0.8:
            text = f"显著低于行业基准 {bench:g}%，主业盈利能力存疑"
            # 小尾巴：摊薄说明读基准库配置（不再硬编码），所有渲染口统一带出
            note = _BENCH_NOTES.get(str(industry or ""), "") if industry else ""
            if note:
                text += f"（{note}）"
            return (text, True)
        if bench is not None and v < bench:
            return (f"低于行业基准 {bench:g}%，盈利能力偏弱，需关注毛利变动原因", False)
        if bench is not None:
            return ("不低于行业基准，盈利能力未见明显异常", False)
        return ("—", None)
    if key == "operating_cashflow_to_net_profit_ratio":
        net_profit = None
        if isinstance(context, dict):
            try:
                net_profit = float(context.get("net_profit_current"))
            except (TypeError, ValueError):
                net_profit = None
        if net_profit is not None and net_profit < 0:
            return ("净利润为负，OCF/NP 指标参考意义有限，需结合经营现金流净额绝对值判断", False)
        if v < 0:
            return ("经营现金流为负且与净利润背离，利润含金量存疑", True)
        if v < 0.5:
            return ("OCF/NP<0.5，利润现金含量偏低，盈利质量存疑", True)
        if bench is not None and v < bench:
            return (f"低于行业基准 {bench:g}，利润现金保障偏弱", False)
        return ("利润现金保障未见明显异常", False)
    if key == "debt_to_asset_ratio_pct":
        if v >= 85:
            return ("资产负债率≥85%，接近资不抵债红线，偿债风险嫌疑显著", True)
        if bench is not None and v > bench + 10:
            return (f"高于行业基准 {bench:g}% 逾10个百分点，杠杆偏高，偿债压力需关注", True)
        if v > 70:
            return ("高于70%通用警戒水平，偿债压力需关注", False)
        return ("杠杆处于合理区间，未见显著偿债风险迹象", False)
    if key == "current_ratio":
        if v < 1:
            return ("流动比率<1，短期偿债能力不足嫌疑，需关注流动性安排", True)
        if bench is not None and v < bench:
            return (f"低于行业基准 {bench:g}，短期流动性需关注", False)
        return ("短期偿债能力未见明显异常", False)
    if key == "quick_ratio":
        if v < 0.5:
            return ("速动比率<0.5，低于通用警戒水平，流动性风险嫌疑", True)
        if bench is not None and v < bench:
            return (f"低于行业基准 {bench:g}，即时偿付能力偏弱", False)
        return ("即时偿付能力未见明显异常", False)
    if key == "cash_to_short_term_debt_ratio":
        if v < 1:
            return ("现金不足以覆盖短期借款，需结合受限资金和到期债务安排复核", True)
        return ("现金对短期借款具备覆盖能力，仍需核对资金可用性", False)
    if key == "cash_ratio":
        if v < 0.2:
            return ("现金比率低于0.2倍，短期流动性缓冲偏弱，需结合到期债务复核", True)
        return ("现金比率已取得，仍需结合受限资金和债务期限结构判断", False)
    if key == "equity_ratio":
        if v > 1:
            return ("产权比率高于1倍，长期资本结构和偿债压力需关注", True)
        return ("产权比率未触发内部筛查提示", False)
    if key == "interest_coverage_ratio":
        if v < 1.5:
            return ("利息保障倍数低于1.5倍，利息支付能力需复核", True)
        return ("利息保障倍数具备一定覆盖能力，仍需核对利息口径", False)
    if key == "operating_cashflow_to_total_liabilities_ratio":
        if v < 0:
            return ("经营现金流为负，对负债的现金覆盖能力偏弱，需结合偿债安排复核", True)
        if v < 0.1:
            return ("经营现金流/总负债低于0.1倍，负债现金覆盖能力偏弱", True)
        return ("经营现金流对总负债具备一定覆盖能力", False)
    if key == "operating_cashflow_to_total_assets_ratio":
        if v < 0:
            return ("经营现金流为负，资产现金创造能力需结合经营与投资活动复核", True)
        if v < 3:
            return ("经营现金流/总资产低于3%，资产现金创造能力偏弱", True)
        return ("经营现金流对总资产具备一定现金创造能力", False)
    if key == "operating_cashflow_to_revenue_ratio":
        if v < 5:
            return ("经营现金流/营业收入低于5%，收入现金转化能力需复核", True)
        return ("经营现金流对营业收入具备一定转化能力", False)
    if key in {"sales_net_margin_pct", "operating_margin_pct", "roe_weighted_pct", "roe_deducted_pct", "roa_pct"}:
        return ("已取得本期盈利能力指标，需结合行业基准、期间和口径比较", False)
    if key in {"total_asset_turnover_ratio", "current_asset_turnover_ratio", "accounts_receivable_turnover_ratio"}:
        return ("已取得资产周转指标，需结合行业基准和期间可比性判断", False)
    if key in {"total_assets_growth_pct", "net_assets_growth_pct"}:
        return ("已取得规模增长指标，需结合增长来源和现金流验证", False)
    if key == "bad_debt_provision_to_gross_receivables_ratio_pct":
        if v < 1:
            return ("坏账准备覆盖率较低，需结合账龄、客户信用和期后回款复核", True)
        return ("坏账准备覆盖率已取得，可进一步结合账龄结构和回款验证", False)
    if key == "inventory_turnover_ratio":
        if bench is not None and bench > 0 and v < bench * 0.6:
            return (f"显著低于行业基准 {bench:g} 次，存货周转偏慢，积压/跌价风险需关注", True)
        if bench is not None and bench > 0 and v < bench:
            return (f"低于行业基准 {bench:g} 次，存货管理效率偏弱", False)
        return ("存货周转未见明显异常", False)
    if key == "accounts_receivable_to_revenue_ratio":
        if (bench is not None and v > bench + 10) or v > 30:
            return ("应收占营收比偏高，回款质量与收入确认真实性需进一步核查", True)
        if bench is not None and v > bench:
            return (f"高于行业基准 {bench:g}%，回款效率偏弱", False)
        return ("该指标未触发内部筛查提示，仍需结合账龄与期后回款核查", False)
    if key == "accounts_receivable_yoy_change_pct":
        rev_growth = None
        if isinstance(context, dict):
            try:
                rev_growth = float(context.get("revenue_yoy_change_pct"))
            except (TypeError, ValueError):
                rev_growth = None
        # 跨指标联动：应收增速显著高于营收增速才是风险信号（修复单指标判读
        # 与量化预警/风险明细结论相反的问题）
        if rev_growth is not None and v - rev_growth > 20:
            return ("应收账款增速显著高于营收增速（剪刀差超20pp），收入确认与回款真实性需重点核查", True)
        if v > 50:
            return ("应收账款同比增速超50%，若显著高于营收增速，存在提前确认收入嫌疑", True)
        if v < 0:
            return ("应收账款同比下降，回款情况改善", False)
        return ("应收账款温和增长，未见显著异常", False)
    if key == "goodwill_to_net_assets_ratio_pct":
        if v > 30:
            return ("商誉占净资产超30%，减值风险敞口较大，需关注减值测试充分性", True)
        if bench is not None and v > bench:
            return (f"高于行业基准 {bench:g}%，商誉敞口需持续关注", False)
        return ("该指标未触发商誉筛查提示，仍需结合减值测试核查", False)
    if key == "construction_in_progress_change_pct":
        if v > 100:
            return ("在建工程较上年末翻倍增长，转固时点与利息资本化合规性需核查", True)
        return ("—", None)
    if key == "other_payables_to_total_assets_ratio_pct":
        if v > 5:
            return ("其他应付款占总资产超5%，资金往来性质与关联方占用嫌疑需核查", True)
        return ("—", None)
    if key == "revenue_yoy_change_pct":
        if v < -20:
            return ("营业收入同比大幅下滑逾20%，经营基本面承压，持续经营能力需关注", True)
        if v < 0:
            return ("营业收入同比负增长，成长性偏弱", False)
        if v > 30:
            return ("营收增速异常偏高，需结合行业景气度与并表因素核实真实性", False)
        return ("营收增长平稳", False)
    if key == "net_profit_yoy_change_pct":
        if v < -50:
            return ("净利润同比大幅下滑逾50%，盈利能力恶化嫌疑，需关注亏损成因", True)
        if v < 0:
            return ("净利润同比下滑，需关注下滑原因及可持续性", False)
        return ("净利润同比增长，盈利趋势未见恶化", False)
    return ("—", None)


# ── 新增专业图表生成器（matplotlib，失败由 _embed_chart 兜底） ──

def _mpl_hex(hc):
    """reportlab HexColor → matplotlib 可用的 #RRGGBB 字符串。"""
    return "#" + hc.hexval()[2:]


def _gen_score_gauge(score_json_str, output_path):
    """综合评分半圆仪表盘：四色风险区间（低→极高）+ 指针 + 分值等级。"""
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.patches import Wedge  # type: ignore
    fp = _setup_chinese_font()
    data = _safe_json(score_json_str)
    try:
        score = max(0.0, min(100.0, float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0.0
    level = str(data.get("level", "") or "")
    level_color = _mpl_hex(SCORE_LEVEL_COLORS.get(str(data.get("level_key", "")), PRIMARY))

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.set_aspect("equal")
    ax.set_xlim(-1.28, 1.28)
    ax.set_ylim(-0.22, 1.28)
    ax.axis("off")
    # 四色风险区间：0-25低(绿) 26-50中(黄) 51-75高(橙) 76-100极高(红)
    for s0, s1, c in [(0, 25, _mpl_hex(GOOD_GREEN)), (25, 50, _mpl_hex(WARN_AMBER)),
                      (50, 75, _mpl_hex(WARN_ORANGE)), (75, 100, _mpl_hex(BAD_RED))]:
        ax.add_patch(Wedge((0, 0), 1.0, 180 - s1 * 1.8, 180 - s0 * 1.8,
                           facecolor=c, edgecolor="white", linewidth=1.5))
    ax.add_patch(Wedge((0, 0), 0.60, 0, 180, facecolor="white", edgecolor="none"))
    for tick in (0, 25, 50, 75, 100):
        ang = math.radians(180 - tick * 1.8)
        ax.text(1.15 * math.cos(ang), 1.15 * math.sin(ang), str(tick),
                ha="center", va="center", fontsize=8, color=_mpl_hex(TEXT_MUTED))
    # 指针（分值越高风险越大，0分指向左侧绿色区）
    ang = math.radians(180 - score * 1.8)
    ax.plot([0, 0.78 * math.cos(ang)], [0, 0.78 * math.sin(ang)],
            color=_mpl_hex(PRIMARY), linewidth=3, solid_capstyle="round")
    ax.add_patch(plt.Circle((0, 0), 0.05, color=_mpl_hex(PRIMARY)))
    ax.text(0, 0.33, f"{score:.1f}", ha="center", va="center", fontsize=26,
            fontweight="bold", color=level_color, fontproperties=fp)
    ax.text(0, 0.15, "综合风险评分（分越高风险越大）", ha="center", fontsize=9,
            color=_mpl_hex(TEXT_MUTED), fontproperties=fp)
    if level:
        ax.text(0, -0.12, f"风险等级：{level}", ha="center", fontsize=13,
                color=level_color, fontweight="bold", fontproperties=fp)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _gen_breakdown_bar(score_json_str, output_path):
    """三维度评分分解横向柱状图（财务50%/披露30%/校验20%），按分值着色。"""
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = _safe_json(score_json_str)
    breakdown = data.get("breakdown", {}) if isinstance(data.get("breakdown"), dict) else {}
    weights = data.get("weights", WEIGHTS) if isinstance(data.get("weights"), dict) else WEIGHTS
    items = [("财务指标风险", "financial"), ("披露合规风险", "disclosure"), ("数据校验风险", "validation")]
    labels, values = [], []
    for name, k in items:
        raw = breakdown.get(k)
        w = weights.get(k, 0)
        try:
            w_txt = f"（权重{float(w) * 100:.0f}%）"
        except (TypeError, ValueError):
            w_txt = ""
        if isinstance(raw, (int, float)):
            values.append(float(raw))
            labels.append(f"{name}{w_txt}")
        else:
            # N 补丁：维度未获取（字符串"未获取"）时图表标注而非画 0 分误导
            values.append(0.0)
            labels.append(f"{name}（未获取）{w_txt}")
    colors = [_mpl_hex(GOOD_GREEN if v < 40 else WARN_AMBER if v < 70 else BAD_RED) for v in values]

    fig, ax = plt.subplots(figsize=(7.5, 2.6))
    y_pos = range(len(items))
    ax.barh(list(y_pos), values, color=colors, height=0.55)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels, fontproperties=fp, fontsize=10)
    ax.set_xlim(0, 100)
    ax.set_xlabel("风险分（0-100）", fontproperties=fp, fontsize=9)
    ax.set_title("综合评分三维度分解（基础分加权构成）", fontproperties=fp, fontsize=12,
                 color=_mpl_hex(PRIMARY))
    for i, v in enumerate(values):
        ax.text(v + 1.5, i, f"{v:.1f}", va="center", fontsize=9, color=_mpl_hex(TEXT_MUTED))
    ax.grid(axis="x", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _gen_level_donut(risk_json_str, output_path):
    """风险等级分布环形图：重大/重要/一般占比，中心标注风险总数。"""
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = _safe_json(risk_json_str)
    counts = _level_counts(_formal_risks(data))
    total = sum(counts.values())
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    if total == 0:
        ax.pie([1], colors=["#DDDDDD"], startangle=90,
               wedgeprops=dict(width=0.42, edgecolor="white"))
        ax.text(0, 0, "系统采信0项\n待复核提示另列", ha="center", va="center", fontsize=10,
                color=_mpl_hex(TEXT_MUTED), fontproperties=fp)
    else:
        names, values, colors = [], [], []
        for lv in ("重大", "重要", "一般"):
            if counts.get(lv, 0) > 0:
                names.append(f"{lv} {counts[lv]} 项")
                values.append(counts[lv])
                colors.append(_mpl_hex(LEVEL_COLORS[lv]))
        ax.pie(values, labels=names, colors=colors, startangle=90,
               wedgeprops=dict(width=0.42, edgecolor="white"),
               textprops=dict(fontproperties=fp, fontsize=10))
        ax.text(0, 0.08, str(total), ha="center", va="center", fontsize=24,
                fontweight="bold", color=_mpl_hex(PRIMARY))
        ax.text(0, -0.22, "风险总数", ha="center", va="center", fontsize=9,
                color=_mpl_hex(TEXT_MUTED), fontproperties=fp)
    ax.set_title("风险等级分布", fontproperties=fp, fontsize=12, color=_mpl_hex(PRIMARY))
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _gen_indicator_bars(payload_json_str, output_path):
    """核心指标 vs 行业基准分组柱状图（左：百分比类，右：倍数类）。

    载荷格式：{"pct": [["毛利率", 21.5, 25.0], ...], "mult": [["流动比率", 0.8, 1.5], ...]}
    每项为 [指标名, 公司值, 行业基准]。
    """
    import matplotlib.pyplot as plt  # type: ignore
    import numpy as np  # type: ignore
    fp = _setup_chinese_font()
    payload = _safe_json(payload_json_str)
    pct = payload.get("pct", []) or []
    mult = payload.get("mult", []) or []
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.5, 3.4))
    for ax, series, title in ((ax1, pct, "百分比类指标（%）"), (ax2, mult, "倍数/周转类指标")):
        if not series:
            ax.axis("off")
            ax.text(0.5, 0.5, "（无行业基准可对比）", ha="center", va="center",
                    transform=ax.transAxes, fontproperties=fp, fontsize=9,
                    color=_mpl_hex(TEXT_MUTED))
            continue
        names = [s[0] for s in series]
        company = [float(s[1]) for s in series]
        bench = [float(s[2]) for s in series]
        x = np.arange(len(names))
        ax.bar(x - 0.19, company, width=0.38, label="公司实际", color=_mpl_hex(PRIMARY))
        ax.bar(x + 0.19, bench, width=0.38, label="行业基准", color="#A9C4EB")
        ax.set_xticks(x)
        ax.set_xticklabels(names, fontproperties=fp, fontsize=8.5, rotation=12)
        ax.set_title(title, fontproperties=fp, fontsize=10, color=_mpl_hex(PRIMARY))
        ax.legend(prop=fp, fontsize=8)
        ax.grid(axis="y", alpha=0.3)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
    fig.suptitle("核心财务指标与行业基准对比", fontproperties=fp, fontsize=12,
                 color=_mpl_hex(PRIMARY))
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


# ── KPI 面板 ──────────────────────────────────────────────

def _kpi_cards(items, font):
    """一行 KPI 色块卡片：items = [(标签, 数值文本, 背景色, 文字色)]。"""
    val_style = ParagraphStyle("KpiVal", fontName=font, fontSize=15, leading=19,
                               alignment=TA_CENTER, spaceAfter=0)
    lab_style = ParagraphStyle("KpiLab", fontName=font, fontSize=8.5, leading=12,
                               alignment=TA_CENTER, spaceAfter=0)
    val_row, lab_row, cmds = [], [], []
    for ci_idx, (label, value, bg, fg) in enumerate(items):
        val_row.append(Paragraph(f"<b>{_esc(value)}</b>", ParagraphStyle(
            f"KpiVal{ci_idx}", parent=val_style, textColor=fg)))
        lab_row.append(Paragraph(_esc(label), ParagraphStyle(
            f"KpiLab{ci_idx}", parent=lab_style, textColor=fg)))
        cmds += [("BACKGROUND", (ci_idx, 0), (ci_idx, -1), bg)]
    t = Table([val_row, lab_row], colWidths=[468 / len(items)] * len(items))
    t.setStyle(TableStyle(cmds + [
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE_GRAY),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.5, white),
    ]))
    return t


# ── 通用文档组件（封面/目录/公司简介/免责/风险明细） ─────────

# 综合评分快照替换（L 补丁）：吞掉旧分数+邻近等级+外层括号，杜绝双分矛盾。
# 兼容「综合风险评分」与「综合评分」两种表述；数字形态兼容「9分」「9/100」及
# markdown 表格单元格形态（50c 实测：正文写「| 综合风险评分 | 55 | 中等风险 |」
# ——数字被竖线分隔且无「分」字后缀，旧正则要求数字紧跟标签，表格形态完全不匹配
# 导致 55 分残留与系统评分同屏双分）；标签与数字间允许「：/为」及加粗标记。
# 等级可内括号（（低风险））、逗号/竖线分隔（，低风险、| 中等风险）两种写法，均吞掉；
# 50d：等级词表含「中低/中高」变体；分隔形态带负向前瞻——等级词后必须紧跟标点/行尾/
# 竖线才吞（防误吞「，低风险行业」「中低风险偏好投资者」类名词短语，实测病句缺陷）。
_SCORE_SNAPSHOT_PAT = re.compile(
    r"（?综合(?:风险)?评分"
    r"[\s|｜]*[：:为]?"
    r"\**[\s|｜]*\d+(?:\.\d+)?\**\s*"
    r"(?:分(?:/\s*100)?|/\s*100|(?=\s*[|｜]))"
    r"(?:（?(?:中低|中高|中等|低|高|极高)风险）?)?"
    r"(?:[\s]*[，,|｜][\s]*(?:等级)?（?(?:中低|中高|中等|低|高|极高)风险）?"
    r"(?=[\s]*[，,。；;|｜）\]\}：:]|$))?"
    r"(?:[\s|｜,，]*处于(?:中低|中高|中等|低|高|极高)风险区间)?"
    r"\s*）?")

# 构建版本戳（Q 补丁）：产物可追溯——git hash + 实例 run_id
_BUILD_HASH = None


def _build_hash():
    """读取 git short hash（失败降级 dev），模块级缓存。"""
    global _BUILD_HASH
    if _BUILD_HASH is None:
        try:
            import subprocess
            _BUILD_HASH = subprocess.check_output(
                ["git", "rev-parse", "--short=7", "HEAD"],
                stderr=subprocess.DEVNULL, text=True).strip() or "dev"
        except Exception:
            _BUILD_HASH = "dev"
    return _BUILD_HASH


def _apply_score_snapshot(text, report):
    """用系统评分快照替换 LLM 正文中的旧评分引用（含"处于X风险区间"等邻近表述）。

    快照缺失时保持原文并记录 warning（L 补丁兜底）；50d：快照存在但 score=None
    （评分未获取/无法判定）时同样归一正文旧分——快照恒存在、正文恒被归一契约，
    否则结论章残留 LLM 旧分与「未获取」口径并存（实测 18:37 版同源缺陷）。
    """
    snap = report.get("comprehensive_score_snapshot") or {}
    score = snap.get("score")
    if score is None and "score" in snap:
        return _SCORE_SNAPSHOT_PAT.sub(
            "综合风险评分 未获取/无法判定（请人工复核）", str(text))
    if not isinstance(score, (int, float)):
        logger.warning("评分快照缺失或 score 为 None，结论章保持 LLM 原文（未做分数替换）")
        return str(text)
    level = str(snap.get("level", "") or "")
    return _SCORE_SNAPSHOT_PAT.sub(f"综合风险评分 {score:.1f}分（{level}）", str(text))


def _system_quality_notes_body(report, st):
    """系统质检说明：被剥离的系统自审条目（内部状态误立项为发行人风险）留痕展示。"""
    notes = report.get("system_quality_notes", []) or []
    if not notes:
        return []
    body = [Paragraph("以下条目由系统识别为「系统内部状态」被误立项为发行人风险，已从风险台账移出，"
                      "转记入系统质检日志（不代表发行人披露问题）：", st['small']), Spacer(1, 4)]
    for i, r in enumerate(notes, 1):
        if not isinstance(r, dict):
            continue
        body.append(Paragraph(
            f"{i}. {r.get('risk_id', '')}｜{_esc(_risk_display_title(r))}"
            f"（维度：{_esc(_risk_display_dimension(r))}）", st['body']))
        ev = str(r.get("evidence", "") or "")[:200]
        if ev:
            body.append(Paragraph(_esc(ev), st['small']))
    body.append(Spacer(1, 6))
    return body


def _cover_elements(title_lines, ci, st, font, report_no="", score_card=None,
                    report_context=None):
    """封面页（封皮）：藏蓝标题色块 + 公司信息表 + 报告编号/密级 + 评分卡片 + AI 声明。

    顶部/底部全宽色带由 _build_pdf_doc 的 onFirstPage 画布回调绘制。

    Args:
        title_lines: 封面大标题行列表（如 ["上市公司年报", "财务健康诊断报告"]）
        ci: company_info 字典
        st: 样式字典
        font: 字体名称
        report_no: 报告编号（文件名前缀），展示于封面
        score_card: 可选 {"score": float, "level": str, "level_key": str}，
            提供时封面附综合评分卡片（综合汇总报告用）
        report_context: 可选最终发布对象/兼容台账，用于展示快照派生的
            口径、单位、门禁、数据状态、风险数量和源文件哈希。

    Returns:
        封面流元素列表（以 PageBreak 结束）
    """
    elements = [Spacer(1, 50)]
    # 大标题区：藏蓝底色块 + 白色标题（专业报告封皮风格）
    white_title = ParagraphStyle("CoverTitle", parent=st['title'], fontName=font,
                                 fontSize=22, leading=30, alignment=TA_CENTER,
                                 textColor=white, spaceAfter=0)
    title_cell = [Paragraph(_esc(line), white_title) for line in title_lines]
    title_block = Table([[title_cell]], colWidths=[16.5 * cm])
    title_block.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 22),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 22),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("LINEBELOW", (0, 0), (-1, -1), 2.5, ACCENT),
    ]))
    elements += [title_block, Spacer(1, 30)]

    # 公司信息表（标签列主色右对齐，值列斑马纹）。这些字段只从最终
    # 兼容壳/快照派生，不从 AI 正文猜测，保证封面也是发布契约的一部分。
    context = report_context if isinstance(report_context, dict) else {}
    snapshot = context.get("report_snapshot") if isinstance(context.get("report_snapshot"), dict) else {}
    snapshot_source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    snapshot_gate = snapshot.get("review_gate") if isinstance(snapshot.get("review_gate"), dict) else {}
    gate = context.get("review_gate") if isinstance(context.get("review_gate"), dict) else snapshot_gate
    snapshot_quality = snapshot.get("data_quality") if isinstance(snapshot.get("data_quality"), dict) else {}
    snapshot_summary = snapshot.get("risk_summary") if isinstance(snapshot.get("risk_summary"), dict) else {}
    summary = context.get("risk_summary") if isinstance(context.get("risk_summary"), dict) else snapshot_summary
    financial = context.get("financial") if isinstance(context.get("financial"), dict) else {}
    scope = (ci.get("scope") or context.get("scope") or financial.get("scope") or "").strip()
    accounting_standard = str(ci.get("accounting_standard", "") or "").strip()
    if "合并" in scope and "中国" not in scope and "中国" in accounting_standard:
        scope = "中国准则合并"
    elif not scope and "中国" in accounting_standard:
        scope = "中国准则合并"
    amount_unit = str(ci.get("amount_unit") or context.get("amount_unit")
                      or financial.get("amount_unit") or "").strip()
    gate_status = str(gate.get("status") or context.get("review_gate_status") or "not_run")
    gate_label = {"not_passed": "未通过", "passed": "已通过", "not_run": "未执行"}.get(
        gate_status, gate_status)
    data_status = str(context.get("data_status") or "")
    if not data_status:
        data_status = "incomplete" if (
            snapshot_quality.get("pending_risks") or snapshot_quality.get("incomplete_facts")
            or snapshot_quality.get("incomplete_metrics")) else "verified"
    data_status_label = {
        "verified": "已核验",
        "incomplete": "数据源不完整，仅供参考",
        "demo_placeholder": "演示占位数据，不代表公司实际数据",
        "unverified": "来源未核验，仅供人工复核",
    }.get(data_status, data_status)
    source_hash = str(snapshot_source.get("source_hash") or snapshot.get("source_hash")
                      or context.get("source_hash") or ci.get("source_file_sha256") or "").strip()
    source_document = str(snapshot_source.get("document_name") or ci.get("source_document") or "").strip()
    pending_count = summary.get("pending_risks", snapshot_quality.get("pending_risks", 0))
    formal_count = summary.get("total_risks", 0)
    cover_val_style = _cell_style(font, 11)
    cover = [
        ["公司名称", Paragraph(_esc(ci.get("company_name", "未提供")), cover_val_style)],
        ["股票代码", Paragraph(_esc(ci.get("stock_code", "未提供")), cover_val_style)],
        ["报告年度", Paragraph(_esc(ci.get("report_year", "未提供")), cover_val_style)],
        ["报告期间", Paragraph(_esc(ci.get("report_period") or ci.get("period") or "未提供"), cover_val_style)],
        ["会计准则", Paragraph(_esc(accounting_standard or "未提供"), cover_val_style)],
        ["数据口径", Paragraph(_esc(scope or "未记录"), cover_val_style)],
        ["金额单位", Paragraph(_esc(amount_unit or "未记录"), cover_val_style)],
        ["行业分类", Paragraph(_esc(ci.get("industry", "未提供")), cover_val_style)],
        ["审计意见", Paragraph(_esc(ci.get("audit_opinion", "未提供")), cover_val_style)],
        ["正式采信风险", Paragraph(_esc(str(formal_count)), cover_val_style)],
        ["待复核提示", Paragraph(_esc(str(pending_count)), cover_val_style)],
        ["审查门禁", Paragraph(_esc(f"{gate_status}（{gate_label}）"), cover_val_style)],
        ["人工复核", Paragraph(_esc(f"{str(bool(gate.get('human_review_required'))).lower()}（需要人工复核）"
                                      if gate.get("human_review_required") else "false"), cover_val_style)],
        ["数据状态", Paragraph(_esc(f"{data_status}（{data_status_label}）"), cover_val_style)],
        ["源文件", Paragraph(_esc(source_document or "未记录"), cover_val_style)],
        ["源文件SHA-256", Paragraph(_esc(source_hash or "未记录"), cover_val_style)],
        ["报告日期", Paragraph(_esc(datetime.now().strftime("%Y年%m月%d日")), cover_val_style)],
    ]
    ct = Table(cover, colWidths=[120, 250], hAlign="CENTER")
    ct.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, 0), (0, -1), PRIMARY),
        ("BACKGROUND", (0, 0), (0, -1), ZEBRA),
        ("ALIGN", (0, 0), (0, -1), "RIGHT"),
        ("ALIGN", (1, 0), (1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE_GRAY),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE_GRAY),
    ]))
    elements += [ct, Spacer(1, 16)]

    # 报告编号与密级标识（不硬编码免责声明标识：标识仅来自 AI_DISCLAIMER
    # 常量，保证 domain_guard 置空拦截测试的语义不被绕过）
    meta_bits = []
    if report_no:
        meta_bits.append(f"报告编号：{report_no}")
    meta_bits.append("密级：内部资料")
    # Q 补丁：构建版本戳——产物可追溯（旧实例/旧代码产物一眼可辨）
    _run_id = str(ci.get("run_id", "") or "")
    meta_bits.append(f"构建 {_build_hash()} | 实例 {_run_id[:8] if _run_id else '—'}")
    elements.append(Paragraph(_esc("    |    ".join(meta_bits)),
                              ParagraphStyle("CoverMeta", parent=st['small'], fontName=font,
                                             alignment=TA_CENTER, fontSize=9)))

    # 综合评分卡片（有评分数据时，用于综合汇总报告封面）
    if score_card and isinstance(score_card, dict) and score_card.get("score") is not None:
        try:
            score = float(score_card["score"])
            lv_color = SCORE_LEVEL_COLORS.get(str(score_card.get("level_key", "")), PRIMARY)
            sc_val = ParagraphStyle("ScVal", fontName=font, fontSize=30, leading=36,
                                    alignment=TA_CENTER, textColor=white, spaceAfter=0)
            sc_lab = ParagraphStyle("ScLab", fontName=font, fontSize=10, leading=14,
                                    alignment=TA_CENTER, textColor=white, spaceAfter=0)
            sc_cell = [Paragraph("综合风险评分（分越高风险越大）", sc_lab), Spacer(1, 4),
                       Paragraph(f"<b>{score:.1f}</b> <font size=13>/ 100</font>", sc_val), Spacer(1, 4),
                       Paragraph(_esc(f"风险等级：{score_card.get('level', '—')}"), sc_lab)]
            sc_table = Table([[sc_cell]], colWidths=[8.5 * cm], hAlign="CENTER")
            sc_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), lv_color),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ]))
            elements += [Spacer(1, 14), sc_table]
            if score_card.get("score_note"):
                elements += [Spacer(1, 6), Paragraph(_esc(score_card["score_note"]), st['small'])]
        except (TypeError, ValueError):
            pass

    # 封面底部 AI 辅助生成声明
    disclaimer_style = ParagraphStyle("Disclaimer", parent=st['small'], fontName=font,
                                      fontSize=9, leading=14, alignment=TA_CENTER,
                                      textColor=HexColor("#CC0000"), spaceAfter=6)
    elements += [Spacer(1, 24), Paragraph(AI_DISCLAIMER, disclaimer_style), PageBreak()]
    return elements


def _toc_elements(toc_items, st, font):
    """目录页：章节导航表（章节 | 内容说明），斑马纹样式，以 PageBreak 结束。"""
    elements = [Paragraph("目  录", st['title']),
                HRFlowable(width="40%", thickness=1.5, color=ACCENT, hAlign="CENTER",
                           spaceBefore=0, spaceAfter=24)]
    toc_data = [["章节", "内容说明"]]
    for title, desc in toc_items:
        toc_data.append([Paragraph(_esc(title), _cell_style(font, 11)),
                         Paragraph(_esc(desc), _cell_style(font, 10))])
    elements += [_styled_table(toc_data, [185, 283], font, font_size=11), PageBreak()]
    return elements


def _profile_body(report, st):
    """公司简介章正文：取 company_info.company_profile（兼容顶层别名）。

    缺失时渲染占位说明，不崩溃。

    Returns:
        正文流元素列表（首元素为含简介/占位文本的 Paragraph）
    """
    ci = report.get("company_info", {}) or {}
    profile = ci.get("company_profile") or report.get("company_profile") or ""
    profile = str(profile).strip()
    if not profile:
        return [Paragraph("（公司简介未提供：可从年报「公司概况」章节补充）", st['small'])]
    return [Paragraph(_para_text(profile), st['body'])]


def _disclaimer_page_elements(st):
    """免责声明页：独立章节，明确 AI 生成性质与使用限制。"""
    elements = [PageBreak(), Paragraph("免责声明", st['h1']),
                HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=12)]
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
    return elements


def _system_facts_text(risk, validation="", financial="",
                       disclosure="", audit_opinion=""):
    """系统量化事实（纯文本版，PDF/Excel 复用）：从确定性工具结果回填量化数字。

    背景：风险明细的 evidence/data_analysis 是 LLM 散文，量化数字不可追溯（实测
    缺陷：R005 类 evidence 数字与工具结果矛盾）。本函数按风险维度把相关工具量化
    结果确定性渲染，供人工核对；不修改 LLM 原文。无对应事实返回空串。
    """
    lines = []
    dim = str(risk.get("dimension", "") or "")
    text = f"{risk.get('title', '')} {risk.get('evidence', '')}"
    n = _norm_dim(dim)
    _REL = {"data_reliability", "数据可靠性风险", "数据可靠性"}
    _FIN = {"financial_misstatement", "财务错报风险", "财务错报",
            "going_concern", "持续经营风险", "持续经营"}
    _COMP = {"disclosure_compliance", "信息披露合规风险", "信息披露合规", "信披合规",
             "regulatory_penalty", "监管处罚类高风险", "监管处罚",
             "related_party", "关联交易风险", "关联交易"}
    vd = _safe_json(validation)
    dv = (vd.get("data_validation") or {}) if isinstance(vd, dict) else {}
    checks = dv.get("all_checks") if isinstance(dv.get("all_checks"), list) else []
    if n in _REL or "勾稽" in text:
        for c in checks:
            if isinstance(c, dict) and c.get("check"):
                st_txt = {True: "通过", False: "未通过", None: "未校验/提示"}.get(c.get("passed"))
                lines.append(f"勾稽校验·{c.get('check')}: {st_txt} | "
                             f"差异 {c.get('difference_pct', '—')} | "
                             f"{str(c.get('message', ''))[:80]}")
    if n in _FIN or "财务" in text:
        fi = _safe_json(financial)
        alerts = fi.get("alerts") if isinstance(fi, dict) else None
        if isinstance(alerts, list) and alerts:
            lines.append("财务预警: " + "；".join(str(a)[:60] for a in alerts[:3]))
    if n in _COMP or "披露" in text:
        dc = _safe_json(disclosure)
        if isinstance(dc, dict) and dc.get("compliance_score") is not None:
            lines.append(f"披露合规: 评分 {dc.get('compliance_score')} | "
                         f"风险分 {dc.get('risk_score', '—')} | "
                         f"检查 {dc.get('checked_items', '—')} 项/通过 {dc.get('passed_items', '—')} 项")
    if "审计意见" in text:
        ao = _safe_json(audit_opinion)
        op = (ao.get("audit_opinion") or {}) if isinstance(ao, dict) else {}
        if isinstance(op, dict) and op.get("opinion_type"):
            lines.append(f"审计意见: {op.get('opinion_type')} | 性质 {op.get('opinion_nature', '—')}")
    return "\n".join(lines)


def _system_facts_note(risk, st, validation="", financial="",
                       disclosure="", audit_opinion=""):
    """系统量化事实核对区块（事实层注入，PDF 版）：小字灰底供人工核对。"""
    text = _system_facts_text(risk, validation, financial,
                              disclosure, audit_opinion)
    if not text:
        return []
    body = [Paragraph("<b>系统量化事实（确定性计算，供核对）：</b>", st['small'])]
    for ln in text.split("\n"):
        if ln.strip():
            body.append(Paragraph(f"  · {_esc(ln.strip())}", st['small']))
    body.append(Spacer(1, 3))
    return body


def _risk_id_label(r: dict) -> str:
    """风险编号展示：主 risk_id + 语义编号（50d semantic_id 存在且不同时并列）。"""
    sid = str(r.get("semantic_id", "") or "").strip()
    rid = str(r.get("risk_id", "") or "").strip()
    if sid and sid != rid:
        return f"{rid}（{sid}）"
    return rid or sid


def _risk_detail_elements(rd_list, st, facts_ctx=None):
    """风险明细章节正文（三级编号结构，3 份拆分报告共用）。

    编号层级：
    - 一级：风险序号 `{i}. {risk_id}｜{结论式标题}`（h2 小标题）
    - 二级：字段标签固定编号 (1)年报原文证据 (2)异常数据分析 (3)法规依据 (4)案例参考 (5)审计核查建议
    - 三级：字段内容多行时逐行自动 (1)(2)… 子编号

    facts_ctx: 可选 dict（validation/financial/disclosure/audit_opinion 工具 JSON）——
    提供时每条风险末尾追加"系统量化事实"核对区块（事实层注入，量化数字可追溯）。

    Returns:
        流元素列表
    """
    elements = []
    if not rd_list:
        return [Paragraph("本维度未识别出风险事项。", st['body'])]
    for idx, risk in enumerate(rd_list, 1):
        lv = risk.get("level", "")
        lc = LEVEL_TEXT_COLORS.get(lv, black)   # 等级文字色（黄为深黄，保证白底可读）
        # 置信度格式化：数值保留两位小数，非数值（LLM 偶发字符串）转义后原样展示
        conf = risk.get('confidence', 0)
        conf_text = f"{conf:.2f}" if isinstance(conf, (int, float)) else _esc(conf)
        dim_text = _risk_display_dimension(risk)
        # 一级编号：序号 + 风险编号 + 结论式标题（标题即小标题，一眼看懂风险实质）
        _sid = _risk_id_label(risk)
        _pend = "【证据不足·待复核提示】" if risk.get("pending_verification") else ""
        re = [
            Paragraph(f"{idx}. {_esc(_sid)}｜{_pend}{_esc(_risk_display_title(risk))}", st['h2']),
            Paragraph(f"<b>风险维度：</b>{_esc(dim_text)}  |  <b>风险等级：</b>"
                      f"<font color='{lc.hexval()}'>{_esc(lv)}</font>  |  <b>置信度：</b>{conf_text}",
                      st['small']), Spacer(1, 5),
        ]
        # 二级编号：按固定顺序渲染 5 个详细字段（仅在字段非空时显示，编号固定不随缺项漂移）
        for fidx, (label, key) in enumerate([("年报原文证据", "evidence"), ("异常数据分析", "data_analysis"),
                                             ("法规依据", "regulatory_basis"), ("案例参考", "case_reference"),
                                             ("审计核查建议", "audit_suggestion")], 1):
            v = risk.get(key, "")
            if not v:
                continue
            re += [Paragraph(f"<b>({fidx}) {label}：</b>", st['small'])]
            # 三级编号：多行内容逐行 (1)(2)…；单行按普通段落渲染
            lines = _numbered_lines(v)
            if lines:
                for li, ln in enumerate(lines, 1):
                    re.append(Paragraph(_esc(f"({li}) {ln}"), st['body']))
            else:
                re.append(Paragraph(_para_text(v), st['body']))
            re.append(Spacer(1, 3))
        # 审计补强五项（涉及科目/适用认定/核查程序/所需材料/企业改进建议）：
        # 编号接续前五项固定为 (6)-(10)，不随缺项漂移；核查程序一律为待执行建议，
        # 缺项由 audit_reinforcement 模板补齐并在下方标注来源，不冒充模型结论。
        from tools.audit_reinforcement import reinforcement_items
        for bidx, (blabel, bvalues) in enumerate(reinforcement_items(risk), 6):
            if not bvalues:
                continue
            re += [Paragraph(f"<b>({bidx}) {blabel}：</b>", st['small'])]
            if len(bvalues) > 1:
                for li, item in enumerate(bvalues, 1):
                    re.append(Paragraph(_esc(f"({li}) {item}"), st['body']))
            else:
                re.append(Paragraph(_para_text(bvalues[0]), st['body']))
            re.append(Spacer(1, 3))
        if str(risk.get("reinforcement_source", "") or "") == "template":
            re += [Paragraph("（上述补强项由系统模板按风险维度生成，需项目组按重要性调整）",
                             st['small']), Spacer(1, 3)]
        # 渲染思维链（reasoning_chain）：结构化展示推理过程（逐步编号）
        chain = risk.get("reasoning_chain", [])
        if chain and isinstance(chain, list):
            re += [Paragraph("<b>推理思维链：</b>", st['small']), Spacer(1, 2)]
            for sidx, step_item in enumerate(chain, 1):
                if isinstance(step_item, dict):
                    step_label = step_item.get("step", "")
                    step_detail = step_item.get("detail", "")
                    if step_label and step_detail:
                        re += [Paragraph(f"  ({sidx}) [{_esc(step_label)}] {_esc(step_detail)}", st['body'])]
            re += [Spacer(1, 3)]
        # 趋势分析（可选字段）：非标量（dict/list）序列化为 JSON 文本，避免渲染 Python repr
        trend = risk.get("trend_analysis")
        if trend:
            trend_text = trend if isinstance(trend, str) else json.dumps(trend, ensure_ascii=False)
            re += [Paragraph("<b>趋势分析：</b>", st['small']),
                   Paragraph(_para_text(trend_text), st['body']), Spacer(1, 3)]
        # 事实层注入：系统量化事实核对区块（确定性工具结果，供人工核对 LLM 数字）
        if facts_ctx:
            re += _system_facts_note(risk, st, **facts_ctx)
        # 风险条目间细线分隔。长条目允许在段落边界自然跨页，避免整条
        # KeepTogether 把大量内容推到下一页并制造大面积空白。
        re.append(HRFlowable(width="100%", thickness=0.5, color=LINE_GRAY, spaceBefore=6, spaceAfter=8))
        elements.extend(re)
    return elements


def _split_risks(rd_list, dims):
    """按维度集合过滤风险明细（简称/全称/英文标识符统一归一后匹配）。"""
    return [r for r in (rd_list or []) if _norm_dim(r.get("dimension", "")) in dims]


def _level_counts(risks):
    """统计风险列表的等级分布：返回 {重大: n, 重要: n, 一般: n}。

    兼容 LLM 输出的非标准等级取值（高/中/低、高风险、risk_level 键名等），
    统一归一为三档后计数，避免非标准取值被静默漏计。
    """
    c = {"重大": 0, "重要": 0, "一般": 0}
    for r in risks or []:
        lv = str(r.get("level") or r.get("risk_level") or "").strip()
        lv = LEVEL_ALIASES.get(lv)
        if lv:
            c[lv] += 1
    return c


def _formal_risks(report: dict) -> list:
    """返回已通过证据门禁的正式风险；旧台账没有该字段时兼容原行为。"""
    # 终局前 report_snapshot 仍可能是 _apply_review_gates 创建的中间快照，
    # 其正式风险层落后于刚完成的门禁结果。只要兼容字段存在，就以当前
    # accepted_risk_details 为准；终局快照适配器会同步回写这两个字段。
    if "accepted_risk_details" in report:
        return [r for r in (report.get("accepted_risk_details") or []) if isinstance(r, dict)]
    snapshot = report.get("report_snapshot") if isinstance(report, dict) else None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("risks"), dict):
        return [r for r in (snapshot["risks"].get("formal") or []) if isinstance(r, dict)]
    return [r for r in (report.get("risk_details") or []) if isinstance(r, dict)]


def _reconcile_summary(report: dict) -> list:
    """校正 risk_summary 与 risk_details 的一致性：KPI 以明细统计为准回写，返回差异警告文案。

    LLM 的 risk_summary 与 risk_details 是两套独立输出，可能自相矛盾
    （如 total_risks=2 而明细 5 条），此处强制以明细为唯一事实源，
    差异项回写为明细统计值，并收集警告供报告内展示。
    """
    rd = _formal_risks(report)
    all_rd = report.get("risk_details") or []
    rs = report.get("risk_summary")
    if not isinstance(rs, dict):
        rs = report["risk_summary"] = {}
    c = _level_counts(rd)
    warnings = []
    for key, computed in (("total_risks", len(rd)), ("major_risks", c["重大"]),
                          ("important_risks", c["重要"]), ("general_risks", c["一般"])):
        declared = rs.get(key)
        if declared is not None and declared != computed:
            warnings.append(f"台账{key}={declared}与明细统计{computed}不一致，已按明细重算")
        rs[key] = computed
    # 维度分布同步以明细为准（修复仲裁新增/系统校验条目未计入维度分布表，实测缺陷：
    # R008 市场风险、V001 数据可靠性风险在明细 9 条中但维度表只有 5 维 7 条；
    # 且键须经 _norm_dim 归一化，否则"财务错报"与"财务错报风险"分裂成两行）
    dim_counts = {}
    for r in rd:
        if isinstance(r, dict):
            d = _norm_dim(r.get("dimension", "") or "")
            dim_counts[d] = dim_counts.get(d, 0) + 1
    if rs.get("risk_dimensions") != dim_counts:
        warnings.append("台账risk_dimensions与明细统计不一致，已按明细重算")
        rs["risk_dimensions"] = dim_counts
    rs["pending_risks"] = sum(1 for r in all_rd if isinstance(r, dict) and r.get("formal_status") != "accepted")
    rs["review_gate_status"] = (report.get("review_gate") or {}).get("status", "not_run")
    # 整体结论是 LLM 生成的自由文本，可能仍带有门禁前的风险数量（例如
    # "系统采信风险0项"）。导出层的统计卡虽已按最终明细重算，但结论段落若不
    # 同步会造成同一份底稿自相矛盾。只修正确定性的数量/分层短语，保留其余分析叙述。
    assessment = report.get("overall_assessment")
    if isinstance(assessment, str) and assessment:
        pending_count = rs["pending_risks"]
        assessment = re.sub(r"识别\s*\d+\s*项风险",
                             f"识别{len(all_rd)}项风险", assessment)
        assessment = re.sub(
            r"识别(?:出)?\s*\d+\s*项待(?:核实|核查)事项",
            f"识别出{pending_count}项待复核提示",
            assessment,
        )
        assessment = re.sub(r"系统采信风险\s*\d+\s*项",
                             f"系统采信风险{len(rd)}项", assessment)
        if pending_count:
            assessment = assessment.replace(
                "所有风险均为建议关注等级",
                f"另有{pending_count}项为待复核提示")
        report["overall_assessment"] = assessment
    return warnings


def _audit_opinion_source(audit_opinion_json: str, disclosure_check_json: str):
    """审计意见数据源状态判定：识别工具与披露检查双源合并。

    背景：identify_audit_opinion 未被 LLM 调用时 sources 一律标"未获取"，但披露
    检查（check_disclosure_compliance）已输出 audit_opinion 字段（如"未经审计
    （半年度报告）"），导致封面显示意见、数据来源说明却标未获取的"薛定谔状态"
    （实测缺陷）。

    Args:
        audit_opinion_json: identify_audit_opinion 工具结果（可为空）
        disclosure_check_json: check_disclosure_compliance 工具结果（可为空）

    Returns:
        (used: bool, note: str)
    """
    ao = _safe_json(audit_opinion_json)
    if ao and "error" not in ao:
        return True, ""
    dc = _safe_json(disclosure_check_json)
    op = dc.get("audit_opinion") if isinstance(dc, dict) else None
    if op and str(op).strip() and str(op).strip() != "未识别":
        return True, f"来源：披露规范性检查识别（{str(op).strip()}）"
    return False, "LLM 未调用审计意见识别工具或年报文本未包含审计报告段落"


def _assemble_chapters(chapters, st, font):
    """为章节列表分配中文序号并生成目录元素；h1 标题下方加主色横线。

    Args:
        chapters: [(章节名, 正文流元素列表, 目录说明)]

    Returns:
        (toc_elements, chapter_elements) 元组
    """
    toc_items = []
    body = []
    for i, (title, chapter_body, desc) in enumerate(chapters):
        num = _CN_NUM[i] if i < len(_CN_NUM) else str(i + 1)
        toc_items.append((f"{num}、{title}", desc))
        heading = [Paragraph(f"{num}、{title}", st['h1']),
                   HRFlowable(width="100%", thickness=1.5, color=ACCENT,
                              spaceBefore=0, spaceAfter=10)]
        if chapter_body:
            # 标题与首个正文元素绑定，避免章节标题单独落在页底；后续正文
            # 仍由 Platypus 自然分页，不把整章或整条长风险锁在一起。
            anchor_count = 1
            # 专项报告通常先放一行“维度子集说明”，随后才是第一个
            # 风险 h2。标题应和首条风险小标题同页，避免章节标题与风险正文断页。
            for idx, flowable in enumerate(chapter_body[1:], 1):
                style = getattr(flowable, "style", None)
                if getattr(style, "name", "") == "CNH2":
                    anchor_count = idx + 1
                    break
            body.append(KeepTogether(heading + chapter_body[:anchor_count]))
            body += chapter_body[anchor_count:]
        else:
            body += heading
        body.append(Spacer(1, 14))
    return _toc_elements(toc_items, st, font), body


def _build_pdf_doc(elements, out_path, font, footer_label=""):
    """构建 PDF 文件：A4 版面 + 封面全宽色带 + 三段式页脚（报告名/密级/页码）。

    采用两遍构建获取总页数：第一遍统计页数，第二遍渲染「第 X 页 / 共 Y 页」。
    platypus 流元素可安全复用，第二遍覆盖写入同一输出路径。

    Args:
        elements: 全部文档流元素
        out_path: 输出文件路径
        font: 字体名称
        footer_label: 页脚左侧报告名（如「财务健康诊断报告」）
    """
    state = {"total": None}
    page_w, page_h = A4

    def _chrome(canvas, doc_obj):
        canvas.saveState()
        # 封面页：顶部全宽藏蓝色带 + 辅色细条
        if doc_obj.page == 1:
            canvas.setFillColor(PRIMARY)
            canvas.rect(0, page_h - 0.9 * cm, page_w, 0.9 * cm, stroke=0, fill=1)
            canvas.setFillColor(ACCENT)
            canvas.rect(0, page_h - 1.05 * cm, page_w, 0.15 * cm, stroke=0, fill=1)
        # 页脚：分隔线 + 左报告名 / 中密级标注 / 右页码
        canvas.setStrokeColor(LINE_GRAY)
        canvas.setLineWidth(0.5)
        canvas.line(2 * cm, 1.7 * cm, page_w - 2 * cm, 1.7 * cm)
        canvas.setFillColor(TEXT_MUTED)
        canvas.setFont(font, 7.5)
        canvas.drawString(2 * cm, 1.35 * cm, footer_label or "上市公司年报风险识别报告")
        canvas.drawCentredString(page_w / 2, 1.35 * cm, "内部资料 · AI 辅助生成")
        page_text = f"第 {doc_obj.page} 页"
        if state["total"]:
            page_text += f" / 共 {state['total']} 页"
        canvas.drawRightString(page_w - 2 * cm, 1.35 * cm, page_text)
        canvas.restoreState()

    def _new_doc():
        return SimpleDocTemplate(out_path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                                 topMargin=2.5 * cm, bottomMargin=2.5 * cm)

    # doc.build 会原地消费流元素（split/wrap 改写），故第一遍用深拷贝统计页数，
    # 第二遍必须用原始 elements 列表重新构建，否则产出空文件。
    doc = _new_doc()
    doc.build(copy.deepcopy(elements), onFirstPage=_chrome, onLaterPages=_chrome)
    state["total"] = doc.page
    _new_doc().build(elements, onFirstPage=_chrome, onLaterPages=_chrome)


def _check_disclaimer(elements, doc_kind):
    """领域约束后置校验：导出内容必须包含 AI 免责声明。

    在真正构建/落盘 PDF 前拦截：若模板被误改导致声明缺失，则不产出文件，
    直接返回带修复方向的错误，避免生成看似正式审计意见的无声明报告。

    Returns:
        None（校验通过）或错误信息字符串（被拦截）
    """
    from tools.domain_guard import collect_flowable_texts, assert_disclaimer_present, DisclaimerMissingError
    try:
        assert_disclaimer_present(collect_flowable_texts(elements), doc_kind=doc_kind)
        return None
    except DisclaimerMissingError as e:
        logger.error(f"PDF导出被拦截: {e}")
        return f"导出被拦截：{e}"


# ── 拆分报告数据章节（指标与审计关注/披露检查/合规标准/评分解读/方法论） ──

def _financial_section_body(fin_json, st, font, risk_json_str, industry="", note=""):
    """财务健康报告「财务指标与审计关注分析」章正文。

    数据源为 calculate_financial_indicators 工具结果（{indicators, alerts}）；
    入参为空/解析失败时返回空列表（调用方跳过本章）。
    内容：四维分组判读表（数值|基准|偏离|专业判读）→ 指标对比柱状图
    → 量化预警 → 雷达图 → 热力图。判读为确定性阈值模板生成。
    note 为跨期穿透提示（50c）：应收背离告警触发时注入的确定性注记，
    章首橙色渲染（防 LLM 用当期静态现金流比率掩盖应收激增隐患）。
    """
    try:
        fin = json.loads(fin_json) if isinstance(fin_json, str) else fin_json
    except Exception:
        return []
    if not isinstance(fin, dict):
        return []
    body = []
    # 50c：跨期穿透提示（确定性注记，章首可见）
    if note:
        body.append(Paragraph(
            "<font color='" + WARN_ORANGE.hexval() + "'><b>跨期穿透提示：</b>"
            + _esc(note) + "</font>", st['body']))
        body.append(Spacer(1, 6))
    indicators = fin.get("indicators", {})
    if not isinstance(indicators, dict):
        indicators = {}
    benchmarks = _load_benchmarks(str(industry or ""))
    amount_unit = str(fin.get("amount_unit", "") or "")
    amount_indicator_keys = {"net_profit_current", "net_profit_previous"}

    # (1) 四维专业判读表
    if indicators:
        rows = [["分析维度", "指标", "数值", "行业基准", "偏离基准", "专业判读"]]
        chart_pct, chart_mult = [], []
        for dim_name, keys in _DIM_GROUPS:
            first_in_group = True
            for k in keys:
                if k not in indicators or indicators[k] is None:
                    continue
                v = indicators[k]
                try:
                    vnum = float(v)
                except (TypeError, ValueError):
                    vnum = None
                bench = benchmarks.get(_BENCH_KEY_MAP[k]) if k in _BENCH_KEY_MAP else None
                bench_text = f"{bench:g}" if isinstance(bench, (int, float)) else "—"
                dev_text = "—"
                if vnum is not None and isinstance(bench, (int, float)) and bench != 0:
                    dev_text = f"{(vnum - bench) / bench * 100:+.1f}%"
                verdict, bad = _interpret_indicator(k, vnum, bench, indicators, industry)
                verdict_html = (f"<font color='{TEXT_RED}'>{_esc(verdict)}</font>" if bad
                                else _esc(verdict))
                cell = _cell_style(font, 8.5)
                display_value = _fmt_num(
                    v,
                    input_unit=amount_unit if k in amount_indicator_keys else None,
                )
                rows.append([
                    _esc(dim_name) if first_in_group else "",
                    Paragraph(_esc(_INDICATOR_CN.get(k, k)), cell),
                    Paragraph(display_value, cell),
                    bench_text, dev_text,
                    Paragraph(verdict_html, cell),
                ])
                first_in_group = False
                # 收集对比柱状图序列（仅有行业基准的指标可对比）
                if bench is not None and vnum is not None:
                    short_name = _INDICATOR_CN.get(k, k).split("(")[0]
                    if k in _PCT_KEYS:
                        chart_pct.append([short_name, vnum, bench])
                    else:
                        chart_mult.append([short_name, vnum, bench])
        if len(rows) > 1:
            body += [Paragraph("<b>（一）核心指标与审计关注分析（对比行业基准）</b>", st['small']),
                     _styled_table(rows, [60, 96, 74, 48, 50, 152], font, font_size=8.5),
                     Spacer(1, 6),
                     Paragraph("注：" + _esc(_benchmark_basis_note(bool(benchmarks))) + ""
                               "「—」表示该指标无对应基准或不可量化对比。红色判读为负面风险信号。",
                               st['caption'])]
        # 指标对比柱状图
        if chart_pct or chart_mult:
            bars = _embed_chart(_gen_indicator_bars,
                                json.dumps({"pct": chart_pct, "mult": chart_mult}, ensure_ascii=False),
                                width_cm=16.0)
            if bars:
                body += [bars, Paragraph("图：核心财务指标与行业基准对比", st['caption'])]

    # (2) 量化预警提示
    alerts = fin.get("alerts", [])
    if alerts:
        body += [Paragraph("<b>（二）量化预警提示：</b>", st['body']), Spacer(1, 2)]
        for i, a in enumerate(alerts, 1):
            body.append(Paragraph(_esc(f"({i}) {a}"), st['body']))
        body.append(Spacer(1, 8))

    # (3) 指标计算过程与依据（公式→输入→代入→结果→阈值→状态→原文定位）
    body += _indicator_chain_body(fin_json, st, font)

    # (4) 雷达图 + 热力图：雷达图需注入 calculated_indicators 才有真实数值
    radar_json = risk_json_str
    if indicators:
        try:
            merged = dict(json.loads(risk_json_str)) if risk_json_str else {}
            merged["calculated_indicators"] = indicators
            radar_json = json.dumps(merged, ensure_ascii=False)
        except Exception:
            pass
    radar = _embed_chart(_generate_radar_chart, radar_json)
    if radar:
        radar_data = _safe_json(radar_json)
        radar_payload = (radar_data.get("visualization_payloads") or {}).get("radar") \
            if isinstance(radar_data, dict) else {}
        radar_metrics = radar_payload.get("metrics", []) if isinstance(radar_payload, dict) else []
        has_comparable_benchmark = any(
            isinstance(item, dict)
            and isinstance(item.get("actual"), (int, float))
            and isinstance(item.get("benchmark"), (int, float))
            for item in radar_metrics
        )
        radar_caption = (
            "图：五维财务指标雷达图（红=公司实际，蓝=已核验行业基准；"
            "仅对可比维度绘制）"
            if has_comparable_benchmark else
            "图：五维财务指标雷达图（仅展示已获取公司值；数据不完整或无可核验行业基准，"
            "仅供参考）"
        )
        body += [Paragraph("<b>（四）财务指标雷达图：</b>", st['small']),
                 radar, Paragraph(radar_caption, st['caption'])]
    heatmap = _embed_chart(_generate_risk_heatmap, risk_json_str)
    if heatmap:
        body += [Paragraph("<b>（五）审计风险热力图（五维度×等级）：</b>", st['small']),
                 heatmap, Paragraph("图：风险热力图（列色按等级：重大红/重要黄/一般蓝；"
                                    "格内为去重后的正式风险数）", st['caption'])]
    return body


def _fmt_score(v) -> str:
    """评分展示统一格式：非整数保留一位小数，整数不拖尾（85.7 → "85.7"、100.0 → "100"），
    与系统量化事实/底层数据严格一致（防概览 86 vs 底层 85.7 的四舍五入出入）。"""
    try:
        return f"{float(v):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _indicator_chain_body(fin_json, st, font) -> list:
    """「指标计算过程与依据」章：逐项呈现公式→输入→代入→结果→阈值→状态→原文定位。

    数据来自计算层的结构化记录（metric_results/evidence），渲染走
    :mod:`tools.indicator_view` 与网页同源；未计算指标另列并附原因，
    不以中性值补齐，也不把模型散文当依据。
    """
    from tools.indicator_view import build_indicator_view

    view = build_indicator_view(fin_json)
    if not view.get("available") or not view.get("groups"):
        note = str(view.get("note", "") or "未获取可呈现的指标计算结果")
        return [Paragraph("<b>（三）指标计算过程与依据：</b>", st['small']),
                Paragraph(_esc(note), st['small'])]
    body = [Paragraph("（三）指标计算过程与依据", st['small']),
            Paragraph("下表逐项给出财务指标与审计关注分析的公式、输入值及单位、期间与口径、"
                      "代入过程、结果、阈值及来源、状态与原文定位，供人工复算与追溯；"
                      "代入过程（含结果）由本地确定性计算生成，未经模型转述。", st['caption'])]
    header = ["指标 / 结果", "公式、输入值及单位、代入过程", "期间、阈值、状态及原文定位"]
    widths = [100, 236, 132]
    evidence_index = {str(e.get('evidence_id')): e for e in _safe_json(fin_json).get('evidence', []) if isinstance(e, dict)}
    for group in view.get("groups", []):
        rows = [header]
        for metric in group.get("metrics", []):
            threshold = ("—" if metric.get("threshold") in (None, "") else str(metric["threshold"]))
            if metric.get("threshold_source"):
                threshold += f"（{metric['threshold_source']}）"
            cell = _cell_style(font, 8.5)
            references = []
            for evidence_id in metric.get('evidence_ids', []):
                source = evidence_index.get(str(evidence_id), {})
                references.append(' '.join(str(source.get(k) or '') for k in ('page', 'locator')).strip() or str(evidence_id))
            source_ref = '；'.join(dict.fromkeys(references)) or '未提供原文定位'
            calculation = '<br/>'.join(_esc(str(metric.get(k, ''))) for k in ('formula', 'inputs_text', 'substitution'))
            basis = '<br/>'.join(_esc(str(v)) for v in (metric.get('period_scope', ''), threshold, metric.get('status', ''), source_ref))
            rows.append([
                Paragraph(_esc(str(metric.get('name', ''))) + '<br/><b>' + _esc(str(metric.get('display_value', ''))) + '</b>', cell),
                Paragraph(calculation, cell),
                Paragraph(basis, cell),
            ])
        body += [Paragraph(f"<b>{_esc(group.get('label', ''))}</b>", st['small']),
                 _styled_table(rows, widths, font, font_size=8.5), Spacer(1, 6)]
    uncalculated = view.get("uncalculated") or []
    if uncalculated:
        lines = [f"{_esc(item.get('name', ''))}（{_esc(item.get('status', ''))}）："
                 f"{_esc(item.get('reason', '') or '未说明原因')}" for item in uncalculated]
        body += [Paragraph("<b>未计算指标（不以中性值补齐，需人工确认输入或期间口径）：</b>",
                           st['small'])]
        body += [Paragraph(f"  · {line}", st['small']) for line in lines]
    if view.get("note"):
        body.append(Paragraph(_esc(str(view["note"])), st['caption']))
    return body


def _pct_change_delta(cur, prev):
    """同比变动百分比（与 financial_calculator._pct_change 同语义）。
    无两期数据或上期为零时返回 None（缺失不编造）。"""
    try:
        c, p = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return 0.0 if c == 0 else None
    return round((c - p) / abs(p) * 100, 2)


def _statements_basic_analysis(fin_json):
    """三大报表基本分析（确定性规则层，非 LLM 判读）。

    解析 calculate_financial_indicators 输出中的 statement_items（原始科目值
    inputs_echo），按三表分组计算：本期/上期/同比%/占比%；仅有两期数据时算同比
    （资产负债表科目大多只有期末值，同比列显示 "—"），占比（资产结构 % =
    科目/总资产，营收占比 % = 科目/营收）。判读只输出建议性短语
    （"关注/提示"），不下定性结论。

    Args:
        fin_json: calculate_financial_indicators 的 JSON 输出

    Returns:
        (tables, notes, missing_flag)
        tables: [("表名", 口径说明, [行...])]，行 = (科目, 本期, 上期, 同比, 占比)
        notes: 判读文本列表；missing_flag: 三表科目全缺时为 True
    """
    try:
        fin = json.loads(fin_json) if isinstance(fin_json, str) else fin_json
        items = (fin or {}).get("statement_items") or {}
    except Exception:
        return [], [], True
    bs = items.get("balance_sheet") or {}
    inc = items.get("income_statement") or {}
    csf = items.get("cashflow_statement") or {}
    if not (bs or inc or csf):
        return [], [], True

    tables = []
    notes = []
    ta = bs.get("total_assets_current")
    rev = inc.get("revenue_current")
    report_period = str((fin or {}).get("period", "") or "")
    ar_c, ar_p = bs.get("accounts_receivable_current"), bs.get("accounts_receivable_previous")

    # ── 资产负债表（期末值；与上年末比较，不称同比）──
    bs_rows = []
    for key, name in (
            ("total_assets_current", "总资产"), ("total_liabilities_current", "总负债"),
            ("net_assets_current", "净资产"), ("cash_and_equivalents_current", "现金及现金等价物"),
            ("accounts_receivable_current", "应收账款"), ("inventory_current", "存货"),
            ("current_assets_current", "流动资产"), ("current_liabilities_current", "流动负债"),
            ("short_term_debt_current", "短期借款"), ("goodwill_current", "商誉"),
            ("other_receivables_current", "其他应收款")):
        v = bs.get(key)
        if v is None:
            continue
        prev = bs.get(key.replace("_current", "_previous"))
        yoy = _pct_change_delta(v, prev) if prev is not None else None
        ratio = round(v / ta * 100, 2) if ta else None
        bs_rows.append((name, v, prev, yoy, ratio))
    if bs_rows:
        tables.append(("资产负债表", "期末值；变动列为较上年末", bs_rows))
    # 期末应收余额（点时）与半年度累计收入（期间流量）通常不是同一比较
    # 基础。明确为中期报告时不生成“应收同比高于收入”的判读；没有期间
    # 元数据的通用测试/旧数据沿用原规则，避免无依据改变既有离线输出。
    if not re.search(r"半年度?|上半年|中期|H[12]", report_period, re.I):
        if ar_c is not None and ar_p is not None and rev is not None and inc.get("revenue_previous"):
            ar_yoy = _pct_change_delta(ar_c, ar_p)
            rev_yoy = _pct_change_delta(rev, inc.get("revenue_previous"))
            if ar_yoy is not None and rev_yoy is not None and ar_yoy > rev_yoy + 5:
                notes.append("关注销售回款质量：应收账款变动高于营业收入变动（期间口径待核实）")
    gw, na = bs.get("goodwill_current"), bs.get("net_assets_current")
    if gw is not None and na and gw / na > 0.3:
        notes.append("提示：商誉占净资产比例偏高（>30%），关注减值风险")
    sd, cash = bs.get("short_term_debt_current"), bs.get("cash_and_equivalents_current")
    if sd is not None and cash is not None and sd > cash:
        notes.append("提示：短期借款高于现金及现金等价物，短期偿债压力需关注")

    # ── 利润表（半年度累计，同比对上年同期）──
    inc_rows = []
    for key, name in (
            ("revenue_current", "营业收入"), ("cost_of_goods_current", "营业成本"),
            ("net_profit_current", "净利润（合并）")):
        v = inc.get(key)
        if v is None:
            continue
        prev = inc.get(key.replace("_current", "_previous"))
        yoy = _pct_change_delta(v, prev) if prev is not None else None
        ratio = round(v / rev * 100, 2) if rev else None
        inc_rows.append((name, v, prev, yoy, ratio))
    if inc_rows:
        tables.append(("利润表", "半年度累计，同比对上年同期", inc_rows))
    if rev and inc.get("cost_of_goods_current"):
        gm = round((rev - inc["cost_of_goods_current"]) / rev * 100, 2)
        if gm < 0:
            notes.append("提示：毛利率为负，需关注主营业务盈利能力")
    np_c, np_p = inc.get("net_profit_current"), inc.get("net_profit_previous")
    if np_c is not None and np_p is not None:
        np_yoy = _pct_change_delta(np_c, np_p)
        if np_yoy is not None:
            if abs(np_yoy) <= 0.05:
                notes.append("净利润同比基本持平")
            else:
                notes.append(f"净利润同比{'上升' if np_yoy > 0 else '下降'} {abs(np_yoy)}%")

    # ── 现金流量表（半年度累计，同比对上年同期）──
    csf_rows = []
    for key, name in (
            ("operating_cashflow_current", "经营活动现金流量净额"),
            ("interest_income_current", "利息收入"), ("interest_expense_current", "利息支出")):
        v = csf.get(key)
        if v is None:
            continue
        prev = csf.get(key.replace("_current", "_previous"))
        yoy = _pct_change_delta(v, prev) if prev is not None else None
        csf_rows.append((name, v, prev, yoy, None))
    if csf_rows:
        tables.append(("现金流量表", "半年度累计，同比对上年同期", csf_rows))
    ocf, np = csf.get("operating_cashflow_current"), inc.get("net_profit_current")
    if ocf is not None and np is not None and np > 0:
        cash_ratio = ocf / np
        if cash_ratio < 1:
            notes.append(f"提示：经营现金流/净利润={cash_ratio:.2f}<1，盈利含金量需关注")

    return tables, notes, False


def _statements_section_body(fin_json, st, font):
    """财务健康报告「三大报表基本分析」章正文：三表结构化表格 + 确定性判读。

    纯规则渲染（不依赖 LLM）：金额用 _fmt_num 万/亿自动换算，同比/占比保留
    两位小数；科目缺失跳过该行；三表关键科目全缺时输出可见降级提示。
    """
    tables, notes, missing = _statements_basic_analysis(fin_json)
    try:
        fin_data = json.loads(fin_json) if isinstance(fin_json, str) else fin_json
    except Exception:
        fin_data = {}
    amount_unit = (fin_data or {}).get("amount_unit", "") if isinstance(fin_data, dict) else ""
    if missing:
        return [Paragraph(
            "<font color='" + WARN_ORANGE.hexval() + "'><b>提示：</b>因关键科目缺失未能生成完整"
            "三表分析，请以年报原始财务报表为准。</font>", st['body'])]
    body = []
    for tname, cal, rows in tables:
        body.append(Paragraph(f"<b>{_esc(tname)}</b>（{_esc(cal)}）", st['body']))
        body.append(Spacer(1, 3))
        change_label = "较上年末" if tname == "资产负债表" else "同比"
        tbl_rows = [["科目", "本期", "上期", change_label, "占比"]]
        for name, cur, prev, yoy, ratio in rows:
            tbl_rows.append([
                _esc(name),
                _fmt_num(cur, input_unit=amount_unit) if cur is not None else "—",
                _fmt_num(prev, input_unit=amount_unit) if prev is not None else "—",
                f"{yoy:+.2f}%" if yoy is not None else "—",
                f"{ratio:.2f}%" if ratio is not None else "—",
            ])
        body.append(_styled_table(tbl_rows, [110, 100, 100, 76, 76], font, font_size=8.5))
        body.append(Spacer(1, 6))
    if notes:
        body.append(Paragraph("<b>判读要点：</b>", st['body']))
        for n in notes:
            body.append(Paragraph(_esc(n), st['small']))
        body.append(Spacer(1, 4))
    return body


def _disclosure_section_body(dc_json, st, font, audit_opinion_json="", note=""):
    """合规报告「披露规范性检查结果」章正文（KPI 卡 + 审计意见 + 问题清单）。

    数据源为 check_disclosure_compliance 工具结果；
    入参为空/解析失败/含 error 时返回空列表（调用方跳过本章）。
    audit_opinion_json 为 identify_audit_opinion 结果（可选）：优先用其 opinion_type
    （能识别半年报"未经审计"属性），消除"后端未识别、前端未经审计"的状态矛盾（P6）。
    note 为信披结论一致性注记（50b）：仲裁阶段补充披露类风险后，若本检查结果为
    满分/无缺失，渲染注记提示结论以仲裁后口径为准（消除「100 分」与仲裁新增
    信披风险自相矛盾）。
    """
    try:
        dc = json.loads(dc_json) if isinstance(dc_json, str) else dc_json
    except Exception:
        return []
    if not isinstance(dc, dict) or "error" in dc:
        return []
    body = []
    is_screening = dc.get("score_basis") == "internal_text_screening"

    def _f(key, default=None):
        try:
            return float(dc.get(key))
        except (TypeError, ValueError):
            return default

    comp_score = _f("compliance_score")
    risk_score = _f("risk_score")
    cards = []
    if is_screening:
        scan_score = _f("text_scan_score", comp_score)
        if scan_score is not None:
            cards.append(("文本筛查参考分", _fmt_score(scan_score), PRIMARY, white))
        cards.append(("待复核提示", str(len(dc.get("issues") or [])), ACCENT, white))
    elif comp_score is not None:
        cards.append(("披露合规评分", _fmt_score(comp_score),
                      GOOD_GREEN if comp_score >= 80 else WARN_AMBER if comp_score >= 60 else BAD_RED,
                      white))
    if not is_screening and risk_score is not None:
        cards.append(("披露风险评分", _fmt_score(risk_score),
                      GOOD_GREEN if risk_score < 30 else WARN_AMBER if risk_score < 60 else BAD_RED,
                      white))
    checked, passed = dc.get("checked_items"), dc.get("passed_items")
    if checked is not None:
        cards.append(("筛查项" if is_screening else "检查项", str(checked), PRIMARY, white))
    if passed is not None:
        # 通过项带分母（如 14/15）：勾稽校验计入检查项后，计数与扣分原因透明自洽
        cards.append(("文本命中项" if is_screening else "通过项",
                      f"{passed}/{checked}" if checked is not None else str(passed), ACCENT, white))
    if cards:
        body += [_kpi_cards(cards, font), Spacer(1, 10)]
        # P7: 实质数据勾稽校验透明化——勾稽未通过时单独成行标红，让「14/14 全绿
        # 却扣 15 分」的观感矛盾消失（实测缺陷：扣分逻辑生效但 UI 未展示失败项）
        rec_failed = dc.get("reconciliation_checks")
        if rec_failed is None:
            rec_failed = 1 if any("勾稽" in str(i) for i in (dc.get("issues") or [])) else 0
        if rec_failed:
            rec_note = ("文本筛查参考分已联动调整，差异性质仍需复核，详见下方待复核提示"
                        if is_screening else "披露合规评分已联动扣减，详见下方问题清单")
            body.append(Paragraph(
                f"<font color='{TEXT_RED}'><b>数据勾稽校验：未通过（{rec_failed} 项勾稽差异，"
                + rec_note + "）</b></font>", st['body']))
            body.append(Spacer(1, 6))
        # 口径说明：合规评分仅覆盖披露检查器规则项；风险明细含 LLM 研判的更广信号
        # （勾稽差异、关联交易定价等），两者不直接互斥（实测缺陷：100 分与重要风险并存）
        # 明确各信号归属维度，避免双重扣分：勾稽差异计入数据校验维度，关联交易计入综合研判
        if is_screening:
            body.append(Paragraph(
                "注：文本筛查参考分来自内部规则，不构成实质合规判断。即使为100分，也不代表"
                "披露完整性、交易合规性或风险消除已获确认。关键词未命中及交叉引用事项需结合"
                "全文、附注和公告复核；本清单与最终风险台账的数量、状态分别列示。", st['caption']))
            if dc.get("input_scope") in ("excerpt", "summary", "节选", "摘要"):
                body.append(Paragraph("输入为节选或摘要，仅展示文本筛查参考分，"
                                      "整份报告的披露合规分及风险分未评定。", st['body']))
        else:
            body.append(Paragraph("注：披露合规评分仅反映披露形式合规（法定章节/披露规则检查，共 "
                                  f"{str(checked) if checked is not None else '—'} 项）。"
                                  "勾稽差异按现行策略同时计入校验维度与联动扣减披露分，"
                                  "已在评分模型中备案；见风险明细。", st['caption']))
        # 50b：信披结论一致性注记——仲裁阶段补充披露类风险后，若本检查结果为
        # 满分/无缺失，附橙色注记提示结论以仲裁后口径为准（消除「100 分」与
        # 仲裁新增信披风险自相矛盾）。
        if note:
            note_label = "披露复核说明" if is_screening else "披露合规一致性提示"
            body.append(Paragraph(
                "<font color='" + WARN_ORANGE.hexval() + "'><b>" + note_label + "：</b>"
                + _esc(note) + "</font>", st['body']))
            body.append(Spacer(1, 6))

    opinion = dc.get("audit_opinion")
    # P6: 优先用 identify_audit_opinion 结果（识别半年报"未经审计"属性），disclosure_checker 兜底
    ao = _safe_json(audit_opinion_json)
    ao_op = ao.get("audit_opinion") or {}
    if (isinstance(ao_op, dict) and ao_op.get("identified") and ao_op.get("opinion_type")
            and (not is_screening or not opinion or opinion == "未识别")):
        opinion = ao_op.get("opinion_type")
    if opinion:
        if is_screening:
            opinion_note = ("中期报告可以不经审计，但证监会和交易所另有规定的除外；"
                            "仅作资料属性记录，不作为独立公司风险。"
                            if "未经审计" in str(opinion) and any(
                                word in str(opinion) for word in ("半年度", "中期")) else
                            "文本识别结果需核对所属期间、审计对象及完整报告。")
            body.append(Paragraph(f"<b>财务报告审计状态：</b>{_esc(opinion)}。" + opinion_note, st['body']))
        else:
            body.append(Paragraph(f"<b>审计意见类型：</b>{_esc(opinion)}"
                                  "（审计意见类型直接影响财报可信度判断，非标意见须重点关注）", st['body']))
    issues = dc.get("issues", [])
    if issues:
        body.append(Paragraph("<b>文本筛查待复核提示：</b>" if is_screening else
                              "<b>发现的披露问题：</b>", st['body']))
        for i, issue in enumerate(issues, 1):
            body.append(Paragraph(_esc(f"({i}) {issue}"), st['body']))
        body.append(Spacer(1, 6))
    missing = dc.get("sections_missing", [])
    if missing:
        if is_screening:
            body.append(Paragraph(f"<b>当前文本未识别的章节：</b>{_esc('、'.join(map(str, missing)))}"
                                  "（需核对报告类型、全文、章节别名及适用规则，尚未认定披露缺失）", st['body']))
        else:
            body.append(Paragraph(f"<b>年报缺失章节：</b>{_esc('、'.join(map(str, missing)))}"
                                  "（对照年报内容与格式准则核查缺失原因）", st['body']))
        # M 补丁：关键章节缺失提示——"主要会计数据和财务指标"是投资者阅读核心章节，
        # 缺失时附显性提示（不改评分权重，覆盖人工判断；v28 实证：86 分与缺失并存）
        missing_txt = " ".join(str(x) for x in missing)
        if any(kw in missing_txt for kw in ("主要会计数据", "财务指标", "主要财务数据")):
            missing_label = "当前文本未识别到" if is_screening else "缺失的章节中包含"
            body.append(Paragraph(
                "<font color='" + WARN_ORANGE.hexval() + "'><b>提示：</b>" + missing_label +
                "「主要会计数据和财务指标」类投资者阅读核心章节，可能影响对核心财务数据的"
                "直接阅读与横向可比，建议人工确认披露完整性并评估影响。</font>", st['body']))
            body.append(Spacer(1, 6))
    if is_screening and dc.get("observations"):
        body.append(Paragraph("<b>已识别的披露信息：</b>", st['body']))
        for observation in dc["observations"]:
            body.append(Paragraph(_esc(observation), st['small']))
    return body


def _compliance_standard_body(dc_json, st, font, comp_risks=None):
    """合规报告「合规标准对照」章正文：监管要求与本次检查结果的确定性对照表。

    comp_risks 为合规维度风险子集（LLM 台账）：关联交易对照行需与其联动，
    避免「披露检查未识别问题」与「风险明细有关联交易风险」自相矛盾。
    """
    dc = _safe_json(dc_json)
    issues = dc.get("issues", []) if isinstance(dc.get("issues"), list) else []
    issues_text = " ".join(str(i) for i in issues)
    missing = dc.get("sections_missing", []) if isinstance(dc.get("sections_missing"), list) else []

    if dc.get("score_basis") == "internal_text_screening":
        period_parts = [str(dc.get(key) or "") for key in ("period", "report_period", "report_type", "audit_opinion")]
        period_parts.extend(str(fact.get("period") or "") for fact in (dc.get("facts") or [])
                            if isinstance(fact, dict))
        period_context = " ".join(period_parts)
        is_half_year = any(term in period_context for term in ("半年度", "半年报", "中期", "half_year", "interim"))
        is_annual = not is_half_year and any(term in period_context for term in ("年度报告", "年报", "annual"))
        report_year = re.search(r"20\d{2}", period_context)
        modern_half_year = is_half_year and report_year is not None and int(report_year.group()) >= 2025
        if is_half_year:
            report_rule = "《半年度报告内容与格式准则第3号》"
            chapter_requirement = "按半年度报告要求核对章节、财务信息及重大事项，允许符合条件的概述和交叉引用"
        elif is_annual:
            report_rule = "《年度报告内容与格式准则第2号》"
            chapter_requirement = "按年度报告要求核对章节及披露内容，并确认附注和交叉引用"
        else:
            report_rule = "适用的定期报告内容与格式准则（报告类型待核验）"
            chapter_requirement = "先确认年度或中期报告类型、披露时点及适用规则版本，再核对章节要求"
        report_rule += "；具体版本须按披露时点核验"
        related_rule = ("《半年度报告内容与格式准则第3号》（2025年版）第38条"
                        if modern_half_year else "适用的定期报告及交易所关联交易披露规则")
        guarantee_rule = ("《半年度报告内容与格式准则第3号》（2025年版）第39条"
                          if modern_half_year else "适用的定期报告及交易所重大担保披露规则")
        try:
            scan_value = dc.get("text_scan_score", dc.get("compliance_score"))
            score_result = f"文本筛查参考分 {_fmt_score(float(scan_value))} 分；不构成实质合规判断"
        except (TypeError, ValueError):
            score_result = "未获取文本筛查参考分；不作合规结论"
        if dc.get("input_scope") in ("excerpt", "summary", "节选", "摘要"):
            score_result += "；输入为节选，全文合规及风险分未评定"
        related_ids = [str(r.get("risk_id") or "") for r in (comp_risks or [])
                       if isinstance(r, dict) and _norm_dim(r.get("dimension", "")) == "related_party"]
        related_result = ("文本筛查存在关联交易待复核提示，需核对所引附注、公告及实际交易资料"
                          if "关联" in issues_text else
                          "本次扫描未形成关联交易待复核项，不等同于已确认交易合规")
        if related_ids:
            related_result += "；台账相关事项 " + "、".join(related_ids) + " 按各自复核状态另行列示"
        guarantee_result = ("存在担保待复核提示；先核对重大性、担保范围及交叉引用，再判断具体披露义务"
                            if "担保" in issues_text else
                            "本次扫描未形成担保待复核项，仍需核对事项范围及适用条件")
        rows = [["核查参考", "适用条件及核查内容", "对应项目", "本次文本筛查结果"],
                ["《证券法》及适用版本的《上市公司信息披露管理办法》",
                 "核对真实、准确、完整、及时披露义务；具体主体、时点及法律责任需另行评估",
                 "内容及披露时点", "仅凭文本关键词无法确认重大遗漏、延迟披露或违法事实"],
                ["内部文本筛查规则（非法定评分标准）",
                 "分值用于辅助定位复核事项，不以80分或其他内部阈值认定规范、合规或违规",
                 "文本筛查参考", score_result],
                [report_rule, chapter_requirement, "章节线索",
                 ("当前文本未识别：" + "、".join(map(str, missing)) + "；需核对原始全文及章节别名")
                 if missing else "本次扫描未形成章节缺口提示，不等同于已确认法定内容完整"],
                [related_rule,
                 "核对适用版本、重大关联交易的范围和门槛、定价与程序；财务公司交易另核额度、余额和利率，30%不是统一监管安全线",
                 "关联交易", related_result],
                [guarantee_rule,
                 "重大担保核对金额、期限、对象、类型、决策程序及适用的担保物或反担保；核对符合条件的概述、交叉引用和既有公告",
                 "重大担保", guarantee_result],
                ["与所选会计口径相符的财务报告列报准则",
                 "区分会计准则、合并范围和比较期间；勾稽差异的原因和性质需另行复核",
                 "数据与列报", "详见原始事实、指标计算及数据校验记录；扫描结果不替代审计程序"]]
        if is_half_year:
            rows.append([
                ("《半年度报告内容与格式准则第3号》（2025年版）第9条"
                 if modern_half_year else "适用的半年度报告审计要求"),
                "半年度财务报告可不经审计，但证监会和交易所另有规定的除外；应核对适用版本及是否触发例外",
                "审计状态", "未经审计仅是资料属性，不自动认定为公司缺陷或独立风险"])
        formatted_rows = [rows[0]] + [[Paragraph(_esc(str(cell)), _cell_style(font, 8.5))
                                      for cell in row] for row in rows[1:]]
        return [
            Paragraph("本章列示文本筛查的核查参考。尚未对实质合规作出认定，"
                      "需按原始报告、披露时点及事项范围验证规则适用性；候选事项以台账中的复核状态为准。",
                      st['body']),
            Spacer(1, 4),
            _styled_table(formatted_rows, [118, 168, 72, 122], font, font_size=8.5),
        ]

    def _has(*kws):
        return any(kw in issues_text for kw in kws)

    try:
        comp_score = float(dc.get("compliance_score"))
        score_result = (f"合规评分 {_fmt_score(comp_score)} 分"
                        + ("（≥80，整体披露较为规范）" if comp_score >= 80 else "（<80，披露规范性需关注）"))
    except (TypeError, ValueError):
        score_result = "本次未获取合规评分数据"

    def _related_party_result() -> str:
        """关联交易对照行结果：披露检查与台账风险联动判定，避免前后矛盾。"""
        if _has("关联"):
            return "发现关联交易相关披露问题，详见合规风险明细"
        related_ids = [r.get("risk_id", "") for r in (comp_risks or [])
                       if _norm_dim(r.get("dimension", "")) == "related_party"]
        if related_ids:
            return (f"披露检查未发现披露问题，但台账识别到关联交易风险"
                    f"（{'、'.join(x for x in related_ids if x) or '风险条目'}），详见合规风险明细")
        return "未识别关联交易披露问题"

    rows = [["法规依据", "核心监管要求", "对应检查项", "本次检查结果"]]
    rows += [
        [Paragraph(_esc("《证券法》（2019修订）第七十八条"), _cell_style(font, 8.5)),
         Paragraph(_esc("信息披露义务人应及时依法真实、准确、完整披露信息，不得有虚假记载、误导性陈述或重大遗漏"), _cell_style(font, 8.5)),
         Paragraph("披露完整性/及时性", _cell_style(font, 8.5)),
         Paragraph(_esc("发现披露完整性问题（延迟披露/重大遗漏/披露不充分），需核查" if _has("延迟", "及时", "遗漏", "未发现详细披露", "未充分披露", "未发现详细")
                        else "未发现延迟披露或重大遗漏问题迹象"), _cell_style(font, 8.5))],
        [Paragraph(_esc("《证券法》第一百九十七条"), _cell_style(font, 8.5)),
         Paragraph(_esc("虚假记载/误导性陈述/重大遗漏的法律责任：责令改正、警告并处一百万元以上一千万元以下罚款"), _cell_style(font, 8.5)),
         Paragraph("风险定级参考", _cell_style(font, 8.5)),
         Paragraph(_esc("作为重大披露违规风险等级的处罚后果参照"), _cell_style(font, 8.5))],
        [Paragraph(_esc("《上市公司信息披露管理办法》（2025修订）"), _cell_style(font, 8.5)),
         Paragraph(_esc("信息披露义务人应当真实、准确、完整、及时、公平地披露信息，董监高须保证披露质量"), _cell_style(font, 8.5)),
         Paragraph("披露规范性评分", _cell_style(font, 8.5)),
         Paragraph(_esc(score_result), _cell_style(font, 8.5))],
        [Paragraph(_esc("《公开发行证券的公司信息披露内容与格式准则第2号——年度报告的内容与格式》"), _cell_style(font, 8.5)),
         Paragraph(_esc("年报应包含公司概况、公司治理、财务会计报告等法定必备章节"), _cell_style(font, 8.5)),
         Paragraph("法定章节完备性", _cell_style(font, 8.5)),
         Paragraph(_esc(f"缺失章节：{'、'.join(map(str, missing))}，需核查缺失原因" if missing
                        else "未发现法定章节缺失"), _cell_style(font, 8.5))],
        [Paragraph(_esc("《企业会计准则第30号——财务报表列报》"), _cell_style(font, 8.5)),
         Paragraph(_esc("财务报表列报格式、勾稽关系与可比性要求"), _cell_style(font, 8.5)),
         Paragraph("数据校验与勾稽", _cell_style(font, 8.5)),
         Paragraph(_esc("数据校验风险详见《综合汇总报告》三维度评分分解"), _cell_style(font, 8.5))],
        [Paragraph(_esc("沪深交易所股票上市规则（关联交易披露）"), _cell_style(font, 8.5)),
         Paragraph(_esc("关联交易应充分披露定价公允性、决策程序与独立董事意见"), _cell_style(font, 8.5)),
         Paragraph("关联交易披露充分性", _cell_style(font, 8.5)),
         Paragraph(_esc(_related_party_result()), _cell_style(font, 8.5))],
    ]
    return [
        Paragraph("本章将本次披露规范性检查对照现行监管规则逐项列示，供人工复核时追溯检查依据。", st['body']),
        Spacer(1, 4),
        _styled_table(rows, [118, 168, 72, 122], font, font_size=8.5),
    ]


def _score_section_body(score_json, st, font, dc_json="", note=""):
    """汇总报告「综合评分解读」章正文：KPI 卡 + 仪表盘 + 三维度分解 + 抬升理由。

    数据源为 calculate_comprehensive_score 工具结果；入参无效时返回空列表。
    dc_json 为披露检查结果（可选）：三维度分解表的披露依据列展示原始分数，
    避免「披露合规评分/披露风险分/披露维度风险分」三口径并存无换算说明。
    note 为等级底线注记（50c）：风险等级底线规则触发时注入，KPI 卡后橙色渲染。
    """
    sc = _safe_json(score_json)
    if "error" in sc:
        return []
    try:
        score = float(sc["score"])
    except (KeyError, TypeError, ValueError):
        return []
    level = str(sc.get("level", "—"))
    level_key = str(sc.get("level_key", ""))
    lv_color = SCORE_LEVEL_COLORS.get(level_key, PRIMARY)
    body = []

    # KPI 卡：总分/等级/基础分/抬升分
    cards = [("综合风险评分", f"{score:.1f}", lv_color, white),
             ("风险等级", level, lv_color, white)]
    try:
        cards.append(("基础分（加权）", f"{float(sc.get('base_score')):.1f}", PRIMARY, white))
    except (TypeError, ValueError):
        pass
    try:
        cards.append(("模型抬升", f"+{float(sc.get('escalation', 0)):.1f}", ACCENT, white))
    except (TypeError, ValueError):
        pass
    body += [_kpi_cards(cards, font), Spacer(1, 12)]
    score_note = sc.get("assessment_note") or sc.get("score_note")
    if score_note:
        body += [Paragraph(_esc(score_note), st['body']), Spacer(1, 6)]
    if sc.get("level_floor_adjustment"):
        body += [Paragraph(
            f"等级底线调整：+{float(sc['level_floor_adjustment']):.1f}分；"
            "基础分、模型抬升分与底线调整分别记录，不改变原始指标计算。", st['small']), Spacer(1, 6)]

    # 50c：风险等级底线注记——最终台账等级分布与综合评级矛盾时系统强制上调，
    # 附橙色注记解释原因（原模型评分已作废）
    if note:
        body.append(Paragraph(
            "<font color='" + WARN_ORANGE.hexval() + "'><b>风险等级底线提示：</b>"
            + _esc(note) + "</font>", st['body']))
        body.append(Spacer(1, 6))

    # 仪表盘 + 分解柱状图
    gauge = _embed_chart(_gen_score_gauge, score_json if isinstance(score_json, str)
                         else json.dumps(sc, ensure_ascii=False), width_cm=11.0)
    if gauge:
        body += [gauge, Paragraph("图：综合风险评分仪表盘（四色区间对应风险等级映射）", st['caption'])]
    breakdown_bar = _embed_chart(_gen_breakdown_bar, score_json if isinstance(score_json, str)
                                 else json.dumps(sc, ensure_ascii=False), width_cm=14.0)
    if breakdown_bar:
        body += [breakdown_bar, Paragraph("图：三维度评分分解（财务50%/披露30%/校验20%）", st['caption'])]

    # 三维度分解表
    breakdown = sc.get("breakdown", {}) if isinstance(sc.get("breakdown"), dict) else {}
    weights = sc.get("weights", WEIGHTS) if isinstance(sc.get("weights"), dict) else WEIGHTS
    rows = [["评分维度", "权重", "风险分", "评分依据"]]
    dim_desc = {"financial": "基于量化预警指标的数量与严重程度（重大/严重关键词加权）",
                "validation": "基于财务数据勾稽校验未通过项数量"}
    # 披露维度依据展示原始检查分数（风险分/合规评分），消除多口径歧义
    dc = _safe_json(dc_json)
    dc_note = "基于披露规范性检查输出的披露风险分"
    try:
        if dc.get("risk_score") is not None:
            dc_note = f"披露检查风险分 {_fmt_score(dc['risk_score'])}"
            if dc.get("compliance_score") is not None:
                dc_note += f"（合规评分 {_fmt_score(dc['compliance_score'])}，越高越合规）"
    except (TypeError, ValueError):
        pass
    dim_desc["disclosure"] = dc_note
    for name, k in [("财务指标风险", "financial"), ("披露合规风险", "disclosure"), ("数据校验风险", "validation")]:
        try:
            v = f"{float(breakdown.get(k, 0)):.1f}"
        except (TypeError, ValueError):
            v = "—"
        try:
            w = f"{float(weights.get(k, 0)) * 100:.0f}%"
        except (TypeError, ValueError):
            w = "—"
        rows.append([name, w, v, Paragraph(_esc(dim_desc[k]), _cell_style(font, 9))])
    body += [_styled_table(rows, [86, 46, 52, 296], font, font_size=9), Spacer(1, 8)]

    # 50d：评分口径说明（维度未获取时的归一化说明）——评分透明化，
    # 消除「分数无法解释、疑似编造」的误读（实测 18:37 版 15.0 分罗生门）
    sc_notes = sc.get("notes") or []
    if sc_notes and isinstance(sc_notes, list):
        body.append(Paragraph("<b>评分口径说明：</b>", st['body']))
        for i, n in enumerate(sc_notes, 1):
            body.append(Paragraph(_esc(f"({i}) {n}"), st['body']))
        body.append(Spacer(1, 6))

    # 抬升理由（量化模型/审计意见触发的可追溯说明）
    reasons = sc.get("escalation_reasons", [])
    if reasons and isinstance(reasons, list):
        body.append(Paragraph("<b>风险分抬升理由（Z-Score/M-Score/审计意见触发）：</b>", st['body']))
        for i, r in enumerate(reasons, 1):
            body.append(Paragraph(_esc(f"({i}) {r}"), st['body']))
        body.append(Spacer(1, 6))
    summary = sc.get("summary")
    if summary:
        body.append(Paragraph(f"<b>评分结论：</b>{_esc(summary)}", st['body']))
    return body


def _methodology_body(score_json, risk_models_json, validation_json, st, font):
    """汇总报告「风险评估方法论」章正文：评分模型 + 等级映射 + 量化模型解读 + 局限性。"""
    body = [
        Paragraph("<b>（一）综合评分模型</b>", st['body']),
        Paragraph("本系统综合风险评分采用三维度加权模型：基础分 = 财务指标风险 × 50% + "
                  "披露合规风险 × 30% + 数据校验风险 × 20%。若 Altman Z-Score / Beneish M-Score "
                  "量化模型或审计意见识别触发预警信号，则在基础分上叠加抬升分（可追溯、逐条列示），"
                  "最终映射为四级风险等级。", st['body']),
        Spacer(1, 4),
    ]
    rows = [["评分维度", "权重", "评分依据"]]
    for name, k in [("财务指标风险", "financial"), ("披露合规风险", "disclosure"), ("数据校验风险", "validation")]:
        rows.append([name, f"{WEIGHTS.get(k, 0) * 100:.0f}%",
                     {"financial": "量化预警数量与严重程度", "disclosure": "披露检查风险分",
                      "validation": "勾稽校验未通过项数"}[k]])
    body += [_styled_table(rows, [120, 60, 300], font, font_size=9), Spacer(1, 8)]

    # 等级映射表（与 risk_scorer.RISK_LEVELS 保持一致，运行时读取防漂移）
    body.append(Paragraph("<b>（二）风险等级映射</b>", st['body']))
    lv_rows = [["分数区间", "风险等级", "含义"]]
    lv_desc = {"low": "未见显著异常，常规关注即可", "medium": "存在需关注的异常迹象，建议针对性核查",
               "high": "多项指标偏离且部分交叉印证，建议重点审计程序",
               "critical": "重大风险信号密集，建议全面核查并评估持续经营/错报风险"}
    prev = 0
    for threshold, name, key in RISK_LEVELS:
        lv_rows.append([f"{prev}-{threshold}", name, Paragraph(_esc(lv_desc.get(key, "")), _cell_style(font, 9))])
        prev = threshold + 1
    body += [_styled_table(lv_rows, [80, 80, 320], font, font_size=9), Spacer(1, 8)]

    # 量化模型解读（有结果时展示实际取值）
    body.append(Paragraph("<b>（三）第三方量化模型（Altman Z-Score / Beneish M-Score）</b>", st['body']))
    body.append(Paragraph("Altman Z-Score（1968）用于破产风险预测：Z&lt;1.81 落入财务困境区，"
                          "1.81-2.99 为灰色区，&gt;2.99 为安全区。Beneish M-Score（1999）用于盈余操纵"
                          "识别：M&gt;-1.78 进入操纵嫌疑区。两模型为独立于本系统指标的第三方证据，"
                          "仅用于风险分抬升与交叉印证，不单独作为定性依据。", st['body']))
    body.append(Paragraph("<b>模型适用性提示：</b>Z-Score 原始模型面向一般工商企业，对重资产、"
                          "强周期或国有企业样本判别力可能失真（如现金流充沛的能源央企被误判入困境区），"
                          "此类样本下模型结果仅作交叉印证参考，不以模型单一信号定级。", st['small']))
    rm = _safe_json(risk_models_json).get("risk_models", {})
    if isinstance(rm, dict) and rm:
        for label, key in [("Altman Z-Score", "altman_z_score"), ("Beneish M-Score", "beneish_m_score")]:
            m = rm.get(key, {})
            if isinstance(m, dict) and m.get("available") and m.get("score") is not None:
                try:
                    body.append(Paragraph(f"<b>{label}：</b>{float(m['score']):.3f}"
                                          f"（判定区间：{_esc(m.get('zone', '—'))}）", st['body']))
                except (TypeError, ValueError):
                    pass
        cross = rm.get("cross_interpretation", {})
        if isinstance(cross, dict) and cross.get("interpretation"):
            body.append(Paragraph(f"<b>交叉解读：</b>{_esc(cross['interpretation'])}", st['body']))
    else:
        body.append(Paragraph("（本次分析未获取 Z/M-Score 计算结果：通常因缺少多期报表数据，不影响基础评分。）",
                              st['small']))

    # 数据校验结果摘要（兼容 data_validation 嵌套：validator 输出字段在其下，
    # 直接读顶层会得到 None 显示「—」，与评分读到 0 分矛盾——实测缺陷）
    vd = _safe_json(validation_json)
    if isinstance(vd.get("data_validation"), dict):
        vd = vd["data_validation"]
    if vd and "error" not in vd:
        passed, failed = vd.get("passed_checks"), vd.get("failed_checks")
        result = vd.get("validation_result", "—")
        body += [Spacer(1, 4), Paragraph("<b>（四）数据校验摘要</b>", st['body']),
                 Paragraph(f"财务报表勾稽校验结果：<b>{_esc(result)}</b>"
                           f"（通过 {passed if passed is not None else '—'} 项 / "
                           f"未通过 {failed if failed is not None else '—'} 项）。"
                           "未通过项已计入数据校验风险维度（每项未通过 +30 分）参与综合评分，"
                           "具体勾稽差异以原始校验结果为准，未生成独立风险条目。", st['body'])]

    body += [Spacer(1, 6), Paragraph("<b>数据来源与局限性：</b>"
                                     "本报告数据取自上市公司公开披露年报；行业基准仅在存在来源、期间、样本和可比性核验记录时用于横向比较，"
                                     "系统内部筛查值不代表行业均值或中位数。AI 判读基于规则阈值与知识库检索，存在模型幻觉可能，"
                                     "所有结论均须以具备资质的审计人员复核为准。", st['body'])]
    return body


def _cross_validation_body(report, st):
    """汇总报告「交叉验证分析」章：渲染台账 cross_validation_analysis 字段。"""
    text = str(report.get("cross_validation_analysis", "") or "").strip()
    if not text:
        return []
    return [Paragraph("本章为多指标勾稽比对结论（营收 vs 现金流 vs 应收 vs 毛利），用于检验商业逻辑一致性。",
                      st['small']),
            Paragraph(_para_text(text), st['body'])] + _excluded_note(report, st)


def _risk_chain_body(report, st):
    """汇总报告「风险传导链分析」章：渲染台账 risk_chain_analysis 字段。"""
    text = _strip_structured_review_json(str(report.get("risk_chain_analysis", "") or "")).strip()
    if text:
        # 不把实现键名或 JSON 标记带入公共 PDF；仅保留可读的人工复核说明。
        text = re.sub(r"\b(?:risk_chain_analysis|judgment_1|judgment_2|overall_status|evidence_ids)\b", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    if not text:
        return []
    return [Paragraph("本章识别风险间的因果传导路径（如：应收激增→现金流恶化→偿债能力下降→持续经营风险嫌疑）。",
                      st['small']),
            Paragraph(_para_text(text), st['body'])]


def _multi_year_body(my_json, st, font):
    """财务报告「多年指标趋势分析」章正文（compare_multi_year 工具结果）。

    内容：年度数/预警数 KPI 卡 → 指标×年份时序表 → 指标趋势判定 → 趋势性风险预警。
    入参为空/解析失败/含 error 时返回空列表（调用方跳过本章）。
    """
    my = _safe_json(my_json)
    if "error" in my:
        return []
    years = my.get("years_analyzed", []) or []
    ts = my.get("timeseries", {}) or {}
    series = ts.get("series", []) if isinstance(ts, dict) else []
    alerts = my.get("trend_alerts", []) or []
    if not years and not series:
        return []
    body = []
    cards = [("分析年度", f"{my.get('year_count', len(years))}", PRIMARY, white)]
    cards.append(("趋势预警", str(my.get('alert_count', len(alerts))),
                  BAD_RED if alerts else GOOD_GREEN, white))
    body += [_kpi_cards(cards, font), Spacer(1, 8)]
    # 时序表格：指标 × 年份（年份列在表头）
    if years and series:
        rows = [["指标"] + [_esc(str(y)) for y in years]]
        for s in series:
            if not isinstance(s, dict):
                continue
            data = s.get("data", []) or []
            cells = [Paragraph(_esc(s.get("name", "")), _cell_style(font, 8.5))]
            for v in data:
                # no_scale：多年对比数据原始单位未知（万元/亿元/元均可能），
                # 自动换算会把 185000（万元口径）误显为 18.50 万元，故原值展示
                cells.append(Paragraph(_fmt_num(v, no_scale=True) if v is not None else "未披露", _cell_style(font, 8.5)))
            rows.append(cells)
        if len(rows) > 1:
            body += [Paragraph(f"<b>（一）核心指标多年趋势（最近 {len(years)} 年）：</b>", st['small']),
                     _styled_table(rows, [150] + [52] * len(years), font, font_size=8.5),
                     Spacer(1, 6)]
    # 指标趋势判定
    trends = my.get("trends", {}) or {}
    if isinstance(trends, dict) and trends:
        body.append(Paragraph("<b>（二）指标趋势判定：</b>", st['body']))
        for k, v in trends.items():
            if isinstance(v, dict):
                trend = str(v.get("trend", "未判定") or "未判定")
                status = {
                    "calculated": "已计算",
                    "insufficient_data": "数据不足",
                    "not_comparable": "不可比",
                }.get(str(v.get("status", "") or ""), str(v.get("status", "") or "未记录"))
                value_text = f"趋势：{trend}；状态：{status}"
            else:
                value_text = str(v)
            body.append(Paragraph(_esc(f"• {k}：{value_text}"), st['body']))
        body.append(Spacer(1, 6))
    # 趋势性风险预警
    if alerts:
        body.append(Paragraph("<b>（三）趋势性风险预警：</b>", st['body']))
        for i, a in enumerate(alerts, 1):
            body.append(Paragraph(_esc(f"({i}) {a}"), st['body']))
        body.append(Spacer(1, 6))
    return body


def _audit_opinion_body(ao_json, st, font):
    """合规报告「审计意见识别与风险信号」章正文（identify_audit_opinion 工具结果）。

    内容：意见类型/性质/可信度影响 KPI 卡 → 含义与影响 → 证据摘录 → 持续经营信号
    → 关键审计事项 → 事务所变更 → 联动风险提示。入参无效时返回空列表。
    """
    ao = _safe_json(ao_json)
    if "error" in ao:
        return []
    opinion = ao.get("audit_opinion", {}) or {}
    gc = ao.get("going_concern", {}) or {}
    kam = ao.get("key_audit_matters", {}) or {}
    auditor_change = ao.get("auditor_change", {}) or {}
    linkages = ao.get("linkage_alerts", []) or []
    if not opinion and not gc.get("flagged") and not linkages:
        return []
    body = []
    if opinion.get("identified"):
        # P 补丁：意见性质优先读 opinion_nature（未经审计→"不适用"、未识别→"未获取"），
        # 不再由 is_standard_opinion 二值推导（实测缺陷：未经审计被标"非标准"）
        nature = str(opinion.get("opinion_nature", "") or "")
        is_std = opinion.get("is_standard_opinion")
        if not nature:
            nature = "标准" if is_std else ("非标准" if is_std is False else "未获取")
        cards = [
            ("意见类型", _esc(str(opinion.get("opinion_type", "—"))), PRIMARY, white),
            ("意见性质", _esc(nature),
             GOOD_GREEN if nature == "标准" else (BAD_RED if nature == "非标准" else WARN_AMBER), white),
            ("可信度影响", _esc(str(opinion.get("credibility_impact", "—"))), WARN_ORANGE, white),
        ]
        body += [_kpi_cards(cards, font), Spacer(1, 8)]
        if opinion.get("meaning"):
            body.append(Paragraph(f"<b>含义：</b>{_esc(opinion['meaning'])}", st['body']))
        if opinion.get("implication"):
            body.append(Paragraph(f"<b>对分析的影响：</b>{_esc(opinion['implication'])}", st['body']))
        if opinion.get("evidence_excerpt"):
            body.append(Paragraph(f"<b>证据摘录：</b>{_esc(opinion['evidence_excerpt'])}", st['small']))
    else:
        body.append(Paragraph(f"<b>审计意见：</b>{_esc(str(opinion.get('opinion_type', '未识别')))}"
                              f"——{_esc(str(opinion.get('note', '')))}", st['body']))
    if gc.get("flagged"):
        body += [Spacer(1, 4),
                 Paragraph(f"<b>持续经营重大不确定性信号：</b>{_esc(str(gc.get('signal', '')))}"
                           f"（风险等级：{_esc(str(gc.get('risk_level', ''))) }）", st['body']),
                 Paragraph(_esc(str(gc.get("implication", ""))) +
                           "该信号已计入综合评分抬升；台账中持续经营维度风险等级若低于重要，请人工复核是否上调。", st['body'])]
    matters = kam.get("matters", []) or []
    if matters:
        body += [Spacer(1, 4), Paragraph("<b>关键审计事项（高风险领域提示）：</b>", st['body'])]
        for i, mt in enumerate(matters, 1):
            if isinstance(mt, dict):
                body.append(Paragraph(_esc(f"({i}) {mt.get('matter', '')}——{mt.get('risk_direction', '')}"), st['body']))
        if kam.get("note"):
            body.append(Paragraph(_esc(str(kam["note"])), st['small']))
    if auditor_change.get("flagged"):
        body += [Spacer(1, 4),
                 Paragraph("<b>会计师事务所变更：</b>年报出现变更会计师事务所表述，需关注变更原因。", st['body'])]
    if linkages:
        body += [Spacer(1, 4), Paragraph("<b>联动风险提示：</b>", st['body'])]
        for i, ln in enumerate(linkages, 1):
            body.append(Paragraph(_esc(f"({i}) {ln}"), st['body']))
    return body


def _industry_benchmark_body(report, fin_json, st, font):
    """综合汇总「行业基准对比」章正文：优先台账字段，缺失时确定性生成。

    台账 industry_benchmark 不在 SP 输出模板中（LLM 几乎不会输出），因此
    缺省时用财务指标 + 行业基准库确定性生成对比表，保证本章恒渲染（有指标时）。
    """
    bench_data = report.get("industry_benchmark")
    if isinstance(bench_data, dict) and bench_data:
        qualified = _has_verified_benchmark_provenance(bench_data)
        bench_body = [Paragraph(
            "以下为台账提供的参考信息；" + _benchmark_basis_note(qualified), st['small'])]
        for k, v in bench_data.items():
            if isinstance(v, dict):
                bench_body.append(Paragraph(f"<b>{_esc(k)}</b>", st['body']))
                for sk, sv in v.items():
                    bench_body.append(Paragraph(f"  {_esc(sk)}: {_esc(sv)}", st['small']))
            else:
                bench_body.append(Paragraph(f"<b>{_esc(k)}</b>: {_esc(v)}", st['body']))
        return bench_body
    fin = _safe_json(fin_json)
    indicators = fin.get("indicators", {}) if isinstance(fin, dict) else {}
    ci = report.get("company_info", {}) or {}
    industry = str(ci.get("industry", "") or "")
    benchmarks = _load_benchmarks(industry)
    rows = [["指标", "数值", "行业基准", "偏离基准", "判读"]]
    any_row = False
    for k, v in indicators.items():
        if v is None:
            continue
        try:
            vnum = float(v)
        except (TypeError, ValueError):
            continue
        bench = benchmarks.get(_BENCH_KEY_MAP[k]) if k in _BENCH_KEY_MAP else None
        if not isinstance(bench, (int, float)):
            continue
        dev = f"{(vnum - bench) / bench * 100:+.1f}%" if bench != 0 else "—"
        verdict, bad = _interpret_indicator(k, vnum, bench, industry=industry)
        verdict_html = (f"<font color='{TEXT_RED}'>{_esc(verdict)}</font>" if bad else _esc(verdict))
        rows.append([_esc(_INDICATOR_CN.get(k, k)), _fmt_num(v), f"{bench:g}", dev,
                     Paragraph(verdict_html, _cell_style(font, 8.5))])
        any_row = True
    if not any_row:
        return [Paragraph("本次未取得经来源、统计期间、样本和可比性核验的行业基准，无法进行横向对比；项目内置筛查参考值不视为行业统计均值。", st['small'])]
    return [
        Paragraph(f"本报告对核心财务指标与<b>{_esc(industry or '所在行业')}</b>行业基准进行横向对比。"
                  + _esc(_benchmark_basis_note(True)), st['body']),
        Spacer(1, 4),
        _styled_table(rows, [110, 70, 70, 70, 160], font, font_size=8.5),
    ]


def _review_conclusion_body(report, st):
    """综合汇总「审计合伙人复核意见」章正文：渲染后处理阶段回写的复核意见。

    复核意见由 _post_process 回写进台账 review_conclusion 字段（与前端消息同源），
    保证 PDF 与前端展示一致；无复核内容时返回空列表。

    警示：辩论由 LLM 生成，其中引用的外部数据（同行对比/市场行情等）未经系统
    数据核验，可能为模型幻觉，渲染时须附醒目提示（实测缺陷：辩论捏造同行数据）。
    """
    text = str(report.get("review_conclusion", "") or "").strip()
    c1 = report.get("c1_review") or {}
    semantic = report.get("semantic_review") or {}
    if not isinstance(semantic, dict):
        semantic = {}
    if not semantic and text:
        # Older ledgers did not persist semantic_review separately. Recover the
        # structured object for display, then remove it from the prose below.
        objects = _structured_review_objects(text)
        if objects:
            semantic = objects[0][2]
    if not text and not c1 and not semantic:
        return []
    body = [
        Paragraph("<b>⚠️ 核验提示：</b>本复核意见由多智能体辩论（LLM）生成，其中引用的外部数据"
                  "（同行业公司对比、历史行情、账龄结构等）未经系统数据核验，可能包含模型幻觉，"
                  "一律不得作为审计底稿依据；请以报告正文数据、工具输出与数据来源说明为准，"
                  "需人工逐项核实。", st['small']),
        Spacer(1, 4),
    ]
    # Render C2 as a compact, human-readable table. The complete semantic_review
    # object remains in the JSON ledger/Excel export for traceability.
    checks = semantic.get("checks") if isinstance(semantic, dict) else None
    if isinstance(checks, list) and checks:
        first_by_id = {
            str(item.get("risk_id")): item
            for item in (semantic.get("judgment_1", {}).get("checks", [])
                         if isinstance(semantic.get("judgment_1"), dict) else [])
            if isinstance(item, dict) and item.get("risk_id")
        }
        second_by_id = {
            str(item.get("risk_id")): item
            for item in (semantic.get("judgment_2", {}).get("checks", [])
                         if isinstance(semantic.get("judgment_2"), dict) else [])
            if isinstance(item, dict) and item.get("risk_id")
        }
        decision_names = {"supported": "支持", "not_supported": "不支持",
                          "pending": "待定", "missing": "缺失"}
        state_names = {"consistent": "一致", "disputed": "存在分歧",
                       "pending_review": "待复核", "invalid": "无效"}
        review_rows = [["风险ID", "第一次判断", "第二次判断", "条件对齐", "证据状态", "证据编号", "最终状态"]]
        for check in checks:
            if not isinstance(check, dict) or not check.get("risk_id"):
                continue
            rid = str(check["risk_id"])
            left = first_by_id.get(rid, {})
            right = second_by_id.get(rid, {})
            left_ids = left.get("evidence_ids") or check.get("evidence_ids_1") or []
            right_ids = right.get("evidence_ids") or check.get("evidence_ids_2") or []
            aligned = left.get("conditions_aligned") is True and right.get("conditions_aligned") is True
            if left_ids and right_ids:
                evidence_state = "两轮均有引用"
            elif left_ids or right_ids:
                evidence_state = "引用不完整"
            else:
                evidence_state = "无引用"
            evidence_ids = []
            for evidence_id in [
                    *(left.get("evidence_ids") or []),
                    *(right.get("evidence_ids") or []),
                    *(check.get("evidence_ids_1") or []),
                    *(check.get("evidence_ids_2") or [])]:
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(str(evidence_id))
            review_rows.append([
                rid,
                decision_names.get(str(check.get("decision_1", left.get("decision", "missing"))), "未知"),
                decision_names.get(str(check.get("decision_2", right.get("decision", "missing"))), "未知"),
                ("第1是/第2是" if aligned else
                 f"第1{'是' if left.get('conditions_aligned') is True else '否'}/"
                  f"第2{'是' if right.get('conditions_aligned') is True else '否'}"),
                evidence_state,
                ", ".join(_public_evidence_reference(item) for item in evidence_ids)
                if evidence_ids else "—",
                state_names.get(str(check.get("state", "")), str(check.get("state", "未记录"))),
            ])
        if len(review_rows) > 1:
            overall = state_names.get(str(semantic.get("overall_status", "")),
                                      str(semantic.get("overall_status", "未记录")))
            body += [Paragraph(f"<b>C2关键语义复核摘要：</b>最终状态：{_esc(overall)}。", st['small']),
                     _styled_table(review_rows, [42, 52, 52, 48, 62, 100, 58],
                                   _register_chinese_font(), font_size=7.5),
                     Spacer(1, 6)]
    actionable = _review_actionable_text(text)
    if actionable:
        body += [Paragraph("<b>待执行程序：</b>", st['small']),
                 Paragraph(_para_text(actionable), st['body'])]

    suggestions = []
    for risk in report.get("risk_details", []) or []:
        if not isinstance(risk, dict) or not risk.get("audit_suggestion"):
            continue
        suggestions.append([_risk_id_label(risk), str(risk.get("audit_suggestion"))])
    if suggestions:
        body += [Spacer(1, 6), Paragraph("<b>风险条目待执行程序索引：</b>", st['small']),
                 _styled_table([["风险ID", "程序"]] + suggestions,
                               [52, 310], _register_chinese_font(), font_size=8)]
    phases = c1.get("phases") if isinstance(c1, dict) else {}
    if isinstance(phases, dict) and phases:
        status_names = {"completed": "已完成", "running": "进行中", "failed": "失败",
                        "not_started": "未开始", "not_run": "未执行"}
        rows = [["C1阶段", "状态", "输出", "结构化备注"]]
        for key in c1.get("stage_order", ["advocate", "skeptic", "arbiter"]):
            phase = phases.get(key) or {}
            if not isinstance(phase, dict):
                continue
            output = "已生成" if phase.get("output_available") else "无可用输出"
            note = ""
            if key == "arbiter":
                note = str(phase.get("verdict", "") or "")
                if phase.get("adjustment_count") is not None:
                    note += f"；调整 {phase.get('adjustment_count', 0)} 条"
            rows.append([
                str(phase.get("label", key)),
                status_names.get(str(phase.get("status", "")), str(phase.get("status", "未记录"))),
                output,
                note,
            ])
        body += [Spacer(1, 8), Paragraph("<b>C1串行阶段结构化状态：</b>", st['small']),
                 _styled_table(rows, [110, 70, 70, 230],
                               _register_chinese_font(), font_size=8.5)]
        if c1.get("arbiter_incomplete"):
            body.append(Paragraph("仲裁状态：未完成，不能标记为复核通过；有效数据和产物仍可供人工复核。", st['small']))
    return body


def _pending_items_body(report, st):
    """综合汇总报告「待处理事项」章：单独呈现未进入正式风险统计的条目。"""
    pending = [item for item in (report.get("pending_items") or []) if isinstance(item, dict)]
    if not pending:
        return []
    # PDF 面向评审和管理层只给出状态提醒；完整事项、证据和下一步程序保留在
    # Excel 审计底稿与 TXT 风险台账，避免长文本表格把正文变成“未完成清单”。
    return [Paragraph(
        f"本报告识别出 {len(pending)} 项待复核提示，未计入正式风险评分和正式风险总数；"
        "详细事项及证据索引见 Excel 审计底稿和 TXT 风险台账。",
        st['small']), Spacer(1, 4)]


def _excluded_note(report, st):
    """系统排除提示：正文（LLM 原始分析）可能引用已被系统复核排除的条目。

    背景：P11 归母口径复核将未分配利润勾稽条目移入备查录后，LLM 生成的正文
    （整体评估/交叉验证/结论）仍可能引用该条目（如"三项重要风险"），与最终
    清单口径冲突（实测缺陷：正文三项重要 vs 清单两条重要）。此处追加可见提示。
    """
    excluded = report.get("excluded_items", []) or []
    if not excluded:
        return []
    ids = "、".join(str(r.get("risk_id", "")) for r in excluded
                     if isinstance(r, dict) and r.get("risk_id"))
    return [Paragraph(
        f"<font color='{TEXT_RED}'><b>系统复核提示：</b>正文结论由 LLM 基于原始分析生成，"
        f"可能引用已被系统复核排除的条目（{ids}，详见备查录）；风险清单与统计以最终明细为准。</font>",
        st['small']), Spacer(1, 6)]


def _excluded_items_body(report, st):
    """综合汇总「已排除嫌疑事项备查录」章：渲染反向校验器移除的伪风险条目。

    这些条目由 _post_process 反向校验器识别为"分析结论为无风险/风险极低"的一般/低
    等级条目（实测缺陷：R006 凑数），不计入风险明细/KPI/热力图，移至备查录保留可追溯性。
    """
    excluded = report.get("excluded_items", []) or []
    if not excluded:
        return []
    body = [Paragraph("以下事项经反向校验（分析结论为无风险/风险极低）已从风险台账移除，"
                      "不再计入风险明细与统计，仅作备查留档：", st['small']), Spacer(1, 4)]
    for i, r in enumerate(excluded, 1):
        if not isinstance(r, dict):
            continue
        title = _esc(_risk_display_title(r))
        dim = _esc(_risk_display_dimension(r))
        analysis = _esc(str(r.get("data_analysis", "") or "")[:200])
        body.append(Paragraph(f"{i}. {r.get('risk_id', '')}｜{title}（维度：{dim}）", st['body']))
        if analysis:
            body.append(Paragraph(f"已排除依据：{analysis}", st['small']))
        body.append(Spacer(1, 4))
    return body


def _data_source_note(st, font, sources):
    """「数据来源与完整性说明」章正文：逐项标注各数据源使用状态与缺失原因。

    Args:
        st: 样式集合
        font: 字体名
        sources: [(数据源名称, 是否已使用 bool, 未获取时的原因说明 str)]
    """
    rows = [["数据源", "状态", "说明"]]
    for item in sources:
        if len(item) >= 4:
            name, used, missing_note, source_status = item[:4]
        else:
            name, used, missing_note = item[:3]
            source_status = "verified" if used else "unverified"
        status_label = {
            "verified": "已核验",
            "incomplete": "数据源不完整，仅供参考",
            "unverified": "来源未核验，仅供人工复核",
            "demo_placeholder": "演示占位数据，不代表公司实际数据",
        }.get(str(source_status), str(source_status) or ("已使用" if used else "未获取"))
        if not used:
            note = missing_note or "需人工复核"
        elif source_status == "incomplete":
            # 已调用工具但仅部分结果可核验时，不能复用“未获取”原因。
            # 否则真实运行会被误报为未上传年报或未调用工具。
            note = "已获取，但部分结果不完整或仅作有限检查，请人工复核"
        elif source_status == "demo_placeholder":
            note = "演示占位数据，不代表公司实际数据"
        else:
            note = ""
        rows.append([
            Paragraph(_esc(name), _cell_style(font, 8.5)),
            Paragraph(_esc(status_label), _cell_style(font, 8.5)),
            Paragraph(_esc(note), _cell_style(font, 8.5)),
        ])
    return [
        Paragraph("本报告各章节数据来源及完整性说明如下；未获取的数据源对应章节已省略"
                  "或降级为兜底文案，供人工复核时追溯数据可得性：", st['small']),
        Spacer(1, 4),
        _styled_table(rows, [110, 52, 318], font, font_size=8.5),
    ]


def _product_consistency_body(report, st, font, uncovered=None):
    """产物一致性说明：三份报告同源风险台账 + 维度过滤映射。

    降低人工跨报告核对成本：同一日期/公司可能存在多轮运行产物（同名文件被
    覆盖的历史问题），本小节标注运行实例与子报告口径，使"子报告与总报告"
    的包含关系在 PDF 内自解释。uncovered 非空时附未落入子集的维度清单。
    """
    ci = report.get("company_info") or {}
    run_id = str(ci.get("run_id", "") or "")
    inst = run_id[:8] if run_id else "—"
    rows = [
        ["报告", "风险口径", "说明"],
        ["财务健康诊断报告", "财务维度子集", "仅含财务风险/持续经营/数据可靠性维度风险"],
        ["合规与信息披露报告", "合规维度子集", "仅含信披合规/监管处罚/关联交易维度风险"],
        ["综合汇总报告", "完整风险清单", "全部维度风险 + 交叉验证 + 整体评估（唯一权威口径）"],
    ]
    body = [
        Paragraph("产物一致性说明：本批三份报告由同一次分析生成，风险清单同源"
                  f"（运行实例 {_esc(inst)}）。财务诊断与合规报告仅含对应维度的风险子集，"
                  "完整风险清单、交叉验证与整体评估以《综合汇总报告》为准；若子报告"
                  "与上述口径不一致，说明对应文件来自不同运行实例，请以最新实例为准。",
                  st['small']),
        Spacer(1, 4),
        _styled_table(rows, [150, 90, 250], font, font_size=8.5),
    ]
    if uncovered:
        dims = sorted({str(r.get("dimension", "")) for r in uncovered})
        body.append(Spacer(1, 6))
        body.append(Paragraph(
            f"子集完备性提示：{len(uncovered)} 项风险（维度：{'、'.join(_esc(d) for d in dims)}）"
            "未落入财务/合规子集，仅在本综合汇总报告中可见，请人工核对维度标注是否缺失。",
            st['small']))
    return body


def _sub_conclusion_body(label, risks, st, font, risk_json_str=""):
    """拆分报告「本报告结论」章正文：等级分布 KPI 卡 + 环形图 + 统计结论。"""
    c = _level_counts(risks)
    body = [_kpi_cards([
        ("重大风险", str(c["重大"]), BAD_RED if c["重大"] else PRIMARY, white),
        ("重要风险", str(c["重要"]), WARN_ORANGE if c["重要"] else PRIMARY, white),
        ("一般风险", str(c["一般"]), ACCENT if c["一般"] else PRIMARY, white),
        ("合计", str(len(risks)), PRIMARY, white),
    ], font), Spacer(1, 10)]
    if risks and risk_json_str:
        donut = _embed_chart(_gen_level_donut, risk_json_str, width_cm=8.0)
        if donut:
            body += [donut, Paragraph("图：本报告风险等级分布", st['caption'])]
    text = (f"本报告聚焦{label}维度，系统采信相关风险 {len(risks)} 项"
            f"（重大 {c['重大']} 项 / 重要 {c['重要']} 项 / 一般 {c['一般']} 项）。"
            f"完整风险清单、交叉验证与整体评估结论详见《综合汇总报告》。"
            f"采信数量为零不代表不存在风险；待复核提示及分析限制须另行阅读。")
    body.append(Paragraph(text, st['body']))
    body.append(Paragraph(_esc(AI_DISCLAIMER), st['small']))
    return body


# ── 核心导出逻辑 ──────────────────────────────────────────

def _export_legacy_single(report: dict, output_path: str) -> str:
    """兼容模式：按旧结构生成单份汇总 PDF（显式传 output_path 的调用路径）。

    报告结构（共 5 大章节）：封面页 → 目录 → 风险总览 → 风险明细 →
    整体评估结论 → 行业基准对比（可选）→ 免责声明。

    Returns:
        成功时返回含下载链接的提示文本，失败时返回错误信息
    """
    _reconcile_summary(report)
    font = _register_chinese_font()
    st = _build_styles(font)
    elements = []

    ci = report.get("company_info", {})     # 公司基本信息
    rs = report.get("risk_summary", {})     # 风险统计摘要
    rd = report.get("risk_details", [])     # 风险明细列表

    # ═══ 章节1：封面页 ═══
    elements += _cover_elements(["上市公司年报", "审计风险评估报告"], ci, st, font,
                                 report_context=report)

    # ═══ 目录页 ═══
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

    # ═══ 章节2：风险总览 ═══
    elements += [Paragraph("一、风险总览", st['h1']), Spacer(1, 10)]
    c_rd = _level_counts(rd)
    summary = (
        f"本次审计风险识别共发现 <b>{len(rd)}</b> 项风险，"
        f"其中重大风险 <b><font color='red'>{c_rd['重大']}</font></b> 项，"
        f"重要风险 <b><font color='#FF8C00'>{c_rd['重要']}</font></b> 项，"
        f"一般风险 <b><font color='#4472C4'>{c_rd['一般']}</font></b> 项。"
    )
    elements += [Paragraph(summary, st['body']), Spacer(1, 10)]

    # 五维度风险分布表（兼容 LLM 返回字典/列表/整数等多种格式）
    dims = rs.get("risk_dimensions", {})
    dim_data = [["风险维度", "风险数量"]]
    for k, v in dims.items():
        if isinstance(v, (int, float)):
            count = v
        elif isinstance(v, dict):
            count = v.get("count", v.get("数量", len(v)))
        elif isinstance(v, list):
            count = len(v)
        else:
            count = 0
        if count > 0:
            dim_data.append([DIM_CN.get(_norm_dim(k), k), str(count)])
    if len(dim_data) > 1:
        elements += [_styled_table(dim_data, [200, 100], font, font_size=10), Spacer(1, 15)]

    # 风险清单摘要表（ID + 维度 + 标题 + 等级，等级列按颜色标注）
    elements += [Paragraph("风险清单摘要", st['h2'])]
    list_cell_style = _cell_style(font, 9)
    rows = [["风险ID", "维度", "标题", "等级"]]
    levels_col = []
    for r in rd:
        rows.append([
            _risk_id_label(r),
            Paragraph(_esc(_risk_display_dimension(r)), list_cell_style),
            Paragraph(_esc(_risk_display_title(r)), list_cell_style),
            r.get("level", ""),
        ])
        levels_col.append(r.get("level", ""))
    elements += [_styled_table(rows, [60, 90, 170, 50], font, font_size=9,
                               level_cols=[(3, levels_col)], level_colors_map=LEVEL_TEXT_COLORS),
                 PageBreak()]

    # ═══ 章节3：风险明细（编号化结构） ═══
    elements += [Paragraph("二、风险明细", st['h1']), Spacer(1, 10)]
    elements += _risk_detail_elements(rd, st)

    # ═══ 章节4：整体评估结论 ═══
    # L 补丁：评分快照替换（吞全 span），杜绝结论章与封面双分矛盾
    _assessment_text = _apply_score_snapshot(report.get("overall_assessment", ""), report)
    elements += [PageBreak(), Paragraph("三、整体风险评估结论", st['h1']), Spacer(1, 10),
                 Paragraph(_para_text(_assessment_text), st['body'])]
    elements += _excluded_note(report, st)

    # ═══ 章节5：行业基准对比（可选） ═══
    if "industry_benchmark" in report:
        elements += [Spacer(1, 15), Paragraph("四、行业基准对比", st['h1'])]
        for k, v in report["industry_benchmark"].items():
            if isinstance(v, dict):
                elements.append(Paragraph(f"<b>{_esc(k)}</b>", st['body']))
                for sk, sv in v.items():
                    elements.append(Paragraph(f"  {_esc(sk)}: {_esc(sv)}", st['small']))

    # ═══ 免责声明页 ═══
    elements += _disclaimer_page_elements(st)

    guard = _check_disclaimer(elements, doc_kind="PDF风险报告")
    if guard:
        return guard

    _build_pdf_doc(elements, output_path, font)

    from local_storage import upload_file_to_storage
    prefix = _build_file_prefix(report)
    url = upload_file_to_storage(output_path, f"reports/{prefix}_审计风险报告.pdf", "application/pdf")
    if url.startswith("/") or url.startswith("http") or url.startswith("file://"):
        return f"PDF风险报告已生成，下载链接: {url}"
    return f"PDF风险报告已生成(本地): {output_path}"


def _export_split_reports(report: dict, financial_indicators_json: str, disclosure_check_json: str,
                          comprehensive_score_json: str = "", risk_models_json: str = "",
                          validation_json: str = "", compare_multi_year_json: str = "",
                          audit_opinion_json: str = "", module: str = "") -> str:
    """拆分模式（默认）：按风险维度生成独立 PDF 报告。

    1. 财务健康诊断报告：公司简介 + 财务指标与审计关注分析（判读表/对比图/雷达图/热力图）
       + 多年指标趋势分析 + 财务维度风险明细 + 结论
    2. 合规与信息披露报告：公司简介 + 披露检查结果 + 审计意见识别与风险信号
       + 合规标准对照 + 合规维度风险明细 + 结论
    3. 综合汇总报告：公司简介 + 综合评分解读 + 风险总览 + 交叉验证/风险传导链
       + 审计合伙人复核意见 + 全部风险明细 + 整体结论 + 行业基准 + 风险评估方法论

    每份均含封面（封皮）、目录、数据来源与完整性说明与免责声明；
    免责声明校验对每份文档分别执行。

    Args:
        report: 已解析的风险台账字典
        financial_indicators_json: calculate_financial_indicators 工具结果（可为空）
        disclosure_check_json: check_disclosure_compliance 工具结果（可为空）
        comprehensive_score_json: calculate_comprehensive_score 工具结果（可为空）
        risk_models_json: calculate_risk_models 工具结果（可为空）
        validation_json: validate_financial_data 工具结果（可为空）
        compare_multi_year_json: compare_multi_year 工具结果（可为空）
        audit_opinion_json: identify_audit_opinion 工具结果（可为空）
        module: 任务模块标识，按模块裁剪产物——"financial" 仅生成财务健康诊断报告，
            "compliance" 仅生成合规与信息披露报告；"synthesis"/空/未知值生成全部三份

    Returns:
        与实际产物一一对应的「PDF报告（XX）已生成，下载链接: ...」文本
        （保留「下载链接:」字样供前端提取）
    """
    snapshot = report.get("report_snapshot") if isinstance(report, dict) else None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("risks"), dict):
        from core.report_snapshot import snapshot_as_legacy_payload
        report = snapshot_as_legacy_payload(snapshot, report)
        report["report_snapshot"] = snapshot
        financial_indicators_json = json.dumps(snapshot.get("financial") or {}, ensure_ascii=False, default=str)
        disclosure_check_json = json.dumps(snapshot.get("disclosure") or {}, ensure_ascii=False, default=str)
        comprehensive_score_json = json.dumps(snapshot.get("score") or {}, ensure_ascii=False, default=str)
        risk_models_json = json.dumps(snapshot.get("risk_models") or {}, ensure_ascii=False, default=str)
        validation_json = json.dumps(snapshot.get("validation") or {}, ensure_ascii=False, default=str)
        compare_multi_year_json = json.dumps(snapshot.get("multi_year") or {}, ensure_ascii=False, default=str)

    warnings = _reconcile_summary(report)
    font = _register_chinese_font()
    # 模块 → 产物集合：点单模块只出对应专项报告，综合研判/未指定出全套三份
    mod = str(module or "").strip().lower()
    if mod == "financial":
        wanted = {"financial"}
    elif mod == "compliance":
        wanted = {"compliance"}
    else:
        wanted = {"financial", "compliance", "synthesis"}
    st = _build_styles(font)
    ci = report.get("company_info", {})
    # 正式章节只消费通过证据门禁的风险；未通过条目单独在待处理事项章展示。
    rd_all = _formal_risks(report)
    rs = report.get("risk_summary", {})
    risk_json_str = json.dumps(report, ensure_ascii=False)
    prefix = _build_file_prefix(report)
    industry = str(ci.get("industry", "") or "")

    score_dict = _safe_json(comprehensive_score_json)
    score_card = None
    if "error" not in score_dict and score_dict.get("score") is not None:
        score_card = {"score": score_dict.get("score"), "level": score_dict.get("level"),
                      "level_key": score_dict.get("level_key"), "score_note": score_dict.get("assessment_note") or score_dict.get("score_note")}

    # 数据来源与完整性说明（三份报告共用；缺失项标注原因，避免章节静默消失）
    def _used(j):
        d = _safe_json(j)
        return bool(d) and "error" not in d

    # 审计意见双源合并判定：识别工具未调用时，披露检查的 audit_opinion 字段仍算已获取
    ao_used, ao_note = _audit_opinion_source(audit_opinion_json, disclosure_check_json)
    # 未获取原因只写原因本身：同表「状态」列已写「未获取」，原因再带前缀会重复
    # （与网页端同一口径，实测缺陷）。
    snapshot_quality = snapshot.get("data_quality", {}) if isinstance(snapshot, dict) else {}
    presentation_mode = str(snapshot_quality.get("presentation_mode") or report.get("presentation_mode") or "strict")

    def _source_status(name, used):
        if presentation_mode in {"demo", "demo_placeholder"}:
            return "demo_placeholder"
        if not used:
            return "unverified"
        if name == "财务指标数据" and snapshot_quality.get("incomplete_metrics"):
            return "incomplete"
        if name == "多年指标趋势":
            my = _safe_json(compare_multi_year_json)
            if len(my.get("years_analyzed", []) or []) < 2:
                return "incomplete"
        if name == "数据勾稽校验" and str((_safe_json(validation_json).get("data_validation") or {}).get("validation_result", "")) not in {"通过", ""}:
            return "incomplete"
        return "verified"

    sources = [
        ("财务指标数据", _used(financial_indicators_json),
         "未上传年报或文本过短（P1 预处理跳过），LLM 未产出结构化财务指标",
         _source_status("财务指标数据", _used(financial_indicators_json))),
        ("披露规范性检查", _used(disclosure_check_json),
         "LLM 未调用披露检查工具或年报文本不足以检查",
         _source_status("披露规范性检查", _used(disclosure_check_json))),
        ("综合风险评分", _used(comprehensive_score_json),
         "LLM 未调用评分工具且系统兜底评分失败",
         _source_status("综合风险评分", _used(comprehensive_score_json))),
        ("多年指标趋势", _used(compare_multi_year_json),
         "未提供多年财务数据（需至少 2 个年度）",
         _source_status("多年指标趋势", _used(compare_multi_year_json))),
        ("审计意见识别", ao_used, ao_note,
         _source_status("审计意见识别", ao_used)),
        ("量化模型预警", _used(risk_models_json),
         "缺少多期报表数据（Altman Z-Score / Beneish M-Score 需多年数据）",
         _source_status("量化模型预警", _used(risk_models_json))),
        ("数据勾稽校验", _used(validation_json),
         "缺少结构化财务数据（勾稽校验需三大报表字段）",
         _source_status("数据勾稽校验", _used(validation_json))),
    ]

    # 审计意见信号与台账覆盖对照提示（信号→风险条目联动缺失时的可见提示）
    # 持续经营重大不确定性信号已计入评分抬升，但不会自动修改 LLM 台账中对应
    # 风险条目的等级；关键审计事项/事务所变更信号不会自动生成风险条目。
    # 此处追加可见提示，避免「合规报告称重大、综合报告为一般」类矛盾静默存在。
    ao = _safe_json(audit_opinion_json)
    gc = ao.get("going_concern", {}) or {}
    if gc.get("flagged"):
        gc_levels = [str(r.get("level", "")).strip() for r in rd_all
                     if _norm_dim(r.get("dimension", "")) == "going_concern"]
        if not any(lv in ("重大", "重要") for lv in gc_levels):
            warnings.append("审计意见识别到持续经营重大不确定性信号（风险等级：重大），"
                            "台账中持续经营维度风险等级未达重要，建议人工复核是否上调")
        else:
            warnings.append("审计意见识别到持续经营重大不确定性信号，已与台账持续经营风险相互印证")
    kam = ao.get("key_audit_matters", {}) or {}
    matters = kam.get("matters", []) if isinstance(kam.get("matters"), list) else []
    missing_signals = []
    if matters:
        names = "、".join(str(m.get("matter", "")) for m in matters[:3] if isinstance(m, dict))
        missing_signals.append(f"关键审计事项{len(matters)}项（{names}）")
    if (ao.get("auditor_change", {}) or {}).get("flagged"):
        missing_signals.append("会计师事务所变更（意见购买嫌疑提示）")
    if missing_signals:
        warnings.append("审计意见提示的以下信号未在风险明细中单独立项，请人工复核覆盖："
                        + "；".join(missing_signals))

    fin_risks = _split_risks(rd_all, FINANCIAL_DIMS)
    comp_risks = _split_risks(rd_all, COMPLIANCE_DIMS)
    # 子集完备性校验：财务+合规子集未覆盖的条目（其他/未知维度）仅综合汇总可见，
    # 日志告警便于人工核对是否属维度标注缺失（不中断产出，与 _reconcile_summary 同风格）
    fin_ids = {r.get("risk_id") for r in fin_risks if isinstance(r, dict)}
    comp_ids = {r.get("risk_id") for r in comp_risks if isinstance(r, dict)}
    uncovered = [r for r in rd_all if isinstance(r, dict)
                 and r.get("risk_id") not in fin_ids and r.get("risk_id") not in comp_ids]
    if uncovered:
        _uncovered_dims = sorted({str(r.get("dimension", "")) for r in uncovered})
        logger.warning(f"子集完备性：{len(uncovered)} 项风险未落入财务/合规子集"
                       f"（维度：{_uncovered_dims}），仅综合汇总可见")
    # 环形图与 KPI 卡须同口径：传过滤后子集，避免「本报告结论」章内数字自相矛盾
    fin_json = json.dumps({"risk_details": fin_risks}, ensure_ascii=False)
    comp_json = json.dumps({"risk_details": comp_risks}, ensure_ascii=False)

    # S1 事实层注入：系统量化事实上下文（确定性工具结果，供风险明细核对区块渲染）
    facts_ctx = {"validation": validation_json, "financial": financial_indicators_json,
                 "disclosure": disclosure_check_json, "audit_opinion": audit_opinion_json}

    def _subset_note(label, sub):
        """S2 专项报告口径标注：本报告仅含维度子集，完整清单见综合汇总报告。"""
        return [Paragraph(
            f"本报告仅含{label}维度子集 {len(sub)} 项（按维度过滤），"
            f"完整风险清单共 {len(rd_all)} 项详见《综合汇总报告》。", st['small']),
            Spacer(1, 4)]

    docs = []   # [(文件名后缀, 报告标签, 封面标题行, 章节列表, 封面评分卡)]

    # ── 文档1：财务健康诊断报告 ──
    if "financial" in wanted:
        fin_chapters = [("公司简介", _profile_body(report, st), "公司全称/行业/主营业务等基本情况")]
        # 三大报表基本分析：确定性规则渲染（非 LLM），位于指标判读之前（用户指定顺序：
        # 先看三表明细再看指标判读）
        st_body = _statements_section_body(financial_indicators_json, st, font)
        if st_body:
            fin_chapters.append(("三大报表基本分析", st_body,
                                 "资产负债表/利润表/现金流量表关键科目与结构判读"))
        fin_body = _financial_section_body(financial_indicators_json, st, font, risk_json_str, industry,
                                            note=report.get("cashflow_penetration_note", ""))
        if fin_body:
            fin_chapters.append(("财务指标与审计关注分析", fin_body,
                                 "盈利/偿债/运营/成长指标、现金流质量、资产质量与审计关注"))
        my_body = _multi_year_body(compare_multi_year_json, st, font)
        if my_body:
            fin_chapters.append(("多年指标趋势分析", my_body,
                                 f"最近 {_safe_json(compare_multi_year_json).get('year_count', '多')} 年核心指标时序、趋势判定与预警"))
        fin_detail_body = _subset_note("财务健康", fin_risks) + _risk_detail_elements(fin_risks, st, facts_ctx)
        fin_pending = dict(report, pending_items=_split_risks(report.get("pending_items", []), FINANCIAL_DIMS))
        if fin_pending["pending_items"]:
            fin_chapters.append(("待复核提示", _pending_items_body(fin_pending, st), "人工复核数量与底稿索引"))
        fin_chapters += [
            ("财务风险明细", fin_detail_body, f"财务健康维度风险 {len(fin_risks)} 项的详细分析"),
            ("本报告结论", _sub_conclusion_body("财务健康", fin_risks, st, font, fin_json),
             "风险等级分布与汇总指引"),
            ("数据来源与完整性说明", _data_source_note(st, font, sources),
             "各数据源使用状态与缺失原因追溯"),
        ]
        docs.append(("财务健康诊断报告", "财务健康", ["上市公司年报", "财务健康诊断报告"],
                     fin_chapters, None))

    # ── 文档2：合规与信息披露报告 ──
    if "compliance" in wanted:
        comp_chapters = [("公司简介", _profile_body(report, st), "公司全称/行业/主营业务等基本情况")]
        dc_body = _disclosure_section_body(disclosure_check_json, st, font, audit_opinion_json,
                                            note=report.get("disclosure_consistency_note", ""))
        if dc_body:
            comp_chapters.append(("披露规范性检查结果", dc_body, "合规评分、披露问题与缺失章节"))
        ao_body = _audit_opinion_body(audit_opinion_json, st, font)
        if ao_body:
            comp_chapters.append(("审计意见识别与风险信号", ao_body,
                                  "意见类型/可信度影响/持续经营信号/关键审计事项"))
        comp_chapters.append(("合规标准对照", _compliance_standard_body(disclosure_check_json, st, font, comp_risks),
                              "证券法/信披管理办法/年报格式准则等监管要求对照"))
        # 合规风险明细章首部内嵌热力图（增强可视化；生成失败静默跳过）
        comp_detail_body = []
        comp_heatmap = _embed_chart(_generate_risk_heatmap, risk_json_str)
        if comp_heatmap:
            comp_detail_body += [Paragraph("<b>审计风险热力图（五维度×等级）：</b>", st['small']),
                                 comp_heatmap,
                                 Paragraph("图：风险热力图（实色为正式采信风险，斜线为待复核提示；待复核层不计入正式评分）", st['caption'])]
        comp_detail_body += _subset_note("合规与披露", comp_risks)
        comp_detail_body += _risk_detail_elements(comp_risks, st, facts_ctx)
        comp_pending = dict(report, pending_items=_split_risks(report.get("pending_items", []), COMPLIANCE_DIMS))
        if comp_pending["pending_items"]:
            comp_chapters.append(("待复核提示", _pending_items_body(comp_pending, st), "人工复核数量与底稿索引"))
        comp_chapters += [
            ("合规风险明细", comp_detail_body, f"合规与披露维度风险 {len(comp_risks)} 项的详细分析"),
            ("本报告结论", _sub_conclusion_body("合规与信息披露", comp_risks, st, font, comp_json),
             "风险等级分布与汇总指引"),
            ("数据来源与完整性说明", _data_source_note(st, font, sources),
             "各数据源使用状态与缺失原因追溯"),
        ]
        docs.append(("合规与信息披露报告", "合规信披", ["上市公司年报", "合规与信息披露报告"],
                     comp_chapters, None))

    # ── 文档3：综合汇总报告 ──
    if "synthesis" in wanted:
        sum_chapters = [("公司简介", _profile_body(report, st), "公司全称/行业/主营业务等基本情况")]

        # 综合评分解读章（有评分数据时）
        score_body = _score_section_body(comprehensive_score_json, st, font, disclosure_check_json,
                                          note=report.get("level_floor_note", ""))
        if score_body:
            sum_chapters.append(("综合评分解读", score_body,
                                 "综合风险评分、三维度分解、模型抬升理由与评分结论"))

        # 风险总览章：KPI 卡 + 环形图 + 维度表 + 热力图 + 清单
        overview_body = []
        # P2: 数据一致性提示为内部校验日志，仅 logger 记录，不渲染到客户可见 PDF
        if warnings:
            logger.info("数据一致性自动重算：" + "；".join(warnings))
        # KPI 以明细为唯一事实源（risk_summary 已由 _reconcile_summary 回写，双保险）
        c_all = _level_counts(rd_all)
        overview_body += [_kpi_cards([
            ("风险总数", str(len(rd_all)), PRIMARY, white),
            ("重大风险", str(c_all["重大"]), BAD_RED, white),
            ("重要风险", str(c_all["重要"]), WARN_ORANGE, white),
            ("一般风险", str(c_all["一般"]), ACCENT, white),
        ], font), Spacer(1, 10)]
        donut = _embed_chart(_gen_level_donut, risk_json_str, width_cm=8.0)
        if donut:
            overview_body += [donut, Paragraph("图：风险等级分布", st['caption'])]
        dims = rs.get("risk_dimensions", {})
        dim_data = [["风险维度", "风险数量"]]
        for k, v in dims.items():
            if isinstance(v, (int, float)):
                count = v
            elif isinstance(v, dict):
                count = v.get("count", v.get("数量", len(v)))
            elif isinstance(v, list):
                count = len(v)
            else:
                count = 0
            if count > 0:
                dim_data.append([DIM_CN.get(_norm_dim(k), k), str(count)])
        if len(dim_data) > 1:
            overview_body += [_styled_table(dim_data, [240, 120], font, font_size=10), Spacer(1, 10)]
        heatmap = _embed_chart(_generate_risk_heatmap, risk_json_str)
        if heatmap:
            overview_body += [Paragraph("<b>审计风险热力图（五维度×等级）：</b>", st['small']), heatmap,
                              Paragraph("图：风险热力图（列色按等级：重大红/重要黄/一般蓝；"
                                        "格内为去重后的正式风险数）", st['caption'])]
        # 风险清单摘要表（50d：风险ID 列并列展示语义编号）
        list_cell_style = _cell_style(font, 9)
        # 50d：待核实事项提示（confidence < 0.5 条目不得作为已确认风险结论引用）
        _pend_note = str(report.get("pending_verification_note", "") or "")
        _pend_note = _pend_note.replace("待核实事项", "待复核提示").replace("待核查事项", "待复核提示")
        if _pend_note:
            # note 文本自带提示前缀（与前端消息层同源），此处不再重复拼接。
            overview_body.append(Paragraph(
                "<font color='" + WARN_ORANGE.hexval() + "'>"
                + _esc(_pend_note) + "</font>", st['body']))
            overview_body.append(Spacer(1, 4))
        rows = [["风险ID", "维度", "标题", "等级"]]
        levels_col = []
        for r in rd_all:
            rows.append([
                _risk_id_label(r),
                Paragraph(_esc(_risk_display_dimension(r)), list_cell_style),
                Paragraph(_esc(_risk_display_title(r)), list_cell_style),
                r.get("level", ""),
            ])
            levels_col.append(r.get("level", ""))
        overview_body += [Paragraph("<b>风险清单摘要：</b>", st['small']),
                          _styled_table(rows, [60, 90, 170, 50], font, font_size=9,
                                        level_cols=[(3, levels_col)], level_colors_map=LEVEL_TEXT_COLORS)]
        sum_chapters.append(("风险总览", overview_body, "风险统计、等级分布、维度热力图与清单摘要"))

        # 交叉验证与风险传导链（台账已有字段，空则跳过）
        cv_body = _cross_validation_body(report, st)
        if cv_body:
            sum_chapters.append(("交叉验证分析", cv_body, "多指标勾稽比对与商业逻辑一致性检验"))
        rc_body = _risk_chain_body(report, st)
        if rc_body:
            sum_chapters.append(("风险传导链分析", rc_body, "风险因果传导路径识别"))
        # 审计合伙人复核意见（_post_process 回写台账，与前端消息同源）
        rv_body = _review_conclusion_body(report, st)
        if rv_body:
            sum_chapters.append(("系统辅助复核记录", rv_body,
                                 "多智能体辩论复核结论（与前端展示同源）"))
        pending_body = _pending_items_body(report, st)
        if pending_body:
            sum_chapters.append(("待复核提示", pending_body,
                                 "人工复核数量与底稿索引"))
        # 已排除嫌疑事项备查录（反向校验器移除的伪风险，可追溯留档）
        ex_body = _excluded_items_body(report, st)
        if ex_body:
            sum_chapters.append(("已排除嫌疑事项备查录", ex_body,
                                 "分析结论为无风险/风险极低的事项，已移出风险台账，备查留档"))

        # 整体评估结论（空值兜底文案，避免空白章）
        assessment = str(report.get("overall_assessment", "") or "").strip()
        if not assessment:
            assessment = ("本次未生成整体评估结论，请结合风险明细、交叉验证分析与"
                          "数据来源与完整性说明进行人工复核。")
        # L 补丁：评分快照替换（吞全 span），杜绝结论章与封面双分矛盾
        assessment = _apply_score_snapshot(assessment, report)
        # M 补丁：系统质检说明（自审剥离留痕）
        qc_body = _system_quality_notes_body(report, st)
        if qc_body:
            sum_chapters.append(("系统质检说明", qc_body, "系统内部状态误立项为发行人风险的剥离留痕"))
        sum_chapters += [
            ("风险明细", _risk_detail_elements(rd_all, st, facts_ctx), f"共 {len(rd_all)} 条风险的详细分析"),
            ("整体风险评估结论", [Paragraph(_para_text(assessment), st['body'])]
             + [Paragraph(_esc(AI_DISCLAIMER), st['small'])] + _excluded_note(report, st),
             "综合评估意见与审计建议"),
        ]
        sum_chapters.append(("行业基准对比", _industry_benchmark_body(report, financial_indicators_json, st, font),
                             "与行业平均水平的横向比较"))
        sum_chapters.append(("风险评估方法论",
                             _methodology_body(comprehensive_score_json, risk_models_json,
                                               validation_json, st, font),
                             "评分模型、等级映射、Z/M-Score 量化模型与数据来源说明"))
        sum_chapters.append(("数据来源与完整性说明", _data_source_note(st, font, sources),
                             "各数据源使用状态与缺失原因追溯"))
        sum_chapters.append(("产物一致性说明", _product_consistency_body(report, st, font, uncovered),
                             "三份报告同源台账与维度过滤映射说明"))
        docs.append(("综合汇总报告", "综合汇总", ["上市公司年报", "综合汇总报告"],
                     sum_chapters, score_card))

    # ── 逐份构建：免责声明校验 → 落盘 → 上传 → 收集链接 ──
    from local_storage import upload_file_to_storage
    lines = []
    for suffix, label, title_lines, chapters, cover_score in docs:
        elements = _cover_elements(title_lines, ci, st, font, report_no=prefix,
                                   score_card=cover_score, report_context=report)
        toc_els, chapter_els = _assemble_chapters(chapters, st, font)
        elements += toc_els + chapter_els
        elements += _disclaimer_page_elements(st)
        guard = _check_disclaimer(elements, doc_kind=f"PDF{suffix}")
        if guard:
            lines.append(f"PDF报告（{label}）{guard}")
            continue
        out = os.path.join(tempfile.gettempdir(), f"{prefix}_{suffix}_{uuid.uuid4().hex[:8]}.pdf")
        _build_pdf_doc(elements, out, font, footer_label=f"{suffix} · AI 辅助生成")
        url = upload_file_to_storage(out, f"reports/{prefix}_{suffix}.pdf", "application/pdf")
        if url.startswith("/") or url.startswith("http") or url.startswith("file://"):
            lines.append(f"PDF报告（{label}）已生成，下载链接: {url}")
        else:
            lines.append(f"PDF报告（{label}）已生成(本地): {out}")
    # 用双换行分隔：保证每条链接是独立 Markdown 段落。前端会按行去重移除与
    # 附件卡片重复的产物行，若用单换行拼接，三条会被 Markdown 视为同一段落，
    # 去重正则匹配到段落首行后会把三条链接整段移除（实测：正文丢链接）。
    return "\n\n".join(lines)


def _export_pdf_impl(risk_report_json: str, output_path: str = None,
                       financial_indicators_json: str = "", disclosure_check_json: str = "",
                       comprehensive_score_json: str = "", risk_models_json: str = "",
                       validation_json: str = "", compare_multi_year_json: str = "",
                       audit_opinion_json: str = "", module: str = "") -> str:
    """将风险台账 JSON 导出为格式化 PDF 风险报告的核心实现（分发器）。

    - 显式传 output_path：兼容模式，按旧结构生成单份汇总 PDF 到指定路径
    - 缺省 output_path：拆分模式，按 module 裁剪产物（financial 仅财务健康
      诊断报告，compliance 仅合规与信息披露报告，其余生成全部三份），
      上传存储并返回与实际产物一一对应的下载链接

    Args:
        risk_report_json: 风险台账 JSON 字符串或已解析的字典
        output_path: 可选输出路径（触发兼容单文件模式）
        financial_indicators_json: calculate_financial_indicators 工具结果（可空）
        disclosure_check_json: check_disclosure_compliance 工具结果（可空）
        comprehensive_score_json: calculate_comprehensive_score 工具结果（可空）
        risk_models_json: calculate_risk_models 工具结果（可空）
        validation_json: validate_financial_data 工具结果（可空）
        compare_multi_year_json: compare_multi_year 工具结果（可空）
        audit_opinion_json: identify_audit_opinion 工具结果（可空）

    Returns:
        成功时返回含下载链接的提示文本，失败时返回错误信息
    """
    try:
        report = json.loads(risk_report_json) if isinstance(risk_report_json, str) else risk_report_json
    except json.JSONDecodeError as e:
        return f"JSON解析失败: {e}"
    if not isinstance(report, dict):
        report = {}

    snapshot = report.get("report_snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("risks"), dict):
        # 导出器只负责版式和模块筛选。所有风险、指标、评分、期间和证据
        # 均从终局快照派生，旧字段只保留给没有快照的历史调用。
        from core.report_snapshot import snapshot_as_legacy_payload
        report = snapshot_as_legacy_payload(snapshot, report)
        report["report_snapshot"] = snapshot
        financial_indicators_json = json.dumps(snapshot.get("financial") or {}, ensure_ascii=False, default=str)
        disclosure_check_json = json.dumps(snapshot.get("disclosure") or {}, ensure_ascii=False, default=str)
        comprehensive_score_json = json.dumps(snapshot.get("score") or {}, ensure_ascii=False, default=str)
        risk_models_json = json.dumps(snapshot.get("risk_models") or {}, ensure_ascii=False, default=str)
        validation_json = json.dumps(snapshot.get("validation") or {}, ensure_ascii=False, default=str)
        compare_multi_year_json = json.dumps(snapshot.get("multi_year") or {}, ensure_ascii=False, default=str)

    if output_path:
        return _export_legacy_single(report, output_path)
    return _export_split_reports(report, financial_indicators_json, disclosure_check_json,
                                 comprehensive_score_json, risk_models_json, validation_json,
                                 compare_multi_year_json, audit_opinion_json, module=module)


@tool
def export_pdf_report(risk_report_json: str, financial_indicators_json: str = "",
                      disclosure_check_json: str = "", comprehensive_score_json: str = "",
                      risk_models_json: str = "", validation_json: str = "",
                      compare_multi_year_json: str = "", audit_opinion_json: str = "",
                      module: str = "") -> str:
    """将风险台账 JSON 导出为独立 PDF 报告，上传到存储并返回下载链接。

    默认拆分生成（module 缺省/为 synthesis 时全部三份）：
    1. 财务健康诊断报告（指标与审计关注分析 + 指标对比图/雷达图/热力图 + 多年趋势 + 财务维度风险明细）
    2. 合规与信息披露报告（披露检查结果 + 审计意见识别 + 合规标准对照 + 合规维度风险明细）
    3. 综合汇总报告（综合评分解读 + 风险总览 + 交叉验证/传导链 + 复核意见 + 全部风险明细 + 方法论）

    module 为 financial 时仅生成第 1 份，为 compliance 时仅生成第 2 份。
    每份均含封面（封皮）、目录、数据来源与完整性说明与免责声明页脚水印。
    可选入参为对应工具结果，提供时报告中会附上相应专项章节与图表。

    Args:
        risk_report_json: 风险台账 JSON 字符串，包含 company_info、risk_summary、
            risk_details、overall_assessment 等字段
        financial_indicators_json: calculate_financial_indicators 的完整结果（可选）
        disclosure_check_json: check_disclosure_compliance 的完整结果（可选）
        comprehensive_score_json: calculate_comprehensive_score 的完整结果（可选）
        risk_models_json: calculate_risk_models 的完整结果（可选）
        validation_json: validate_financial_data 的完整结果（可选）
        compare_multi_year_json: compare_multi_year 的完整结果（可选）
        audit_opinion_json: identify_audit_opinion 的完整结果（可选）
        module: 任务模块标识（financial/compliance/synthesis），缺省生成全部三份

    Returns:
        与实际产物一一对应的含下载链接提示文本，或错误信息
    """
    return _export_pdf_impl(risk_report_json, None, financial_indicators_json, disclosure_check_json,
                            comprehensive_score_json, risk_models_json, validation_json,
                            compare_multi_year_json, audit_opinion_json, module=module)
