"""综合研判串跑编排（三层任务单元的流水线逻辑）

架构定稿的混合运行模式中，点击「综合研判」会依次串跑三段：
  ① 财务健康度诊断 → ② 合规与经营风险扫描 → ③ 综合研判（交叉验证前两者）

本模块只放**纯逻辑**（阶段定义、阶段载荷构造、摘要提取、交叉验证指令），
不含 SSE 与 Agent 缓存，便于单元测试；实际编排循环在 main.py 的 stream_sse 中，
因为那里才持有 Agent 实例缓存与 SSE 推送能力。

段间传递采用「已完成分析摘要」文本注入，而非共享 LangGraph 状态：
1. 三段分别使用不同的工具子集与提示词，状态隔离更安全；
2. 摘要注入是纯文本，不受滑窗裁剪与 tool_calls 配对约束影响；
3. 任一段失败时可降级继续（把失败说明作为摘要传下去），不中断整条链路。
"""

import json

# 三段流水线定义：(模块标识, 阶段名, 进度起点, 进度终点)
# 进度区间与 P1 计划一致：① 0-35%、② 35-65%、③ 65-100%
SYNTHESIS_STAGES = [
    ("financial", "第一阶段 · 财务健康度诊断", 5, 35),
    ("compliance", "第二阶段 · 合规与经营风险扫描", 35, 65),
    ("synthesis", "第三阶段 · 综合研判与交叉验证", 65, 95),
]

# 各阶段追加给用户消息的任务指令（模块标记由 main.py 的 MODULE_MARKERS 注入）
STAGE_INSTRUCTIONS = {
    "financial": (
        "本阶段只做财务健康度诊断：核实三大报表的准则、合并范围、单位和期间。"
        "复用已完成的财务校验、指标和量化模型结果，缺少结果时再按工具链补充计算。"
        "仅在基准来源明确且行业、期间和计算定义可比时比较行业基准。"
        "Altman Z-Score 与 Beneish M-Score 须先核实适用性和必要因子；"
        "中期（半年度/季度）报告照常出具分值，但必须同时引用结果中的 interim_note/limitation，"
        "标注年度阈值属近似套用、仅作交叉印证、不单独作为风险定级依据；"
        "已有 not_applicable / insufficient_data 结果时保留其限制，"
        "除非取得可追溯的新增输入，不得重复调用或补造数据强行计算。"
        "严格按固定章节结构输出，不要进行披露合规与监管处罚分析（后续阶段会做）。"
    ),
    "compliance": (
        "本阶段只做合规与经营风险扫描：检查信息披露规范性，识别审计意见类型及其对"
        "年报可信度的影响程度，检索相关法规与监管处罚案例，结合行业经营风险特征分析。"
        "严格按固定章节结构输出，不要重复上一阶段的财务指标计算。"
    ),
    "synthesis": (
        "本阶段做综合研判：基于下方两个阶段的已完成结论和核心工具结构化结果，"
        "复用财务校验、指标、量化模型、披露检查和法规检索结果完成交叉验证，"
        "形成最终风险台账与量化评分。缺少综合评分时调用 calculate_comprehensive_score；"
        "只有法规依据仍有具体缺口时才补充 search_regulations。"
        "不要重新计算已有基础指标或重复调用 calculate_risk_models；"
        "模型 not_applicable / insufficient_data 属有效状态，必须原样保留原因，"
        "不得为取得分值重新调用、补造期间或改用另一会计准则。"
        "中期报告下 Z/M 已按年度模型近似套用并给出分值，引用时必须一并写明其 "
        "interim_note/limitation（仅作交叉印证、不单独支撑风险定级）。"
        "图表、PDF 报告与 Excel 底稿由系统依据最终结构化结果统一生成，"
        "本阶段不调用图表或导出工具。"
        "最终风险台账的每条风险必须在 evidence 字段中明确引用前两阶段工具的真实输出："
        "财务错报/持续经营类风险须引用 calculate_financial_indicators 的 alerts 或 "
        "validate_financial_data 的 failed_checks；信息披露合规类风险须引用 "
        "check_disclosure_compliance 的 issues / sections_missing；审计意见相关风险须引用 "
        "identify_audit_opinion 的 opinion_type / credibility_impact。"
        "禁止仅写'存在风险''需关注'等空泛描述；无法对应到具体工具证据的条目，"
        "confidence 不得高于 0.5 并标注待核实。"
    ),
}

# 第三阶段的交叉验证硬性要求（写入用户消息，确保 flash 模型也遵守）
CROSS_VALIDATION_INSTRUCTION = """
【交叉验证硬性要求】
必须输出一个「交叉验证矩阵」表格，逐条列出候选风险，并为每条标注验证状态：
- 相互印证：财务诊断与合规扫描均发现指向同一问题的证据（置信度可上调）
- 存在矛盾：两阶段结论相反（必须说明矛盾点，置信度下调，列为待进一步核实）
- 单方发现：仅一个阶段发现证据（置信度不得高于 0.7）
表格列固定为：风险编号 | 风险描述 | 财务诊断发现 | 合规扫描发现 | 验证状态 | 置信度。
其中「财务诊断发现」「合规扫描发现」两列必须直接引用前两阶段工具输出的具体内容
（如 calculate_financial_indicators 的 alert 文本、validate_financial_data 的 failed_check、
check_disclosure_compliance 的 issue、identify_audit_opinion 的 opinion_type），
不得使用空泛概括。
矩阵之后再输出风险明细（每条含五步思维链）与综合结论。
"""

# 摘要截断长度：过长会挤占第三阶段上下文，过短会丢结论
_SUMMARY_MAX_CHARS = 4000


def summarize_tool_result(content, *, tool_name: str = "") -> str:
    """Serialize a structural projection without cutting JSON or dropping late findings.

    Full source text and repeated fact tables remain in the tool ledger. All computed
    values, unavailable states, checks, alerts and evidence links survive this handoff.
    """
    if isinstance(content, dict):
        result = content
    else:
        try:
            result = json.loads(content)
        except (TypeError, ValueError):
            if tool_name == "search_regulations" and isinstance(content, str):
                result = {"status": "unstructured_text", "regulation_text": content}
            else:
                result = {"status": "invalid_tool_result",
                          "reason": "前序工具结果不是有效 JSON，不能据此认定检查通过或不存在风险"}
    if not isinstance(result, dict):
        result = {"status": "invalid_tool_result", "reason": "前序工具结果顶层须为 JSON 对象"}

    omitted = set()
    repeated_fields = {"facts", "statement_items", "raw_value", "excerpt", "report_text", "source_text"}

    def project(value):
        if isinstance(value, dict):
            projected = {}
            for key, item in value.items():
                if key in repeated_fields:
                    omitted.add(key)
                else:
                    projected[key] = project(item)
            return projected
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    summary = project(result)
    summary["_handoff_summary"] = {
        "kind": "structured_projection",
        "omitted_fields": sorted(omitted),
        "note": "结构化摘要；仅省略重复事实表和原文，完整结果保留于工具台账。"
                "指标数值、状态、限制、检查结果、风险提示及证据编号未按长度截断。",
    }
    try:
        return json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps({
            "status": "invalid_tool_result",
            "reason": "前序工具结果含非法 JSON 值，须核实完整工具台账",
            "_handoff_summary": {"kind": "unavailable"},
        }, ensure_ascii=False)


def extract_stage_summary(messages, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """从某一阶段的结果消息中提取该阶段的分析结论文本。

    取最后一条有正文内容的 AI 消息（即该阶段的最终报告），并截断到 max_chars。
    兼容 LangChain 消息对象与 dict 两种形态。

    Args:
        messages: 阶段执行后的消息列表（agent 返回的 result["messages"]）
        max_chars: 摘要最大字符数，超出则截断并标注

    Returns:
        该阶段的结论文本；无可用内容时返回空串（调用方应据此降级）
    """
    if not messages:
        return ""
    for m in reversed(list(messages)):
        # dict 形态（前端/预处理注入）与对象形态（LangChain）都要兼容
        if isinstance(m, dict):
            content = str(m.get("content", "") or "")
            has_tool_calls = bool(m.get("tool_calls"))
            msg_type = m.get("type", "")
        else:
            content = str(getattr(m, "content", "") or "")
            has_tool_calls = bool(getattr(m, "tool_calls", None))
            msg_type = type(m).__name__.lower()
        # 跳过工具消息与纯工具调用消息，只要最终的文字结论
        if "tool" in str(msg_type).lower() or has_tool_calls:
            continue
        if content.strip():
            if len(content) > max_chars:
                return content[:max_chars] + "\n…（摘要已截断，完整内容见该阶段报告）"
            return content
    return ""


def build_stage_payload(base_payload: dict, module: str, marker: str,
                        prior_summaries: list,
                        prior_tool_results: dict | None = None) -> dict:
    """构造某一阶段的请求载荷。

    做法：复制原始载荷，把最后一条用户消息替换为「模块标记 + 阶段指令 +
    前序阶段摘要 + 前两阶段核心工具结构化结果 + 原始用户内容」的组合，
    使该阶段既能命中模块路由，又能看到前序结论及可追溯的证据。

    Args:
        base_payload: 原始请求载荷（含 messages）
        module: 模块标识（financial / compliance / synthesis）
        marker: 该模块的关键词标记（由 main.MODULE_MARKERS 提供，避免循环依赖）
        prior_summaries: 前序阶段的 (阶段名, 摘要文本) 列表
        prior_tool_results: 前两阶段核心工具的结果字典（工具名 -> 结果字符串），
            仅在 synthesis 阶段注入，供 LLM 引用真实证据。

    Returns:
        新的载荷 dict（不修改入参）
    """
    messages = list((base_payload or {}).get("messages", []))
    if not messages:
        return base_payload

    # 取最后一条用户消息的原始内容（可能含年报全文）
    last = messages[-1]
    original = last.get("content", "") if isinstance(last, dict) else getattr(last, "content", "")

    parts = [marker, STAGE_INSTRUCTIONS.get(module, "")]
    if prior_summaries:
        parts.append("\n【前序阶段已完成的分析结论】")
        for stage_name, summary in prior_summaries:
            if summary:
                parts.append(f"\n—— {stage_name} ——\n{summary}")
            else:
                # 阶段失败也要显式告知，避免第三阶段误以为该维度无风险
                parts.append(f"\n—— {stage_name} ——\n（该阶段未产出有效结论，请在交叉验证中标注为单方发现）")
    if module == "synthesis" and prior_tool_results:
        # 方案 2：把前两段核心工具结构化结果注入第三阶段，让 LLM 直接引用
        parts.append("\n【前两阶段核心工具结构化结果（供交叉验证引用，每条风险须据此给出 evidence）】")
        _CORE_TOOLS = [
            ("calculate_financial_indicators", "财务指标计算结果"),
            ("validate_financial_data", "财务数据校验结果"),
            ("check_disclosure_compliance", "披露合规检查结果"),
            ("identify_audit_opinion", "审计意见识别结果"),
            ("calculate_risk_models", "量化模型结果与适用性限制"),
            ("search_regulations", "已检索法规依据"),
            ("calculate_comprehensive_score", "已有综合评分结果"),
        ]
        for tool_name, label in _CORE_TOOLS:
            content = (prior_tool_results or {}).get(tool_name, "")
            if not content:
                continue
            text = summarize_tool_result(content, tool_name=tool_name)
            parts.append(f"\n--- {tool_name}（{label}，结构化摘要）---\n```json\n{text}\n```")
    if module == "synthesis":
        parts.append(CROSS_VALIDATION_INSTRUCTION)
    parts.append("\n【原始分析请求与数据】\n" + str(original))

    new_last = {"role": "user", "content": "\n".join(p for p in parts if p)}
    return {**(base_payload or {}), "messages": messages[:-1] + [new_last]}
