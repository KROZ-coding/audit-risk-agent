# AGENTS.md — 项目导航入口

> 面向在本仓库工作的编码 Agent / 贡献者的速查地图。仅描述结构、路径与操作约定，
> 不复制运行时系统提示词内容（Agent 人设与规则见 `config/agent_llm_config.json` 的 `sp` 字段）。

## 项目定位

**上市公司年报风险识别**（本地运行版）：一个基于 FastAPI + LangGraph ReAct Agent
的 Multi-Agent 系统。用户上传年报（PDF/Excel 等），系统按固定工具链完成财务校验、指标计算、
披露合规检查、法规检索、综合评分，并生成 PDF 报告、Excel 底稿与可视化图表。

- 技术栈：Python ≥3.10、FastAPI、LangGraph、`langchain-openai`（DeepSeek 全链路 `deepseek-v4-flash`，config 可切 `deepseek-v4-pro`）、
  ChromaDB（向量检索，回退 TF-IDF）、matplotlib、reportlab、openpyxl。
- 入口：`src/main.py`（FastAPI app + CLI）。核心 Agent：`src/agents/agent.py`。

## 目录职责表

| 路径 | 职责 |
|------|------|
| `src/main.py` | FastAPI 服务与 CLI 入口；`/run` `/stream_run` `/upload` 等路由；`TOOL_PIPELINE` 进度映射 |
| `src/agents/agent.py` | 构建 ReAct Agent、注册工具、多智能体辩论、兜底导出、消息滑动窗口 |
| `src/tools/` | 全部审计与 C 端轻量工具（16 个工具，见下方常见任务路径） |
| `src/local_knowledge.py` | 知识库加载与检索（ChromaDB / TF-IDF） |
| `src/local_shims.py` | 替代 coze SDK 的本地兼容层（context、logging、config 等） |
| `src/storage/` | 数据库、memory checkpoint、S3 存储封装 |
| `src/web/` | 前端 `index.html`（可视化界面） |
| `config/agent_llm_config.json` | LLM 参数、系统提示词 `sp`、已启用工具列表 |
| `knowledge_base/` | 法规与案例 txt 语料（检索数据源） |
| `.chroma_db/` | ChromaDB 向量库持久化目录（自动生成，勿手改） |
| `local_storage/reports` `local_storage/charts` | 导出的 PDF/Excel 报告与 PNG 图表 |
| `scripts/` | 启动、环境加载、知识库初始化、打包脚本 |
| `tests/` | 单元测试与效果评估脚本 |

## 常见任务路径

| 想做的事 | 去哪里改 |
|----------|----------|
| 调整财务指标计算 | `src/tools/financial_calculator.py` |
| 调整数据校验规则 | `src/tools/data_validator.py` |
| 调整披露合规检查 | `src/tools/disclosure_checker.py` |
| 调整综合风险评分 | `src/tools/risk_scorer.py` |
| 调整法规/案例检索 | `src/tools/knowledge_search.py` + `src/local_knowledge.py` |
| 调整多年对比 | `src/tools/multi_year_comparison.py` |
| 调整 PDF / Excel 导出 | `src/tools/pdf_export.py` / `src/tools/excel_export.py` |
| 调整图表生成 | `src/tools/visualizer.py`（热力图/雷达图/趋势图） |
| 调整 PDF 解析 | `src/tools/pdf_parser.py` |
| 批量分析多公司 | `src/tools/batch_processor.py` |
| 改 Agent 提示词/工具启用 | `config/agent_llm_config.json` |
| 改进度映射/HTTP 路由 | `src/main.py`（`TOOL_PIPELINE`、`@app.post` 路由） |

### 本地运行

```bash
uv sync                       # 安装依赖
uv run python scripts/init_knowledge_base.py   # 首次构建向量库
uv run python -m main         # 启动服务（默认 :5000），或 uvicorn main:app --port 5000
```

> Windows PowerShell 用 `;` 分隔命令，勿用 `&&`。`.env` 需配置 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。

## ⚠️ 高风险区（改动前务必阅读）

### 1. 工具执行顺序（分级门禁）

Agent 按推荐链路调用工具，顺序约束分两级（见 `src/tools/domain_guard.py`）：

```
validate_financial_data → calculate_financial_indicators
→ (check_disclosure_compliance ∥ search_regulations 并行)
→ calculate_comprehensive_score → 导出（由系统兜底，LLM 可不调用）
```

- **硬约束（fail-closed）**：「先 validate 后 calculate」，违反即中断分析；
- **软约束（可见警告）**：其余相对次序属推荐顺序非数据依赖，违反时不中断，
  在报告末尾附「工具链顺序提示」供人工复核（兜底导出照常执行）。

- 顺序与规则由 `config/agent_llm_config.json` 的 `sp` 字段约束，进度映射在 `src/main.py` 的 `TOOL_PIPELINE`。
- 新增/重命名工具时，须同步更新：`src/agents/agent.py`（注册）、`config` 的 `tools` 列表、`main.py` 的 `TOOL_NAME_TO_STEP`；同时同步工具计数表述——`src/agents/agent.py` 中 `build_agent` docstring 与 `README.md` 项目结构里的「N 个核心分析工具」（当前为 16，须与 `build_agent` 的 `tools` 列表长度、`config` 的 `tools` 数组长度一致）。
- 注：`export_pdf_report` / `export_excel_report` 不注册给 LLM（导出由系统后处理兜底执行，保证以完整数据生成报告）；计数口径为 LLM 注册工具数，config 的 `tools` 数组与 `build_agent` 列表均不含导出工具。
- **校验命令**：`uv run pytest tests/ -q`（验证核心工具行为不回归）。

### 2. 报告导出（PDF + Excel 缺一不可）

- `export_pdf_report` 与 `export_excel_report` 必须成对调用；`agent.py` 含兜底导出机制防遗漏。
- 输出落盘到 `local_storage/reports`，图表到 `local_storage/charts`，通过 `/local_storage` 静态路由暴露。
- 改动导出后校验：运行一次完整分析，确认返回的 URL 可访问且文件生成。
- **校验命令**：`uv run pytest tests/test_financial_calculator.py tests/test_data_validator.py -q`。

### 3. 知识库检索（数据源与向量库一致性）

- 检索数据源为 `knowledge_base/*.txt`；向量库持久化在 `.chroma_db/`。
- 新增/修改语料后须重建索引：`uv run python scripts/init_knowledge_base.py`，
  或运行时调用 `/api/reload_kb` 热重载。
- ChromaDB 不可用时自动回退 TF-IDF；改动检索逻辑请两条路径都验证。

### 4. 效果评估（不可与单元测试混用）

- 评估脚本：`tests/evaluation_report.py`，结果写入 `tests/evaluation_results.json`。
- 工具级快速评估：`uv run python tests/evaluation_report.py --mode tool`（<1 秒）。
- 全链路评估：`uv run python tests/evaluation_report.py --mode agent`（每用例约 30-60 秒，需 LLM）。

## 更深文档链接

> **when-to-load 路由**：当任务是「上传单份年报做完整审计风险分析 / 出报告」时（最高频任务），
> 先加载 `docs/exec-plan-单份年报完整分析.md` —— 内含触发条件、固定工具链步骤与验收校验；
> 多公司批量分析则改走 `src/tools/batch_processor.py`（`batch_analyze_companies`）。

- `docs/exec-plan-单份年报完整分析.md` — 单份年报完整分析的可复用执行清单（固定工具链 + 验收校验）
- `README.md` — 项目总览与部署说明
- `DATA_SOURCES.md` — 知识库与数据来源说明
- `年报风险识别系统专用readme.html` — 完整操作指南（运行时 `/readme` 可访问）
- `2026北京市大学生数智会计创新应用竞赛手册.html` — 竞赛评分维度与要求
- `pyproject.toml` — 依赖、`pytest` 配置（`pythonpath=["src"]`）
- `config/agent_llm_config.json` — Agent 系统提示词与工具清单（运行时行为的权威来源）
