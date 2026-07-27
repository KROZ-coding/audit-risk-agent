"""上市公司年报风险识别智能体

本模块是整个系统的核心入口，负责：
1. 加载 LLM 配置并构建 LangGraph ReAct Agent
2. 注册所有审计工具（财务计算、法规检索、图表生成、报告导出等）
3. 通过 _AgentWrapper 提供兜底导出机制，防止 LLM 遗漏 PDF/Excel 导出调用
4. 实现消息滑动窗口，限制上下文长度避免 token 溢出
5. 多智能体辩论机制：主 Agent 输出后由风险关注方、风险否定方、裁判仲裁人三方辩论复核
"""
import os
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage, ToolMessage, AIMessage
from local_shims import default_headers, request_context, new_context
from storage.memory.memory_saver import get_memory_saver

from tools.pdf_parser import parse_pdf_report
from tools.financial_calculator import calculate_financial_indicators
from tools.knowledge_search import search_regulations
from tools.excel_export import export_excel_report
from tools.pdf_export import export_pdf_report
from tools.multi_year_comparison import compare_multi_year
from tools.data_validator import validate_financial_data
from tools.visualizer import generate_risk_heatmap, generate_radar_chart, generate_trend_chart
from tools.batch_processor import batch_analyze_companies
from tools.disclosure_checker import check_disclosure_compliance
from tools.risk_scorer import calculate_comprehensive_score
from tools.domain_guard import ToolCallOrderViolation, assert_hard_order, check_soft_order
from utils.llm import thinking_extra_body

logger = logging.getLogger(__name__)

# ─── 多智能体辩论机制 ────────────────────────────────────────
# 三个辩论角色的系统提示词，替代原有的单轮 Agent-as-a-Judge 复核

# 风险关注方：专注挖掘和强调目标公司的潜在风险点
ADVOCATE_SYSTEM_PROMPT = """你是风险关注方(Risk Advocate)，一位资深CPA审计师。
你的职责是对风险分析报告进行深度审查，从五大维度（财务错报、关联交易、信披合规、持续经营、监管处罚）挖掘可能被低估或遗漏的风险信号。

分析要求：
1. 逐条审查风险台账中的每条风险，评估其风险等级是否合理
2. 指出可能被低估的风险（如应为重大但被标为一般的条目）
3. 补充可能遗漏的风险维度或异常信号
4. 评估证据链完整性（数据→分析→法规→案例→建议是否闭环）
5. 特别关注跨维度风险传导链（如应收激增→现金流恶化→持续经营风险）

输出格式（严格遵循）：
【关注要点】：逐条列出你认为被低估、遗漏或需补充的风险点（每条注明风险ID或新发现）
【等级异议】：你认为风险等级偏低的条目及理由
【补充建议】：建议追加的审计程序或需额外关注的指标

注意：使用审慎措辞，不得作出定性结论。你的角色是最大化风险关注度。"""

# 风险否定方：逐条反驳关注方的论点，提供合理解释和反面证据
SKEPTIC_SYSTEM_PROMPT = """你是风险否定方(Risk Skeptic)，一位经验丰富的企业财务顾问。
你的职责是对风险关注方提出的每条质疑进行客观反驳，提供合理的商业解释、行业对比数据和历史先例。

反驳要求：
1. 逐条回应风险关注方的每个质疑点
2. 提供合理的商业逻辑解释（如行业周期、战略调整、会计政策变更的正当理由）
3. 引用同行业可比公司的类似数据表现作为反证
4. 区分真正的风险信号与正常的业务波动
5. 对证据不充分的指控提出质疑

输出格式（严格遵循）：
【逐条回应】：针对关注方的每个质疑点，给出反驳理由和依据
【合理性质疑】：指出关注方可能过度解读或误解的数据
【行业参照】：提供同行业可比公司的正常波动范围作为参照

注意：使用客观、理性的措辞。你的角色是防止过度风险化，确保结论公允。"""

# 裁判仲裁人：综合双方意见做最终裁决
ARBITER_SYSTEM_PROMPT = """你是审计仲裁人(Arbiter)，一位拥有20年经验的资深审计合伙人（CPA）。
你的职责是听取风险关注方和风险否定方的辩论意见后，对每条争议风险进行独立仲裁，给出最终裁定。

仲裁要求：
1. 对双方有争议的风险点逐条裁定，明确采纳哪方观点或取折中结论
2. 最终确认每条风险的等级（重大/重要/一般），并说明裁定理由
3. 对双方都认同的风险，确认其等级并强调核心证据
4. 识别辩论中双方都未充分覆盖的遗漏风险
5. 给出整体仲裁结论

输出格式（严格遵循）：
【逐条裁定】：对每条争议风险的最终裁定（含采纳方、裁定等级、理由）
【确认风险】：双方一致认同的风险条目确认
【遗漏补充】：双方辩论中均未充分覆盖的风险点（如有）
【仲裁结论】：通过 / 需补充 / 需重新分析
【建议】：建议追加的审计程序或关注的额外指标
【裁定JSON】：机器可读裁定，单行输出，格式严格为：
{"adjustments":[{"risk_id":"R001","final_level":"重大","reason":"裁定理由"}],"verdict":"通过"}
其中 final_level 只能取 重大/重要/一般；仅列出需调整等级或需补充备注的条目，
无任何调整时 adjustments 输出空数组；本行将被系统解析并回写风险台账，务必保持合法 JSON。

注意：使用审慎、中立的措辞，不得作出定性结论。你的裁定须基于证据和审计准则。"""

# 复核功能开关，可通过环境变量 REVIEW_ENABLED=false 关闭
REVIEW_ENABLED = os.getenv("REVIEW_ENABLED", "true").lower() != "false"

# ─── 唯一 LLM 依赖的最小可靠性恢复配置（有限次重试 + 指数退避）───
# 系统仅依赖单一 LLM（DeepSeek/OpenAI 协议）。为避免瞬时故障（限流 / 超时 /
# 网络抖动）直接导致分析失败，对 LLM 调用增加有限次重试与指数退避；重试全部
# 耗尽后不静默吞掉，交由调用方按『降级可见』原则向用户提示。
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))          # 首次失败后的额外重试次数（有限）
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.5"))  # 指数退避基准秒数

# LLM 配置文件路径（相对于工作目录）
LLM_CONFIG = "config/agent_llm_config.json"
# 消息滑动窗口最大条数，超出后丢弃最早的消息以控制 token 消耗
MAX_MESSAGES = 40

# 风险维度中英文映射：中文 → 英文标识符（用于 JSON 结构化输出）
CN_TO_EN_DIM = {
    "财务错报风险": "financial_misstatement",
    "关联交易风险": "related_party",
    "信息披露合规风险": "disclosure_compliance",
    "持续经营风险": "going_concern",
    "监管处罚类高风险": "regulatory_penalty",
}
# 反向映射：英文 → 中文
EN_TO_CN_DIM = {v: k for k, v in CN_TO_EN_DIM.items()}


def _windowed_messages(old, new):
    """消息滑动窗口（配对安全版）：合并新旧消息后裁剪至约 MAX_MESSAGES 条，防止上下文超长。

    关键约束：OpenAI/DeepSeek 协议要求带 tool_calls 的 AIMessage 后必须紧跟
    tool_call_id 匹配的 ToolMessage，否则返回 400。因此不能用无脑切片 [-N:]，
    否则可能：① 切掉某条 AIMessage 对应的 ToolMessage（tool_calls 无应答）；
    ② 保留孤儿 ToolMessage（前面的 AIMessage 被切走）。

    裁剪策略：
    1. 先合并出完整消息列表；
    2. 从列表尾部保留最后 MAX_MESSAGES 条作为初始候选窗口；
    3. 向前扩展窗口起点，跳过开头的孤儿 ToolMessage —— 即窗口不能以 ToolMessage
       开头（它的 tool_calls 母消息已在窗口外），逐条前移直到窗口首条不是 ToolMessage；
    4. 若窗口首条是带 tool_calls 的 AIMessage，其应答 ToolMessage 均在其后，天然完整。
    这样保证窗口内不出现「孤儿 ToolMessage」和「无应答 tool_calls」。
    """
    merged = add_messages(old, new)  # type: ignore
    if len(merged) <= MAX_MESSAGES:
        return merged

    # 初始窗口起点：保留最后 MAX_MESSAGES 条
    start = len(merged) - MAX_MESSAGES
    # 向后收缩：窗口不能以孤儿 ToolMessage 开头（其 tool_calls 母消息在窗口外）
    while start < len(merged) and isinstance(merged[start], ToolMessage):
        start += 1
    return merged[start:]


def _merge_tool_ledger(old, new):
    """工具链记账台账 reducer：与受滑窗裁剪的 messages 解耦，累积完整链路记录。

    背景：messages 会被 _windowed_messages 裁剪以限制 LLM 上下文长度，早期的
    validate/calculate 等工具消息可能被丢弃。若顺序门禁与综合评分兜底直接读取
    裁剪后的 messages，会出现「误判工具顺序违规」或「读到空结果」。本台账在裁剪前
    由 post_model_hook 增量累积，永不裁剪，作为这些记账用途的权威来源。

    台账结构：
    - seq:     按调用先后排列的工具名序列（完整，不受滑窗影响）
    - results: {工具名: 该工具最近一次结果内容}
    - seen:    已记账的 ToolMessage id 集合（去重，避免重复累积）

    Args:
        old: 既有台账（首次为 None）
        new: 增量更新，形如 {"entries": [[msg_id, tool_name, content], ...]}

    Returns:
        合并后的台账字典。
    """
    base = {"seq": [], "results": {}, "seen": []}
    if isinstance(old, dict):
        base["seq"] = list(old.get("seq", []))
        base["results"] = dict(old.get("results", {}))
        base["seen"] = list(old.get("seen", []))
    if not new:
        return base
    seen = set(base["seen"])
    for entry in new.get("entries", []):
        mid, name, content = entry
        if mid in seen:
            continue
        seen.add(mid)
        base["seen"].append(mid)
        base["seq"].append(name)
        base["results"][name] = content
    return base


def _accumulate_tool_ledger(state):
    """post_model_hook：在消息被后续滑窗裁剪掉之前，将新出现的工具结果累积进台账。

    该钩子在每次模型节点执行后运行，此时最近一批工具的 ToolMessage 仍处于窗口内，
    据此增量记账即可在裁剪前捕获完整链路。仅返回 tool_ledger 增量，绝不修改 messages，
    因此不影响滑窗对 LLM 上下文的限长作用。

    Args:
        state: 当前图状态（含 messages 与 tool_ledger）。

    Returns:
        {"tool_ledger": {"entries": [...]}}；无新增时返回 {} 表示不更新状态。
    """
    ledger = state.get("tool_ledger") or {}
    seen = set(ledger.get("seen", []))
    entries = []
    for m in state.get("messages", []):
        if isinstance(m, ToolMessage):
            mid = getattr(m, "id", None) or f"pos-{id(m)}"
            if mid not in seen:
                entries.append([mid, m.name, m.content])
                seen.add(mid)
    if not entries:
        return {}
    return {"tool_ledger": {"entries": entries}}


class AgentState(MessagesState):
    """Agent 状态定义，继承自 LangGraph 的 MessagesState。

    Attributes:
        messages: 对话消息列表，通过 _windowed_messages 实现自动滑动窗口
        remaining_steps: 剩余工具调用步数上限，防止死循环
        tool_ledger: 工具链记账台账，与滑窗解耦、永不裁剪，供顺序门禁与综合评分兜底读取
    """
    messages: Annotated[list[AnyMessage], _windowed_messages]
    remaining_steps: int = 25
    tool_ledger: Annotated[dict, _merge_tool_ledger]


def _normalize_dims(parsed: dict) -> dict:
    """归一化风险维度名称：将 LLM 可能输出的中文维度名统一转换为英文标识符。

    处理逻辑：
    - 遍历 risk_details 列表中的每条风险
    - 先尝试精确匹配中文→英文映射表
    - 再尝试忽略大小写的英文→英文匹配
    - 都无法匹配则保持原值不变

    Args:
        parsed: 已解析的风险台账字典，包含 risk_details 列表

    Returns:
        归一化后的风险台账字典（原地修改）
    """
    en_to_cn_lower = {k.lower(): v for k, v in EN_TO_CN_DIM.items()}
    for rd in parsed.get("risk_details") or []:
        dim = rd.get("dimension", "").strip()
        rd["dimension"] = CN_TO_EN_DIM.get(dim, en_to_cn_lower.get(dim.lower(), dim))
    return parsed


def _extract_risk_json(text: str) -> str | None:
    """从 AI 回复文本中提取风险台账 JSON（大括号配对法）。

    算法步骤：
    1. 定位 "company_info" 关键字的位置
    2. 向前查找最近的外层 { 作为 JSON 起始位置
    3. 从该 { 开始逐字符扫描，用 depth 计数器匹配大括号层级
    4. 当 depth 回到 0 时，找到 JSON 结束位置
    5. 尝试 json.loads 解析，验证必须包含 company_info 和 risk_details 字段

    Args:
        text: AI 回复的原始文本

    Returns:
        成功时返回 JSON 字符串，失败时返回 None
    """
    # 第一步：定位关键字，快速判断文本中是否包含风险台账
    start = text.find('"company_info"')
    if start < 0:
        return None
    # 第二步：向前查找外层大括号起始位置
    brace_start = text.rfind('{', 0, start)
    if brace_start < 0:
        return None
    # 第三步：大括号深度计数，找到配对的结束位置
    # 维护字符串状态（in_string / escape），仅在字符串外部计数括号，
    # 防止风险描述文本内含有 { } 时（如 "evidence":"货币资金{注:含受限}..."）
    # 导致 depth 提前失衡、抠出语法残缺的子串
    depth, end = 0, -1
    in_string = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if in_string:
            # 字符串内：处理转义，遇未转义的 " 退出字符串
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        # 字符串外：正常计数括号
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    # 第四步：尝试解析并验证必要字段存在性
    try:
        parsed = json.loads(text[brace_start:end])
        return text[brace_start:end] if ("company_info" in parsed and "risk_details" in parsed) else None
    except json.JSONDecodeError:
        return None


# 仲裁回写允许的风险等级白名单（非法等级一律丢弃，防止 LLM 输出污染台账）
_ARBITER_VALID_LEVELS = {"重大", "重要", "一般"}


def _extract_arbiter_adjustments(debate_text: str) -> list:
    """从辩论结果文本中提取【裁定JSON】的 adjustments 列表。

    解析策略：定位最后一个【裁定JSON】标记，从其后首个 { 开始用
    字符串感知的大括号配对扫描截取 JSON（与 _extract_risk_json 同款状态机，
    防裁定理由内含括号导致截断）。任一环节失败均返回空列表（不阻断主流程）。

    Args:
        debate_text: _run_debate 返回的三方拼接文本

    Returns:
        adjustments 列表（每项含 risk_id / final_level / reason），失败时为 []
    """
    marker = "【裁定JSON】"
    idx = debate_text.rfind(marker)
    if idx < 0:
        return []
    brace_start = debate_text.find("{", idx)
    if brace_start < 0:
        return []
    # 字符串感知的大括号配对扫描
    depth, end = 0, -1
    in_string = False
    escape = False
    for i in range(brace_start, len(debate_text)):
        ch = debate_text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return []
    try:
        parsed = json.loads(debate_text[brace_start:end])
        adjustments = parsed.get("adjustments", [])
        return adjustments if isinstance(adjustments, list) else []
    except json.JSONDecodeError:
        return []


def _apply_arbiter_adjustments(risk_json: str, adjustments: list):
    """按仲裁裁定回写风险台账的等级与备注（白名单校验 + 可追溯）。

    回写规则（防结论污染）：
    - 仅接受 risk_id 精确匹配到台账内已有条目的裁定，不新增条目；
    - final_level 必须在 _ARBITER_VALID_LEVELS 白名单内，否则丢弃；
    - 等级发生变更时保留 original_level 字段可追溯，同时写入 arbiter_note 裁定理由。

    Args:
        risk_json: 风险台账 JSON 字符串
        adjustments: _extract_arbiter_adjustments 解析出的裁定列表

    Returns:
        (回写后的 risk_json, 实际应用的裁定条数)；无有效裁定时原样返回。
    """
    try:
        parsed = json.loads(risk_json)
    except (json.JSONDecodeError, TypeError):
        return risk_json, 0
    details = parsed.get("risk_details") or []
    by_id = {str(rd.get("risk_id", "")): rd for rd in details if isinstance(rd, dict)}
    applied = 0
    for adj in adjustments:
        if not isinstance(adj, dict):
            continue
        rd = by_id.get(str(adj.get("risk_id", "")))
        level = adj.get("final_level", "")
        if rd is None or level not in _ARBITER_VALID_LEVELS:
            continue
        if rd.get("level") != level:
            rd["original_level"] = rd.get("level", "")
            rd["level"] = level
        rd["arbiter_note"] = str(adj.get("reason", ""))[:500]
        applied += 1
    if applied:
        return json.dumps(parsed, ensure_ascii=False), applied
    return risk_json, 0


def _invoke_llm_with_retry(llm, messages, *, label="LLM"):
    """对单次 ChatOpenAI 调用增加有限次重试 + 指数退避。

    为唯一的 LLM 依赖提供最小可靠性恢复路径：瞬时故障时按指数退避重试至多
    LLM_MAX_RETRIES 次；若重试全部失败，向上抛出最后一次异常，由调用方按
    『降级可见』原则处理（绝不静默返回空结果，避免『看似成功实则缺失』的断裂）。

    Args:
        llm: ChatOpenAI 实例
        messages: 传给 llm.invoke 的消息列表
        label: 日志标识，便于定位是哪一步调用失败

    Returns:
        llm.invoke 的返回值

    Raises:
        Exception: 有限次重试全部失败后抛出的最后一次异常
    """
    last_err = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            return llm.invoke(messages)
        except Exception as e:  # noqa: BLE001 - 瞬时故障统一重试，最终失败向上抛出
            last_err = e
            if attempt >= LLM_MAX_RETRIES:
                break
            delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"{label} 调用失败（第 {attempt + 1}/{LLM_MAX_RETRIES + 1} 次），"
                f"{delay:.1f}s 后重试: {e}"
            )
            time.sleep(delay)
    raise last_err


class _AgentWrapper:
    """Agent 包装器：在 Agent 执行完成后兜底检查 PDF/Excel 导出是否被调用。

    设计原因：LLM 有时可能遗漏调用 export_pdf_report 或 export_excel_report，
    导致用户拿不到完整的审计报告文件。本包装器在后处理阶段自动检测并补调。

    工作流程：
    1. 透传 invoke/ainvoke 调用给底层 agent
    2. 检查 ToolMessage 记录，判断 PDF/Excel 是否已导出
    3. 若遗漏，从最后一条 AI 消息中提取风险 JSON，手动调用导出工具
    4. 将导出链接追加到 AI 回复末尾
    """

    def __init__(self, agent):
        """初始化包装器。

        Args:
            agent: LangGraph create_react_agent 返回的原始 agent 实例
        """
        self._agent = agent

    async def ainvoke(self, payload, config=None, **kw):
        """异步调用 agent 并执行兜底后处理"""
        return self._post_process(await self._agent.ainvoke(payload, config=config, **kw))

    def invoke(self, payload, config=None, **kw):
        """同步调用 agent 并执行兜底后处理"""
        return self._post_process(self._agent.invoke(payload, config=config, **kw))

    @staticmethod
    def _iter_chunk_messages(chunk):
        """从 astream 的单个 chunk 中提取 LangChain 消息对象。

        astream 在 updates 模式下逐节点产出增量消息，chunk 形如
        {"agent": {"messages": [...]}} 或 {"tools": {"messages": [...]}}。
        """
        if not isinstance(chunk, dict):
            return
        if "messages" in chunk and isinstance(chunk["messages"], list):
            for m in chunk["messages"]:
                yield m
        for node_key in ("agent", "tools"):
            node = chunk.get(node_key)
            if isinstance(node, dict):
                for m in node.get("messages", []):
                    yield m

    async def astream(self, payload, config=None, **kw):
        """流式透传底层 agent，并在流结束后补跑 _post_process。

        设计背景：底层 create_react_agent 的 astream 不经过 _AgentWrapper 的
        后处理（_post_process 仅在 invoke/ainvoke 中调用），导致 /stream_run 丢失
        兜底 PDF/Excel 导出、多智能体辩论复核、综合评分。此处在流结束后
        手动补齐，保证 /stream_run 与 /run 行为一致。

        工作流程：
        1. 逐 chunk 透传给上游（前端据此追踪真实工具进度）；
        2. 同时按消息 id 去重累积消息对象（保留顺序）；
        3. 流结束后对累积消息跑 _post_process（就地修改最后一条 AIMessage）；
        4. 额外 yield 一个带 __post_processed__ 标记的 chunk，携带后处理后的消息，
           供上游用于构建含辩论/导出/评分的最终报告。
        """
        seen = {}
        order = []
        async for chunk in self._agent.astream(payload, config=config, **kw):
            yield chunk
            for m in self._iter_chunk_messages(chunk):
                mid = getattr(m, "id", None) or id(m)
                if mid not in seen:
                    seen[mid] = m
                    order.append(mid)
        messages = [seen[i] for i in order]
        if not messages:
            return
        try:
            # 先推一个「后处理开始」标记：辩论三轮 + 兜底导出可能耗时 30-60s，
            # 上游据此更新进度文案，避免前端进度条长时间静止让用户以为卡死
            yield {"__post_processing__": True}
            self._post_process({"messages": messages})
            # messages 已被 _post_process 就地补充（最后 AIMessage 含导出链接+辩论+评分）
            yield {"__post_processed__": True, "messages": messages}
        except ToolCallOrderViolation:
            # 顺序强制门禁 fail-closed：不吞掉异常，向上抛出以清晰错误中断流式响应
            raise
        except Exception as e:
            logger.warning(f"流式后处理失败（不影响主报告）: {e}")

    def _post_process(self, result):
        """兜底后处理：检测并补调遗漏的导出工具，并执行多智能体辩论复核。

        处理步骤：
        1. 扫描所有 ToolMessage，收集已被调用的工具名称集合
        2. 判断 export_pdf_report / export_excel_report 是否在已调用集合中
        3. 从最后一条 AIMessage 中提取风险台账 JSON
        4. 对提取的 JSON 执行维度归一化后调用遗漏的导出工具
        5. 将导出结果链接追加到 AI 消息内容末尾
        6. 若 REVIEW_ENABLED 开启，执行多智能体辩论复核并追加结果

        Args:
            result: agent 返回的结果字典，包含 messages 列表

        Returns:
            处理后的结果字典
        """
        messages = result.get("messages", []) if result else []
        if not messages:
            return result

        # 第一步：从「不受滑窗裁剪的 tool_ledger」与「（可能已被裁剪的）messages」
        # 两个来源合并出工具链记账，供顺序门禁与综合评分兜底读取。
        called_seq, tool_results, called = self._gather_tool_bookkeeping(result, messages)

        # ─── 领域约束分级门禁：硬约束 fail-closed（含系统代跑补救），软约束降级可见警告 ───
        # 硬约束「先校验后计算」：LLM 漏调 validate 时不再直接炸掉全场（用户白等），
        # 而是系统用 calculate 的同一份入参代跑校验：通过→附提示继续；
        # 校验不通过或无法代跑→仍 fail-closed（底线：绝不在未校验数据上出报告）。
        # 其余链路次序（disclosure/search/score/export）属推荐顺序非数据依赖，
        # 乱序仅在报告末尾附可见提示。
        # 采用 tool_ledger 的完整序列而非裁剪后的 messages，避免早期消息被丢弃后误判。
        soft_order_warn = self._enforce_tool_call_order(called_seq, messages)

        need_pdf = "export_pdf_report" not in called
        need_excel = "export_excel_report" not in called

        # 第二步：取最后一条 AI 消息
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None or not last_ai.content:
            return result

        # 第三步：从 AI 回复中提取风险台账 JSON
        risk_json = _extract_risk_json(str(last_ai.content))
        if not risk_json:
            # 无法提取风险 JSON 时，若导出工具也未被调用则直接返回
            if not need_pdf and not need_excel:
                # 早退路径同样要保证软约束顺序提示可见（降级可见原则）
                if soft_order_warn:
                    if isinstance(last_ai.content, str):
                        last_ai.content += soft_order_warn
                    else:
                        last_ai.content.append(soft_order_warn)
                return result
            # 否则尝试用 AI 文本作为兜底输入
            risk_json = json.dumps({"company_info": {}, "risk_details": [], "overall_assessment": str(last_ai.content)[:2000]}, ensure_ascii=False)

        # 第四步：归一化风险维度名称
        try:
            risk_json = json.dumps(_normalize_dims(json.loads(risk_json)), ensure_ascii=False)
        except Exception:
            pass

        # ─── 第五步：多智能体辩论机制（前置于导出兜底，替代原有的 Agent-as-a-Judge 复核）───
        # 辩论仲裁的【裁定JSON】会回写 risk_details 的等级/备注（白名单校验，保留
        # original_level 可追溯），因此必须先于兜底导出执行，使 PDF/Excel 反映仲裁后
        # 的最终等级，让辩论对结构化结论产生实质影响而非仅附录文本。
        # （若 LLM 已自行调用过导出工具，已落盘文件不受回写影响，仲裁意见仍随回复展示）
        # Flash 快速模式：若用户消息中包含“快速模式”关键词则跳过辩论
        skip_debate = any(
            "快速模式" in str(m.content)
            for m in messages if hasattr(m, 'content') and isinstance(m.content, str)
        )
        review_block = None
        if REVIEW_ENABLED and not skip_debate:
            debate_result = self._run_debate(risk_json)
            if debate_result:
                # 仲裁裁定回写：解析【裁定JSON】并按白名单规则修正风险等级/备注
                adjustments = _extract_arbiter_adjustments(debate_result)
                if adjustments:
                    risk_json, applied = _apply_arbiter_adjustments(risk_json, adjustments)
                    if applied:
                        logger.info(f"仲裁裁定已回写风险台账：{applied} 条等级/备注调整")
                review_block = f"\n\n---\n### 🔍 审计合伙人复核意见\n{debate_result}"
            else:
                # 降级可见原则：多智能体复核失败不再静默——除 _run_debate 内写日志外，
                # 还须在报告中追加醒目提示，避免用户误以为结论已通过复核环节
                # （与 PDF/Excel/综合评分兜底失败的可见降级保持一致）。
                review_block = (
                    "\n\n---\n### 🔍 审计合伙人复核意见\n"
                    "⚠️ 多智能体复核未能完成，本次结论未经过复核环节，请结合人工判断审慎采信（详见服务日志）。"
                )

        # 第六步：并行兜底缺失产物——图表（热力图/雷达图）+ PDF/Excel 导出，
        # 均使用仲裁回写后的台账。图表已从 LLM 链路摘除（每张图省一次 LLM 往返
        # 约 5-15s），与导出同为本地任务且互无依赖，线程并行后总耗时≈最慢单项。
        # 快速模式（与跳辩论同一信号）保持零图表定位，不补图。
        # 降级可见原则：任一产物失败都在报告文本中附醒目提示，不静默吞掉。
        need_heatmap = "generate_risk_heatmap" not in called and not skip_debate
        need_radar = "generate_radar_chart" not in called and not skip_debate

        def _backfill_chart(tool_obj, label):
            """图表兜底：成功返回展示文本（提 URL），失败降级为可见警告。"""
            try:
                raw = str(tool_obj.invoke({"risk_report_json": risk_json}))
                url = raw
                try:
                    url = json.loads(raw).get("download_url") or raw
                except Exception:
                    pass
                return f"\n\n📊 {label}: {url}"
            except Exception as e:
                logger.warning(f"{label}兜底生成失败: {e}")
                return f"\n\n⚠️ {label}生成失败，本次报告未附该图表（详见服务日志）。"

        def _backfill_export(tool_obj, prefix, fail_text):
            """导出兜底：保持原有文案与失败提示格式。"""
            try:
                return f"\n\n{prefix}{tool_obj.invoke({'risk_report_json': risk_json})}"
            except Exception as e:
                logger.warning(f"{prefix.strip()}兜底导出失败: {e}")
                return fail_text

        jobs = {}
        if need_heatmap:
            jobs["heatmap"] = lambda: _backfill_chart(generate_risk_heatmap, "风险热力图")
        if need_radar:
            jobs["radar"] = lambda: _backfill_chart(generate_radar_chart, "财务雷达图")
        if need_pdf:
            jobs["pdf"] = lambda: _backfill_export(
                export_pdf_report, "📎 PDF报告: ",
                "\n\n⚠️ PDF报告导出失败，本次结论未生成可下载的 PDF 报告，请重试或联系维护者（详见服务日志）。")
        if need_excel:
            jobs["excel"] = lambda: _backfill_export(
                export_excel_report, "📊 Excel底稿: ",
                "\n\n⚠️ Excel底稿导出失败，本次结论未生成可下载的 Excel 底稿，请重试或联系维护者（详见服务日志）。")

        links = ""
        if jobs:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = {name: pool.submit(fn) for name, fn in jobs.items()}
                outputs = {name: fut.result() for name, fut in futures.items()}
            # 展示顺序固定：图表在前、报告文件在后（与阅读动线一致）
            for key in ("heatmap", "radar", "pdf", "excel"):
                links += outputs.get(key, "")

        if links:
            if isinstance(last_ai.content, str):
                last_ai.content += links
            else:
                last_ai.content.append(links)

        # 第七步：将辩论复核意见（或失败提示）追加到回复末尾（保持链接在前的展示顺序）
        if review_block:
            if isinstance(last_ai.content, str):
                last_ai.content += review_block
            else:
                last_ai.content.append(review_block)

        # ─── 综合评分兜底：确保输出中始终包含 comprehensive_score JSON ───
        try:
            from tools.risk_scorer import calculate_comprehensive_score
            # 从工具链台账读取财务分析 / 校验 / 披露检查结果（台账不受滑窗裁剪，
            # 即使早期工具消息已被窗口丢弃，仍能读到真实结果而非空值）
            financial_result = tool_results.get("calculate_financial_indicators", "{}")
            validation_result = tool_results.get("validate_financial_data", "{}")
            disclosure_result = tool_results.get("check_disclosure_compliance", "{}")
            score_output = calculate_comprehensive_score.invoke({
                "financial_analysis_json": financial_result,
                "disclosure_check_json": disclosure_result,
                "validation_json": validation_result,
            })
            score_text = f"\n\n<!--COMPREHENSIVE_SCORE-->\n{score_output}"
            if isinstance(last_ai.content, str):
                last_ai.content += score_text
            else:
                last_ai.content.append(score_text)
        except Exception as e:
            logger.warning(f"综合评分兜底失败: {e}")
            # 综合评分直接决定审计结论，兜底失败同样须对用户可见，防止静默降级。
            fail_text = "\n\n⚠️ 综合风险评分生成失败，本次结论未包含综合评分，请结合人工判断复核并重试（详见服务日志）。"
            if isinstance(last_ai.content, str):
                last_ai.content += fail_text
            else:
                last_ai.content.append(fail_text)

        # 软约束顺序提示（如有）：附在报告末尾供人工复核（降级可见，不中断产出）
        if soft_order_warn:
            if isinstance(last_ai.content, str):
                last_ai.content += soft_order_warn
            else:
                last_ai.content.append(soft_order_warn)

        return result

    @staticmethod
    def _gather_tool_bookkeeping(result, messages):
        """合并工具链记账：优先采用不受滑窗裁剪的 tool_ledger，辅以 messages 扫描。

        解耦背景：invoke / ainvoke 路径下 result["messages"] 为滑窗裁剪后的子集，
        早期的 validate / calculate 等工具消息可能已被丢弃；tool_ledger 在裁剪前由
        post_model_hook 完整累积，因此作为顺序与结果的权威来源。astream 路径下
        无 tool_ledger，但累积的 messages 为全量，回退 messages 扫描仍可独立成立。

        Args:
            result: agent 返回的状态字典（可能含 tool_ledger）。
            messages: 待扫描的消息列表（invoke 下为窗口子集，astream 下为全量）。

        Returns:
            (called_seq, results, called)
            - called_seq: 按调用先后排列的工具名序列（优先取台账的完整序列）
            - results:    {工具名: 最近一次结果内容}（台账为主，messages 补充）
            - called:     已调用工具名集合（台账与 messages 的并集）
        """
        ledger = (result or {}).get("tool_ledger") or {}
        ledger_seq = list(ledger.get("seq", []))
        ledger_results = dict(ledger.get("results", {}))

        msg_seq = [m.name for m in messages if isinstance(m, ToolMessage)]
        msg_results = {m.name: m.content for m in messages if isinstance(m, ToolMessage)}

        # 顺序：台账为裁剪前完整记录，优先采用；缺失时回退 messages 扫描
        called_seq = ledger_seq if ledger_seq else msg_seq
        # 结果：以 messages 为底，再用台账覆盖（台账保存全历史最近值，权威度更高）
        results = dict(msg_results)
        results.update(ledger_results)
        # 已调用集合：两来源并集（避免早期导出工具被裁剪后被误判为未调用而重复导出）
        called = set(msg_seq) | set(ledger_seq)
        return called_seq, results, called

    @staticmethod
    def _try_backfill_validation(messages):
        """硬约束补救：LLM 漏调 validate 时，系统用 calculate 的同一份入参代跑校验。

        从消息中的 AIMessage.tool_calls 找到 calculate_financial_indicators 的
        financial_data_json 入参，直接喂给 validate_financial_data（该工具对缺失
        字段容错：缺什么跳过什么，不会误报）。

        Returns:
            (ok, note)
            - ok=True：代跑校验完成且未发现勾稽不平，note 为附入报告的提示文本；
            - ok=False：拿不到入参 / 校验未通过 / 代跑异常，note 为原因（调用方应维持 fail-closed）。
        """
        calc_args = None
        for m in messages:
            for tc in (getattr(m, "tool_calls", None) or []):
                if tc.get("name") == "calculate_financial_indicators":
                    calc_args = (tc.get("args") or {}).get("financial_data_json")
        if not calc_args:
            return False, "无法从对话中取得计算工具的财务数据入参，无法代跑校验"
        try:
            raw = validate_financial_data.invoke({"financial_data_json": calc_args})
            parsed = json.loads(raw)
            dv = parsed.get("data_validation", {})
            if dv.get("failed_checks", 0) > 0:
                return False, f"系统代跑校验发现 {dv['failed_checks']} 项勾稽不平（{dv.get('validation_result')}）"
            return True, (
                "\n\n⚠️ 数据校验补救提示：本次分析中模型未主动执行财务数据校验，"
                f"系统已用同一份数据代为补跑（{dv.get('passed_checks', 0)} 项通过、"
                f"{dv.get('skipped_checks', 0)} 项因字段缺失跳过，未发现勾稽不平），"
                "分析继续。请结合人工判断复核数据可靠性。"
            )
        except Exception as e:  # noqa: BLE001 — 代跑失败必须回退 fail-closed，不得静默放行
            return False, f"代跑校验异常：{e}"

    def _enforce_tool_call_order(self, called_seq, messages=None):
        """分级门禁：硬约束 fail-closed（含代跑补救），软约束返回可见警告文本。

        - 硬约束（先 validate 后 calculate）：违反时先尝试系统代跑校验——
          代跑通过则降级为可见提示继续分析（用户不白等）；代跑不可行或
          校验不通过才抛 ToolCallOrderViolation（底线：数据必须经过校验）；
        - 软约束（链路其余相对次序）：违反不中断，返回警告文本由调用方
          附入报告（降级可见）。

        Args:
            called_seq: 按实际调用先后顺序排列的工具名称序列。
            messages: 完整消息列表（代跑补救时用于提取 calculate 入参）。

        Returns:
            软约束违规/补救提示文本；完全合规时返回 None。

        Raises:
            ToolCallOrderViolation: 硬约束违反且代跑补救失败时。
        """
        backfill_note = None
        try:
            hard_msg = assert_hard_order(called_seq)
            logger.info(f"硬约束门禁通过: {hard_msg}")
        except ToolCallOrderViolation as violation:
            ok, note = self._try_backfill_validation(messages or [])
            if not ok:
                logger.error(f"硬约束违规且代跑补救失败（{note}），维持 fail-closed")
                raise violation
            logger.warning(f"硬约束违规已由系统代跑校验补救：{note.strip()}")
            backfill_note = note
        soft_ok, soft_msg = check_soft_order(called_seq)
        if soft_ok:
            return backfill_note
        logger.warning(f"工具链软约束顺序提示（不中断）: {soft_msg}")
        soft_text = (
            "\n\n⚠️ 工具链顺序提示：本次分析的部分工具调用次序与推荐链路不一致"
            f"（{soft_msg}）。结论与报告已完整产出，请结合人工判断复核该环节。"
        )
        return (backfill_note or "") + soft_text if backfill_note else soft_text

    def _run_debate(self, risk_json: str) -> str | None:
        """多智能体辩论机制：风险关注方 -> 风险否定方 -> 裁判仲裁。

        三步辩论流程：
        1. 风险关注方(Risk Advocate)：审查风险台账，挖掘被低估或遗漏的风险
        2. 风险否定方(Risk Skeptic)：逐条反驳关注方的质疑，提供合理商业解释
        3. 裁判仲裁人(Arbiter)：综合双方意见，对每条争议风险做最终裁定

        Args:
            risk_json: 风险台账 JSON 字符串

        Returns:
            辩论结果文本（含三方意见），失败时返回 None
        """
        try:
            from langchain_core.messages import SystemMessage, HumanMessage

            # 创建辩论专用 LLM 实例（较低温度确保严谨；max_tokens 限 1200 压缩三轮串行耗时，
            # 辩论意见重在结论而非篇幅）
            # max_retries=0：重试/退避统一由 _invoke_llm_with_retry 控制，避免与 SDK 内置重试叠加
            debate_llm = ChatOpenAI(
                model=os.getenv("REVIEW_MODEL", "deepseek-v4-flash"),
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
                temperature=0.2,
                max_tokens=1200,
                timeout=90,   # flash 单轮通常 <15s，90s 足以覆盖慢轮
                max_retries=0,
                extra_body=thinking_extra_body(),  # 仅 DeepSeek 禁用思考模式（辩论提速），其它 provider 不下发私有字段
            )

            # Step 1: 风险关注方分析
            logger.info("辩论 Step 1/3: 风险关注方分析中...")
            advocate_response = _invoke_llm_with_retry(debate_llm, [
                SystemMessage(content=ADVOCATE_SYSTEM_PROMPT),
                HumanMessage(content=f"请审查以下审计风险分析报告的风险台账。\n\n风险台账数据：\n{risk_json[:6000]}"),
            ], label="辩论·风险关注方")
            advocate_text = advocate_response.content or ""
            logger.info(f"风险关注方分析完成（{len(advocate_text)}字）")

            # Step 2: 风险否定方反驳
            logger.info("辩论 Step 2/3: 风险否定方反驳中...")
            skeptic_response = _invoke_llm_with_retry(debate_llm, [
                SystemMessage(content=SKEPTIC_SYSTEM_PROMPT),
                HumanMessage(content=(
                    f"请对风险关注方的意见进行逐条反驳。\n\n"
                    f"风险台账数据：\n{risk_json[:4000]}\n\n"
                    f"风险关注方意见：\n{advocate_text[:3000]}"
                )),
            ], label="辩论·风险否定方")
            skeptic_text = skeptic_response.content or ""
            logger.info(f"风险否定方反驳完成（{len(skeptic_text)}字）")

            # Step 3: 裁判仲裁
            logger.info("辩论 Step 3/3: 裁判仲裁中...")
            arbiter_response = _invoke_llm_with_retry(debate_llm, [
                SystemMessage(content=ARBITER_SYSTEM_PROMPT),
                HumanMessage(content=(
                    f"请综合双方辩论意见，对争议风险进行逐条裁定。\n\n"
                    f"风险关注方意见：\n{advocate_text[:2500]}\n\n"
                    f"风险否定方意见：\n{skeptic_text[:2500]}"
                )),
            ], label="辩论·裁判仲裁")
            arbiter_text = arbiter_response.content or ""
            logger.info(f"裁判仲裁完成（{len(arbiter_text)}字）")

            # 组装辩论结果，使用结构化标记便于前端三段式渲染
            return (
                f"【风险关注方】\n{advocate_text}\n\n"
                f"【风险否定方】\n{skeptic_text}\n\n"
                f"【裁判仲裁】\n{arbiter_text}"
            )
        except Exception as e:
            logger.warning(f"辩论机制失败（不影响主报告）: {e}")
            return None

    def __getattr__(self, name):
        """透传未定义属性到底层 agent，保持接口兼容"""
        return getattr(self._agent, name)


def build_agent(ctx=None, model_override=None):
    """构建审计风险分析 Agent。

    完整流程：
    1. 读取 agent_llm_config.json 获取 LLM 参数（模型名、温度、top_p 等）
    2. 从环境变量获取 API Key 和 Base URL
    3. 创建 ChatOpenAI 实例（兼容 DeepSeek API）
    4. 注册全部 13 个审计工具
    5. 通过 LangGraph create_react_agent 构建 ReAct 模式 Agent
    6. 用 _AgentWrapper 包装以提供兜底导出机制

    Args:
        ctx: 可选的请求上下文对象，用于传递请求头等信息
        model_override: 可选的主分析模型名覆盖（快速模式传入 FAST_MODEL；缺省时用 config
            中的 model，当前全链路 deepseek-v4-flash。普通/快速模式的差异现在主要
            在链路（图表+辩论复核 vs 精简）而非模型）

    Returns:
        _AgentWrapper 包装后的 agent 实例
    """
    # 从环境变量或默认路径获取工作目录
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), "..", ".."))
    config_path = os.path.join(workspace_path, LLM_CONFIG)

    # 加载 LLM 配置参数
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    # 从环境变量获取 API 密钥和服务地址
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")

    # 创建 LLM 实例（ChatOpenAI 兼容 DeepSeek 等 OpenAI 协议的服务）
    # max_retries：对唯一 LLM 依赖的最小可靠性恢复——主 Agent 的模型调用发生在
    # LangGraph 图内部，无法逐次包裹，故采用 SDK 内置的有限次重试 + 指数退避应对
    # 瞬时故障；重试全部耗尽后异常会沿 ainvoke/invoke 上抛，由 main.py 以可见错误
    # 呈现给用户（SSE error / HTTP 错误），不静默。
    # thinking disabled：DeepSeek v4 默认开思考模式，会：① 要求历史 assistant 消息
    # 原样回传 reasoning_content（预处理注入的合成轨迹无该字段 → 400）；
    # ② 每轮额外生成大量 reasoning token 拖慢响应。审计思维链已由 sp 的结构化
    # reasoning_chain 承担，模型内部思考属重复劳动，统一禁用。
    llm = ChatOpenAI(
        model=model_override or cfg['config'].get("model", "deepseek-v4-pro"),
        api_key=api_key,
        base_url=base_url,
        temperature=cfg['config'].get('temperature', 0.3),
        top_p=cfg['config'].get('top_p', 0.9),
        max_tokens=cfg['config'].get('max_completion_tokens', 32768),
        streaming=True,
        timeout=cfg['config'].get('timeout', 600),
        max_retries=LLM_MAX_RETRIES,
        default_headers=default_headers(ctx) if ctx else {},
        extra_body=thinking_extra_body(),
    )

    # 注册全部审计工具列表（顺序不影响执行，Agent 自主决定调用顺序）
    tools = [
        parse_pdf_report,              # PDF 年报解析
        calculate_financial_indicators, # 财务指标计算
        search_regulations,            # 法规知识库检索
        compare_multi_year,            # 多年数据对比分析
        export_excel_report,           # Excel 审计底稿导出
        export_pdf_report,             # PDF 风险报告导出
        validate_financial_data,       # 财务数据一致性校验
        generate_risk_heatmap,         # 风险热力图生成
        generate_radar_chart,          # 财务雷达图生成
        generate_trend_chart,          # 趋势折线图生成
        batch_analyze_companies,       # 多公司批量分析
        check_disclosure_compliance,   # 信息披露规范性检查
        calculate_comprehensive_score, # 综合风险评分
    ]

    # 使用 LangGraph 的 create_react_agent 构建 ReAct 模式智能体
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=cfg.get("sp"),           # 系统提示词（System Prompt）
        checkpointer=get_memory_saver(), # 会话记忆存储
        state_schema=AgentState,         # 自定义状态 schema
        post_model_hook=_accumulate_tool_ledger,  # 裁剪前累积工具链台账，与滑窗解耦
    )

    # 包装为 _AgentWrapper 以提供兜底导出能力
    return _AgentWrapper(agent)
