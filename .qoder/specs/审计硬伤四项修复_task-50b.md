# 审计硬伤与输出缺陷修复

## 根因诊断（已核实）

| 硬伤 | 根因 | 落点 |
|------|------|------|
| 一、R003 算术幻觉（83993-45755-116 算成 47911，捏造 9789 差异） | 未分配利润勾稽实际已通过，但 D 补丁（agent.py `_drop_stale_validation_risks` L675-717）只认"校验项：xxx"字面引用，LLM 自编数字的伪风险匹配不到 | agent.py 后处理 |
| 二、存贷双高/应收-现金流背离被"央企背景"轻视 | financial_calculator 已产出红旗告警（L451 存贷双高、L357 应收背离），但无锚定保护，仲裁可随意降级 | agent.py 仲裁回写 + ARBITER_SYSTEM_PROMPT |
| 三、正文 9/100 vs 实际 16.5 罗生门 | **深挖确认**：`_SCORE_SNAPSHOT_PAT`（pdf_export.py L764-768）要求数字后必须跟"分"字，LLM 写"综合风险评分 9/100（低风险）"的 `/100` 格式完全不匹配——L 补丁在 PDF 层与消息层（agent.py L1326-1329）双双失效，这才是旧分残留的真正根因 | pdf_export.py 正则 |
| 四、信披 100 分 vs 仲裁新增 R005 打脸 | 仲裁新增信披维度条目后，前文"合规评分 100 分无缺失"表述无一致性检查 | agent.py 后处理 |
| 五、仲裁以"绝对值低于行业基准"掩盖 +67% 趋势异常 | ARBITER_SYSTEM_PROMPT（L154-188）无"趋势优先于绝对值"约束 | agent.py 提示词常量 |
| 六（新发现）、热力图/雷达图反映仲裁前等级 | LLM 在主运行阶段（仲裁前）调用图表工具；兜底仅在未调用时补生成（agent.py L1516-1518），仲裁改级后图表颜色与最终等级表矛盾 | agent.py 后处理 |
| 七（新发现）、辩论金额幻觉无防伪 | `_score_mismatch_warning`（L411-430）只校验"评分XX分"引用，辩论中的金额（如 47,911、402.65亿）不与台账数字指纹比对 | agent.py |
| 八（新发现，低优先级）、公司简介纯 LLM 文本无数据核对 | `_profile_body`（pdf_export.py L940-953）直接渲染 company_profile，与计算指标矛盾时（如"现金流增长"vs 告警"现金流为负"）无提示 | pdf_export.py |

核心策略：不信任 LLM 的算术与裁定，用确定性代码在后处理层建立防线。

## 一、勾稽伪风险拦截（治 R003 类算术幻觉）

`src/agents/agent.py`：扩展 `_drop_stale_validation_risks`（或新增姊妹函数，D 补丁后调用）：

1. 为三个校验项定义科目指纹：未分配利润一致性→(未分配利润, 勾稽|差异|不一致)、现金流勾稽→(现金流, 勾稽|差异)、资产负债表平衡→(资产负债, 平衡|不平)。
2. 当某 check passed=True 时，凡 risk_details 条目的 title/evidence/data_analysis 命中该科目指纹组合（科目词+差异宣称词），且条目数字指纹（复用 `_extract_amount_numbers` L670）与 check 输出的 actual_change/diff 等权威数字无交集 → 判定为 LLM 自编算术伪风险，移入 excluded_items 留痕并记日志。

## 二、系统告警锚定保护（治造假信号被降级）

1. 台账标记：`_post_process` 解析 `tool_results["calculate_financial_indicators"]` 的 alerts，对含红旗词（存贷双高、现金流为负、应收增速显著高于、商誉）的告警，在对应 risk_details 条目写 `system_anchored: true`（按科目指纹匹配，匹配不到则按告警文本新建锚定条目）。
2. 回写保护：`_apply_arbiter_adjustments`（L571+）对 system_anchored 条目降级至"一般"时拒绝（保留最低"重要"级），arbiter_note 追加"系统红旗告警锚定：降级被拦截，原裁定理由存档"。
3. 仲裁提示词：ARBITER_SYSTEM_PROMPT 数据纪律追加两条——红旗信号不得以企业性质/股东背景/行业地位降级，只能以可核验反证降级；同比变动超 30% 的科目不得仅以"绝对值低于行业基准"维持低等级，必须正面回应趋势异常。

## 三、L 补丁正则扩展（治 9/100 罗生门——替换原"新增函数"方案，一针见血）

`src/tools/pdf_export.py` L764-768：`_SCORE_SNAPSHOT_PAT` 数字部分由 `\d+\.?\d*\s*分` 扩展为 `\d+(?:\.\d+)?\s*(?:分|/\s*100)`，同步兼容"9分"与"9/100"两种 LLM 写法。该正则被 PDF 渲染层与消息层（agent.py L1326-1329）共用，改一处即同时根治两个展示层，无需新增替换函数。补 2 个正则单测（/100 格式命中、不误伤"100分满分披露"类表述）。

## 四、信披结论一致性标注（治 100 分 vs R005 打脸）

agent.py `_post_process` 仲裁回写后：若 risk_details 含 dimension 归一为 disclosure_compliance 的条目，而 check_disclosure_compliance 结果为满分/无缺失 → 写入 `disclosure_consistency_note`（"仲裁阶段补充披露类风险 N 条，合规结论以仲裁后口径为准"）；pdf_export 信披章节与 Excel 底稿渲染该注记，网页回复 review_block 前追加一行可见提示。

## 五、图表仲裁后重生（治热力图与最终等级矛盾）

agent.py `_post_process`：仲裁回写 `applied > 0` 且存在 level 实际变更时，忽略"LLM 已调用"标记，用仲裁后 risk_json 强制重新生成热力图与雷达图（复用 `_backfill_chart`，与既有兜底同路径）；新 URL 追加到回复并注明"（仲裁后更新版）"。趋势图不受仲裁影响（数据来自多年对比），不重生。

## 六、辩论金额防伪（治 47,911 类幻觉数字穿透到展示层）

扩展 `_score_mismatch_warning`（或新增 `_amount_mismatch_warning`，在 L1399 同位置调用）：从 risk_json 台账构建数字指纹集合（复用 `_extract_amount_numbers`），扫描辩论三段文本中的金额表述（含亿元/百万元/万元单位换算归一），不在指纹集合中的金额汇总为"以下数字未见于风险台账，属辩论方引述，未经系统核验：…"附在复核意见末尾（不篡改原文，降级可见）。

## 七、测试

1. tests/test_export_formatting.py：`_SCORE_SNAPSHOT_PAT` 命中"9/100（低风险）"并替换、不误伤无关数字。
2. tests/test_debate_review.py：伪勾稽风险拦截（通过校验+自编数字条目被移出）；锚定条目降级拦截；金额防伪警示触发。
3. tests/test_pipeline_integration.py：图表重生触发条件（applied>0 且有 level 变更）；disclosure_consistency_note 触发条件。
4. 回归：`.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/evaluation_report.py`（基线 447 passed）。

## 依赖与顺序

三、五、六、七相互独立；一、二、四均在仲裁回写后、导出前执行，注意与既有补丁（M 补丁剥离、run_number 注入）的顺序兼容；公司简介核对（问题八）列为可选低优先级，本轮不做。

## 已否决的替代方案

- 新增独立的 `_patch_reply_score` 回复替换函数（初版方案）：被否决——根因是共享正则缺 `/100` 形态，扩展现有 `_SCORE_SNAPSHOT_PAT` 改一处治两层，新增函数反而造成两套替换逻辑分叉。
- 通用算术表达式重算器（从 evidence 抠算式重算）：正则解析中文财务叙述脆弱、误伤面大；"校验器是算术唯一权威+科目指纹拦截"更确定。
- 系统告警直接固化为不可辩风险：剥夺辩论机制价值，改为"可辩但不可降穿底线"的锚定保护。
- 仲裁后重跑 check_disclosure_compliance：新增 LLM 调用成本高，注记标注即可消除自相矛盾。
- 公司简介数字核对（问题八）：需建立指标-文本语义映射，收益/复杂度比低，留作后续迭代。