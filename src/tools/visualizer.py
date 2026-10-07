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

from utils.filename import resolve_company_year, sanitize_filename, build_file_prefix as _build_file_prefix, to_roman, count_existing_runs
from core.report_snapshot import build_visualization_payload


# 雷达图五维指标规格：(展示名, calculated_indicators 键, 行业基准键)
# 固定五维口径，逐项判定数据完整度；不因某一维缺失而整图失败。
_RADAR_METRIC_SPECS = [
    ('毛利率(%)', 'gross_margin_pct', 'gross_margin'),
    ('资产负债率(%)', 'debt_to_asset_ratio_pct', 'debt_to_asset_ratio'),
    ('应收占营收比(%)', 'accounts_receivable_to_revenue_ratio', 'ar_to_revenue_ratio'),
    ('存货周转率(次)', 'inventory_turnover_ratio', 'inventory_turnover'),
    ('流动比率(倍)', 'current_ratio', 'current_ratio'),
]



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


def _formal_risk_details(data: dict) -> list[dict]:
    """图表只统计正式风险；待复核项目单独保留在报告台账中。"""
    snapshot = data.get("report_snapshot") if isinstance(data, dict) else None
    if isinstance(snapshot, dict):
        layers = snapshot.get("risks") if isinstance(snapshot.get("risks"), dict) else {}
        return [item for item in (layers.get("formal") or []) if isinstance(item, dict)]
    source = data.get("accepted_risk_details") if "accepted_risk_details" in data else data.get("risk_details", [])
    unique = {}
    for item in source or []:
        if not isinstance(item, dict):
            continue
        risk_id = str(item.get("risk_id") or item.get("semantic_id") or "")
        if not risk_id:
            continue
        unique.setdefault(risk_id, item)
    return list(unique.values())


def _norm_dimension(value: str) -> str:
    aliases = {
        "财务错报": "financial_misstatement", "财务错报风险": "financial_misstatement",
        "财务": "financial_misstatement", "related_party": "related_party", "关联交易": "related_party",
        "关联交易风险": "related_party", "信披合规": "disclosure_compliance",
        "信息披露合规风险": "disclosure_compliance", "信息披露合规": "disclosure_compliance",
        "持续经营": "going_concern", "持续经营风险": "going_concern",
        "监管处罚": "regulatory_penalty", "监管处罚类高风险": "regulatory_penalty",
    }
    text = str(value or "").strip()
    return aliases.get(text, text.lower())


def _norm_level(value: str) -> str:
    return {
        "重大": "重大", "高风险": "重大", "极高风险": "重大", "严重": "重大", "高": "重大",
        "重要": "重要", "中等风险": "重要", "中": "重要",
        "一般": "一般", "低风险": "一般", "轻微": "一般", "低": "一般",
    }.get(str(value or "").strip(), "")


# ── 热力图 ────────────────────────────────────────────────

def _empty_heatmap_reason(data: dict, formal: list) -> str:
    """空热力图的成因文案：三种来源互斥，按台账实际内容判定。

    空白热力图无法与渲染失败区分，必须写明「为什么没有内容」。不猜测上游
    分析环节的失败原因（那属于数据来源说明的职责），只看本图拿到的台账：

    - 台账没有候选风险条目；
    - 有候选但都没有通过证据门禁（未进入 accepted_risk_details）；
    - 已采信但维度或等级不在本图口径内（如数据可靠性风险、无法归一化的等级）。
    """
    candidates = [r for r in (data.get("risk_details") or []) if isinstance(r, dict)]
    if not candidates:
        return "台账未包含风险条目"
    if not formal:
        return "候选风险均未通过证据门禁，不计入正式风险"
    return "正式风险的维度或等级不在本图口径内"

def _generate_risk_heatmap(risk_report_json: str, output_path: str) -> str:
    """生成风险热力图，返回文件路径。

    算法说明：
    - 构建 5×3 矩阵（5大风险维度 × 3个风险等级）
    - 按唯一风险编号计数（不按置信度加权：模型自评置信度不是风险强度证据）
    - 列色固定按等级口径（重大红、重要黄、一般蓝），格内深浅表示该格数量在
      全图内的相对大小，格内标注真实去重数量，色盲用户凭数字也能读图

    Args:
        risk_report_json: 风险台账 JSON 字符串
        output_path: 输出图片路径

    Returns:
        生成的图片文件路径
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(risk_report_json)
    risk_details = _formal_risk_details(data)

    # 定义五大风险维度（中英文）和三级风险等级
    dimensions_cn = ['财务风险', '关联方交易与资金往来', '信息披露与合规', '持续经营与偿债', '监管问询与处罚']
    dimensions_en = ['financial_misstatement', 'related_party', 'disclosure_compliance', 'going_concern', 'regulatory_penalty']
    levels = ['重大', '重要', '一般']

    snapshot = data.get("report_snapshot") if isinstance(data, dict) else None
    if isinstance(snapshot, dict):
        heat_payload = (data.get("visualization_payloads") or {}).get("heatmap")
        if not isinstance(heat_payload, dict):
            heat_payload = build_visualization_payload(snapshot, "heatmap")
        matrix = np.asarray(heat_payload.get("formal_matrix") or np.zeros((5, 3)), dtype=float)
        pending_matrix = np.asarray(heat_payload.get("pending_matrix") or np.zeros((5, 3)), dtype=float)
        pending_details = [item for item in ((snapshot.get("risks") or {}).get("pending") or []) if isinstance(item, dict)]
        unknown_mapping = heat_payload.get("unknown_mapping") or []
    else:
        # 兼容旧台账：仍按唯一编号统计正式风险。
        matrix = np.zeros((len(dimensions_cn), len(levels)))
        pending_matrix = np.zeros_like(matrix)
        pending_details = []
        unknown_mapping = []
        for r in risk_details:
            d = _norm_dimension(r.get('dimension', ''))
            l = _norm_level(r.get('level', ''))
            # 匹配风险维度（支持中英文双向匹配）
            for i, (dim_cn, dim_en) in enumerate(zip(dimensions_cn, dimensions_en)):
                if d == dim_en:
                    for j, lv in enumerate(levels):
                        if lv == l:
                            matrix[i, j] += 1
                            break
                    break

    # 渲染热力图：等级列配色与 PDF/Excel 的等级色一致，避免同一等级在不同产物
    # 里颜色不同（此前用连续色阶，看不出列与等级的对应关系）
    level_rgb = {'重大': (1.0, 0.0, 0.0), '重要': (1.0, 0.83, 0.0),
                 '一般': (0.27, 0.45, 0.77)}
    fig, ax = plt.subplots(figsize=(10, 6))
    max_v = max(1.0, float(max(matrix.max(), pending_matrix.max())))
    for i in range(len(dimensions_cn)):
        for j, lv in enumerate(levels):
            v = float(matrix[i, j])
            if v <= 0:
                face = '#FFFFFF'
            else:
                # 强度带下限：真实台账里单格多为 1~2 条，若按下限 0.25 起算会被冲淡到
                # 看不出等级色。0.55 起步保证「有风险」与「无风险」一眼可辨，
                # 上限 1.0 表示该格数量为全图最大。
                strength = 0.55 + 0.45 * (v / max_v)
                face = tuple(1 - (1 - c) * strength for c in level_rgb[lv])
            ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=face,
                                       edgecolor='#B0B0B0', linewidth=0.8))
            if v > 0:
                # 文字色按底色亮度自动取黑/白（黄底配白字不可读，红底配黑字对比不足）
                lum = 0.299 * face[0] + 0.587 * face[1] + 0.114 * face[2]
                ax.text(j, i, f'{int(v)}', ha='center', va='center', fontsize=12,
                        color='black' if lum > 0.6 else 'white')
            pending = float(pending_matrix[i, j])
            if pending > 0:
                # 斜线覆盖表示待核查层，避免与正式风险的实色混为同一统计。
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1,
                                           facecolor='none', edgecolor='#666666',
                                           linewidth=1.2, hatch='///', alpha=0.85))
                label = f'待复核 {int(pending)}'
                ax.text(j, i + 0.27, label, ha='center', va='center', fontsize=7.5,
                        color='#555555', fontproperties=fp)
    ax.set_xlim(-0.5, len(levels) - 0.5)
    ax.set_ylim(len(dimensions_cn) - 0.5, -0.5)
    ax.set_xticks(range(len(levels)))
    ax.set_yticks(range(len(dimensions_cn)))
    ax.set_xticklabels(levels, fontproperties=fp, fontsize=12)
    ax.set_yticklabels(dimensions_cn, fontproperties=fp, fontsize=11)
    # 无数据时图面必须自证「没有数据」而不是一片空白：空白热力图无法与渲染失败
    # 区分，实测会被当成图表 bug（用户提问「为什么热力图里面没有内容」）。
    # 原因按台账实际情况区分，不猜测分析环节的失败来源。
    total_mapped = int(matrix.sum())
    pending_mapped = int(pending_matrix.sum())
    if total_mapped == 0 and pending_mapped == 0:
        ax.text(0.5, 0.5, f"本次无正式风险数据\n（{_empty_heatmap_reason(data, risk_details)}）",
                transform=ax.transAxes,
                ha='center', va='center', fontproperties=fp, fontsize=13,
                color='#6B7280', linespacing=1.6)
    elif len(risk_details) > total_mapped or len(pending_details) > pending_mapped or unknown_mapping:
        # 未落入五类维度的条目（如数据可靠性风险）或等级无法归一化的条目会被
        # 静默漏掉，图下注明差值，避免「图上 3 条、台账 5 条」对不上账。
        omitted = len(unknown_mapping) or max(0, len(risk_details) - total_mapped) + max(0, len(pending_details) - pending_mapped)
        ax.text(0.5, -0.12, f"另有 {omitted} 条事项未映射到五维×三级矩阵，未计入本图",
                transform=ax.transAxes, ha='center', va='top',
                fontproperties=fp, fontsize=9, color='#6B7280')
    # 设置标题（公司名+年份，别名兼容）
    company, year = resolve_company_year(data.get('company_info', {}))
    company = company or '未提供'
    ax.set_title(f'{company}{year} 审计风险热力图\n'
                 '列色按等级（重大红/重要黄/一般蓝），深浅为该格数量在全图中的相对大小；'
                 '实色=正式采信风险，斜线=待复核提示；两层均按唯一风险编号去重',
                 fontproperties=fp, fontsize=12, pad=15)
    ax.text(0.5, -0.19,
            '正式采信风险与待复核提示分层展示；待复核提示不计入正式评分和正式风险总数。',
            transform=ax.transAxes, ha='center', va='top', fontproperties=fp,
            fontsize=9, color='#555555')
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

    数据完整性兜底（核心原则：有多少数据就画多少，缺的标注数据不全，绝不伪造）：
    - 固定五维指标，逐项判定「公司实际值」与「可核验行业基准」是否齐备；
    - 有任一维度可对比即绘制多边形，缺失维度不画 0 值、不以默认值补基准，
      而是把已获取的数值记录在图上并标注缺失原因（无行业基准/缺公司值/数据未获取）；
    - 完全没有可比维度时仍出图自证「无数据」，并写明原因，不再抛异常或静默消失。

    Args:
        risk_report_json: 风险台账 JSON（含 calculated_indicators 和 company_info）
        output_path: 输出图片路径
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(risk_report_json)
    ci = data.get('company_info', {})
    snapshot = data.get("report_snapshot") if isinstance(data, dict) else None
    snapshot_radar = None
    if isinstance(snapshot, dict):
        snapshot_radar = (data.get("visualization_payloads") or {}).get("radar")
        if not isinstance(snapshot_radar, dict):
            snapshot_radar = build_visualization_payload(snapshot, "radar")
        ci = snapshot.get("company") or ci
    # 别名兼容解析公司名/年度，供图例与标题使用
    _company, _year = resolve_company_year(ci)
    indicators_data = data.get('calculated_indicators', {})
    if not isinstance(indicators_data, dict):
        indicators_data = {}
    if isinstance(snapshot_radar, dict):
        indicators_data.update({str(item.get("metric_id")): item.get("actual")
                                for item in snapshot_radar.get("metrics", [])
                                if isinstance(item, dict) and item.get("metric_id")})
    industry = ci.get('industry') or data.get('industry')
    industry = str(industry or '').strip()

    # 从 industry_benchmarks.json 读取行业基准值：只接受带来源可核验的基准
    # （sourced_benchmark_value 契约，见 core/benchmark_contract.py）。
    # 未核验的数值一律视为「无行业基准」，绝不把内置默认值冒充同业均数。
    industry_benchmark = {}
    if industry:
        workspace = os.getenv('COZE_WORKSPACE_PATH', os.path.join(os.path.dirname(__file__), '..', '..'))
        bm_path = os.path.join(workspace, 'assets', 'industry_benchmarks.json')
        try:
            with open(bm_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            # F4 行业匹配加固：改用 match_keywords + match_priority（特异性优先、
            # 关键词最长者胜），替换旧的「子串互含 + 首个命中即 break」——旧逻辑
            # 会把"医药制造业"错配到通用"制造业"基准（毛利率 25% vs 医药约 70%），
            # 方向性误导。无任何关键词命中时不做基准对比（宁缺毋错）。
            from core.benchmark_contract import match_industry_entry
            v, _matched_name = match_industry_entry(industry, raw.get('industries', raw))
            if v is not None:
                bms = v.get('benchmarks', v)
                for key in ('gross_margin', 'debt_to_asset_ratio', 'ar_to_revenue_ratio',
                            'inventory_turnover', 'current_ratio'):
                    spec = bms.get(key) if isinstance(bms, dict) else None
                    from core.benchmark_contract import sourced_benchmark_value
                    value = sourced_benchmark_value(spec, v)
                    if value is not None:
                        industry_benchmark[key] = value
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            industry_benchmark = {}

    # 终局快照中的显式载荷优先于图表函数自行读取配置文件；没有经过来源
    # 核验的行业值不会被当作基准，实际值仍然独立展示。
    if isinstance(snapshot_radar, dict):
        industry_benchmark = {
            str(item.get("metric_id")): item.get("benchmark")
            for item in snapshot_radar.get("metrics", [])
            if isinstance(item, dict) and isinstance(item.get("benchmark"), (int, float))
        }

    # 逐维判定数据完整度
    metrics = []
    for label, actual_key, benchmark_key in _RADAR_METRIC_SPECS:
        actual = indicators_data.get(actual_key)
        benchmark = industry_benchmark.get(benchmark_key)
        if benchmark is None:
            benchmark = industry_benchmark.get(actual_key)
        actual_ok = isinstance(actual, (int, float)) and not isinstance(actual, bool)
        bench_ok = (isinstance(benchmark, (int, float)) and not isinstance(benchmark, bool)
                    and benchmark > 0)
        if actual_ok and bench_ok:
            state = 'ok'
        elif actual_ok:
            state = 'no_benchmark'
        elif bench_ok:
            state = 'no_actual'
        else:
            state = 'missing'
        metrics.append({
            'label': label,
            'state': state,
            'actual': float(actual) if actual_ok else None,
            'benchmark': float(benchmark) if bench_ok else None,
        })

    complete = [m for m in metrics if m['state'] == 'ok']
    has_any_actual = any(m['actual'] is not None for m in metrics)
    labels = [m['label'] for m in metrics]
    n = len(labels)
    # 固定五维：极坐标角度均匀分布
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()

    # 标准化半径：公司值/基准×100，基准线恒为 100；缺失维度用 NaN 断开线段，
    # 不画 0 值伪造。fill 仅在全维齐备时使用，避免 NaN 多边形产生怪异形状。
    company_vals = [
        (m['actual'] / m['benchmark'] * 100) if m['state'] == 'ok' else float('nan')
        for m in metrics
    ]
    benchmark_vals = [100.0 if m['state'] == 'ok' else float('nan') for m in metrics]
    complete_ratios = [v for v in company_vals if not np.isnan(v)]
    r_max = max([100.0] + complete_ratios) if complete_ratios else 100.0
    r_top = r_max * 1.22 if complete_ratios else 120.0

    angles_c = angles + angles[:1]
    company_c = company_vals + company_vals[:1]
    benchmark_c = benchmark_vals + benchmark_vals[:1]

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.set_ylim(0, r_top)

    if complete:
        # 线段：NaN 处自动断开，只连相邻且完整的维度
        ax.plot(angles_c, company_c, 'o-', color='red', linewidth=2, markersize=6,
                label=f'{_company or "公司"}实际值')
        ax.plot(angles_c, benchmark_c, 'o-', color='blue', linewidth=2, markersize=6,
                label=f'{industry or "行业"}基准')
        if len(complete) == n:
            ax.fill(angles_c, company_c, alpha=0.25, color='red')
            ax.fill(angles_c, benchmark_c, alpha=0.25, color='blue')
        ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), prop=fp, fontsize=9)

    # 逐轴标注：完整维度标数值；缺失维度标「已获取值 + 缺失原因」
    reason_text = {
        'no_benchmark': '无行业基准',
        'no_actual': '缺公司值',
        'missing': '数据未获取',
    }
    miss_r = r_max * 1.06 if complete_ratios else 78.0
    for i, m in enumerate(metrics):
        ang = angles[i]
        if m['state'] == 'ok':
            cv = m['actual'] / m['benchmark'] * 100
            ax.annotate(f"{m['actual']:.1f}(基准{m['benchmark']:.1f})", xy=(ang, cv),
                        fontproperties=fp, fontsize=8, ha='center', va='bottom',
                        color='darkred', fontweight='bold')
        else:
            prefix = f"{m['actual']:.1f} " if m['actual'] is not None else ""
            ax.text(ang, miss_r, f"{prefix}{reason_text[m['state']]}",
                    ha='center', va='center', fontsize=8.5, color='#8A8F98',
                    fontproperties=fp)

    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontproperties=fp, fontsize=10)
    ax.set_ylabel('公司值/行业基准（基准=100）', fontproperties=fp, fontsize=9)

    # 标题：数据不全时显式标注，避免「图上缺轴」被误读为渲染失败
    title = f'{_company or "未提供"}{_year} 财务指标雷达图'
    if industry and industry_benchmark:
        title += f'\n({industry}行业基准对比，标准化后)'
    else:
        title += '\n（无可核验行业基准，仅展示公司实际值与缺失原因）'
    missing_labels = [m['label'] for m in metrics if m['state'] != 'ok']
    if missing_labels:
        title += '\n[数据不全] ' + '、'.join(missing_labels) + ' 缺可比对象，详见轴端标注'
    ax.set_title(title, fontproperties=fp, fontsize=12, pad=20)

    # 无任何可比数据时，图面自证「无数据」并写明原因
    if not complete:
        if has_any_actual:
            center = ('数据不全：已获取公司值但缺少可比行业基准，\n'
                      '图面仅记录已获取数值并标注缺失原因')
        else:
            center = '本次未获取财务指标数据\n（轴端已标注各维度缺失原因）'
        ax.text(0.0, r_top * 0.42, center,
                ha='center', va='center', fontproperties=fp, fontsize=11.5,
                color='#6B7280', linespacing=1.8)
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

def _render_trend_scarce(data, years_data, output_path, fp, unit_label, unit_note):
    """趋势数据不足（<2年）时仍出图：把已获取的数据记录在图上并标注数据不全。

    1 年数据用柱状展示已获取科目（营收/净利润/经营现金流）并显式说明不足以判断趋势；
    0 年数据出占位图自证「未获取多年财务数据」，不返回文本、不留空白。
    """
    import matplotlib.pyplot as plt  # type: ignore
    fig, ax = plt.subplots(figsize=(10, 4.6))
    company_name = data.get('company_name', '未提供')
    if years_data:
        years = [str(y.get('year', '')) for y in years_data]
        series = [
            ('营业收入', 'revenue', '#2F5496'),
            ('净利润', 'net_profit', '#388E3C'),
            ('经营现金流', 'operating_cashflow', '#C0392B'),
        ]
        width = 0.26
        plotted_any = False
        for i, (label, key, color) in enumerate(series):
            vals = []
            for y in years_data:
                raw = y.get(key)
                try:
                    vals.append(float(raw) if raw is not None and raw != '' else None)
                except (TypeError, ValueError):
                    vals.append(None)
            xs = [xi for xi, v in enumerate(vals) if v is not None]
            ys = [vals[xi] for xi in xs]
            if xs:
                plotted_any = True
                offsets = [xi + (i - 1) * width for xi in xs]
                ax.bar(offsets, ys, width=width, label=label, color=color, alpha=0.85)
                for xi, v in zip(offsets, ys):
                    ax.text(xi, v, f'{v:,.0f}', ha='center', va='bottom', fontsize=8)
        ax.set_xticks(range(len(years)))
        ax.set_xticklabels(years, fontproperties=fp, fontsize=11)
        ax.set_xlabel('年度', fontproperties=fp, fontsize=11)
        ax.set_ylabel(f'金额({unit_label})', fontproperties=fp, fontsize=11)
        if plotted_any:
            ax.legend(prop=fp, fontsize=9)
        ax.grid(axis='y', alpha=0.3)
        ax.axhline(y=0, color='gray', linestyle='--', linewidth=0.8)
        note = f'数据不全：当前仅 {len(years)} 年数据，不足 2 年无法判断趋势，已获取数据如上'
    else:
        ax.text(0.5, 0.5, '未获取多年财务数据\n（趋势图需至少 2 个年度）',
                ha='center', va='center', transform=ax.transAxes,
                fontproperties=fp, fontsize=13, color='#6B7280', linespacing=1.6)
        note = '数据不全：未获取多年财务数据'
    title = f'{company_name} 多年财务指标趋势\n{note}'
    if unit_note:
        title += f'\n{unit_note}'
    ax.set_title(title, fontproperties=fp, fontsize=12)
    fig.tight_layout()
    return _finalize(fig, output_path)


def _generate_trend_chart(trend_data_json: str, output_path: str) -> str:
    """生成多年财务指标趋势折线图（双面板），返回文件路径。

    左面板：关键科目绝对值趋势（营收、净利润、经营现金流、应收账款、存货）
    右面板：关键比率趋势（资产负债率、毛利率）
    使用输入中的明确金额单位；缺少单位时显示“原始单位”，不按量级猜测。

    Args:
        trend_data_json: 包含 company_name 和 years 列表的 JSON
        output_path: 输出图片路径

    Returns:
        生成的图片路径，或数据不足时的提示文本
    """
    import matplotlib.pyplot as plt  # type: ignore
    fp = _setup_chinese_font()
    data = json.loads(trend_data_json)
    metadata = data.get('_metadata') or data.get('metadata') or {}
    company_name = data.get('company_name', '未提供')
    years_data = data.get('years', [])

    # 金额单位必须来自输入元数据；缺失时保留原始数值并明确标注未知单位。
    unit_label = data.get('amount_unit') or (metadata.get('amount_unit') if isinstance(metadata, dict) else '') or '原始单位'
    unit_state = str(data.get('amount_unit_state') or '').strip()
    unit_note = str(data.get('amount_unit_note') or '').strip()
    if unit_state in {'conflicting', 'incomplete', 'missing'}:
        state_label = {'conflicting': '单位冲突', 'incomplete': '单位不完整', 'missing': '单位缺失'}[unit_state]
        unit_label = f'{unit_label}（{state_label}）'

    # 数据不全兜底：不足 2 年无法构成趋势，但已获取的数据仍记录在图上并标注。
    if len(years_data) < 2:
        return _render_trend_scarce(data, years_data, output_path, fp, unit_label, unit_note)

    # 提取各年度关键科目数据（None 表示未披露，不参与绘图与量级检测）
    years = [str(y.get('year', '')) for y in years_data]
    revenue = [y.get('revenue') for y in years_data]
    net_profit = [y.get('net_profit') for y in years_data]
    ocfs = [y.get('operating_cashflow') for y in years_data]
    ar = [y.get('accounts_receivable') for y in years_data]
    inventory = [y.get('inventory') for y in years_data]

    def _num(value):
        try:
            return float(value) if value is not None and value != '' else None
        except (TypeError, ValueError):
            return None

    revenue = [_num(v) for v in revenue]
    net_profit = [_num(v) for v in net_profit]
    ocfs = [_num(v) for v in ocfs]
    ar = [_num(v) for v in ar]
    inventory = [_num(v) for v in inventory]

    def _plot_series(ax, values, label, marker, color):
        """绘制单个科目折线：缺失年份（None）跳过，不画 0 值误导线。"""
        xs = [i for i, v in enumerate(values) if v is not None]
        ys = [values[i] for i in xs]
        if xs:
            ax.plot(xs, ys, marker + '-', label=label, color=color, linewidth=2, markersize=6)

    # 创建双面板图表
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    x = range(len(years))

    # 左面板：关键科目绝对值趋势
    _plot_series(ax1, revenue, '营业收入', 'o', 'steelblue')
    _plot_series(ax1, net_profit, '净利润', 's', 'green')
    _plot_series(ax1, ocfs, '经营现金流', 'd', 'red')
    _plot_series(ax1, ar, '应收账款', '^', 'orange')
    _plot_series(ax1, inventory, '存货', 'v', 'purple')
    ax1.axhline(y=0, color='gray', linestyle='--', linewidth=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(years)
    ax1.set_xlabel('年度', fontproperties=fp, fontsize=11)
    ax1.set_ylabel(f'金额({unit_label})', fontproperties=fp, fontsize=11)
    ax1.set_title('关键科目趋势', fontproperties=fp, fontsize=13)
    if any(values for values in (revenue, net_profit, ocfs, ar, inventory)):
        ax1.legend(prop=fp, fontsize=9, loc='upper left')
    ax1.grid(True, alpha=0.3)

    # 右面板：关键比率趋势。任何缺失或零分母均保持 None，不用默认值补齐。
    debt_ratios = []
    gross_margins = []
    for y in years_data:
        assets = _num(y.get('total_assets'))
        liabilities = _num(y.get('total_liabilities'))
        rev = _num(y.get('revenue'))
        cogs = _num(y.get('cost_of_goods'))
        # 总资产为零或负数时，资产负债率没有可解释的分母，必须保持缺失。
        debt_ratios.append(
            liabilities / assets * 100
            if assets is not None and assets > 0 and liabilities is not None
            else None
        )
        gross_margins.append((rev - cogs) / rev * 100 if rev and cogs is not None else None)
    has_ratios = any(v is not None for v in debt_ratios + gross_margins)
    if has_ratios:
        _plot_series(ax2, debt_ratios, '资产负债率', 'o', 'darkred')
        _plot_series(ax2, gross_margins, '毛利率', 's', 'darkgreen')
        ax2.set_xticks(x)
        ax2.set_xticklabels(years)
        ax2.set_xlabel('年度', fontproperties=fp, fontsize=11)
        ax2.set_ylabel('比率(%)', fontproperties=fp, fontsize=11)
        ax2.set_title('关键比率趋势', fontproperties=fp, fontsize=13)
        ax2.legend(prop=fp, fontsize=9, loc='upper left')
        ax2.grid(True, alpha=0.3)
    else:
        ax2.axis('off')  # 无数据时隐藏右面板

    title = f'{company_name} 多年财务指标趋势'
    if unit_note:
        title += f'\n{unit_note}'
    fig.suptitle(title, fontproperties=fp, fontsize=14, y=1.02)
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
        # 与 PDF/Excel/热力图/雷达图同规格：文件名末尾追加罗马数字序号，
        # 同一天多轮运行不互相覆盖。优先取数据内注入的 run_number，
        # 次选自动计算（系统兜底在线程池内执行，ContextVar 不跨线程，须显式透传）。
        run_number = data.get("run_number")
        if run_number is None:
            existing = count_existing_runs(company)
            run_number = existing + 1
        roman = to_roman(run_number)
        suffix = f"_{roman}" if run_number > 0 else ""
        filename = f"{date_str}_{company}_趋势图{suffix}.png"
        path = _generate_trend_chart(trend_data_json, os.path.join(tempfile.gettempdir(), filename))
        if path.startswith('需要') or path.startswith('无'):
            return path
        return _upload('趋势图', path, filename)
    except Exception as e:
        return f'趋势图生成失败: {str(e)}'
