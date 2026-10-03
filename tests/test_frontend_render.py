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
    """按 JavaScript 词法状态提取函数，避免模板串/正则中的花括号干扰。"""
    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", html)
    assert match, f"index.html 未找到 {name}"
    start = match.start()
    brace_start = html.find("{", start)
    assert brace_start != -1, f"{name} 缺少函数体"
    depth, i = 1, brace_start + 1
    state = "code"
    escaped = False
    regex_class = False
    template_returns = []
    template_expr_depths = []

    def can_start_regex(pos: int) -> bool:
        j = pos - 1
        while j >= 0 and html[j].isspace():
            j -= 1
        if j < 0 or html[j] in "=([{!,:;?&|+-*%^~<>":
            return True
        end = j + 1
        while j >= 0 and (html[j].isalnum() or html[j] in "_$"):
            j -= 1
        return html[j + 1:end] in {"return", "throw", "case", "delete", "void", "typeof", "instanceof", "in", "of"}

    while i < len(html):
        c = html[i]
        if state == "line_comment":
            if c in "\r\n":
                state = "code"
            i += 1
            continue
        if state == "block_comment":
            if html.startswith("*/", i):
                state, i = "code", i + 2
            else:
                i += 1
            continue
        if state in {"single", "double"}:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif (state == "single" and c == "'") or (state == "double" and c == '"'):
                state = "code"
            i += 1
            continue
        if state == "regex":
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == "[":
                regex_class = True
            elif c == "]":
                regex_class = False
            elif c == "/" and not regex_class:
                state = "code"
            i += 1
            continue
        if state == "template":
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == "`":
                state = template_returns.pop() if template_returns else "code"
            elif html.startswith("${", i):
                template_expr_depths.append(0)
                state = "code"
                i += 2
                continue
            i += 1
            continue

        # code state, including JavaScript expressions inside ${...}
        if c == "/" and i + 1 < len(html) and html[i + 1] == "/":
            state, i = "line_comment", i + 2
            continue
        if c == "/" and i + 1 < len(html) and html[i + 1] == "*":
            state, i = "block_comment", i + 2
            continue
        if c == "'":
            state, escaped, i = "single", False, i + 1
            continue
        if c == '"':
            state, escaped, i = "double", False, i + 1
            continue
        if c == "`":
            template_returns.append("code")
            state, escaped, i = "template", False, i + 1
            continue
        if c == "/" and can_start_regex(i):
            state, regex_class, escaped, i = "regex", False, False, i + 1
            continue
        if c == "{":
            depth += 1
            if template_expr_depths:
                template_expr_depths[-1] += 1
        elif c == "}":
            if template_expr_depths and template_expr_depths[-1] == 0:
                template_expr_depths.pop()
                state = "template"
                i += 1
                continue
            depth -= 1
            if template_expr_depths:
                template_expr_depths[-1] -= 1
            if depth == 0:
                return html[start:i + 1]
        i += 1
    raise AssertionError(f"{name} 花括号不配对")


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """拼装 node 可执行脚本：浏览器全局 stub + 提取的三个函数 + 夹具驱动。"""
    html = open(INDEX_HTML, encoding="utf-8").read()
    declarations = re.findall(r"(?m)^\s*const STORAGE_TEXT_LIMIT = \d+;\s*$", html)
    assert len(declarations) == 1, "index.html 中 STORAGE_TEXT_LIMIT 必须只声明一次"
    limit_m = re.search(r"const STORAGE_TEXT_LIMIT = \d+;", html)
    assert limit_m, "index.html 未找到 STORAGE_TEXT_LIMIT 常量"
    storage_decl = limit_m.group(0)
    extracted_functions = [
        _extract_function(html, n)
        for n in ("_chartPoints", "_chartFallbackTable",
                   "_structuredLedgerSpan", "_normalizeStructuredLedgerBlocks",
                   "_removeStructuredLedgerBlocks",
                   "renderMarkdown", "renderMarkdownSafe", "_truncateForStorage")
    ]
    # 常量提取和函数提取分开：harness 只注入一份全局限制值。
    extracted_functions = [
        re.sub(r"(?:^|\n)\s*const STORAGE_TEXT_LIMIT = \d+;\s*", "\n", source)
        for source in extracted_functions
    ]
    extracted = storage_decl + "\n\n" + "\n\n".join(extracted_functions)
    assert len(re.findall(r"\bconst STORAGE_TEXT_LIMIT\b", extracted)) == 1
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
  const note = '（正文已截断，完整内容见 PDF/JSON 产物）';
  // 6a：JSON 台账块整段剥离（剥离后不超阈值则无需截断）
  const body = '## 结论\\n\\n这是正文内容。'.padEnd(3000, '析');
  const jsonBlock = '```json\\n' + JSON.stringify({company_info: {}, risk_details: 'x'.repeat(30000)}) + '\\n```';
  const r1 = _truncateForStorage(body + '\\n\\n' + jsonBlock);
  // 6b：无 JSON 但超阈值 → 截断点必须落在段落边界（双换行）
  const paras = Array.from({ length: 300 }, (_, i) => '第' + i + '段内容'.padEnd(100, '字')).join('\\n\\n');
  const r2 = _truncateForStorage(paras);
  // lastIndexOf 返回段间 '\\n\\n' 的起始位置：保留段 = paras.slice(0, cut)，切点处必为 '\\n\\n'
  const cut2 = r2.length - ('\\n\\n' + note).length;
  // 6c：不超阈值原样返回
  const r3 = _truncateForStorage('短文本');
  // 6d：发布层回写的裸 JSON 台账也不能进入历史正文
  const bareLedger = JSON.stringify({company_info: {}, risk_details: []});
  const r4 = _truncateForStorage('正文' + bareLedger);
  out.f6 = {
    jsonStripped: !r1.includes('risk_details') && r1.includes('这是正文内容') && !r1.includes(note),
    paraBoundary: r2.endsWith(note) && paras.slice(cut2, cut2 + 2) === '\\n\\n' && r2.length <= STORAGE_TEXT_LIMIT + 2 + note.length,
    shortUntouched: r3 === '短文本',
    bareJsonStripped: !r4.includes('company_info') && !r4.includes('risk_details') && r4.includes('正文'),
  };
}

__REGRESSION_FIXTURES__

console.log(JSON.stringify(out));
""".replace("__EXTRACTED__", extracted).replace("__REGRESSION_FIXTURES__", r"""
function decodeHtml(text) {
  return text.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
function codeBodies(html) {
  return Array.from(html.matchAll(/<code\b[^>]*>([\s\S]*?)<\/code>/g), m => m[1]);
}
function rowCells(html, tag) {
  const cell = new RegExp('<' + tag + '\\b[^>]*>([\\s\\S]*?)<\\/' + tag + '>', 'g');
  return Array.from(html.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/g), row =>
    Array.from(row[1].matchAll(cell), m => decodeHtml(m[1].replace(/<[^>]+>/g, '')).trim()));
}
const inlineMarkup = /<(?:span|a|br|p|strong|em|img|script|code)\b/i;

// F7: A compact ledger must remain copyable JSON and start collapsed.
{
  const ledger = {risk_details: [{
    finding: '重要事项，无重大诉讼，低风险',
    url: 'https://example.test/report?a=1&b=2',
    file: '/local_storage/reports/report.pdf',
    literal: '`重大` **重要** *一般* [报告](https://example.test/report)',
    notes: '第一行\n\n第二行',
    html: '<img src=x onerror="alert(1)"> & <script>alert(2)</script>',
  }]};
  const compact = JSON.stringify(ledger);
  const h = renderMarkdownSafe('```json\n' + compact + '\n```');
  const code = codeBodies(h)[0] || '';
  let parsed = null;
  try { parsed = JSON.parse(decodeHtml(code)); } catch (_) {}
  const small = renderMarkdownSafe('```json\n{"risk_details":[]}\n```');
  out.f7 = {
    collapsed: /<details\b[^>]*>[\s\S]*<pre\b/.test(h) && !/<details\b[^>]*\bopen\b/.test(h),
    smallLedgerCollapsed: /<details\b/.test(small),
    formatted: decodeHtml(code) === JSON.stringify(ledger, null, 2),
    dataIntact: JSON.stringify(parsed) === compact,
    noInjectedMarkup: !inlineMarkup.test(code),
    htmlEscaped: h.includes('&lt;img') && !/<(?:img|script)\b/.test(h),
  };
}

// F8: Both forms of code preserve literal Markdown, URLs and blank lines.
{
  const raw = '重要 **重大** `一般`\n\nhttps://example.test/report?a=1&b=2\n'
    + '/local_storage/reports/report.pdf\n<img src=x onerror="alert(1)">';
  const inline = '无重大诉讼 **重要** https://example.test/a?x=1&y=2 <b>text</b>';
  const h = renderMarkdownSafe('```text\n' + raw + '\n```\n\n`' + inline + '`');
  const bodies = codeBodies(h);
  const longLine = renderMarkdownSafe('```text\n' + 'value='.padEnd(1600, 'x') + '\n```');
  const manyLines = renderMarkdownSafe('```text\n' + Array.from({length: 13}, (_, i) => 'line ' + i).join('\n') + '\n```');
  out.f8 = {
    blockIntact: decodeHtml(bodies[0] || '') === raw,
    inlineIntact: decodeHtml(bodies[1] || '') === inline,
    codeClean: bodies.length === 2 && bodies.every(code => !inlineMarkup.test(code)),
    longLineCollapsed: /<details\b/.test(longLine),
    manyLinesCollapsed: /<details\b/.test(manyLines),
  };
}

// F9: Only explicit grades receive badges; descriptions keep their meaning.
{
  const prose = renderMarkdownSafe('无重大诉讼；不存在重大风险；重要事项已披露；一般经营情况正常。');
  const table = renderMarkdownSafe('| 项目 | 风险等级 | 说明 |\n|---|---|---|\n'
    + '| 事项A | 重要 | 无重大诉讼，重要事项已披露 |\n'
    + '| 事项B | 一般 | 重大事项核查完成 |');
  const verdict = renderMarkdownSafe('【风险判定】：**重大**：需要复核。');
  const cells = Array.from(table.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g), m => m[1]);
  out.f9 = {
    proseUncolored: !/class="risk-/.test(prose),
    gradesColored: /class="risk-important"/.test(cells[1] || '')
      && /class="risk-general"/.test(cells[4] || ''),
    descriptionsUncolored: !/class="risk-/.test((cells[2] || '') + (cells[5] || '')),
    verdictColored: /class="risk-major"/.test(verdict),
  };
}

// F10: Empty cells occupy their original columns, including edge cells.
{
  const h = renderMarkdownSafe('| A | | C |\n|---|---|---|\n| left | | right |\n| | middle | |');
  out.f10 = {
    headings: rowCells(h, 'th').filter(row => row.length),
    rows: rowCells(h, 'td').filter(row => row.length),
  };
}

// F11: Grouping preserves every finding, grade and indicator under its module.
{
  const modules = ['财务健康诊断', '合规与经营风险扫描'];
  const rows = [
    [modules[0], '应收账款与营收背离', '重要', '应收+62.67%，营收-6.25%'],
    [modules[0], '利润同步下滑', '重要', '净利润-5.40%'],
    [modules[0], '资本结构稳健', '一般', '资产负债率40.00%'],
    [modules[1], '关联交易规模较大', '重要', '关联交易251435百万元'],
    [modules[1], '担保披露不充分', '一般', '担保余额151161百万元'],
    [modules[1], '无处罚、无问询、无重大诉讼', '一般', '处罚案例相似度较低'],
  ];
  const t = '## 双模块结论汇总\n\n| 模块 | 核心发现 | 风险等级 | 关键指标 |\n|---|---|---|---|\n'
    + rows.map(row => '| ' + row.join(' | ') + ' |').join('\n');
  const h = renderMarkdownSafe(t);
  const groups = Array.from(h.matchAll(/<section\b[^>]*class="[^"]*\bmodule-summary-group\b[^"]*"[^>]*>([\s\S]*?)<\/section>/g), m => m[1]);
  out.f11 = {
    groupCount: groups.length,
    tableCount: (h.match(/<table\b[^>]*class="[^"]*\bmodule-summary-table\b/g) || []).length,
    headings: groups.map(group => (group.match(/<h3\b[^>]*>([\s\S]*?)<\/h3>/) || [])[1]),
    rows: groups.map(group => rowCells(group, 'td').filter(row => row.length)),
    expectedRows: modules.map(module => rows.filter(row => row[0] === module).map(row => row.slice(1))),
    columns: groups.map(group => rowCells(group, 'th').filter(row => row.length)),
    moduleNamesNotRepeated: modules.every(module => h.split(module).length - 1 === 1),
    negativeFindingUncolored: h.includes('无处罚、无问询、无重大诉讼'),
  };
}

// F12: Invalid chart specs fall back to escaped, literal code.
{
  const samples = [
    '<img src=x onerror="alert(1)">\n重要 https://example.test/x',
    JSON.stringify({title: '<script>alert(1)</script>', note: '`重大` **重要**'}),
  ];
  const initialCharts = _pendingCharts.length;
  out.f12 = {safeFallbacks: samples.every(raw => {
    const h = renderMarkdownSafe('```echarts\n' + raw + '\n```');
    const code = codeBodies(h)[0] || '';
    return decodeHtml(code) === raw && !inlineMarkup.test(code)
      && !/<(?:img|script)\b/.test(h) && !h.includes('echart-canvas');
  }), noChartsQueued: _pendingCharts.length === initialCharts};
}

// F13: Fourth-level headings and ordinary inline formatting still render.
{
  const h = renderMarkdownSafe('#### 审计建议\n\n**重点**与*说明*');
  out.f13 = {
    headingRendered: h.includes('<h4>审计建议</h4>') && !h.includes('####'),
    headingOutsideParagraph: !/<p>\s*<h4\b/.test(h),
    emphasisRendered: h.includes('<strong>重点</strong>') && h.includes('<em>说明</em>'),
  };
}

// F14: 图表数据两种形态都要能渲染（实测事故：交叉验证状态分布饼图全空）。
{
  const spec = {title: '交叉验证状态分布', type: 'pie', xAxis: [],
    series: [{name: '验证状态', data: [{name: '相互印证', value: 2}, {name: '单方发现', value: 3}, {name: '存在矛盾', value: 0}]}]};
  const numeric = {title: '占比', type: 'pie', xAxis: ['A', 'B'], series: [{name: '占比', data: [2, 3]}]};
  const bar = {title: '三维度', type: 'bar', xAxis: ['财务'], series: [{name: '风险分', data: [8]}]};
  const fb = _chartFallbackTable(spec);
  const fmt = pts => pts.map(p => p.name + '=' + p.value);
  out.f14 = {
    objectPoints: fmt(_chartPoints(spec.series[0].data, spec.xAxis)),
    numericPoints: fmt(_chartPoints(numeric.series[0].data, numeric.xAxis)),
    fallbackHasNames: fb.includes('相互印证') && fb.includes('单方发现'),
    fallbackHasValues: fb.includes('>2<') && fb.includes('>3<'),
    noGarbage: !fb.includes('[object Object]'),
    barFallbackUntouched: _chartFallbackTable(bar).includes('>8<'),
  };
}

// F15: 「风险等级：**重要**（暂定关注）」必须渲染成徽章 + 后缀、无游离 </strong>。
{
  const bullet = renderMarkdownSafe(
    '- **应收与营收背离**：风险等级：**重要**（暂定关注），状态：待核查，置信度：0.70。依据：应收同比上升');
  const table = renderMarkdownSafe('| 语义编号 | 关注等级 |\n|---|---|\n| R001 | 重要（暂定关注） |');
  const accepted = renderMarkdownSafe('- **资本结构稳健**：风险等级：**一般**，状态：已采信风险。');
  const count = (h, re) => (h.match(re) || []).length;
  out.f15 = {
    badgeRendered: /<span class="risk-important">重要<\/span>/.test(bullet),
    suffixKeptOutsideBold: bullet.includes('</span>（暂定关注）'),
    noOrphanClose: count(bullet, /<strong>/g) === count(bullet, /<\/strong>/g)
      && !/（暂定关注）<\/strong>/.test(bullet),
    titleStillBold: /<strong>应收与营收背离<\/strong>/.test(bullet),
    tableLevelBadged: /<span class="risk-important">重要<\/span>/.test(table)
      && table.includes('（暂定关注）'),
    acceptedPlainLevel: /<span class="risk-general">一般<\/span>/.test(accepted)
      && !/risk-provisional/.test(accepted),
  };
}

// F16: 发布层回写的裸 JSON 台账必须自动进入默认收起的次级文本框。
{
  const ledger = {
    company_info: {company_name: '快照公司', stock_code: '000001'},
    risk_details: [{risk_id: 'R001', title: '应收账款需核查', note: '`重要` **原样保留**'}],
    review_gate: {status: 'not_passed'},
  };
  const raw = '报告结论\n\n' + JSON.stringify(ledger, null, 2) + '\n\n后续说明';
  const h = renderMarkdownSafe(raw);
  const code = codeBodies(h)[0] || '';
  out.f16 = {
    folded: /<details class="code-fold">[\s\S]*<pre[\s\S]*company_info/.test(h)
      && !/<details\b[^>]*\bopen\b/.test(h),
    label: h.includes('结构化风险台账 JSON'),
    prosePreserved: h.includes('报告结论') && h.includes('后续说明'),
    rawNotProse: h.indexOf('company_info') > h.indexOf('<details')
      && h.indexOf('company_info') < h.indexOf('</details>'),
    dataIntact: JSON.stringify(JSON.parse(decodeHtml(code))) === JSON.stringify(ledger),
  };
}

// F17: 结构化台账解析失败时仍需折叠展示，不能把原始 JSON 当 Markdown 处理。
{
  const malformed = '前置说明\n\n{"company_info":{"company_name":"坏数据"},'
    + '"risk_details":[{"title":"缺逗号"}],}\n\n后置说明';
  const h = renderMarkdownSafe(malformed);
  out.f17 = {
    folded: /<details class="code-fold">[\s\S]*<pre[\s\S]*<\/details>/.test(h),
    escaped: h.includes('"company_info"') && !/<(?:img|script|a|strong|em)\b/.test(h),
    prosePreserved: h.includes('前置说明') && h.includes('后置说明'),
  };
}
""")
    d = tmp_path_factory.mktemp("frontend_render")
    p = d / "harness.js"
    p.write_text(harness_js, encoding="utf-8")
    syntax = subprocess.run(["node", "--check", str(p)], capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert syntax.returncode == 0, f"harness 语法检查失败:\n{syntax.stderr}"
    return str(p)


@pytest.fixture(scope="module")
def render_results(harness):
    r = subprocess.run(["node", harness], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"harness 执行失败:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_frontend_render_invariants(render_results):
    """渲染不变量：卡片不吞块级内容、判定段内编号不误切、表格/图表保留、畸形输入不炸。"""
    out = render_results

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
    assert out["f6"]["bareJsonStripped"], "存储截断未剥离裸 JSON 台账块"
    assert out["f6"]["paraBoundary"], "存储截断未落在段落边界（断表风险回归）"
    assert out["f6"]["shortUntouched"], "短文本被误截断"


def test_json_ledger_is_collapsed_and_lossless(render_results):
    result = render_results["f7"]
    assert result["collapsed"], "紧凑 JSON 台账应默认折叠"
    assert result["smallLedgerCollapsed"], "台账折叠不应依赖原始行数"
    assert result["formatted"], "合法 JSON 应按两空格缩进，便于查看和复制"
    assert result["dataIntact"], "JSON 内容被 Markdown 或风险徽章污染，复制后不可解析"
    assert result["noInjectedMarkup"], "JSON 代码中混入了 HTML 渲染标签"
    assert result["htmlEscaped"], "JSON 中的 HTML 内容未安全转义"


def test_bare_json_ledger_is_collapsed_and_lossless(render_results):
    result = render_results["f16"]
    assert result["folded"], "裸 JSON 台账没有进入默认折叠的次级文本框"
    assert result["label"], "裸 JSON 台账缺少结构化台账标题"
    assert result["prosePreserved"], "裸 JSON 台账前后的正文被吞掉"
    assert result["rawNotProse"], "裸 JSON 台账仍作为普通正文输出"
    assert result["dataIntact"], "裸 JSON 台账展开后内容不可解析或被改写"


def test_malformed_bare_json_ledger_is_safe_and_collapsed(render_results):
    result = render_results["f17"]
    assert result["folded"], "解析失败的裸 JSON 台账没有进入折叠容器"
    assert result["escaped"], "解析失败的台账没有安全转义"
    assert result["prosePreserved"], "解析失败的台账吞掉了前后正文"


def test_code_preserves_literals_and_folds_large_blocks(render_results):
    result = render_results["f8"]
    assert result["blockIntact"], "代码块中的空行、反引号、URL 或 Markdown 被修改"
    assert result["inlineIntact"], "行内代码中的字面内容被重新渲染"
    assert result["codeClean"], "代码中混入了风险徽章、链接或段落标签"
    assert result["longLineCollapsed"], "单行超长代码没有折叠"
    assert result["manyLinesCollapsed"], "多行代码没有折叠"


def test_risk_badges_only_mark_explicit_grades(render_results):
    result = render_results["f9"]
    assert result["proseUncolored"], "否定语句或普通事项被误标为风险"
    assert result["gradesColored"], "风险等级列中的明确等级没有着色"
    assert result["descriptionsUncolored"], "表格说明中的普通词语被误标为风险"
    assert result["verdictColored"], "明确风险判定中的等级未生成徽章"


def test_markdown_tables_preserve_empty_cells(render_results):
    result = render_results["f10"]
    assert result["headings"] == [["A", "", "C"]], "空表头导致列位置变化"
    assert result["rows"] == [["left", "", "right"], ["", "middle", ""]], "空单元格导致数据错列"


def test_module_summary_groups_preserve_findings(render_results):
    result = render_results["f11"]
    assert result["groupCount"] == 2, "双模块汇总未分为两个独立分组"
    assert result["tableCount"] == 2, "每个模块应有一张三列表格"
    assert result["headings"] == ["财务健康诊断", "合规与经营风险扫描"]
    assert result["columns"] == [[["核心发现", "风险等级", "关键指标"]]] * 2
    assert result["rows"] == result["expectedRows"], "分组后发现、等级或指标丢失或归属错误"
    assert result["moduleNamesNotRepeated"], "每行重复模块名，汇总仍然难以扫描"
    assert result["negativeFindingUncolored"], "无重大诉讼被误拆成红色风险徽章"


def test_invalid_echarts_fallback_is_escaped(render_results):
    result = render_results["f12"]
    assert result["safeFallbacks"], "无效 echarts 降级时注入 HTML 或修改了原始代码"
    assert result["noChartsQueued"], "无效 echarts 不应进入图表队列"


def test_fourth_level_headings_render_as_blocks(render_results):
    result = render_results["f13"]
    assert result["headingRendered"], "四级标题仍显示 Markdown 标记"
    assert result["headingOutsideParagraph"], "四级标题被错误嵌入段落"
    assert result["emphasisRendered"], "保护代码时影响了正文的粗体和斜体"


def test_pie_chart_accepts_object_and_numeric_data(render_results):
    """饼图数据形态兼容：{name,value}对象数组与数值数组+xAxis都必须可用。"""
    result = render_results["f14"]
    assert result["objectPoints"] == ["相互印证=2", "单方发现=3", "存在矛盾=0"], "饼图对象数组未解析出名称/数值"
    assert result["numericPoints"] == ["A=2", "B=3"], "饼图数值数组+xAxis 兼容回归"
    assert result["fallbackHasNames"], "无 echarts 时降级表格未输出类别名称"
    assert result["fallbackHasValues"], "无 echarts 时降级表格未输出数值"
    assert result["noGarbage"], "降级表格出现 [object Object]"
    assert result["barFallbackUntouched"], "柱状图降级表格被改坏"


def test_head_tables_fold_by_default_and_constrain_overflow():
    """网页头部两张表（数据来源完整性 / 指标计算过程）默认收起，且不得横向撑破容器。"""
    html = open(INDEX_HTML, encoding="utf-8").read()
    assert '<details class="report-meta-details" open>' not in html, "数据来源与完整性表默认展开"
    assert '<details class="report-meta-details">' in html, "数据来源与完整性表折叠容器丢失"

    indicator = _extract_function(html, "renderIndicatorView")
    assert '<details class="code-fold metric-fold">' in indicator, "指标计算过程表未套折叠容器"
    open_idx = indicator.index('<details class="code-fold')
    table_idx = indicator.index('<table class="metric-table"')
    close_idx = indicator.index("</details>")
    assert open_idx < table_idx < close_idx, "指标表不在折叠容器内"

    assert "min-width:920px" in html, "指标表未守住最小可读宽度"
    rule = html.split(".bubble .code-fold > pre")[1].split("}")[0]
    assert "overflow-x: auto" in rule and "max-width: 100%" in rule, "折叠块缺少横向滚动约束（JSON 溢出回归）"
    assert ".table-scroll { max-width: 100%; overflow-x: auto; }" in html, "卡片内表格缺少横滚约束"

def test_provisional_risk_level_renders_badge_without_orphan_strong(render_results):
    """候选等级「重要（暂定关注）」须渲染为徽章 + 后缀，且不得留下游离 </strong>。

    事故背景：后端曾把 risk_level_label 整串加粗（``**重要（暂定关注）**``），前端徽章正则
    只吃掉裸等级词，后缀之后残留一个无配对的 ``</strong>``，页面上出现半截样式。
    """
    result = render_results["f15"]
    assert result["badgeRendered"], "候选等级未生成彩色徽章"
    assert result["suffixKeptOutsideBold"], "「（暂定关注）」后缀丢失或被并入徽章"
    assert result["noOrphanClose"], "风险分点出现游离 </strong>（等级标签加粗范围回归）"
    assert result["titleStillBold"], "风险标题的粗体被误伤"
    assert result["tableLevelBadged"], "关注等级列未生成徽章（台账表回归）"
    assert result["acceptedPlainLevel"], "已采信正式等级被误标为暂定"
