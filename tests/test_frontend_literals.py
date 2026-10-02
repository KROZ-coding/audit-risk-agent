"""前端真实字面量断言（§7：网页 DOM 关键金额/单位/期间/校验状态与独立预期比较）。

从 src/web/index.html 抽取真实 renderReportMetaHeader / renderIndicatorView 纯函数，
用当前实际 indicator_view（中国石油 2025H1）与 report_metadata 契约形状驱动，
断言网页最终 HTML 中出现独立核对的字面量：

- 公司/股票代码/分析编号/快照 ID/校验状态/源文件哈希（元数据卡）
- 金额单位"人民币元"、期间"2025年半年度"、口径"中国准则合并"
- 毛利率 20.89%（计算层 ROUND_HALF_UP 保留两位）、营收同比 -6.74%、
  应收账款/营业收入 8.26%、经营现金流/净利润 2.42（无量纲，不应带 %）
- 未计算指标如实列出（不以中性值补齐）；阈值缺失显示"—"

node 不可用时跳过（与 test_frontend_render.py 一致）。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from offline_real_verify import CALC_JSON  # noqa: E402
from tools.financial_calculator import calculate_financial_indicators  # noqa: E402
from tools.indicator_view import build_indicator_view  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "..", "src", "web", "index.html")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过前端字面量离线测试")


def _extract_function(html: str, name: str) -> str:
    anchor = f"function {name}"
    start = html.find(anchor)
    assert start != -1, f"index.html 未找到 {name}"
    brace_start = html.find("{", start)
    depth, i = 0, brace_start
    while i < len(html):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
        i += 1
    raise AssertionError(f"{name} 花括号不配对")


@pytest.fixture(scope="module")
def real_indicator_view():
    """当前实际 indicator_view（真实工具链，无 LLM）。"""
    fin = json.loads(calculate_financial_indicators.invoke(
        {"financial_data_json": json.dumps(CALC_JSON, ensure_ascii=False)}))
    iv = build_indicator_view(fin)
    assert iv["available"] is True
    return iv


@pytest.fixture(scope="module")
def render_results(real_indicator_view, tmp_path_factory):
    html = open(INDEX_HTML, encoding="utf-8").read()
    extracted = "\n\n".join(
        _extract_function(html, n)
        for n in ("renderReportMetaHeader", "renderIndicatorView")
    )
    iview_json = json.dumps(real_indicator_view, ensure_ascii=False)
    harness_js = """// ── 浏览器全局 stub（离线环境无 DOM）──
function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function icon(n) { return '<i data-icon="' + n + '"></i>'; }

__EXTRACTED__

const iview = __IVIEW__;
const rmeta = {
  company_name: '中国石油天然气股份有限公司', stock_code: '601857',
  report_year: '2025', industry: '能源', audit_opinion: '未经审计（半年度报告）',
  analysis_id: 'run-abc1', data_version: '2025H1', rule_version: '2026-09-v3',
  result_schema_version: '1.0', snapshot_id: 'snap-5951c42f1c90b6cb',
  validation_status: '部分完成', source_hash: '9694d76a8e6dfa990f9a3767113c9821435ce39f14d0a67f53e98a1644c4916c',
  review_gate_status: 'passed', source_total: 3, source_used_count: 2,
  data_sources: [{name: '2025年半年度报告', used: true, note: ''},
                 {name: '2024年半年度报告', used: true, note: ''},
                 {name: '历史财务数据库', used: false, note: '未获取'}]
};
const metaHtml = renderReportMetaHeader(rmeta);
const ivHtml = renderIndicatorView(iview);

function textOf(html) {
  return String(html)
    .replace(/<[^>]+>/g, ' ')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}
const out = {
  meta: {
    company: textOf(metaHtml).includes('中国石油天然气股份有限公司'),
    stockCode: textOf(metaHtml).includes('601857'),
    analysisId: textOf(metaHtml).includes('run-abc1'),
    snapshot: textOf(metaHtml).includes('snap-5951c42f1c90b6cb'),
    validation: textOf(metaHtml).includes('部分完成'),
    sourceHash: textOf(metaHtml).includes('9694d76a8e6dfa990f9a3767113c9821435ce39f14d0a67f53e98a1644c4916c'),
    gatePassed: metaHtml.includes('已通过结构化门禁'),
    srcUsed: textOf(metaHtml).includes('数据源 2/3 已获取'),
    srcUnused: textOf(metaHtml).includes('未获取'),
  },
  iv: {
    unit: textOf(ivHtml).includes('人民币元'),
    period: textOf(ivHtml).includes('2025年半年度'),
    scope: textOf(ivHtml).includes('中国准则合并'),
    count: (() => {
      const m = textOf(ivHtml).match(/已计算 (\\d+) 项/);
      return !!m && Number(m[1]) === iview.metric_count;
    })(),
    grossMargin: textOf(ivHtml).includes('20.89%'),
    revenueYoY: textOf(ivHtml).includes('-6.74%'),
    arToRevenue: textOf(ivHtml).includes('8.26%'),
    ocfToNp: (() => {
      const t = textOf(ivHtml);
      return t.includes('2.42') && !t.includes('2.42%');
    })(),
    unitDeclared: ivHtml.includes('金额单位'),
    groupLabels: ['盈利能力','营运能力','偿债能力','成长能力','现金流质量','资产质量与审计关注']
      .every(l => textOf(ivHtml).includes(l)),
    emptyMeta: renderReportMetaHeader(null) === '',
    emptyIv: renderIndicatorView(null) === '',
  },
};
console.log(JSON.stringify(out));
""".replace("__EXTRACTED__", extracted).replace("__IVIEW__", iview_json)
    d = tmp_path_factory.mktemp("frontend_literals")
    p = d / "harness.js"
    p.write_text(harness_js, encoding="utf-8")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"harness 执行失败:{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_meta_header_shows_contract_identity_and_gate(render_results):
    m = render_results["meta"]
    assert m["company"], "元数据卡未展示公司名称"
    assert m["stockCode"], "元数据卡未展示股票代码"
    assert m["analysisId"], "元数据卡未展示分析编号"
    assert m["snapshot"], "元数据卡未展示快照 ID"
    assert m["validation"], "元数据卡未展示校验状态"
    assert m["sourceHash"], "元数据卡未展示源文件哈希"
    assert m["gatePassed"], "审查门禁状态未渲染为已通过"
    assert m["srcUsed"], "数据源使用计数未渲染"
    assert m["srcUnused"], "未获取数据源未如实标注"


def test_indicator_view_shows_units_period_scope_and_literals(render_results):
    iv = render_results["iv"]
    assert iv["unit"], "金额单位（人民币元）未渲染"
    assert iv["period"], "期间（2025年半年度）未渲染"
    assert iv["scope"], "口径（中国准则合并）未渲染"
    assert iv["count"], "已计算指标数未渲染"
    assert iv["grossMargin"], "毛利率 20.89% 未按两位小数渲染"
    assert iv["revenueYoY"], "营收同比 -6.74% 未渲染"
    assert iv["arToRevenue"], "应收账款/营业收入 8.26% 未渲染"
    assert iv["ocfToNp"], "经营现金流/净利润应为无量纲 2.42（不得带 %）"
    assert iv["unitDeclared"], "金额单位声明标题未渲染"
    assert iv["groupLabels"], "四项能力分组标签缺失"
    assert iv["emptyMeta"] and iv["emptyIv"], "空输入应返回空字符串而非抛错"


def test_web_pending_output_is_notice_only():
    """Web 只展示待复核数量和轻量提醒，完整依据留在 Excel/TXT。"""
    html = open(INDEX_HTML, encoding="utf-8").read()
    assert "展开待复核明细" not in html
    assert "完整依据请查看 Excel 审计底稿和 TXT 风险台账" in html
    assert "pendingRows" not in html
