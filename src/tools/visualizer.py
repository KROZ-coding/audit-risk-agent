"""可视化看板工具 - 风险热力图、财务雷达图、趋势折线图

本模块提供三种审计可视化图表的生成能力：
1. 风险热力图：五大风险维度 × 风险等级矩阵，颜色深浅表示风险强度
2. 财务雷达图：五维财务指标与公司所在行业基准的对比
3. 趋势折线图：多年关键财务科目和比率的变化趋势

所有图表均使用 matplotlib 渲染，支持中文字体自动配置。
"""
import json
import os
import tempfile
import numpy as np
from langchain_core.tools import tool
from datetime import datetime

from utils.filename import resolve_company_year, sanitize_filename


def _build_file_prefix(data: dict) -> str:
    """根据报告数据生成统一的文件名前缀：日期_公司名_年份。

    Args:
        data: 包含 company_info 的报告字典

    Returns:
        格式为 "YYYYMMDD_公司名_年份" 的安全文件名前缀
    """
    ci = data.get("company_info", {})
    # 别名兼容：LLM 可能用 name/report_period 等键名，避免文件名变「未知公司_未知」
    raw_company, raw_year = resolve_company_year(ci)
    company = sanitize_filename(raw_company or "未知公司")
    year = sanitize_filename(raw_year) if raw_year else ""
    date_str = datetime.now().strftime("%Y%m%d")
    if year:
        return f"{date_str}_{company}_{year}"
    return f"{date_str}_{company}"


# ── 公共辅助 ──────────────────────────────────────────────

# 中文字体初始化标志（全局单例，避免重复注册）
_font_initialized = False
_font_prop = None


def _setup_chinese_font():
    """配置 matplotlib 中文字体，全局仅初始化一次。

    搜索顺序：
    1. 项目 assets 目录下的文泉驿微米黑 (wqy-microhei.ttc)
    2. Linux 系统字体目录下的文泉驿字体

    Returns:
        FontProperties 对象，若所有字体均注册失败则返回 None
    """
    global _font_initialized, _font_prop
    if _font_initialized:
        return _font_prop

    import logging
    logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
    import matplotlib; matplotlib.use('Agg')  # type: ignore  # 使用非交互后端
    import matplotlib.pyplot as plt  # type: ignore
    import matplotlib.font_manager as fm  # type: ignore

    # 从环境变量或默认路径获取工作目录
    workspace = os.getenv('COZE_WORKSPACE_PATH', os.path.join(os.path.dirname(__file__), '..', '..'))
    for fp in [
        os.path.join(workspace, 'assets', 'wqy-microhei.ttc'),
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]:
        if os.path.exists(fp):
            try:
                fm.fontManager.addfont(fp)
                p = fm.FontProperties(fname=fp)
                # 将中文字体设置为 matplotlib 默认无衬线字体的首选项
                plt.rcParams['font.sans-serif'] = [p.get_name()] + plt.rcParams['font.sans-serif']
                plt.rcParams['font.family'] = 'sans-serif'
                plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示异常
                _font_prop = p
                _font_initialized = True
                return p
            except Exception:
                continue
    _font_initialized = True
    return None


def _finalize(fig, output_path):
    """保存图表到磁盘并关闭 matplotlib figure 释放内存。

    Args:
        fig: matplotlib Figure 对象
        output_path: 输出文件的完整路径

    Returns:
        输出文件路径
    """
    fig.savefig(output_path, dpi=150, bbox_inches='tight')
    import matplotlib.pyplot as plt; plt.close(fig)  # type: ignore
    return output_path


def _upload(chart_type: str, path: str, filename: str) -> str:
    """将生成的图表文件上传到存储，返回 JSON 格式结果。

    Args:
        chart_type: 图表类型名称（如 '风险热力图'）
        path: 图表文件本地路径
        filename: 存储用的文件名

    Returns:
        JSON 字符串，包含 chart_type、download_url/file_path、status
    """
    from local_storage import upload_file_to_storage
    url = upload_file_to_storage(path, f'charts/{filename}', 'image/png')
    # 根据 URL 格式决定返回键名：HTTP 路径用 download_url，本地路径用 file_path
    key = 'download_url' if url.startswith('/') or url.startswith('http') else 'file_path'
    return json.dumps({'chart_type': chart_type, key: url, 'status': 'success'}, ensure_ascii=False)


# ── 热力图 ────────────────────────────────────────────────

def _generate_risk_heatmap(risk_report_json: str, output_path: str) -> str:
    """生成风险热力图，返回文件路径。

    算法说明：
    - 构建 5×3 矩阵（5大风险维度 × 3个风险等级）
    - 遍历 risk_details，按维度+等级匹配后累加置信度作为热力值
    - 使用 YlOrRd 色图渲染，格内显示风险数量和置信度加权值

    Args:
        risk_report_json: 风险台账 JSON 字符串
        output_path: 输出图片路径

    Returns:
        生成的图片文件路径
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(risk_report_json)
    risk_details = data.get('risk_details', [])

    # 定义五大风险维度（中英文）和三级风险等级
    dimensions_cn = ['财务错报风险', '关联交易风险', '信息披露合规风险', '持续经营风险', '监管处罚类高风险']
    dimensions_en = ['financial_misstatement', 'related_party', 'disclosure_compliance', 'going_concern', 'regulatory_penalty']
    levels = ['重大', '重要', '一般']

    # 构建维度×等级矩阵，值为置信度累加
    matrix = np.zeros((len(dimensions_cn), len(levels)))
    for r in risk_details:
        d = r.get('dimension', '')
        l = r.get('level', '')
        # 匹配风险维度（支持中英文双向匹配）
        for i, (dim_cn, dim_en) in enumerate(zip(dimensions_cn, dimensions_en)):
            if dim_cn in d or dim_en in d or d in dim_cn or d == dim_en:
                # 匹配风险等级并累加置信度
                for j, lv in enumerate(levels):
                    if lv in l:
                        matrix[i, j] += r.get('confidence', 0)
                        break
                break

    # 渲染热力图
    fig, ax = plt.subplots(figsize=(10, 6))
    im = ax.imshow(matrix, cmap='YlOrRd', aspect='auto', vmin=0, vmax=max(1, matrix.max()))
    # 在格内标注数值
    for i in range(len(dimensions_cn)):
        for j in range(len(levels)):
            v = matrix[i, j]
            text = ax.text(j, i, f'{int(v)}({v:.1f})' if v > 0 else '', ha='center', va='center',
                           fontsize=12, color='white' if v > 0.5 else 'black')
    ax.set_xticks(range(len(levels)))
    ax.set_yticks(range(len(dimensions_cn)))
    ax.set_xticklabels(levels, fontproperties=fp, fontsize=12)
    ax.set_yticklabels(dimensions_cn, fontproperties=fp, fontsize=11)
    # 设置标题（公司名+年份，别名兼容）
    company, year = resolve_company_year(data.get('company_info', {}))
    company = company or '未提供'
    ax.set_title(f'{company}{year} 审计风险热力图\n格内:风险数(置信度加权)', fontproperties=fp, fontsize=13, pad=15)
    cbar = plt.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label('风险强度(置信度加权)', fontproperties=fp, fontsize=10)
    return _finalize(fig, output_path)


@tool
def generate_risk_heatmap(risk_report_json: str) -> str:
    """生成审计风险热力图（五大维度×风险等级），返回图片下载URL。

    Args:
        risk_report_json: 包含 company_info 和 risk_details 的风险台账 JSON
    """
    try:
        data = json.loads(risk_report_json)
        prefix = _build_file_prefix(data)
        filename = f"{prefix}_风险热力图.png"
        path = _generate_risk_heatmap(risk_report_json, os.path.join(tempfile.gettempdir(), filename))
        return _upload('风险热力图', path, filename)
    except Exception as e:
        return f'热力图生成失败: {str(e)}'


# ── 雷达图 ────────────────────────────────────────────────

def _generate_radar_chart(risk_report_json: str, output_path: str) -> str:
    """生成财务指标雷达图，返回文件路径。

    对比五个维度的公司实际值与行业基准值：
    毛利率、资产负债率、应收占营收比、存货周转率、流动比率

    Args:
        risk_report_json: 风险台账 JSON（含 calculated_indicators 和 company_info）
        output_path: 输出图片路径
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(risk_report_json)
    ci = data.get('company_info', {})
    # 别名兼容解析公司名/年度，供图例与标题使用
    _company, _year = resolve_company_year(ci)
    indicators_data = data.get('calculated_indicators', {})
    industry = ci.get('industry', '制造业')

    # 从 industry_benchmarks.json 读取行业基准值
    workspace = os.getenv('COZE_WORKSPACE_PATH', os.path.join(os.path.dirname(__file__), '..', '..'))
    bm_path = os.path.join(workspace, 'assets', 'industry_benchmarks.json')
    industry_benchmark = {}
    try:
        with open(bm_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        # 在基准数据中查找匹配的行业
        ind_data = raw.get('industries', raw)
        for k, v in ind_data.items():
            if industry in k or industry in v:
                bms = v.get('benchmarks', v)
                industry_benchmark = {
                    'gross_margin': bms.get('gross_margin', {}).get('average', 25),
                    'debt_to_asset_ratio': bms.get('debt_to_asset_ratio', {}).get('average', 50),
                    'ar_to_revenue_ratio': bms.get('ar_to_revenue_ratio', {}).get('average', 20),
                    'inventory_turnover': bms.get('inventory_turnover', {}).get('average', 6),
                    'current_ratio': bms.get('current_ratio', {}).get('average', 1.5),
                }
                break
    except Exception:
        pass

    # 构建五维指标数据：(名称, 公司实际值, 行业基准值)
    metrics = [
        ('毛利率(%)', indicators_data.get('gross_margin_pct', 30), industry_benchmark.get('gross_margin', 25)),
        ('资产负债率(%)', indicators_data.get('debt_to_asset_ratio_pct', 75), industry_benchmark.get('debt_to_asset_ratio', 50)),
        ('应收占营收比(%)', indicators_data.get('accounts_receivable_to_revenue_ratio', 40), industry_benchmark.get('ar_to_revenue_ratio', 20)),
        ('存货周转率(次)', indicators_data.get('inventory_turnover_ratio', 2.33), industry_benchmark.get('inventory_turnover', 6)),
        ('流动比率(倍)', indicators_data.get('current_ratio', 0.8), industry_benchmark.get('current_ratio', 1.5)),
    ]
    labels = [m[0] for m in metrics]
    company_vals = [float(m[1]) for m in metrics]
    benchmark_vals = [float(m[2]) for m in metrics]

    # 构建极坐标数据（首尾相连形成闭合多边形）
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]
    company_vals += company_vals[:1]
    benchmark_vals += benchmark_vals[:1]

    # 绘制雷达图
    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.fill(angles, company_vals, alpha=0.25, color='red', label=f'{_company or "公司"}实际值')
    ax.fill(angles, benchmark_vals, alpha=0.25, color='blue', label=f'{industry}基准')
    ax.plot(angles, company_vals, 'o-', color='red', linewidth=2, markersize=6)
    ax.plot(angles, benchmark_vals, 'o-', color='blue', linewidth=2, markersize=6)

    # 标注每个维度的具体数值
    for i, (angle, cv, bv) in enumerate(zip(angles[:-1], company_vals[:-1], benchmark_vals[:-1])):
        ax.annotate(f'{cv:.1f}(基准{bv:.1f})', xy=(angle, cv),
                    fontproperties=fp, fontsize=8, ha='center', va='bottom',
                    color='darkred', fontweight='bold')

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontproperties=fp, fontsize=10)
    ax.set_title(f'{_company or "未提供"}{_year} 财务指标雷达图\n({industry}行业基准对比)',
                 fontproperties=fp, fontsize=13, pad=20)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), prop=fp, fontsize=9)
    return _finalize(fig, output_path)


@tool
def generate_radar_chart(risk_report_json: str) -> str:
    """生成财务指标雷达图（与行业基准对比），返回图片下载URL。

    Args:
        risk_report_json: 包含 company_info 和 calculated_indicators 的风险台账 JSON
    """
    try:
        data = json.loads(risk_report_json)
        prefix = _build_file_prefix(data)
        filename = f"{prefix}_财务雷达图.png"
        path = _generate_radar_chart(risk_report_json, os.path.join(tempfile.gettempdir(), filename))
        return _upload('雷达图', path, filename)
    except Exception as e:
        return f'雷达图生成失败: {str(e)}'


# ── 趋势图 ────────────────────────────────────────────────

def _generate_trend_chart(trend_data_json: str, output_path: str) -> str:
    """生成多年财务指标趋势折线图（双面板），返回文件路径。

    左面板：关键科目绝对值趋势（营收、净利润、经营现金流、应收账款、存货）
    右面板：关键比率趋势（资产负债率、毛利率）
    自动根据数值量级调整单位（元/万元/亿元）

    Args:
        trend_data_json: 包含 company_name 和 years 列表的 JSON
        output_path: 输出图片路径

    Returns:
        生成的图片路径，或数据不足时的提示文本
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(trend_data_json)
    company_name = data.get('company_name', '未提供')
    years_data = data.get('years', [])

    # 至少需要 2 年数据才能绘制趋势图
    if len(years_data) < 2:
        return '需要至少2年的财务数据才能生成趋势图'

    # 提取各年度关键科目数据
    years = [str(y.get('year', '')) for y in years_data]
    revenue = [y.get('revenue', 0) for y in years_data]
    net_profit = [y.get('net_profit', 0) for y in years_data]
    ocfs = [y.get('operating_cashflow', 0) for y in years_data]
    ar = [y.get('accounts_receivable', 0) for y in years_data]
    inventory = [y.get('inventory', 0) for y in years_data]

    # 自动检测数值量级，选择合适的显示单位
    max_val = max(max(revenue), max(net_profit), max(ocfs), max(ar), max(inventory), 1)
    unit_label = '元'
    divisor = 1
    if max_val >= 1e8:
        divisor = 1e8
        unit_label = '亿元'
    elif max_val >= 1e4:
        divisor = 1e4
        unit_label = '万元'
    # 按量级缩放所有数值
    revenue = [v / divisor for v in revenue]
    net_profit = [v / divisor for v in net_profit]
    ocfs = [v / divisor for v in ocfs]
    ar = [v / divisor for v in ar]
    inventory = [v / divisor for v in inventory]

    # 创建双面板图表
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    x = range(len(years))

    # 左面板：关键科目绝对值趋势
    ax1.plot(x, revenue, 'o-', label='营业收入', color='steelblue', linewidth=2, markersize=6)
    ax1.plot(x, net_profit, 's-', label='净利润', color='green', linewidth=2, markersize=6)
    ax1.plot(x, ocfs, 'd-', label='经营现金流', color='red', linewidth=2, markersize=6)
    ax1.plot(x, ar, '^-', label='应收账款', color='orange', linewidth=2, markersize=6)
    ax1.plot(x, inventory, 'v-', label='存货', color='purple', linewidth=2, markersize=6)
    ax1.axhline(y=0, color='gray', linestyle='--', linewidth=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(years)
    ax1.set_xlabel('年度', fontproperties=fp, fontsize=11)
    ax1.set_ylabel(f'金额({unit_label})', fontproperties=fp, fontsize=11)
    ax1.set_title('关键科目趋势', fontproperties=fp, fontsize=13)
    ax1.legend(prop=fp, fontsize=9, loc='upper left')
    ax1.grid(True, alpha=0.3)

    # 右面板：关键比率趋势（仅当有总资产数据时绘制）
    has_ratios = all(y.get('total_assets', 0) > 0 for y in years_data)
    if has_ratios:
        debt_ratios = [y.get('total_liabilities', 0) / max(y.get('total_assets', 1), 1) * 100 for y in years_data]
        gross_margins = []
        for y in years_data:
            rev = y.get('revenue', 1)
            cogs = y.get('cost_of_goods', rev * 0.7)  # 缺省营业成本时按营收 70% 估算
            gross_margins.append((rev - cogs) / max(rev, 1) * 100 if rev > 0 else 0)
        ax2.plot(x, debt_ratios, 'o-', label='资产负债率', color='darkred', linewidth=2, markersize=6)
        ax2.plot(x, gross_margins, 's-', label='毛利率', color='darkgreen', linewidth=2, markersize=6)
        ax2.set_xticks(x)
        ax2.set_xticklabels(years)
        ax2.set_xlabel('年度', fontproperties=fp, fontsize=11)
        ax2.set_ylabel('比率(%)', fontproperties=fp, fontsize=11)
        ax2.set_title('关键比率趋势', fontproperties=fp, fontsize=13)
        ax2.legend(prop=fp, fontsize=9, loc='upper left')
        ax2.grid(True, alpha=0.3)
    else:
        ax2.axis('off')  # 无数据时隐藏右面板

    fig.suptitle(f'{company_name} 多年财务指标趋势', fontproperties=fp, fontsize=14, y=1.02)
    plt.tight_layout()
    return _finalize(fig, output_path)


@tool
def generate_trend_chart(trend_data_json: str) -> str:
    """生成多年财务指标趋势折线图，返回图片下载URL。

    Args:
        trend_data_json: 格式为 {"company_name":"公司名","years":[{"year":"2022","revenue":50000,...},...]}
    """
    try:
        data = json.loads(trend_data_json)
        company = sanitize_filename(data.get("company_name", "未知公司"))
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{date_str}_{company}_趋势图.png"
        path = _generate_trend_chart(trend_data_json, os.path.join(tempfile.gettempdir(), filename))
        if path.startswith('需要') or path.startswith('无'):
            return path
        return _upload('趋势图', path, filename)
    except Exception as e:
        return f'趋势图生成失败: {str(e)}'
