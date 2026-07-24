# Exec-Plan：单份年报完整分析

> 项目最高频任务的可复用执行沉淀。当用户上传**一份**年报（PDF/Excel）并要求做审计风险
> 识别 / 出报告时，按本文件的固定工具链与验收校验执行，避免遗漏步骤或颠倒顺序。
> 本文件是**执行清单**，运行时行为的权威来源仍是 `config/agent_llm_config.json` 的 `sp` 字段。

## 何时加载（when-to-load）

满足以下**任一**条件即加载本 exec-plan：

- 用户上传单个年报文件（`.pdf` / `.xlsx` / `.xls`）并要求「分析 / 审计 / 风险识别 / 出报告」。
- 请求语义为「对某上市公司年报做完整审计风险分析」且只涉及**一家公司、一份报告**。

**不适用**（改走其他路径）：

- 多公司 / 多文件批量分析 → 走 `batch_analyze_companies`（见 `src/tools/batch_processor.py`）。
- 仅问答、仅检索法规、仅生成单张图表等**非完整分析**的零散请求。

## 触发条件 → 目标产物

一次完整分析必须产出以下**全部**产物，缺一即视为未完成：

1. 结构化 JSON 结论（遵循 `sp` 中「输出JSON模板」，含 `comprehensive_score`）。
2. PDF 审计风险报告（`local_storage/reports/*_审计风险报告.pdf`）。
3. Excel 审计底稿（`local_storage/reports/*_审计底稿.xlsx`）。
4. 三类图表：风险热力图、财务雷达图、趋势折线图（`local_storage/charts/*.png`）。
5. 回复中**完整展示**上述所有产物的可访问 URL。

## 固定工具链（顺序不可颠倒）

以 `sp` 的「强制规则 / 执行流程」为准，进度映射见 `src/main.py` 的 `TOOL_PIPELINE`。

| 步 | 工具 | 作用 | 硬约束 |
|----|------|------|--------|
| 0 | `parse_pdf_report` | 解析 PDF 年报为结构化数据（Excel 输入可跳过） | 仅 PDF 输入时执行 |
| 1 | `validate_financial_data` | 校验财务数据勾稽/完整性 | **必须先于**步骤 2 |
| 2 | `calculate_financial_indicators` | 计算 16 项财务指标 | 严禁在校验前调用 |
| 3 | `check_disclosure_compliance` | 披露合规性检查 | 与财务分析须同时覆盖 |
| 4 | `search_regulations` | 每条风险检索法规条款 + 同类案例 | 无检索依据不得出结论 |
| 5 | `calculate_comprehensive_score` | 汇总三模块，输出 0-100 分 + 等级 | 依赖步骤 2/3/4 结果 |
| 6a | `export_pdf_report` | 导出 PDF 报告 | **与 6b 成对**，缺一不可 |
| 6b | `export_excel_report` | 导出 Excel 底稿 | **与 6a 成对**，缺一不可 |
| 7 | `generate_risk_heatmap` / `generate_radar_chart` / `generate_trend_chart` | 三类可视化图表 | 三张齐全 |

> 顺序摘要：`parse → validate → calculate → check_disclosure → search → score → export_pdf + export_excel → 图表`。
> `agent.py` 含兜底导出机制防止 PDF/Excel 遗漏，但不可依赖兜底跳过正常调用。

## 措辞与声明约束（不可违背）

- 使用审慎审计术语（「存在…风险嫌疑」「建议进一步核查」），**禁止**定性结论（「存在财务造假」等）。
- 每条风险须在 `risk_details` 输出结构化 `reasoning_chain`（数据发现→指标异常→法规依据→案例对照→风险判定）。
- 回复末尾附加 AI 生成声明（见 `sp`）。

## 验收校验（Definition of Done）

分析结束前逐项确认：

- [ ] 工具调用顺序符合上表，`validate` 早于 `calculate`。
- [ ] `export_pdf_report` 与 `export_excel_report` **均**已调用且返回 URL。
- [ ] 三类图表均已生成且 URL 在回复中展示。
- [ ] 输出 JSON 含 `comprehensive_score`（`score` / `level` / `breakdown`）。
- [ ] 每条风险含 `regulatory_basis` 与 `case_reference`（来自 `search_regulations`）。
- [ ] 回复末尾含 AI 生成声明。

### 回归校验命令

```bash
uv run pytest tests/ -q                                                   # 核心工具行为不回归
uv run pytest tests/test_financial_calculator.py tests/test_data_validator.py -q  # 导出/校验链路
```

## 相关路径

- 工具实现：`src/tools/`（各工具与「常见任务路径」对应，见 `AGENTS.md`）。
- 进度映射：`src/main.py` 的 `TOOL_PIPELINE` / `TOOL_NAME_TO_STEP`。
- 权威提示词：`config/agent_llm_config.json` 的 `sp`。
