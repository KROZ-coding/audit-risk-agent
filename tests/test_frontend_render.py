"""前端渲染稳定性回归测试（node 离线跑 index.html 内的真实渲染函数）。

背景（实测事故链）：
1. 历史存储 6000 字符无差别硬截断切在表格/echarts 块中间，切会话恢复视图时
   表格残缺不渲染、正文断在半句（症状：图表丢失/输出残缺/每次格式不同）；
2. 思维链卡片「风险判定」捕获贪婪吞噬后续内容（重复列表/JSON 折叠面板/审计建议）；
3. textCut 数字行边界过激，「风险判定」段内合法编号被拦腰切断。

本测试从 src/web/index.html 提取 renderMarkdown / renderMarkdownSafe /
_truncateForStorage 三个函数的真实源码，经 node 子进程跑固定夹具断言。
node 不可用时跳过（不阻塞无 node 的 CI 环境）。
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "..", "src", "web", "index.html")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过前端渲染离线测试")


def _extract_function(html: str, name: str) -> str:
    """按大括号配对提取顶层 function 源码（函数体内模板串/正则的花括号均成对）。"""
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
def harness(tmp_path_factory):
    """拼装 node 可执行脚本：浏览器全局 stub + 提取的三个函数 + 夹具驱动。"""
    html = open(INDEX_HTML, encoding="utf-8").read()
    limit_m = re.search(r"const STORAGE_TEXT_LIMIT = \d+;", html)
    assert limit_m, "index.html 未找到 STORAGE_TEXT_LIMIT 常量"
    extracted = limit_m.group(0) + "\n\n" + "\n\n".join(
        _extract_function(html, n)
        for n in ("renderMarkdown", "renderMarkdownSafe", "_truncateForStorage")
    )
    harness_js = """
// ── 浏览器全局 stub（离线环境无 DOM）──
let _pendingCharts = [];
function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function icon(n) { return '<i data-icon="' + n + '"></i>'; }
function renderReviewCard(t) { return '<div class="review-card-stub"></div>'; }

__EXTRACTED__

// ── 卡片范围提取（div 嵌套计数）──
function cardBlock(h) {
  const start = h.indexOf('<div class="reasoning-chain"');
  if (start === -1) return null;
  let i = start, depth = 0;
  while (i < h.length) {
    const open = h.indexOf('<div', i), close = h.indexOf('</div>', i);
    if (open !== -1 && open < close) { depth++; i = open + 4; }
    else if (close !== -1) { depth--; i = close + 6; if (depth === 0) return h.slice(start, i); }
    else break;
  }
  return null;
}

const out = {};

// ── F1：JSON 折叠面板与审计建议列表必须在卡片外 ──
{
  const jsonLines = Array.from({ length: 15 }, (_, i) => '  "k' + i + '": ' + i + ',').join('\\n');
  const t = '【数据发现】：数据A\\n【指标异常】：异常B\\n【法规依据】：法规C\\n【案例对照】：案例D\\n'
    + '【风险判定】：**重要**：判定结论，置信度 0.65\\n\\n#### 审计建议\\n\\n'
    + '1. 核实收入确认时点\\n2. 复核应收账款函证\\n\\n```json\\n{\\n' + jsonLines + '\\n"end": 1\\n}\\n```';
  const h = renderMarkdownSafe(t);
  const card = cardBlock(h);
  out.f1 = {
    cardFound: !!card,
    cardClean: card ? !card.includes('<details') && !card.includes('<pre')
      && !card.includes('核实收入确认时点') && !card.includes('审计建议') : false,
    jsonOutside: !!card && h.indexOf('<details') > h.indexOf(card) + card.length,
    adviceOutside: !!card && h.indexOf('核实收入确认时点') > h.indexOf(card) + card.length,
  };
}

// ── F2：「风险判定」段内合法编号（无前导空行/非连续编号行）不得被误切 ──
{
  const t = '【数据发现】：数据A\\n【指标异常】：异常B\\n【法规依据】：法规C\\n【案例对照】：案例D\\n'
    + '【风险判定】：**重大**：存在以下嫌疑 1. 提前确认收入 2. 放宽信用政策，建议重点关注\\n1. 单行编号补充说明（不应切断）';
  const h = renderMarkdownSafe(t);
  const card = cardBlock(h);
  out.f2 = {
    cardFound: !!card,
    verdictIntact: card ? card.includes('提前确认收入') && card.includes('放宽信用政策')
      && card.includes('单行编号补充说明') : false,
  };
}

// ── F3：空行分隔的编号列表是真实边界，必须切到卡片外 ──
{
  const t = '【数据发现】：数据A\\n【指标异常】：异常B\\n【法规依据】：法规C\\n【案例对照】：案例D\\n'
    + '【风险判定】：**重要**：判定E\\n\\n1. 审计建议一\\n2. 审计建议二';
  const h = renderMarkdownSafe(t);
  const card = cardBlock(h);
  out.f3 = {
    cardFound: !!card,
    adviceCutOut: card ? !card.includes('审计建议一') && h.includes('审计建议一') : false,
  };
}

// ── F4：表格与 echarts 容器完整保留（链外内容不受卡片化影响）──
{
  const t = '【数据发现】：数据A\\n【指标异常】：异常B\\n【法规依据】：法规C\\n【案例对照】：案例D\\n【风险判定】：判定E\\n\\n'
    + '| 年份 | 营收 |\\n|---|---|\\n| 2024 | 100 |\\n| 2025 | 120 |\\n\\n'
    + '```echarts\\n{"title":"营收趋势","type":"line","xAxis":["2024","2025"],"series":[{"name":"营收","data":[100,120]}]}\\n```';
  const h = renderMarkdownSafe(t);
  out.f4 = {
    tableRendered: h.includes('<table>') && h.includes('<th>年份</th>') && h.includes('<td>120</td>'),
    chartContainer: h.includes('echart-block') && h.includes('echart-canvas') && _pendingCharts.length >= 1,
  };
}

// ── F5：畸形输入不抛异常（未闭合代码块/半截表格/孤立标记）──
{
  const samples = [
    '```json\\n{"never": "closed"',
    '| 只有表头 | 没有分隔行 |',
    '【数据发现】：孤立的开始标记，后面什么都没有',
    '【风险判定】：只有最后一步标记',
    '',
  ];
  out.f5 = { allSafe: samples.every(s => {
    try { return typeof renderMarkdownSafe(s) === 'string'; } catch (e) { return false; }
  }) };
}

// ── F6：_truncateForStorage 行为 ──
{
  const note = '（正文已截断，完整内容见 PDF 报告）';
  // 6a：JSON 台账块整段剥离（剥离后不超阈值则无需截断）
  const body = '## 结论\\n\\n这是正文内容。'.padEnd(3000, '析');
  const jsonBlock = '```json\\n' + '{"risk_details": "xxxxx"}'.padEnd(30000, 'y') + '\\n```';
  const r1 = _truncateForStorage(body + '\\n\\n' + jsonBlock);
  // 6b：无 JSON 但超阈值 → 截断点必须落在段落边界（双换行）
  const paras = Array.from({ length: 300 }, (_, i) => '第' + i + '段内容'.padEnd(100, '字')).join('\\n\\n');
  const r2 = _truncateForStorage(paras);
  // lastIndexOf 返回段间 '\\n\\n' 的起始位置：保留段 = paras.slice(0, cut)，切点处必为 '\\n\\n'
  const cut2 = r2.length - ('\\n\\n' + note).length;
  // 6c：不超阈值原样返回
  const r3 = _truncateForStorage('短文本');
  out.f6 = {
    jsonStripped: !r1.includes('risk_details') && r1.includes('这是正文内容') && !r1.includes(note),
    paraBoundary: r2.endsWith(note) && paras.slice(cut2, cut2 + 2) === '\\n\\n' && r2.length <= STORAGE_TEXT_LIMIT + 2 + note.length,
    shortUntouched: r3 === '短文本',
  };
}

console.log(JSON.stringify(out));
""".replace("__EXTRACTED__", extracted)
    d = tmp_path_factory.mktemp("frontend_render")
    p = d / "harness.js"
    p.write_text(harness_js, encoding="utf-8")
    return str(p)


def test_frontend_render_invariants(harness):
    """渲染不变量：卡片不吞块级内容、判定段内编号不误切、表格/图表保留、畸形输入不炸。"""
    r = subprocess.run(["node", harness], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"harness 执行失败:\n{r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert out["f1"]["cardFound"], "思维链卡片未生成"
    assert out["f1"]["cardClean"], "卡片内混入 JSON 面板/审计建议（吞噬回归）"
    assert out["f1"]["jsonOutside"], "JSON 折叠面板不在卡片之后"
    assert out["f1"]["adviceOutside"], "审计建议不在卡片之后"

    assert out["f2"]["cardFound"], "思维链卡片未生成（F2）"
    assert out["f2"]["verdictIntact"], "「风险判定」段内合法编号被 textCut 误切（收敛回归）"

    assert out["f3"]["cardFound"], "思维链卡片未生成（F3）"
    assert out["f3"]["adviceCutOut"], "空行分隔的编号列表未被切到卡片外"

    assert out["f4"]["tableRendered"], "Markdown 表格未渲染（表格丢失回归）"
    assert out["f4"]["chartContainer"], "echarts 容器未生成（图表丢失回归）"

    assert out["f5"]["allSafe"], "畸形输入触发渲染异常（全局兜底回归）"

    assert out["f6"]["jsonStripped"], "存储截断未剥离 JSON 台账块"
    assert out["f6"]["paraBoundary"], "存储截断未落在段落边界（断表风险回归）"
    assert out["f6"]["shortUntouched"], "短文本被误截断"
