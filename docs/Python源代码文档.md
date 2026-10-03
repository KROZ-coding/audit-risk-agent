# 上市公司年报风险智能识别系统 · Python 源代码文档

> 版本：v5.3 GA（`pyproject.toml` version = 5.3.0）
> 文档性质：按模块、按公开类/函数说明用途、入参与出参，便于评审与二次开发查阅。
> 配套文档：`docs/技术报告.md`（实现原理）、`docs/源代码清单.txt`（文件与行数清单）。

## 阅读说明

- 本文档由 AST 静态提取 + 人工归类整理生成，覆盖 `src/` 与 `scripts/` 下的全部 Python 模块。
- 带前导下划线（`_name`）的函数/类为模块内部实现，不在此逐条展开，其关键逻辑见 `docs/技术报告.md` 第 5 章。
- `@tool` 装饰的函数是提交给大模型调用的工具，其参数为 JSON 字符串（便于模型生成），返回值多为 JSON 字符串或提示文本。
- 行数为当前版本的物理行数，用于交付时核对。

---

## src/main.py

接口层与编排层主入口：FastAPI 应用装配、21 个路由、鉴权中间件、上传、SSE、GraphService 编排、端口自愈。

### `src/main.py`（2780 行）

本地运行入口 - 精简版 FastAPI 服务 替代 main.py 中大量 coze_coding_utils 依赖，使用 local_shims 提供兼容。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `expected_artifacts(module, fast)` | 本次运行应生成的产物类型清单（与 Agent 的 artifact_expectations 同构）。 |
| 类 | `GraphService` | 核心服务：封装 Agent 调用、流式推送、任务生命周期管理。 |
| 方法 | `GraphService.run(payload, ctx)` |  |
| 方法 | `GraphService.stream_sse(payload, ctx, run_opt, user)` | 用 astream 跟踪真实工具进度，最后一次性推送完整报告；登录态下顺带落历史 |
| 函数 | `lifespan(app)` |  |
| 函数 | `api_key_guard(request, call_next)` | 写操作接口的 X-API-Key 校验中间件（APP_API_KEY 未配置时直接放行）。 |
| 函数 | `serve_web_ui()` | 提供可视化 Web 界面（禁用缓存，确保前端改动即时生效） |
| 函数 | `serve_baka_readme()` | 提供冰之妖精部署指南 |
| 函数 | `serve_professional_readme()` | 提供年报风险识别系统部署指南（正式版） |
| 函数 | `get_system_status()` | 返回系统各组件详细状态 |
| 函数 | `auth_register(request)` | 注册新用户并自动登录（返回会话令牌，免二次登录）。 |
| 函数 | `auth_login(request)` | 口令登录，签发会话令牌（用户名不存在与口令错误统一提示，防枚举）。 |
| 函数 | `auth_logout(request)` | 登出：删除当前会话令牌（幂等）。 |
| 函数 | `auth_me(request)` | 查询当前登录态（前端启动时校验本地 token 是否仍有效）。 |
| 函数 | `history_list(request)` | 当前用户的分析历史列表（倒序，最多 50 条；未登录返回 401）。 |
| 函数 | `history_detail(record_id, request)` | 单条历史详情；他人记录与不存在统一返回 404（防探测）。 |
| 函数 | `http_run(request)` | 同步执行审计分析（阻塞等待完整结果）。 |
| 函数 | `http_stream_run(request)` | 流式执行审计分析（SSE 事件流推送）。 |
| 函数 | `openai_chat_completions(request)` | OpenAI 兼容的对话补全接口。 |
| 函数 | `list_generated_files()` | 列出 local_storage 中已生成的文件 |
| 函数 | `health_check()` | 服务健康检查接口，用于监控和就绪探测。 |
| 函数 | `maintenance_status()` | 只读查询最近一次定期回收报告（不触发回收）。 |
| 函数 | `upload_files(files)` | 接收上传文件，保存到临时目录并解析提取文本内容。 |
| 函数 | `reload_knowledge_base()` | 热重载知识库，无需重启服务。 |
| 函数 | `upload_knowledge_base(files)` | 上传文件到知识库，自动转换为 txt 格式并热重载 |
| 函数 | `get_evaluation_status()` | 获取评估结果状态（从缓存的 JSON 文件读取）。 |
| 函数 | `run_evaluation(mode)` | 运行效果评估（工具模式或全链路 Agent 模式）。 |
| 函数 | `parse_args()` |  |
| 函数 | `parse_input(input_str)` |  |
| 函数 | `start_http_server(port)` |  |

内部实现（不逐个展开）：`_detect_module`, `_detect_light_module`, `_payload_wants_fast_mode`, `_current_user`, `_extract_history_fields`, `_artifact_local_path`, `_artifact_is_accessible`, `_artifact_key`, `_build_data_sources`, `_record_history`, `_pick_free_port`。

---

## src/agents/

智能体层：LangGraph ReAct 构建、工具裁剪、消息滑窗、工具链台账、后处理兜底、三方辩论复核。

### `src/agents/agent.py`（4882 行）

上市公司年报风险识别智能体 本模块是整个系统的核心入口，负责： 1.

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `ReviewBudgetExceeded` | 审查预算耗尽（次数或阶段限时）：按计划转人工复核，不继续追加调用。 |
| 类 | `ReviewBudget` | 审查阶段预算账本：固定限额、逐次记账、超限转人工。 |
| 方法 | `ReviewBudget.elapsed()` |  |
| 方法 | `ReviewBudget.remaining_calls()` |  |
| 方法 | `ReviewBudget.remaining_seconds()` |  |
| 方法 | `ReviewBudget.can_call()` |  |
| 方法 | `ReviewBudget.note(label, ok, error)` | 记录一次实际发起的调用（成功或失败都计数）。 |
| 方法 | `ReviewBudget.begin_supplement_round()` | 申请一轮补证；超过上限返回 False，调用方转人工而不无限追加投票。 |
| 方法 | `ReviewBudget.exhausted_reason()` |  |
| 方法 | `ReviewBudget.snapshot()` | 固定配置 + 实际用量：随复核结果落盘，供网页/PDF/Excel 记录。 |
| 类 | `AgentState` | Agent 状态定义，继承自 LangGraph 的 MessagesState。 |
| 函数 | `build_agent(ctx, model_override, module)` | 构建年报风险分析 Agent。 |

内部实现（不逐个展开）：`_normalize_related_party_text`, `_normalize_source_bound_text`, `_windowed_messages`, `_merge_tool_ledger`, `_accumulate_tool_ledger`, `_normalize_dims`, `_extract_risk_json`, `_extract_json_object`, `_parse_c2_response`, `_backfill_c2_evidence_ids`, `_compare_c2_reviews`, `_apply_review_gates`, `_norm_dim_for_backfill`, `_collect_system_evidence`, `_collect_tool_evidence_catalog`, `_source_report_evidence_records`, `_system_evidence_records`, `_match_evidence_for_dimension`, `_match_evidence_record_for_dimension`, `_backfill_risk_evidence` 等。

### `src/agents/pipeline.py`（229 行）

综合研判串跑编排（三层任务单元的流水线逻辑） 架构定稿的混合运行模式中，点击「综合研判」会依次串跑三段： ① 财务健康度诊断 → ② 合规与经营风险扫描 → ③ 综合研判（交叉验证前两者） 本模块只放**纯逻辑**（阶段定义、阶段载荷构造、摘要提取、交叉验证指令）， 不含 SSE 与 Agent 缓存，便于单元测试；实际编排循环在 main.py 的 stream_sse 中， 因为那里才持有 Agent 实例缓存与 SSE 推送能力。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `summarize_tool_result(content)` | Serialize a structural projection without cutting JSON or dropping late findings. |
| 函数 | `extract_stage_summary(messages, max_chars)` | 从某一阶段的结果消息中提取该阶段的分析结论文本。 |
| 函数 | `build_stage_payload(base_payload, module, marker, prior_summaries, prior_tool_results)` | 构造某一阶段的请求载荷。 |

---

## src/tools/

能力层：16 个审计工具（解析、校验、指标、模型、披露、检索、评分、可视化、导出等）。

### `src/tools/audit_opinion.py`（326 行）

审计意见类型识别工具 从年报/审计报告文本中识别注册会计师出具的审计意见类型，并映射其对年报数据 可信度的影响程度，为后续风险研判提供"数据可信度权重"依据。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `identify_audit_opinion(report_text, source_metadata_json)` | 识别年报中的审计意见类型，并评估其对年报数据可信度的影响程度。 |

内部实现（不逐个展开）：`_source_metadata`, `_structured_output`, `_match_opinion`。

### `src/tools/audit_reinforcement.py`（290 行）

审计补强五项字段：涉及科目、适用认定、核查程序、所需材料、企业改进建议。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `derive_reinforcement(risk)` | 按维度与科目关键词生成模板补强字段（仅在对应字段缺失时使用）。 |
| 函数 | `get_reinforcement(risk)` | 读取五项补强字段：保留模型输出，缺项用模板补齐（不修改入参）。 |
| 函数 | `ensure_reinforcement(risk)` | 就地补齐五项字段并记录来源，返回是否发生变更。 |
| 函数 | `apply_reinforcement(report_obj)` | 为台账中每条风险补齐五项字段，返回发生变更的条目数。 |
| 函数 | `reinforcement_cell(risk, key, separator)` | 取单项字段的单元格文本（Excel 用），多值以分隔符连接。 |
| 函数 | `reinforcement_items(risk)` | 按固定顺序返回 (标签, 取值列表)，供 PDF 逐项渲染。 |

内部实现（不逐个展开）：`_dedupe`, `_as_list`, `_dim_key`, `_subject_text`。

### `src/tools/batch_processor.py`（297 行）

批量处理工具 - 多家公司年报批量分析 + 行业对比报告 本模块支持对多家上市公司进行并行的财务风险分析，并在独立分析基础上 生成行业横向对比报告，包括： - 各公司独立的风险评分和等级划分（重大/重要/一般） - 按行业分组的风险统计和排名 - 行业平均风险水平对比 使用 ThreadPoolExecutor 实现多线程并行分析，最大 5 个工作线程。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `batch_analyze_companies(companies_data_json)` | 批量分析多家公司年报，独立分析每家公司后生成行业横向对比报告。 |

内部实现（不逐个展开）：`_extract_financial`, `_classify_alerts`, `_score_risk`, `_analyze_single_company`, `_generate_industry_comparison`。

### `src/tools/data_validator.py`（352 行）

财务数据一致性校验。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `validate_financial_data(financial_data_json)` | 执行资产负债表、现金流和未分配利润三类数据质量校验。 |

内部实现（不逐个展开）：`_decimal`, `_number`, `_metadata`, `_normalize_validation_input`, `_fact`, `_base_result`, `_validate_balance_sheet`, `_validate_cashflow_reconciliation`, `_validate_retained_earnings`。

### `src/tools/disclosure_checker.py`（419 行）

信息披露规范性检查工具 参照适用的定期报告披露规则，筛查文本中的章节和关键披露事项。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `check_disclosure_compliance(report_text, validation_json, source_metadata_json)` | 筛查定期报告中的披露线索，输出文本筛查评分和待复核事项。 |

内部实现（不逐个展开）：`_topic_context`, `_policy_change_contexts`, `_disclosure_reference`, `_source_metadata`, `_disclosure_anchor`, `_disclosure_failure`。

### `src/tools/domain_guard.py`（269 行）

领域约束机制化校验（Domain Constraint Guard） 本模块为年报风险识别系统的两条关键领域约束提供机制化校验（断言 / 后置检查）， 在 Agent 执行链与报告导出环节强制执行，防止 LLM 或后续代码回归破坏审计合规底线： 约束一：工具调用顺序 —— 必须"先校验后计算" 在调用 calculate_financial_indicators（财务指标计算）之前，必须已调用 validate_financial_data（财务数据勾稽校验）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `ToolCallOrderViolation` | 工具调用顺序违反领域约束时抛出（先 validate 后 calculate）。 |
| 类 | `DisclaimerMissingError` | 导出内容缺失 AI 免责声明时抛出。 |
| 函数 | `check_tool_call_order(called_tools)` | 后置检查：验证工具调用顺序满足完整声明链路不变量。 |
| 函数 | `assert_tool_call_order(called_tools)` | 断言版本（两层合并）：任一层违规均抛 ToolCallOrderViolation。 |
| 函数 | `assert_hard_order(called_tools)` | 硬约束断言：仅拦截「先校验后计算」违规（fail-closed）。 |
| 函数 | `check_soft_order(called_tools)` | 软约束检查：完整声明链路相对次序，返回 (ok, message) 不抛异常。 |
| 函数 | `collect_flowable_texts(elements)` | 从 reportlab flowable 元素树中提取全部文本（不依赖 reportlab 类型）。 |
| 函数 | `check_disclaimer_present(texts, marker)` | 后置检查：验证导出内容中包含 AI 免责声明标识。 |
| 函数 | `assert_disclaimer_present(texts, doc_kind, marker)` | 断言版本：导出内容缺失免责声明时抛出 DisclaimerMissingError。 |

内部实现（不逐个展开）：`_check_validate_before_calculate`, `_check_pipeline_order`。

### `src/tools/excel_export.py`（772 行）

Excel审计底稿导出工具 将风险台账 JSON 数据导出为格式化的 Excel 审计底稿文件，包含九张工作表： 1. 报告概览：公司基本信息（名称/股票代码/行业等）+ 按明细实时聚合的风险统计摘要 2. 风险台账：通过证据门禁的正式风险完整信息，等级用颜色标注（重大红/重要黄/一般蓝） 3. 规则口径：评分规则、阈值来源、版本号与审查门禁状态 4. 建议与核查程序：企业改进建议、涉及科目、适用认定、核查程序及所需材料 5. 原始事实：字段、原始值与解析值、单位/币种/期间/口径、原文件与哈希、页码/定位/原文摘录、提取方式 6. 指标计算：公式、输入值及单位、代入过程、阈值来源、状态与原文定位 7. 勾稽校验：校验公式、差异、阈值、结果与限制/失败说明 8. 证据索引：来源类型、原文件与哈希、页码/定位、原文摘录、关联事实与指标 9. 待复核事项：未通过门禁条目及待处理原因与下一步程序 风险台账页与建议页只展示通过证据门禁的正式风险，未通过条目进入「待复核事项」页，避免汇总把候选风险混入正式统计；Web 和 PDF 只展示数量与简短提示。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `export_excel_report(risk_report_json, validation_json, financial_indicators_json, disclosure_check_json, audit_opinion_json, comprehensive_score_json, risk_models_json)` | 将风险台账 JSON 数据导出为 Excel 审计底稿文件，上传到对象存储并返回可下载 URL。 |

内部实现（不逐个展开）：`_level_counts`, `_formal_risks`, `_reconcile_risk_summary`, `_display_width`, `_estimate_row_height`, `_export_excel_impl`。

### `src/tools/financial_calculator.py`（869 行）

确定性财务指标计算。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `detect_amount_unit(text)` | 从原文的明确单位声明中提取单位，无法识别时返回 None。 |
| 函数 | `extract_parent_net_profit(text)` | 按原文明确单位提取归母净利润，无法确认单位时返回 None。 |
| 函数 | `scale_amount_fields(data, factor)` | 按原文明确声明的单位整体换算金额字段。 |
| 函数 | `normalize_financial_units(data)` | 保留兼容入口，但不再依据财务结构猜测并修正金额单位。 |
| 函数 | `financial_period(data)` |  |
| 函数 | `is_interim_period(period)` |  |
| 函数 | `calculate_financial_indicators(financial_data_json)` | 计算偿债、营运、盈利、成长及现金流/资产质量指标。 |

内部实现（不逐个展开）：`_decimal`, `_number`, `_format`, `_safe_div`, `_pct_change`, `_is_amount_field`, `_metadata`, `_period_kind`, `_comparison_periods_aligned`, `_opening_period_matches`, `_resolve_aliases`, `_fact_records`, `_input_ref`, `_term_to_key_map`, `_substitution_terms`, `_format_input`, `_build_substitution`, `_metric`, `_evidence`。

### `src/tools/indicator_view.py`（187 行）

指标计算过程视图：盈利、营运、偿债、成长、现金流五项核心能力与资产质量审计关注 + 公式→输入→代入→结果→阈值→状态→原文定位。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `metric_source_ref(metric, evidence_index)` | 原文定位：证据目录中的页码/定位/原文件；无定位时如实标注而非留空。 |
| 函数 | `build_indicator_view(fin_json)` | 构建指标计算过程视图；入参为空或解析失败时返回 available=False 的空视图。 |

内部实现（不逐个展开）：`_input_text`, `_period_scope`。

### `src/tools/industry_outlook.py`（333 行）

行业风向标工具（C 端轻量工具） 输入行业名（或公司名），在线抓取公开财经新闻并结合本地知识库的行业风险 特征，输出未来 3-6 个月的行业景气度预判：利好/利空事件清单（带来源与 日期）、景气度温度计（1-10）、对目标公司年报风险的传导影响提示。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `industry_outlook(industry, company_name)` | 生成「行业风向标」：结合近期新闻与知识库，预判行业未来 3-6 个月景气度。 |

内部实现（不逐个展开）：`_cache_get`, `_cache_put`, `_parse_rss`, `_fetch_one_source`, `_fetch_online_news`, `_search_offline_kb`, `_score_sentiment`, `_outlook_text`, `_transmission_text`。

### `src/tools/investment_advisor.py`（317 行）

智能投资参考卡工具（C 端轻量工具） 基于综合风险评分与财务指标分析结果，为普通投资者生成结构化的 「投资参考卡」：四档参考位、投资亮点/风险警示 TOP3、关注检查清单、 风险承受度画像匹配。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `investment_advisor(comprehensive_score_json, financial_indicators_json, regulatory_inquiry_json, industry_outlook_json)` | 生成面向普通投资者的「投资参考卡」（风险提示定位，非投资建议）。 |

内部实现（不逐个展开）：`_load_dict`, `_map_tier`, `_pick_highlights`, `_pick_warnings`, `_build_checklist`, `_match_profiles`, `_extract_reg_alerts`, `_extract_industry_sentiment`。

### `src/tools/knowledge_search.py`（84 行）

知识库检索工具 - 从法规与案例知识库中检索相关条文和案例 本模块封装了基于 ChromaDB 向量语义检索的知识库能力，为审计风险分析提供法规依据。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `search_regulations(query)` | 检索审计法规知识库，获取与风险关键词相关的法规条文和处罚案例。 |

### `src/tools/multi_year_comparison.py`（601 行）

多年财务数据对比分析工具 支持对多个年度的财务数据进行跨年对比分析，识别趋势性风险。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `compare_multi_year(multi_year_data_json)` | 多年财务数据跨年对比分析，识别趋势性风险。 |

内部实现（不逐个展开）：`_decimal`, `_number`, `_read`, `_text`, `_year_sort_key`, `_adjacent_period`, `_balance_average`, `_id_part`, `_legacy_compare_multi_year_impl`, `_compare_multi_year_impl`。

### `src/tools/pdf_export.py`（3323 行）

PDF风险报告导出工具 - 将风险台账JSON导出为格式化PDF（专业版） 默认拆分导出：一次生成 3 份独立 PDF 报告（老师反馈：拆分更清晰、每份配封皮目录）—— 1. 财务健康诊断报告：公司简介 + 财务指标四维判读（含指标对比图/雷达图）+ 财务维度风险明细 2. 合规与信息披露报告：公司简介 + 披露规范性检查结果 + 合规标准对照 + 合规维度风险明细 3. 综合汇总报告：公司简介 + 综合评分解读（仪表盘）+ 风险总览 + 交叉验证/风险传导链 + 全部风险明细 + 整体结论 + 行业基准对比 + 风险评估方法论 每份 PDF 均含：封面页（色带封皮）、动态目录页、AI 辅助生成声明、三段式页脚 （报告名/机密标注/页码）与免责声明页。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `export_pdf_report(risk_report_json, financial_indicators_json, disclosure_check_json, comprehensive_score_json, risk_models_json, validation_json, compare_multi_year_json, audit_opinion_json, module)` | 将风险台账 JSON 导出为独立 PDF 报告，上传到存储并返回下载链接。 |

内部实现（不逐个展开）：`_norm_dim`, `_register_chinese_font`, `_build_styles`, `_styled_table`, `_esc`, `_para_text`, `_cell_style`, `_numbered_lines`, `_amount_unit_factor`, `_fmt_num`, `_embed_chart`, `_safe_json`, `_benchmark_basis_note`, `_has_verified_benchmark_provenance`, `_structured_review_objects`, `_strip_structured_review_json`, `_review_actionable_text`, `_load_benchmarks`, `_interpret_indicator`, `_mpl_hex` 等。

### `src/tools/pdf_parser.py`（223 行）

PDF 年报解析工具 - 从 PDF 文件中提取文本内容 本模块是审计分析流程的第一步，负责将上市公司年报 PDF 转化为可供 LLM 分析的纯文本。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `parse_pdf_report(file_path)` | 解析 PDF 格式的上市公司年报文件，提取全部页的文本内容。 |

内部实现（不逐个展开）：`_assert_public_host`, `_download_url_to_local`, `_resolve_file_path`。

### `src/tools/regulatory_inquiry.py`（291 行）

监管问询在线查询工具 支持从上交所/深交所公开接口实时查询特定公司的监管问询函记录和最新监管动态。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `search_regulatory_inquiries(query, exchange, limit)` | 查询上市公司监管问询函记录（实时在线查询，网络失败自动跳过不影响分析）。 |

内部实现（不逐个展开）：`_cache_get`, `_cache_put`, `_detect_exchange`, `_fetch_sse`, `_fetch_szse`, `_parse_sina_suggest`, `_resolve_stock_code`。

### `src/tools/risk_models.py`（604 行）

量化风险预警模型工具（Altman Z-Score / Beneish M-Score） 本模块实现两个国际公认的经典财务预警模型，为综合研判提供可量化、可复现的 第三方模型证据（区别于本系统自研的加权评分）： 1. Altman Z-Score —— 财务困境（破产）预警 三个变体按行业与数据可得性自动选择： - 原始 Z-Score：上市制造业（需股权市值） - Z'-Score：制造业但无市值数据（改用股东权益账面价值） - Z''-Score：非制造业与新兴市场（剔除周转率因子 X5） 2. Beneish M-Score —— 盈余操纵预警 八变量完整模型，TATA（权重最高）缺失时仅在五个简化模型因子完整的情况下 降级；任何必要因子缺失都不以中性值代入。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `calculate_risk_models(financial_data_json)` | 计算 Altman Z-Score（财务困境预警）与 Beneish M-Score（盈余操纵预警）两个经典量化模型。 |

内部实现（不逐个展开）：`_metadata`, `_model_facts`, `_model_records`, `_num`, `_ratio`, `_pick`, `_context_value`, `_context_gate`, `_pick_z_variant`, `_zone_of`, `_calc_altman`, `_interim_model_result`, `_calc_beneish`, `_combine`。

### `src/tools/risk_scorer.py`（508 行）

综合风险评分工具 汇总财务指标分析、披露规范性检查、数据校验三大模块的结果， 计算 0-100 综合风险分数并映射为风险等级。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `calculate_comprehensive_score(financial_analysis_json, disclosure_check_json, validation_json, risk_models_json, audit_opinion_json)` | 计算综合风险评分（0-100，分越高风险越大），汇总各模块分析结果。 |

内部实现（不逐个展开）：`_score_records`, `_get_level`, `_calc_financial_risk`, `_calc_disclosure_risk`, `_calc_validation_risk`, `_calc_model_escalation`, `_calc_opinion_escalation`。

### `src/tools/upload_helper.py`（38 行）

对象存储上传辅助函数 - 本地模式下直接委托给 local_storage 本模块是存储层的中转代理，设计目的在于： 1. 解耦工具代码与具体存储实现：工具模块只需调用本函数，无需关心底层是本地存储还是 S3 2. 保持接口签名与 Coze 云端模式的 upload_file_to_storage 一致，便于云端/本地切换 3. 本地模式下将调用委托给 localization 模块的 local_storage，将文件保存到 local_storage/ 目录 未来如需切换到 S3/MinIO/OSS 等对象存储，只需修改本函数内部的委托目标即可。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `upload_file_to_storage(local_path, file_name, content_type, expire_seconds)` | 将本地文件上传到存储系统，返回可访问的 URL。 |

### `src/tools/visualizer.py`（660 行）

可视化看板工具 - 风险热力图、财务雷达图、趋势折线图 本模块提供三种审计可视化图表的生成能力： 1. 风险热力图：五大风险维度 × 风险等级矩阵，颜色深浅表示风险强度 2. 财务雷达图：五维财务指标与公司所在行业基准的对比 3. 趋势折线图：多年关键财务科目和比率的变化趋势 所有图表均使用 matplotlib 渲染，支持中文字体自动配置。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `generate_risk_heatmap(risk_report_json)` | 生成审计风险热力图（五大维度×风险等级），返回图片下载URL。 |
| 函数 | `generate_radar_chart(risk_report_json)` | 生成财务指标雷达图（与行业基准对比），返回图片下载URL。 |
| 函数 | `generate_trend_chart(trend_data_json)` | 生成多年财务指标趋势折线图，返回图片下载URL。 |

内部实现（不逐个展开）：`_setup_chinese_font`, `_finalize`, `_upload`, `_formal_risk_details`, `_norm_dimension`, `_norm_level`, `_empty_heatmap_reason`, `_generate_risk_heatmap`, `_generate_radar_chart`, `_render_trend_scarce`, `_generate_trend_chart`。

---

## src/storage/

存储层：业务数据库/用户认证与分析历史、检查点持久化、对象存储与本地降级。

### `src/storage/database/db.py`（172 行）

数据库连接管理与会话工厂 【当前定位】已正式接线的业务库：承载多用户登录与分析历史三张表 （users / session_tokens / analysis_history，见 shared/model.py 与 user_service.py）， 服务启动时由 main.py lifespan 幂等建表。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `get_db_url()` | 获取数据库连接 URL，优先从环境变量读取，本地开发默认使用 SQLite。 |
| 函数 | `get_engine()` | 获取全局唯一的数据库引擎（惰性初始化，首次调用时创建）。 |
| 函数 | `get_sessionmaker()` | 获取全局唯一的会话工厂（惰性初始化，首次调用时创建）。 |
| 函数 | `get_session()` | 获取一个新的数据库会话实例。 |

内部实现（不逐个展开）：`_create_engine_with_retry`。

### `src/storage/database/shared/model.py`（58 行）

业务数据库 ORM 模型定义 三张表支撑「多用户登录 + 各自分析历史」功能： - User：用户账号（PBKDF2 加盐哈希存储口令，绝不存明文） - SessionToken：登录会话令牌（随机 token 落库，重启不丢登录态，支持过期） - AnalysisHistory：分析历史记录（按 user_id 隔离，游客分析不落历史） 表结构由 db.init_tables() 在服务启动时幂等创建（Base.metadata.create_all）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `Base` | 类 |
| 类 | `User` | 用户账号表。 |
| 类 | `SessionToken` | 登录会话令牌表。 |
| 类 | `AnalysisHistory` | 分析历史记录表：一次完整审计分析的结构化摘要，按用户隔离查询。 |

### `src/storage/database/user_service.py`（200 行）

用户认证与分析历史服务层 为「多用户登录 + 各自分析历史」提供全部数据操作，零新增依赖： - 口令安全：PBKDF2-HMAC-SHA256（120k 次迭代）+ 每用户独立随机盐，绝不存明文 - 会话令牌：secrets.token_hex(32) 随机串落库（session_tokens 表）， 服务重启不丢登录态，默认 7 天过期，登出即删除 - 历史隔离：analysis_history 按 user_id 过滤，接口层永远无法跨用户读取 所有函数自管理数据库会话（with get_session()），调用方无需关心事务； 失败路径返回 None / False 或抛出 ValueError（带用户可读信息），不泄露内部细节。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `register_user(username, password)` | 注册新用户。 |
| 函数 | `login_user(username, password)` | 校验口令并签发会话令牌。 |
| 函数 | `logout_user(token)` | 删除会话令牌（幂等）。 |
| 函数 | `resolve_user(token)` | 由会话令牌解析当前用户；令牌缺失/不存在/过期均返回 None。 |
| 函数 | `save_history(user_id, run_id)` | 写入一条分析历史记录（仅登录用户调用；任何异常只记日志不阻断主流程）。 |
| 函数 | `list_history(user_id, limit)` | 按用户查询历史记录（倒序）。 |
| 函数 | `get_history_detail(user_id, record_id)` | 按 id 取单条历史，强制校验归属（他人记录返回 None，与不存在不区分）。 |

内部实现（不逐个展开）：`_hash_password`, `_history_to_dict`。

### `src/storage/memory/memory_saver.py`（183 行）

会话检查点存储器 - 管理 Agent 会话状态持久化 本模块提供 LangGraph Agent 会话的检查点（Checkpoint）管理功能，用于保存和恢复对话状态。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `MemoryManager` | MemoryManager 单例类 - 管理会话检查点（Checkpoint）的创建和获取。 |
| 方法 | `MemoryManager.init_persistent()` | 在事件循环内初始化 AsyncSqliteSaver（持久化检查点）。 |
| 方法 | `MemoryManager.get_checkpointer()` | 获取检查点存储器（同步）。 |
| 方法 | `MemoryManager.aclose()` | 关闭底层 aiosqlite 连接（持久化模式）。 |
| 函数 | `get_memory_saver()` | 获取全局唯一的检查点存储器，供 LangGraph Agent 使用（同步入口）。 |
| 函数 | `init_memory_saver()` | 异步初始化持久化检查点，由 FastAPI lifespan 在启动时 await 调用。 |
| 函数 | `close_memory_saver()` | 关闭检查点存储器资源，由 FastAPI lifespan 在关闭时调用。 |

内部实现（不逐个展开）：`_checkpoint_db_path`, `_shutdown_memory_manager`, `_ensure_manager`。

### `src/storage/s3/s3_storage.py`（408 行）

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `ListFilesResult` | 类 |
| 类 | `S3SyncStorage` | S3兼容存储实现 |
| 方法 | `S3SyncStorage.upload_file()` |  |
| 方法 | `S3SyncStorage.delete_file()` |  |
| 方法 | `S3SyncStorage.file_exists()` |  |
| 方法 | `S3SyncStorage.read_file()` |  |
| 方法 | `S3SyncStorage.list_files()` | 列出对象，支持前缀过滤与分页；返回 keys/is_truncated/next_continuation_token。 |
| 方法 | `S3SyncStorage.generate_presigned_url()` | 通过 S3 Proxy 生成签名 URL。 |
| 方法 | `S3SyncStorage.stream_upload_file()` | 流式上传（文件对象） - fileobj: 任何带有 read() 方法的文件对象（如 open(..., 'rb') 返回的对象、io.BytesIO 等） - file_name: 原始文件名，用于生成唯一 key - content_type: MIME 类型 - bucket: 目标桶；为空时取环境变量或实例默认值 - multipart_chunksize: 分片大小（默认 5MB，以适配代理层限制） - multipart_threshold: 触发分片上传的阈值（默认 5MB） - max_concurrency: 并发分片上传的并发数（默认 1，避免代理层节流影响） - use_threads: 是否启用线程并发（默认 False） 返回：最终写入的对象 key |
| 方法 | `S3SyncStorage.upload_from_url()` | 从 URL 流式下载并上传到 S3 - url: 源文件 URL - bucket: 目标桶；为空时取环境变量或实例默认值 - timeout: HTTP 请求超时时间（秒，默认 30） 返回：最终写入的对象 key |
| 方法 | `S3SyncStorage.trunk_upload_file()` | 流式上传（字节迭代器，显式分片 Multipart Upload） - chunk_iter: 可迭代对象，逐块产生 bytes；每块大小可变（内部累积到 part_size 再上传），最后一块可小于 5MB - file_name: 原始文件名，用于生成唯一 key - content_type: MIME 类型 - bucket: 目标桶；为空时取环境或实例默认值 - part_size: 每个 part 的最小大小（除最后一个）；默认 5MB 返回：最终写入的对象 key |

---

## src/core/

核心契约：结果契约与行业基准契约（数据结构与来源约束）。

### `src/core/benchmark_contract.py`（15 行）

Only attributed, reviewed peer data may be called an industry benchmark.

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `sourced_benchmark_value(spec, industry_entry)` |  |

### `src/core/result_contract.py`（203 行）

Structured result contracts shared by calculations, review and exports.

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `risk_level_label(risk)` | Keep candidate priority visibly provisional without changing its level key. |
| 函数 | `derive_task_status(manifest)` | 按产物清单派生任务级状态（部分完成必须可表达，不得只报成功/失败）。 |
| 类 | `ContractMixin` | 类 |
| 方法 | `ContractMixin.to_dict()` |  |
| 类 | `Fact` | 类 |
| 类 | `Evidence` | 类 |
| 类 | `MetricResult` | 类 |
| 类 | `RiskFinding` | 类 |
| 类 | `ArtifactManifest` | 类 |
| 函数 | `make_fact(field_name, value)` | Create a fact while preserving the original representation. |

内部实现（不逐个展开）：`_json_value`。

---

## src/utils/

通用工具：文件处理、文件名清洗、LLM 辅助、种子数据。

### `src/utils/_build_seed.py`（20 行）

构建完整性种子（由打包流程自动生成，请勿手动修改）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `seed_digest()` | 返回种子拼接后的 SHA-256 摘要，供打包完整性校验使用。 |

### `src/utils/file/file.py`（328 行）

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `File` | 通用文件对象，支持自动类型推断和路径管理 |
| 方法 | `File.set_cache_path(path)` | 设置缓存路径 |
| 方法 | `File.get_cache_path()` | 获取缓存路径（如果文件实际存在） |
| 方法 | `File.is_remote()` | 判断是网络URL还是本地文件 |
| 函数 | `infer_file_category(path_or_url)` | 根据路径或URL后缀判断文件类型 逻辑： 1. |
| 类 | `FileOps` | 类 |
| 方法 | `FileOps.save_to_local(file_obj, filename)` | 将当前文件对象的内容保存到本地路径, 返回本地路径 如果是本地路径，直接返回 |
| 方法 | `FileOps.read_bytes(file_obj)` | 获取文件的原始二进制数据 场景：上传到OSS、保存到本地、传给图像处理库 |
| 方法 | `FileOps.extract_text(file_obj)` | 提取文本内容 场景：RAG、HTML解析、文档分析 |
| 函数 | `read_docx(cont_stream)` | 使用docx2python按顺序读取内容（docx2python需要文件路径，先保存临时文件） |
| 函数 | `read_ppt(file_input)` |  |

### `src/utils/filename.py`（196 行）

文件名安全清洗工具 - 移除 Windows 非法字符；另提供 company_info 字段别名归一化

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `resolve_company_year(company_info)` | 从 company_info 字典中按别名表解析（公司名, 报告年度）。 |
| 函数 | `sanitize_filename(name, max_len)` | 将任意字符串清洗为 Windows 安全文件名。 |
| 函数 | `to_roman(n)` | 将正整数转为大写罗马数字字符串（1-3999）。 |
| 函数 | `from_roman(s)` | 将大写罗马数字字符串反解为正整数；非标准/空串返回 0。 |
| 函数 | `count_existing_runs(company, year)` | 返回当天该公司已用过的最大运行序号（无既有产物时为 0）。 |
| 函数 | `build_file_prefix(report, run_number)` | 生成统一的文件名前缀：日期_公司名_年份[_罗马数字]。 |

### `src/utils/llm.py`（19 行）

LLM 请求扩展参数辅助 —— 按 provider 决定是否下发私有扩展字段。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `thinking_extra_body()` | 返回适配当前 provider 的 extra_body（非 DeepSeek 返回空 dict）。 |

---

### `src/utils/report_identity.py`（175 行）

报告身份确定性识别模块。背景：综合研判链路完全依赖 LLM 抽取 `company_info`，在 17 万字符原文里经常漏掉封面公司名与报告期，导致产物名退化为「未知公司」、Z/M 模型误报「缺少本期期间」。本模块只做**确定性**识别，不按金额量级或上下文臆测，识别不到就返回空串。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `extract_company_name(text)` | 优先取封面/正文标题行的公司全称（前 8000 字符），退化为全文首个「XX有限公司」主体（取最短匹配避免吞整句）。 |
| 函数 | `extract_stock_code(text)` | 从「股票代码/证券代码」标签后提取 6 位数字。 |
| 函数 | `extract_report_period(text)` | 识别报告期：优先「20XX 年半年度/上半年/中期」，其次「20XX 年度」。 |
| 函数 | `extract_report_year(text)` | 从报告期串中取出 4 位年份。 |
| 函数 | `extract_industry(text)` | 按行业关键词表归一到提示词约定取值域（能源/金融/房地产/医药等），命中越靠前者优先。 |
| 函数 | `extract_audit_opinion(text)` | 识别「未经审计」标记或常见审计意见类型。 |
| 函数 | `extract_report_identity(text)` | 汇总报告身份字段 dict（company_name/stock_code/report_year/period/report_period/industry/audit_opinion），空值字段不输出。 |
| 函数 | `apply_company_info_fallback(company_info, text)` | 将确定性识别结果按别名表补进 `company_info`；**已有非空值优先，绝不覆盖**。供 `_post_process` / `_build_final_report_from_messages` 在生成文件名与 PDF/Excel 之前调用。 |

调用方：`src/agents/agent.py` 的三方辩论复核与 `src/main.py::_build_final_report_from_messages` 均会以「最长一条 HumanMessage（即年报原文）」为输入调用兑底。

---

## src/local_

本地运行支撑：知识库加载与检索（含 TF-IDF 回退）、本地存储、Coze 运行时垫片。

### `src/local_knowledge.py`（242 行）

本地知识库检索 - 基于 ChromaDB 向量语义检索 使用 ChromaDB 向量数据库替代原有的 TF-IDF 文本匹配， 通过 embedding 语义向量化实现更高质量的法规条文和案例检索。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `LocalKnowledgeBase` | 基于 ChromaDB 向量数据库的本地知识库 |
| 方法 | `LocalKnowledgeBase.load()` |  |
| 方法 | `LocalKnowledgeBase.search(query, top_k, min_score)` | 语义检索：通过 ChromaDB 向量相似度匹配，返回最相关的法规条文和案例。 |
| 函数 | `get_knowledge_base()` |  |

### `src/local_shims.py`（377 行）

本地兼容层 - 替代 coze_coding_utils / coze_coding_dev_sdk 的平台绑定部分 在本地 Windows 环境下提供最小可运行的替代实现。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `Context` | 类 |
| 函数 | `normalize_run_id(raw)` | 校验客户端 run_id：合法（强格式）则原样返回，否则服务端重新生成。 |
| 函数 | `new_context(method, headers)` |  |
| 函数 | `default_headers(ctx)` |  |
| 函数 | `setup_logging(log_file, max_bytes, backup_count, log_level, use_json_format, console_output)` |  |
| 类 | `ClassifiedError` | 类 |
| 类 | `ErrorClassifier` | 类 |
| 方法 | `ErrorClassifier.classify(exc, meta)` |  |
| 方法 | `ErrorClassifier.get_error_response(exc, meta)` |  |
| 函数 | `classify_error(exc, meta)` |  |
| 类 | `RunOpt` | 类 |
| 类 | `AgentStreamRunner` | 类 |
| 方法 | `AgentStreamRunner.stream(payload, graph, run_config, ctx)` |  |
| 方法 | `AgentStreamRunner.astream(payload, graph, run_config, ctx, run_opt)` |  |
| 类 | `WorkflowStreamRunner` | 类 |
| 方法 | `WorkflowStreamRunner.stream(payload, graph, run_config, ctx)` |  |
| 方法 | `WorkflowStreamRunner.astream(payload, graph, run_config, ctx, run_opt)` |  |
| 函数 | `agent_stream_handler(payload, ctx, run_id, stream_sse_func, sse_event_func, error_classifier, register_task_func)` |  |
| 函数 | `workflow_stream_handler(payload, ctx, run_id, stream_sse_func, sse_event_func, error_classifier, register_task_func, run_opt)` |  |
| 类 | `graph_helper` | 类 |
| 方法 | `graph_helper.is_agent_proj()` |  |
| 方法 | `graph_helper.get_agent_instance(module_path, ctx)` |  |
| 方法 | `graph_helper.get_graph_instance(module_path)` |  |
| 方法 | `graph_helper.is_dev_env()` |  |
| 方法 | `graph_helper.get_graph_node_func_with_inout(graph, node_id)` |  |
| 函数 | `to_stream_input(client_msg)` |  |
| 函数 | `to_client_message(payload)` |  |
| 类 | `OpenAIChatHandler` | 类 |
| 方法 | `OpenAIChatHandler.handle(payload, ctx)` |  |
| 类 | `LangGraphParser` | 类 |
| 方法 | `LangGraphParser.get_node_metadata(node_id)` |  |
| 函数 | `extract_core_stack()` |  |
| 函数 | `init_run_config(graph, ctx)` |  |
| 函数 | `init_agent_config(graph, ctx)` |  |
| 类 | `cozeloop` | 类 |
| 方法 | `cozeloop.flush()` |  |
| 类 | `AsyncTaskStorageError` | 类 |
| 类 | `async_task_config` | 类 |
| 函数 | `parse_deadline_sec(headers)` |  |
| 函数 | `extract_biz_context(headers)` |  |
| 类 | `AsyncTaskRuntime` | 类 |
| 方法 | `AsyncTaskRuntime.submit(task_id, payload, biz_context, deadline_sec, run_config, ctx)` |  |
| 方法 | `AsyncTaskRuntime.get(task_id)` |  |
| 方法 | `AsyncTaskRuntime.shutdown()` |  |

内部实现（不逐个展开）：`_RequestContextProxy`, `_run_id_log_record_factory`, `_install_run_id_log_record_factory`, `_ErrorCategory`。

### `src/local_storage.py`（108 行）

本地文件存储 - 替代 S3 对象存储 本模块实现本地文件存储功能，将生成的报告文件（PDF/Excel/图表） 保存到项目根目录下的 local_storage/ 子目录，并返回 HTTP 可访问的相对路径。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `ensure_storage_dir()` | 确保本地存储目录存在，不存在则自动创建。 |
| 函数 | `begin_batch(stamp)` | 开启一个新产物批次：重置批次时间戳并返回（格式 YYYYMMDD_HHMMSS，精确到秒）。 |
| 函数 | `current_batch_stamp()` | 返回当前批次时间戳；尚未初始化时自动按当前时间初始化。 |
| 函数 | `reset_batch()` | 清空当前批次时间戳（测试隔离用；下次上传自动重新初始化）。 |
| 函数 | `upload_file_to_storage(local_path, file_name, content_type, expire_seconds)` | 本地存储替代：将文件复制到 local_storage 目录，返回 HTTP 可访问 URL。 |

内部实现（不逐个展开）：`_safe_path`。

---

## src/maintenance.py

运行时资源定期回收：检查点/产物/日志/临时文件四类对象的策略与后台协程。

### `src/maintenance.py`（483 行）

运行时资源定期回收（maintenance）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 类 | `MaintenancePolicy` | 回收策略。 |
| 方法 | `MaintenancePolicy.from_env(cls, project_dir)` |  |
| 方法 | `MaintenancePolicy.resolved_checkpoint_db()` |  |
| 类 | `MaintenanceReport` | 类 |
| 方法 | `MaintenanceReport.to_dict()` |  |
| 方法 | `MaintenanceReport.summary()` |  |
| 函数 | `prune_checkpoints(policy, dry_run)` | 按「最近 N 个线程 + 体积阈值」回收检查点。 |
| 函数 | `prune_artifacts(policy, dry_run)` | 回收 local_storage 下的旧批次目录与旧报告/图表文件。 |
| 函数 | `prune_logs(policy, dry_run)` | 回收 RotatingFileHandler 的旧备份日志（不删除 app.log 本身）。 |
| 函数 | `prune_temp(policy, dry_run)` | 回收项目根目录下过期的 ``.tmp_*`` 临时文件/目录。 |
| 函数 | `run_maintenance(policy, dry_run, sections)` | 执行一次回收，返回结构化报告。 |
| 函数 | `last_report()` |  |
| 函数 | `maintenance_worker(policy)` | 后台周期回收协程；由 FastAPI lifespan 启动，取消即退出。 |

内部实现（不逐个展开）：`_env_bool`, `_env_int`, `_env_float`, `_checkpoint_threads`, `_session_key`, `_safe_rmtree`。

---

## scripts/

工程脚本：打包、验收、评估、离线验证、维护 CLI、样张生成。

### `scripts/acceptance_run.py`（72 行）

P6 端到端验收：驱动 /stream_run 跑一次单模块分析，落盘 SSE 全文与最终报告。

> 本模块以内部实现为主，公开接口见同名类或工具注册表。

### `scripts/check_audit_release.py`（327 行）

单一离线验收入口（方案 §9.3）：机器可读、fail-closed。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `sha256_of(path)` |  |
| 函数 | `git_head(repo)` | 返回当前 commit 与工作区脏文件数（不联网、不 commit/push）。 |
| 函数 | `run_pytest(targets, timeout_seconds)` | 子进程运行 pytest，超时/非零/零收集均按失败处理并返回结构化统计。 |
| 函数 | `run_offline_toolchain(pdf_path, text)` | 跑真实确定性工具链（无 LLM），返回供门禁判定的结构化结果。 |
| 函数 | `check(case, run_pytest_full, pytest_targets, timeout_seconds, pdf_path)` | 执行离线验收，返回机器可读字典。 |
| 函数 | `main(argv)` |  |

内部实现（不逐个展开）：`_guard_env`, `_block_remote_model_imports`, `_parse_count`, `_extract_pdf_text`, `_check_fixture`, `_snapshot_id_of`, `_emit`。

### `scripts/e2e_invariants.py`（162 行）

端到端不变量校验脚本（v5.3GA 收口）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `latest_report(reports_dir, suffix)` |  |
| 函数 | `check(name, ok, detail)` |  |
| 函数 | `main()` |  |

### `scripts/fetch_echarts.py`（60 行）

下载并内置 ECharts 到 src/web/vendor/echarts.min.js 为什么必须本地内置：本系统定位为单机桌面程序，断网场景下不能依赖 CDN， 否则图表区域直接白屏。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `main()` |  |

### `scripts/generate_sample_reports.py`（367 行）

三份 PDF 报告样张生成脚本（打样用，数据自洽版）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `main()` |  |

内部实现（不逐个展开）：`_sample_report`, `_financial_json`, `_disclosure_json`, `_risk_models_json`, `_validation_json`, `_audit_opinion_json`, `_multi_year_input`。

### `scripts/init_knowledge_base.py`（47 行）

初始化知识库 - 将法规文件导入本地知识库（TF-IDF 检索）

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `init_knowledge_base()` |  |

### `scripts/load_env.py`（22 行）

加载项目环境变量脚本 - 本地模式从 .env 文件加载 使用方式: python load_env.py

> 本模块以内部实现为主，公开接口见同名类或工具注册表。

### `scripts/maintenance_cli.py`（60 行）

按期回收命令行工具。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `build_parser()` |  |
| 函数 | `main(argv)` |  |

### `scripts/offline_case.py`（离线验收黄金案例）

离线验收运行器：内嵌**合成**黄金案例（不对应任何真实企业），完整工具链打样；本地真实年报通过 `--case-file` 加载（数据不入库）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `build_ledger(fin, dc, vd, ao, my, company_info=None)` | 台账构造器：仅基于工具输出生成风险条目（来源可追溯）。 |
| 函数 | `main()` |  |

内部实现（不逐个展开）：`_level_of`, `_dim_of`。

### `scripts/pack_source.py`（154 行）

源码提交打包脚本（竞赛用）。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `collect_files(include_skillpacks)` | 按白名单收集待打包的相对路径列表。 |
| 函数 | `main()` |  |

内部实现（不逐个展开）：`_is_skippable`。

### `scripts/real_chain_verify.py`（真实链路验证）

真实链路测试（含 LLM 全流程）：本地真实样本路径由参数/环境变量提供，脚本不含任何公司数据。

| 类型 | 名称 | 说明 |
|---|---|---|
| 函数 | `parse_pdf(path)` | 与 /upload 一致的解析逻辑（pypdf + 20 万字符截断）。 |
| 函数 | `main()` |  |

---

## 附：工具装饰器与调用约定

被 `@tool` 装饰的函数即 LLM 可调用工具。约定如下：

1. **入参为 JSON 字符串**：模型生成的是文本，不是结构化对象，因此工具内部先 `json.loads`，失败返回错误提示而非抛异常；
2. **出参为字符串**：通常返回 JSON 字符串（含 `status`/`data`/`warnings`），便于模型直接阅读与再推理；
3. **失败不抛异常**：工具内部捕获异常并返回 `{"status": "error", ...}`，避免整条链路中断；
4. **结果必须可追溯**：涉及数值的返回都带 `source`/`evidence`/`calculation_version` 等字段，供后处理与导出引用。
