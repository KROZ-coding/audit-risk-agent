# 三模块架构大修实施方案（修订版）

## 决策基线（已确认）

- 三智能体运行：**混合** —— 点单模块只跑该模块（30-90s）；点"综合研判"串跑 ①→②→③（2-4min）
- 图表：**双轨且保留原设计** —— 新增 ECharts 负责报表时序图，原有 matplotlib PNG 可视化全部保留，PDF 仍走 PNG
- 快捷区：**分组网格** —— 核心分析组（三模块）+ 其他工具组，后续加功能往第二组塞，不破坏布局
- 动态历史：**完整多会话列表** —— 新对话开新会话，可切回旧会话并继续追问
- 载体：本地单机（FastAPI + 浏览器 + 一键启动），不做 exe 打包

---

## P0 命名统一

新名：**上市公司年报风险识别**（去"审计"二字）

- `src/web/index.html`：`<title>`、开屏标题、侧栏品牌名、welcome 主副标题
- `config/agent_llm_config.json` 的 `sp`：角色定位去"审计系统"，**保留专业术语**（审计准则/审计意见/审计建议），避免丢专业性
- `src/tools/pdf_export.py`、`excel_export.py`：报告标题页与表头
- `README.md`、`AGENTS.md`、`方案书.md`、`答辩QA速查.html`、`操作指南.html`、`快速上手.txt`
- `scripts/pack_source.py` 包名前缀

不改：Python 包名 `audit-risk-agent`、产物文件名后缀（`_审计底稿.xlsx` 等），避免破坏既有历史记录与测试断言。

---

## P1 三模块架构（核心）

### 1.1 模块路由

复用现有 `FAST_MODE_KEYWORD` 同款关键词标记方案，零协议改动。`src/main.py` 新增：

```python
MODULE_MARKERS = {
    "financial":  "【模块:财务健康度诊断】",
    "compliance": "【模块:合规与经营风险扫描】",
    "synthesis":  "【模块:综合研判】",
}
def _detect_module(payload) -> str | None: ...   # 与 _payload_wants_fast_mode 同构
```

前端 `moduleAction()` 在发送内容首部注入标记。

### 1.2 三套工具子集

`src/agents/agent.py` 的 `build_agent()` 增加 `module` 参数，按模块裁剪注册：

| 模块 | 工具子集 |
|---|---|
| 财务健康度诊断 | `parse_pdf_report`、`validate_financial_data`、`calculate_financial_indicators`、`compare_multi_year`、`generate_radar_chart`、`generate_trend_chart` |
| 合规与经营风险扫描 | `parse_pdf_report`、`check_disclosure_compliance`、`identify_audit_opinion`(新)、`search_regulations` |
| 综合研判 | `calculate_comprehensive_score`、`calculate_risk_models`(新)、`generate_risk_heatmap`、`export_pdf_report`、`export_excel_report` |

`GraphService._get_agent()` 缓存键从 `pro/flash` 扩展为 `{mode}:{module}`。

### 1.3 综合研判串跑编排

新建 `src/agents/pipeline.py`：

```python
async def run_synthesis_pipeline(payload, ctx, service):
    """①→②→③ 串跑；③ 接收前两段结论做交叉验证。
    段间以"已完成分析摘要"注入（复用 _preprocess_inject 的伪工具轨迹思路）。"""
```

- ③ 提示词强制：逐条比对①②结论，标注「相互印证 / 存在矛盾 / 单方发现」
- SSE 进度三段映射：① 0-35%、② 35-65%、③ 65-100%

### 1.4 进度锚点

`src/main.py` 的 `TOOL_PIPELINE` 按新链路重排，新增 `identify_audit_opinion`、`calculate_risk_models` 锚点。

---

## P2 输出格式固定化 + 图表双轨（保留原有可视化）

### 2.1 固定输出骨架（按模块写入 sp）

```
一、企业简介（公司全称/股票代码/所属行业/主营业务/行业地位，3-5 句）
二、[模块主题]结构分析
    1. / 2. / 3. 分点，每点下 (1)(2)(3) 具体数据 + 判断结论
    → 多年对比表格（Markdown，5-6 年）
    → ```echarts 图表块
三、[模块主题]质量分析（同上：分点 + 表格 + 图表）
四、战略 / 效率匹配性分析
五、可持续性与风险评估
六、综合结论与建议
```

硬规则补入 sp：
- 每条结论必须「具体数值（含同比/占比）→ 判断」格式，禁止空泛表述
- 时序表格固定为年份行 × 指标列，覆盖最近 5-6 年，缺失年度标注"未披露"而非编造
- **禁止增删章节标题**（固定化核心约束）
- 综合研判额外输出「交叉验证矩阵」表格

### 2.2 明确保留的现有可视化资产（不动）

以下全部保留，ECharts 只做增量补充，不替换：

- 风险热力图、财务雷达图、趋势折线图（matplotlib PNG，同时进 PDF）
- 综合评分卡（`<!--COMPREHENSIVE_SCORE-->` 标记驱动）
- 风险等级饼图（圆圈图）
- 文件下载卡片（`.file-download-card` + 事件委托白名单机制）
- 思维链五步卡、多智能体辩论复核卡、工具调用链胶囊条
- 风险台账 JSON（```json 包裹，正文末尾一次）

**分工**：ECharts 负责报表类时序/结构图（利润结构、现金流结构等，对应参考稿的 ECHARTS 块）；PNG 负责风险类可视化（热力图/雷达图）并保证 PDF 一致性。

### 2.3 ECharts 本地内置（离线必需）

单机断网场景不能依赖 CDN：

- `echarts.min.js` 存入 `src/web/vendor/echarts.min.js`
- `src/main.py` 新增 `app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"))`
- `index.html` 引本地 script，**禁止任何 CDN 引用**

### 2.4 ```echarts 代码块渲染

sp 约定固定 schema：

```
```echarts
{"title":"利润结构","type":"line","xAxis":["2019","2020"],
 "series":[{"name":"营业收入","data":[1,2]}]}
```
```

`index.html` 的 `renderMarkdown()` 增加分支：识别 ```echarts → 生成带 `ECHARTS` 标题栏的容器（复刻参考稿样式）→ `echarts.init()` 挂载；JSON 解析失败降级为普通代码块，不白屏。

---

## P3 知识库补强 + 新工具（按文档"必建"优先）

### 3.1 新增语料 `knowledge_base/`

必建：
- `审计意见类型库.txt` —— 五类意见（无保留 / 带强调事项段 / 保留 / 否定 / 无法表示）含义、触发条件、对可信度影响程度
- `报表勾稽规则库.txt` —— 三大报表完整勾稽清单（净利润→现金流量表起点、营收↔应收账款、折旧↔固定资产等）
- `关联交易审查库.txt` —— 关联方定义、披露要求、利润操纵手法、监管关注点
- `风险评分模型库.txt` —— Altman Z-Score / Beneish M-Score 公式、阈值、适用边界 + 本系统权重表与等级映射
- `风险评估框架库.txt` —— 维度权重、等级划分、"不构成否决"类风险清单（含"带强调事项段是否直接判高风险"裁定规则）
- `行业基准库.txt` —— 主要行业核心指标均值/中位数（从 sp 内嵌表外化并扩充）

加分：
- `会计政策变更识别库.txt` —— 变更手法、对利润影响方向、合理 vs 不合理判定
- `行业经营风险特征库.txt` —— 房地产销售回款 / 医药研发资本化 / 制造业产能利用率等
- `报告模板与撰写规范.txt` —— 与 P2 骨架同源
- `经典综合案例库.txt` —— 异常→交叉验证→定性→处罚全链路（喂 ③）

补完必须重建向量库：`uv run python scripts/init_knowledge_base.py`

### 3.2 新增工具

`src/tools/risk_models.py`（新）：

```python
@tool
def calculate_risk_models(financial_data_json: str) -> str:
    """计算 Altman Z-Score（破产预警）与 Beneish M-Score（盈余操纵预警），
    返回分值、判定区间与逐项因子明细。"""
```

- Z-Score 支持制造业 / 非制造业模型变体
- M-Score 八因子（DSRI/GMI/AQI/SGI/DEPI/SGAI/LVGI/TATA），缺项按可得因子计算并标注置信度

`identify_audit_opinion`：扩展 `src/tools/disclosure_checker.py` 或新建，从年报文本识别审计意见类型并映射可信度影响等级。

`src/tools/risk_scorer.py`：综合评分纳入 Z/M-Score 作为独立维度。

---

## P4 五年时序数据支持

- `src/tools/financial_calculator.py`：入参扩展可选 `history`（年份→指标），输出各指标多年序列供表格与 ECharts 消费
- `src/tools/multi_year_comparison.py`：作为 ① 主力工具，输出对齐 P2 时序表 schema
- sp 要求提取覆盖最近 5-6 年

---

## P5 前端改造（按你的四点反馈重写）

### 5.1 快捷区：分组可扩展网格

替换现有 5 张平铺卡为两组：

```
核心分析
  [财务健康度诊断]  [合规与经营风险扫描]  [综合研判]
其他工具
  [批量上传年报]  [监管处罚检索]  ...（后续新功能直接往这组加卡）
```

- 组标题用小字灰色分隔，网格 `auto-fill` 自适应，加卡不需改布局
- `moduleAction()` 保留"上传文件 / 输入公司名"二选一弹窗，但注入 P1 的模块标记

### 5.2 系统通知脱离对话区

现状问题：登录成功走 `addMessage('system', '🔐 欢迎…')`（`index.html:2748`）直接插进对话流，知识库上传/异常同理（2212/2215/2218）。

改造规则：
- **改为 toast 浮层**（复用现有 `.cirno-hint` 同类 toast 组件或统一封装 `notify()`）：登录/登出、知识库上传结果、知识库重载、系统状态提示
- **保留在对话流**：分析链路内的错误与中断（SSE error、文件解析失败、已停止生成、请求失败），因为它们是该次分析的组成部分

若你希望连分析错误也走 toast，这条规则可再收紧。

### 5.3 新对话全局跳转

- `clearChat()`（`index.html:2051`）升级为 `startNewSession()`：无论当前处于对话区、效果评估页还是历史弹窗，一律先切回对话视图（隐藏 eval 面板、关闭弹窗）再开新会话
- 侧栏与顶栏的"新建会话"按钮统一绑定该函数

### 5.4 动态历史：完整多会话管理

**数据结构**（前端）：

```js
session = { id, title, threadId, messages: [], files: [], createdAt, updatedAt }
```

- `id/threadId` 用 `crypto.randomUUID()`。注意：后端 `normalize_run_id` 只接受 16-64 位十六进制或连字符，UUID 天然符合，不会被服务端重置
- 持久化：游客走 `localStorage`；登录用户可另落库（沿用 `analysis_history` 表，或新增 `sessions` 表，视实现成本决定）

**侧栏会话列表**：新建 / 切换 / 重命名 / 删除；标题自动取首条用户消息或公司名前若干字。

**关键行为修正**（你反馈的核心问题）：
- `welcomeScreen`（快捷卡区）**只在当前会话为空时显示**；一旦有消息立即隐藏，分析结果不再堆在快捷框下方
- 点"新对话" = 新建 session（新 threadId）+ 清空渲染 + 重新显示 welcome，**旧内容不残留**
- 点历史会话 = 恢复该 session 的消息渲染，并复用其 `threadId`

**续接追问的后端支撑**：checkpointer 已是 `AsyncSqliteSaver` 持久化，`thread_id = ctx.run_id`。前端目前**未发送** `X-Run-Id` 头，需在 `_doStreamRequest` 与 `/upload` 等请求中带上当前 session 的 `threadId`，即可实现旧会话继续追问。

**与现有历史弹窗的关系**：`showHistoryModal()` 保留为"分析记录"查询（含评分/等级/产物文件）；会话列表负责对话上下文，两者定位区分不冲突。

### 5.5 布局与细节优化

- 固定骨架六章节生成侧边小目录（长报告可跳转）
- 思维链卡增加耗时显示，对齐参考稿 `已深度思考(21.5s)`
- 改完必跑 `node --check`（提取 script 块校验）

---

## P6 测试与验收

新增/更新测试：
- `tests/test_module_routing.py`（新）：三种标记路由到对应工具子集；无标记走全量链路
- `tests/test_risk_models.py`（新）：Z-Score/M-Score 已知样例数值校验、缺项降级
- `tests/test_pipeline_synthesis.py`（新）：三段串跑顺序、段间注入、交叉验证字段存在
- `tests/test_pipeline_integration.py`：TOOL_PIPELINE 锚点与新工具同步
- `tests/test_disclosure_checker.py`：审计意见识别用例

验收清单：
1. `uv run python -m pytest -q` 全绿（基线 139 项，预计增至 150+）
2. 三个模块卡各点一次，均在预期耗时内产出固定骨架报告；其他工具组功能不受影响
3. 综合研判含交叉验证矩阵，且 PDF + Excel + 热力图 + 雷达图 + 饼图 + 下载卡片齐全
4. **断网**下 ECharts 仍渲染（验证本地内置）
5. 登录/知识库操作只出 toast，对话区无系统消息残留
6. 任意界面点"新建会话"都能跳回干净的新对话界面
7. 新会话不残留旧报告；切回旧会话能看到完整内容并可继续追问
8. 全站无"审计风险识别系统"旧称残留（grep 校验）

---

## 执行顺序与风险

建议顺序：**P0 → P3 → P1 → P2 → P5 → P4 → P6**
先做纯增量的知识库与新工具（低风险），再改架构与输出，前端改造集中在一段完成，最后补时序与测试。

主要风险与对策：
- **耗时膨胀**：综合研判串三段可能超 4 分钟 → ③ 段禁止重复检索、复用①②结论，必要时 ③ 单独用 flash 模型
- **固定骨架被模型破坏**：flash 可能不严格遵守章节 → 前端渲染容错（缺章节不报错）+ sp 硬约束"禁止增删章节标题"
- **ECharts 数据非法**：渲染层 try/catch 降级为代码块
- **多会话导致 checkpoint 膨胀**：每会话一个 thread，`checkpoints.sqlite` 会持续增长 → 会话删除时清理对应 thread，或设会话数上限
- **前端改动集中风险**：`index.html` 单文件 3400 行且本次涉及会话管理重构 → 每步改完立即 `node --check`，并优先在独立函数内实现新逻辑减少交叉污染
- **先落 checkpoint**：当前工作区有未提交文档与打包脚本，建议开工前 commit 一次作为回滚锚点