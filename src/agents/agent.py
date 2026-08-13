"""上市公司年报风险识别智能体

本模块是整个系统的核心入口，负责：
1. 加载 LLM 配置并构建 LangGraph ReAct Agent
2. 注册所有审计工具（财务计算、法规检索、图表生成、报告导出等）
3. 通过 _AgentWrapper 提供兜底导出机制，防止 LLM 遗漏 PDF/Excel 导出调用
4. 实现消息滑动窗口，限制上下文长度避免 token 溢出
5. 多智能体辩论机制：主 Agent 输出后由风险关注方、风险否定方、裁判仲裁人三方辩论复核
"""
import os
import re
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
from utils.filename import count_existing_runs, to_roman, resolve_company_year, sanitize_filename as _sanitize_fn

from tools.pdf_parser import parse_pdf_report
from tools.financial_calculator import calculate_financial_indicators
from tools.knowledge_search import search_regulations
from tools.excel_export import export_excel_report
from tools.pdf_export import export_pdf_report, DIM_ALIASES
from tools.multi_year_comparison import compare_multi_year
from tools.data_validator import validate_financial_data
from tools.visualizer import generate_risk_heatmap, generate_radar_chart, generate_trend_chart
from tools.batch_processor import batch_analyze_companies
from tools.disclosure_checker import check_disclosure_compliance
from tools.risk_scorer import calculate_comprehensive_score
from tools.risk_models import calculate_risk_models
from tools.audit_opinion import identify_audit_opinion
from tools.investment_advisor import investment_advisor
from tools.industry_outlook import industry_outlook
from tools.regulatory_inquiry import search_regulatory_inquiries
from tools.domain_guard import ToolCallOrderViolation, assert_hard_order, check_soft_order
from utils.llm import thinking_extra_body

logger = logging.getLogger(__name__)

# 三个任务模块的工具子集（对应架构定稿的三层任务单元）。
# 混合运行模式：点单模块只跑该模块（工具裁剪后更快）；点综合研判则串跑三段。
# 注：parse_pdf_report 在前两个模块都保留，因为两者都可能直接接收年报文件。
MODULE_TOOLS = {
    # ① 财务健康度诊断：三大报表深度解析 + 多年时序
    "financial": {
        "parse_pdf_report", "validate_financial_data", "calculate_financial_indicators",
        "compare_multi_year", "calculate_risk_models",
        "generate_radar_chart", "generate_trend_chart",
    },
    # ② 合规与经营风险扫描：披露合规 + 审计意见 + 监管处罚检索
    "compliance": {
        "parse_pdf_report", "check_disclosure_compliance", "identify_audit_opinion",
        "search_regulations",
    },
    # ③ 综合研判：交叉验证前两者结论 + 量化评分 + 产物导出
    # 注：export_pdf_report/export_excel_report 已从 LLM 工具集移除——导出收敛为
    # 系统兜底唯一路径（_post_process），LLM 主动调用时无法传全专项入参，
    # 会产出缺失财务/披露/评分章的残缺版报告（实测缺陷）。
    "synthesis": {
        "calculate_comprehensive_score", "calculate_risk_models", "search_regulations",
        "generate_risk_heatmap",
    },
    # ④ 行业风向研判：在线财经新闻搜索 + 法规/行业风险知识库 + 监管问询佐证
    # （行业维度而非公司财务报表维度，不含财务计算/披露检查/导出工具）
    "outlook": {
        "industry_outlook", "search_regulations", "search_regulatory_inquiries",
    },
}

# C 端轻量工具的独立模块子集（与 MODULE_TOOLS 分开：三模块集合被测试断言
# 锁定，且轻量工具语义上不属于审计分析模块）。轻量路径跳过 _post_process
# 兜底（不辩论/不导出/不评分），路由见 main.py 的 LIGHT_MARKERS。
LIGHT_MODULE_TOOLS = {
    # 智能投资参考卡：可从已有分析取数，也可对新上传年报重跑核心链路；
    # 仅给公司名/股票代码时联网查监管问询(search_regulatory_inquiries)与行业新闻(industry_outlook)出有依据的参考卡
    "advisor": {
        "parse_pdf_report", "validate_financial_data", "calculate_financial_indicators",
        "calculate_comprehensive_score", "investment_advisor",
        "search_regulatory_inquiries", "industry_outlook",
    },
    # 行业风向标：新闻/知识库驱动，可补检法规佐证
    "industry": {
        "industry_outlook", "search_regulations",
    },
}

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

数据纪律（最高优先级，不可违背）：
- 禁止虚构或编造任何外部数据（同行公司财务数据、历史油价、市场行情、监管案例细节等）
- 只能引用风险台账中已有的数值；无法核实的信息一律不得使用
- 内部证据锚定：禁止对台账金额做加减乘除或推导衍生比例（如 A+B 的合计、占比、同比增幅）；
  引用的所有绝对金额与比例必须原样出自风险台账的 evidence/title 字段，台账中不存在的数字一律视为编造
- 系统内部状态隔离：禁止将系统内部信息（评分快照、工具调用记录、系统日志、前后端差异等）
  作为发行人风险条目立项——系统自身的 Bug 不是发行人的披露违规（实测缺陷：评分矛盾被误立项）
- 确需提及外部参考时须注明「（未经数据核验）」，不得伪装成已验证事实
- 不得臆造或猜测综合评分、指标数值等系统计算结果（以提供的评分上下文为准）

注意：使用审慎措辞，不得作出定性结论。你的角色是最大化风险关注度。"""

# 风险否定方：逐条反驳关注方的论点，提供合理解释和反面证据
SKEPTIC_SYSTEM_PROMPT = """你是风险否定方(Risk Skeptic)，一位经验丰富的企业财务顾问。
你的职责是对风险关注方提出的每条质疑进行客观反驳，提供合理的商业解释、行业对比数据和历史先例。

反驳要求：
1. 逐条回应风险关注方的每个质疑点
2. 提供合理的商业逻辑解释（如行业周期、战略调整、会计政策变更的正当理由）
3. 基于风险台账数据与商业逻辑提供反证，禁止虚构同行数据、历史数据或市场行情
4. 区分真正的风险信号与正常的业务波动
5. 对证据不充分的指控提出质疑

输出格式（严格遵循）：
【逐条回应】：针对关注方的每个质疑点，给出反驳理由和依据
【合理性质疑】：指出关注方可能过度解读或误解的数据
【行业参照】：如确需行业参照，仅基于台账数据与常识判断，禁止编造具体数字

数据纪律（最高优先级，不可违背）：
- 禁止编造同行公司财务数据、历史油价、账龄结构等任何外部数据
- 无法从风险台账或用户材料中核实的数据一律不得使用
- 内部证据锚定：禁止对台账金额做加减乘除或推导衍生比例；引用的所有绝对金额与比例
  必须原样出自风险台账 evidence/title 字段，台账中不存在的数字一律视为编造
- 系统内部状态隔离：禁止将系统内部信息（评分快照、工具调用记录、系统日志等）
  作为发行人风险条目立项（实测缺陷：评分矛盾被误立项为信披风险）
- 确需提及外部参考时须注明「（未经数据核验）」
- 不得臆造或猜测综合评分、指标数值等系统计算结果（以提供的评分上下文为准）
- 跨期穿透：禁止用当期静态现金流比率（如 OCF/NP）反驳应收账款激增等趋势性风险传导；
  应收激增类结论必须补充经营性应付项目变动、应收票据贴现与回款质量等穿透分析
- 量化模型预警：Altman Z-Score 落入灰色预警区/财务困境区、Beneish M-Score 超阈值等
  量化信号，不得以企业性质/股东背景/行业地位推翻，只能以可核验的模型适用性说明回应
- 期后事项口径：大额期后事项（收购、分红、担保等）不得以「属期后事项」一句否定其
  评估必要性，须给出不属当期风险的客观理由，同一报告内处置口径必须一致

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

数据纪律（最高优先级，不可违背）：
- 仲裁依据仅限双方意见与风险台账数据
- 辩论中出现的未经系统核验的外部数据（同行对比、市场行情等）不得作为裁定依据，
  此类信息一律视为待核实项，须在裁定中标注「外部数据未经核验，待人工确认」
- 内部证据锚定：禁止对台账金额做加减乘除或推导衍生比例（如 A+B 的合计、占比、
  同比增幅）；引用的所有绝对金额与比例必须原样出自风险台账 evidence/title 字段，
  台账中不存在的数字一律视为编造，不得作为裁定依据
- 系统内部状态隔离：禁止将系统内部信息（评分快照、工具调用记录、系统日志等）
  作为发行人风险条目立项——系统自身的 Bug 不是发行人的披露违规（实测缺陷）
- 不得臆造或猜测综合评分、指标数值等系统计算结果（以提供的评分上下文为准）
- 红旗信号锚定：系统计算的红旗信号（存贷双高、经营现金流为负、应收增速显著高于
  营收增速、商誉占比过高等）不得以企业性质/股东背景/行业地位为由降级，
  只能以可核验的具体反证降级
- 趋势优先于绝对值：同比变动超过 30% 的科目不得仅以「绝对值低于行业基准」维持
  低风险等级，必须正面回应趋势异常的成因（基数效应、会计政策变更等）
- 评分一致性：裁定结论不得自行宣称「与综合评分一致/相符」——综合评分由系统计算，
  仲裁只陈述等级调整；最终台账含 3 个以上重要级风险时，禁止表述「与低风险评分相符」
  类结论（5 个重要级红旗对应的整体风险绝不可能是低风险）
- 跨期穿透：禁止用当期静态现金流比率（如 OCF/NP）反驳应收账款激增等趋势性风险传导；
  应收激增类结论必须补充经营性应付项目变动、应收票据贴现与回款质量等穿透分析
- 期后事项口径统一：大额期后事项（收购、分红、担保等）须逐项回应——单独立项评估
  定价公允性与披露合规，或给出不属当期风险的客观理由；不得以「属期后事项」一句拒绝；
  同一报告内期后事项不得既作当期勾稽解释又不立项，处置口径必须一致
- 量化模型预警：Altman Z-Score 落入灰色预警区/财务困境区、Beneish M-Score 超阈值等
  量化信号，不得以企业性质/股东背景/行业地位推翻，只能以可核验的模型适用性说明
  或专项程序结论回应
- 口径统一裁定：双方引用同一指标但数值不同（含计算口径不同，如计提比例 13.55% 与
  15.7% 并存）时，必须裁定统一口径并写明计算公式，不得放任矛盾进入最终结论
- 证据不足不得强行定级：关键证据缺失（如母公司支持承诺函未获取）的条目，不得维持
  确定性定级表述，应降低置信度（<0.5）并标注待核实

输出格式（严格遵循）：
【逐条裁定】：对每条争议风险的最终裁定（含采纳方、裁定等级、理由）
【确认风险】：双方一致认同的风险条目确认
【遗漏补充】：双方辩论中均未充分覆盖的风险点（如有）
【仲裁结论】：通过 / 需补充 / 需重新分析
【建议】：建议追加的审计程序或关注的额外指标
【裁定JSON】：机器可读裁定，单行输出，格式严格为：
{"adjustments":[{"risk_id":"R001","final_level":"重大","reason":"裁定理由"}],"verdict":"通过"}
其中 final_level 只能取 重大/重要/一般；调整已有条目时仅需 risk_id/final_level/reason；
新增遗漏风险时须额外给出 dimension（五维度之一）/title/evidence/confidence，
risk_id 用新编号（如 R006）；无任何调整时 adjustments 输出空数组；
本行将被系统解析并回写风险台账（含新增条目），务必保持合法 JSON。

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
    """归一化风险维度名称：将 LLM 可能输出的维度名统一转换为英文标识符。

    处理逻辑：
    - 遍历 risk_details 列表中的每条风险
    - 用 DIM_ALIASES（简称/全称/英文大小写变体，与 PDF 导出端共用同一张表）
      归一为规范英文键
    - 无法识别则保持原值不变（导出端 _split_risks 仍会做一次容错匹配）

    Args:
        parsed: 已解析的风险台账字典，包含 risk_details 列表

    Returns:
        归一化后的风险台账字典（原地修改）
    """
    for rd in parsed.get("risk_details") or []:
        dim = rd.get("dimension", "").strip()
        rd["dimension"] = DIM_ALIASES.get(dim, DIM_ALIASES.get(dim.lower(), dim))
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


def _score_mismatch_warning(debate_text: str, score_context: str) -> str:
    """辩论评分引用防伪：校验复核意见中的评分引用与系统评分一致。

    AI 辩论可能把其他公司/历史版本的评分张冠李戴（实测幻觉：引用不存在的
    72 分并据此生成信披风险）。本函数从 score_context 解析系统评分，扫描
    debate_text 中「综合评分/评分 XX分」类引用，收集不一致值并返回警示文本；
    一致或无法解析时返回空串（不篡改 LLM 原文，仅降级可见警示）。
    """
    try:
        m = re.search(r"(\d+(?:\.\d+)?)\s*分", score_context or "")
        if not m:
            return ""
        sys_score = float(m.group(1))
        refs = set()
        for rm in re.finditer(
                r"(?:综合评分|综合风险评分|系统综合评分|评分)\s*(\d+(?:\.\d+)?)\s*分",
                debate_text or ""):
            try:
                v = float(rm.group(1))
            except ValueError:
                continue
            if abs(v - sys_score) > 1e-9:
                refs.add(f"{v:g}分")
        if not refs:
            return ""
        return (f"\n\n⚠️ 复核意见中包含与系统评分不一致的评分引用"
                f"（{'、'.join(sorted(refs))}），请以系统评分 {sys_score:g}分 为准"
                f"（AI 辩论可能产生幻觉，详见服务日志）。")
    except Exception as e:
        logger.warning(f"辩论评分引用防伪校验失败: {e}")
        return ""


# 金额单位 → 基准值换算因子（元基准；长单位在前防短单位截断匹配，含「亿/万」简写形态）
_AMOUNT_UNITS = (
    ("百万元", 1e6), ("千万元", 1e7), ("亿元", 1e8), ("万元", 1e4), ("千元", 1e3),
    ("亿", 1e8), ("万", 1e4), ("元", 1.0),
)
# 金额单位正则片段（与 _AMOUNT_UNITS 顺序一致）
_AMOUNT_UNIT_RE = r"(百万元|千万元|亿元|万元|千元|亿|万|元)"


def _amount_mismatch_warning(debate_text: str, risk_json: str) -> str:
    """辩论金额/比例引用防伪：扫描辩论文本中的金额与比例表述，与风险台账指纹比对。

    LLM 辩论可能自编金额（实测：47,911、402.65亿 等未见台账数字穿透到复核意见）。
    本函数从 risk_json 台账构建数字指纹（裸数字 + 带单位金额按亿元/百万元/万元
    归一为基准值），扫描 debate_text 中「数字+单位」金额、带千分位分隔的裸数字
    与带小数的百分比，不在指纹集合中的金额/比例汇总为警示文本（不篡改 LLM
    原文，仅降级可见警示）。

    50d：单位换算容差匹配——同额不同口径（95,094百万元 vs 951亿元 差 0.06%、
    2,438百万元 vs 24亿元 差 1.6%）不得误报为「未见于台账」（实测 18:37 版
    警告本身有误）；百分比口径防伪——辩论自算比例（如 15.7%）未见于台账时
    警示「仲裁须裁定统一口径」。
    """
    try:
        ledger = json.loads(risk_json)
    except Exception:
        return ""
    if not isinstance(ledger, dict):
        return ""
    ledger_text = json.dumps(ledger, ensure_ascii=False)
    raw_nums = _extract_amount_numbers(ledger_text)
    # 台账带单位金额归一为基准值集合（元基准）
    base_vals = set()
    for m in re.finditer(r"(\d[\d,]*\.?\d*)\s*" + _AMOUNT_UNIT_RE, ledger_text):
        try:
            factor = dict(_AMOUNT_UNITS)[m.group(2)]
            base_vals.add(float(m.group(1).replace(",", "")) * factor)
        except (ValueError, KeyError):
            continue
    # 台账百分比指纹（仅含小数的百分比参与比对，整数百分比多为通用阈值不误伤）
    ledger_pcts = {float(m.group(1)) for m in re.finditer(r"(\d+\.\d+)%", ledger_text)}

    def _close_enough(v: float, tol: float = 0.02) -> bool:
        """基准值集合容差匹配：相对误差 ≤ tol 视为同一金额（同额不同口径）。
        零值特判：精确相等视为命中，其余避免除零。"""
        for b in base_vals:
            if v == b:
                return True
            denom = max(abs(v), abs(b))
            if denom == 0:
                continue
            if abs(v - b) / denom <= tol:
                return True
        return False

    offenders = set()
    for m in re.finditer(r"(\d[\d,]*\.?\d*)\s*" + _AMOUNT_UNIT_RE, debate_text or ""):
        num, unit = m.group(1), m.group(2)
        raw = num.replace(",", "")
        if raw in raw_nums:
            continue
        try:
            base = float(raw) * dict(_AMOUNT_UNITS)[unit]
        except (ValueError, KeyError):
            continue
        if not _close_enough(base):
            offenders.add(f"{num}{unit}")
    # 带千分位分隔的裸数字（如 47,911、1,536.68）属金额表述：先精确比对裸数字
    # 指纹，再按元口径容差比对，最后仅接受亿/百万口径的精确数值一致假设
    # （1,536.68 vs 153,668百万=1,536.68亿；防歧义数字被宽松假设误吞）
    for m in re.finditer(r"\d{1,3}(?:,\d{3})+(\.\d+)?", debate_text or ""):
        raw = m.group(0).replace(",", "")
        if raw in raw_nums:
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if (_close_enough(v)
                or any(v == b / 1e8 or v == b / 1e6 for b in base_vals)):
            continue
        offenders.add(m.group(0))
    # 50d：百分比口径防伪——辩论自算比例未见于台账时警示（实测 15.7% vs 13.55%）；
    # 容差 0.05 个百分点（同额舍入 67.18% vs 67.2% 不误报）
    pct_offenders = sorted({
        m.group(0) for m in re.finditer(r"(\d+\.\d+)%", debate_text or "")
        if not any(abs(float(m.group(1)) - lp) <= 0.05 for lp in ledger_pcts)
    })
    warns = ""
    if offenders:
        warns += (f"\n\n⚠️ 复核意见中包含未见于风险台账的金额引用（{'、'.join(sorted(offenders)[:8])}），"
                  "属辩论方引述，未经系统核验，请以风险台账证据为准（详见服务日志）。")
    if pct_offenders:
        warns += (f"\n\n⚠️ 复核意见中包含未见于风险台账的比例引用（{'、'.join(pct_offenders[:6])}），"
                  "属辩论方推导，仲裁须裁定统一口径并写明公式后方可采纳。")
    return warns


# 50e：正文评分占位符正则——LLM「不编造」纪律输出的表格行形态
# 「| 综合评分 | 系统兜底计算 | — | 以系统结果为准 |」（标签后是文本而非数字，
# _SCORE_SNAPSHOT_PAT 不匹配）；底线规则/L-sink 算出实际分后回填正文核心位置
# （修复 V4 实测缺陷：读者须滑到几千字后的 JSON/警告里才能找到最终分数）。
# 泛化为整行形态（第二/四列任意内容）：保证多阶段替换幂等——L-sink 先回填
# 工具分后，底线预锁/仲裁后底线仍能将整行重刷为最终分。
_SCORE_PLACEHOLDER_PAT = re.compile(
    r"\|\s*综合(?:风险)?评分\s*\|[^|\n]*\|[\s—\-]*\|[^|\n]*\|?")


def _sync_score_into_message(last_ai, score_dict: dict, floor_note: str = "") -> bool:
    """消息层评分同步（50c 提炼助手，L 下沉与等级底线共用）：

    1. 用 _SCORE_SNAPSHOT_PAT 替换正文中的旧分表述（含表格单元格形态）；
    2. 重建内嵌 ```json 台账块的 comprehensive_score 与 comprehensive_score_snapshot；
    3. 同步用 _apply_score_snapshot 重写内嵌块 overall_assessment 的旧分表述——
       50c 前该字段未同步，仲裁零调整（applied=0）时内嵌块旧分残留。
    4. 50e：回填正文评分占位符（「系统兜底计算 | —」→ 实际分数），floor_note
       提供底线规则上调说明。

    Args:
        last_ai: 待回写的 AIMessage（就地修改 content）
        score_dict: 权威评分字典（score/level/level_key/breakdown 等）
        floor_note: 底线规则上调说明（可选，回填占位符表格第 4 列）

    Returns:
        True 表示 content 发生改动。
    """
    if last_ai is None or not isinstance(getattr(last_ai, "content", None), str):
        return False
    _sc = score_dict.get("score") if isinstance(score_dict, dict) else None
    # 50d：评分未获取（score=None）时同样归一正文旧分与内嵌台账块（快照恒存在、
    # 正文恒被归一契约——否则结论章残留 LLM 旧分与「未获取/无法判定」并存）
    if not isinstance(_sc, (int, float)):
        if not (isinstance(score_dict, dict) and "score" in score_dict and _sc is None):
            return False
    try:
        from tools.pdf_export import _SCORE_SNAPSHOT_PAT, _apply_score_snapshot
        _lv = str(score_dict.get("level", "") or "")
        if isinstance(_sc, (int, float)):
            _repl = f"综合风险评分 {float(_sc):.1f}分（{_lv}）"
        else:
            _repl = "综合风险评分 未获取/无法判定（请人工复核）"
        _new_content = _SCORE_SNAPSHOT_PAT.sub(_repl, last_ai.content)
        # 50e：回填正文评分占位符（保留表格结构，读者在正文核心位置直接看到最终分；
        # 默认说明不含「系统兜底计算」子串，避免替换后残留占位关键词）
        if isinstance(_sc, (int, float)):
            _ph_note = floor_note if floor_note else "系统计算结果"
            _ph_repl = f"| 综合评分 | {float(_sc):.1f}分（{_lv}） | — | {_ph_note} |"
            _new_content = _SCORE_PLACEHOLDER_PAT.sub(_ph_repl, _new_content)

        def _rebuild_ledger_block(m):
            try:
                _obj = json.loads(m.group(1))
                if isinstance(_obj, dict):
                    _obj["comprehensive_score"] = {k: score_dict.get(k) for k in
                        ("score", "level", "level_key", "breakdown", "weights",
                         "base_score", "escalation", "escalation_reasons", "summary",
                         "notes") if k in score_dict}
                    _obj["comprehensive_score_snapshot"] = {
                        "score": _sc,
                        "level": _lv,
                    }
                    if isinstance(_obj.get("overall_assessment"), str):
                        _obj["overall_assessment"] = _apply_score_snapshot(
                            _obj["overall_assessment"], _obj)
                    return "```json\n" + json.dumps(
                        _obj, ensure_ascii=False, indent=2) + "\n```"
            except Exception:
                pass
            return m.group(0)
        _new_content = re.sub(
            r"```json\s*(\{.*?\})\s*```", _rebuild_ledger_block,
            _new_content, flags=re.S)
        if _new_content != last_ai.content:
            last_ai.content = _new_content
            return True
    except Exception:
        pass
    return False


def _extract_arbiter_adjustments(debate_text: str):
    """从辩论结果文本中提取【裁定JSON】的 adjustments 列表与 verdict 状态。

    解析策略：定位最后一个【裁定JSON】标记，从其后首个 { 开始用
    字符串感知的大括号配对扫描截取 JSON（与 _extract_risk_json 同款状态机，
    防裁定理由内含括号导致截断）。任一环节失败均返回空列表（不阻断主流程）。

    50d：同步解析 verdict 字段——仲裁结论为「需补充/需重新分析」或缺失时，
    系统须在报告中标明仲裁状态（降级可见，不阻断输出，adjustments 仍应用）。

    Args:
        debate_text: _run_debate 返回的三方拼接文本

    Returns:
        (adjustments, verdict)：adjustments 列表（每项含 risk_id / final_level /
        reason），verdict 字符串（缺失/解析失败为空串）；失败时为 ([], "")
    """
    marker = "【裁定JSON】"
    idx = debate_text.rfind(marker)
    if idx < 0:
        return [], ""
    brace_start = debate_text.find("{", idx)
    if brace_start < 0:
        return [], ""
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
        return [], ""
    try:
        parsed = json.loads(debate_text[brace_start:end])
        adjustments = parsed.get("adjustments", [])
        verdict = str(parsed.get("verdict", "") or "").strip()
        return (adjustments if isinstance(adjustments, list) else []), verdict
    except json.JSONDecodeError:
        return [], ""


def _fallback_judgment_rewrite(old_detail: str, old_level: str, level: str, reason: str) -> str:
    """模板兜底重写 [风险判定]：剔除旧等级措辞，替换为仲裁后新等级与裁定理由。

    无 LLM 或 LLM 重写失败时，保证结构化等级（level 字段）与推理结论恒一致，
    杜绝「表头重要、结论一般」的左右互搏（实测缺陷）。
    """
    new_detail = old_detail.strip()
    # 剔除旧等级短语（兼容「，等级X」「等级X」等形态）
    if old_level:
        for pat in (f"，等级{old_level}", f"，{old_level}", f"等级{old_level}"):
            if pat in new_detail:
                new_detail = new_detail.replace(pat, "")
    new_detail = new_detail.rstrip("。，；; ")
    suffix = f"经多智能体仲裁调整为{level}"
    if reason:
        suffix += f"（裁定理由：{reason}）"
    return f"{new_detail}，{suffix}"


def _rewrite_risk_judgment(rd: dict, level: str, reason: str, llm=None) -> None:
    """按仲裁后的新等级重写风险条目的推理思维链（[风险判定] 步骤）。

    背景：仲裁回写仅改 level 字段，LLM 生成的 reasoning_chain 仍保留旧等级措辞，
    导致底稿「表头重要、[风险判定] 等级一般」自相矛盾（实测缺陷）。

    策略：
    - 优先 LLM 单轮重写 [风险判定] detail（保留前 4 步推理原文，仅重写结论步），
      输出直接作为新 detail；
    - LLM 不可用/调用失败时模板兜底：剔除旧等级短语并追加「经多智能体仲裁调整为X
      （裁定理由：…）」，保证结论与结构化等级恒一致，绝不静默失败。
    """
    chain = rd.get("reasoning_chain")
    if not isinstance(chain, list) or not chain:
        return
    judge = next((s for s in chain if isinstance(s, dict) and s.get("step") == "风险判定"), None)
    if not judge:
        return
    old_detail = str(judge.get("detail", "") or "").strip()
    if not old_detail:
        return
    old_level = str(rd.get("original_level", "") or "").strip()
    new_detail = _fallback_judgment_rewrite(old_detail, old_level, level, reason)
    if llm is not None:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            chain_preview = json.dumps(chain, ensure_ascii=False)[:1200]
            resp = _invoke_llm_with_retry(llm, [
                SystemMessage(content=(
                    "你是审计底稿编辑器。风险条目经多智能体辩论仲裁后风险等级已变更，"
                    "请仅重写推理思维链中【风险判定】步骤的 detail 文本，使结论与新等级一致。"
                    "要求：其他步骤原文不动；只输出新的 detail 文本本身（不超过 100 字），"
                    "不要任何前缀、引号或 JSON 标记。")),
                HumanMessage(content=(
                    f"风险条目：{rd.get('title', '')}（维度：{rd.get('dimension', '')}）。\n"
                    f"原推理思维链：{chain_preview}\n"
                    f"仲裁后等级：{level}\n"
                    f"裁定理由：{reason or '未提供'}")),
            ], label="仲裁·推理链重写")
            text = str(getattr(resp, "content", "") or "").strip()
            if text:
                new_detail = text[:200]
        except Exception as e:
            logger.warning(f"推理链重写失败，使用模板兜底: {e}")
    judge["detail"] = new_detail
    # 兜底同步：其余步骤若残留旧等级措辞（如 [指标异常] 里的「等级一般」），一并替换
    if old_level:
        for s in chain:
            if isinstance(s, dict) and s.get("step") != "风险判定":
                d = str(s.get("detail", "") or "")
                if old_level in d:
                    s["detail"] = d.replace(f"等级{old_level}", f"等级{level}")


def _apply_arbiter_adjustments(risk_json: str, adjustments: list, llm=None):
    """按仲裁裁定回写风险台账的等级/备注，并支持仲裁新增条目（白名单校验 + 可追溯）。

    回写规则（防结论污染）：
    - 已有 risk_id 的裁定：调整 level（须在 _ARBITER_VALID_LEVELS 白名单）并写 arbiter_note，
      等级变更时保留 original_level 可追溯；
    - 新 risk_id 的裁定（仲裁新增遗漏风险，打通辩论-台账管道）：须含非空 title 与
      dimension 且 level 在白名单，否则丢弃（防 LLM 幻觉新增假风险进底稿），
      新增条目标注 source="仲裁新增" 可追溯。

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
        level = adj.get("final_level", "")
        if level not in _ARBITER_VALID_LEVELS:
            continue
        rid = str(adj.get("risk_id", ""))
        rd = by_id.get(rid)
        if rd is not None:
            # 已有条目：调整等级/备注
            # 50b：系统红旗告警锚定保护——system_anchored 条目不得被仲裁降级穿底至
            # 「一般」（红旗信号只能以可核验反证降级，不能以企业性质/背景话术降级），
            # 拦截时保留原等级并留痕存档原裁定理由。
            if (rd.get("system_anchored") and level == "一般"
                    and rd.get("level") != "一般"):
                logger.info(f"锚定降级拦截：{rid} 为系统红旗锚定条目，拒绝降级至一般")
                _reason = str(adj.get("reason", "") or "")
                rd["arbiter_note"] = (
                    f"{_reason[:400]}；系统红旗告警锚定：降级被拦截，原裁定理由存档"
                    if _reason else "系统红旗告警锚定：降级被拦截，原裁定理由存档")
                applied += 1
                continue
            if rd.get("level") != level:
                # 50d：锚定留痕归档——若 original_level 已由锚定强制提升写入，
                # 仲裁再改级时归档到 anchor_original_level，避免留痕被覆盖
                if rd.get("original_level"):
                    rd["anchor_original_level"] = rd.get("original_level")
                rd["original_level"] = rd.get("level", "")
                rd["level"] = level
                # P1: 等级变更必须同步重写推理思维链的 [风险判定]（表头等级与推理
                # 结论互搏的根因是只改 level 不重写 reasoning_chain——实测缺陷）
                _rewrite_risk_judgment(rd, level, str(adj.get("reason", "") or ""), llm=llm)
            rd["arbiter_note"] = str(adj.get("reason", ""))[:500]
            applied += 1
        else:
            # 仲裁新增遗漏风险：须含非空 title 与 dimension，否则丢弃防幻觉
            title = str(adj.get("title", "") or "").strip()
            dimension = str(adj.get("dimension", "") or "").strip()
            if not title or not dimension:
                continue
            new_rid = rid or f"A{len(details) + 1:03d}"
            if new_rid in by_id:
                continue
            # P1: 仲裁新增条目 title 去矛盾化——"未纳入风险台账"等原文措辞与条目
            # 已纳入台账的事实矛盾，替换为"已评估"视角（实测缺陷：R006 title 自打脸）
            for pat, repl in (("未纳入风险台账", "已纳入台账评估"),
                               ("未纳入评估", "已纳入评估"),
                               ("未列入", "已列入"),
                               ("应纳入", "已纳入")):
                if pat in title:
                    title = title.replace(pat, repl)
            conf = adj.get("confidence")
            # J 补丁：仲裁新增条目维度归一——LLM 裁定可能用非标准维度名（实测 v28：
            # "资产质量"），不过 DIM_ALIASES 则子集过滤/Excel 标签/维度统计全部失配
            # （财务健康报告漏 R005 坏账准备条目）。与 LLM 原始条目走同一归一管道。
            from tools.pdf_export import _norm_dim
            new_item = {
                "risk_id": new_rid,
                "dimension": _norm_dim(dimension),
                "title": title,
                "level": level,
                "confidence": float(conf) if isinstance(conf, (int, float)) else 0.7,
                "evidence": str(adj.get("evidence", "") or "")[:500],
                "arbiter_note": str(adj.get("reason", ""))[:500],
                "source": "仲裁新增",
                # P3: 仲裁新增条目强制补全 audit_suggestion（模板化兜底），
                # 避免底稿明细"审计程序建议"栏空置、"审计建议汇总"表漏项（实测缺陷）
                "audit_suggestion": str(
                    adj.get("audit_suggestion", "") or "").strip() or (
                    "建议结合年度审计程序及行业数据进一步核实"
                    "（本条目由多智能体辩论仲裁新增，需人工确认证据完整性）"),
            }
            details.append(new_item)
            by_id[new_rid] = new_item
            applied += 1
    if applied:
        parsed["risk_details"] = details
        return json.dumps(parsed, ensure_ascii=False), applied
    return risk_json, 0


# ── 事实去重（Fact-Deduplication）──
# V 系列（系统勾稽校验）与 R 系列（LLM 研判）可能指向同一财务科目同一差异事实，
# 若并列成两条独立风险会虚高风险总数并导致重复审计程序（实测缺陷：R005 未分配
# 利润勾稽差异 与 V001 未分配利润校验失败并列）。
# 科目词按长度降序排列：长词（经营现金流）先于短词（现金流）命中，避免误指
_SUBJECT_MARKERS = tuple(sorted(
    ("未分配利润", "应收账款", "经营现金流", "现金流", "存货",
     "资产负债", "净利润", "负债", "资产"),
    key=len, reverse=True))


def _extract_amount_numbers(text: str) -> set:
    """提取文本中的数值（剔除千分位逗号），归一化后返回集合，用于事实指纹匹配。"""
    return {t.replace(",", "") for t in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)}


def _drop_stale_validation_risks(report_obj: dict, vd_data: dict) -> int:
    """幽灵数据清洗：剔除引用已通过勾稽校验（过期差异）的风险条目。

    背景：LLM 生成的风险证据可能引用历史/已修复的勾稽差异（实测：历史版本
    引用"现金流勾稽差异16.71%"，而当前校验已通过），不剔除则跨版本幽灵数据
    进入最终底稿。判定依据（保守，防误删）：
    1. 条目 evidence 精确引用"校验项：<check>"，且该 check 当前 passed=True；
    2. 条目 evidence/title 整段包含已通过 check 的 message（≥8 字防过短误伤）。
    无法对应到具体校验项的条目保守保留。被剔除条目移入 excluded_items 留痕。

    Args:
        report_obj: 风险台账（就地修改 risk_details / excluded_items）
        vd_data: validate_financial_data 的解析结果

    Returns:
        剔除条数（0 表示无过期条目）
    """
    checks = (vd_data.get("data_validation") or {}).get("all_checks") or []
    passed_names = {str(c.get("check", "")).strip() for c in checks
                    if c.get("passed") is True and str(c.get("check", "")).strip()}
    passed_messages = {str(c.get("message", "")).strip() for c in checks
                       if c.get("passed") is True
                       and len(str(c.get("message", "")).strip()) >= 8}
    if not passed_names and not passed_messages:
        return 0
    kept, dropped = [], []
    for r in report_obj.get("risk_details", []):
        if not isinstance(r, dict):
            kept.append(r)
            continue
        ev = f"{r.get('evidence', '')} {r.get('title', '')}"
        stale = any(name and f"校验项：{name}" in ev for name in passed_names)
        if not stale:
            stale = any(msg and msg in ev for msg in passed_messages)
        if stale:
            dropped.append(r)
            logger.info(f"幽灵数据清洗：{r.get('risk_id', '?')} 引用已通过的校验项，移出风险明细")
        else:
            kept.append(r)
    if dropped:
        report_obj["risk_details"] = kept
        report_obj.setdefault("excluded_items", []).extend(dropped)
    return len(dropped)


# ── 勾稽伪风险拦截（50b 批次）──
# D 补丁只认「校验项：xxx」字面引用，LLM 自编数字的伪风险（如对已通过的未分配利润
# 勾稽自行加减算出差异）匹配不到。本拦截按科目指纹 + 数字指纹双重判定：check 已通过
# 时，凡条目命中该科目指纹组合（科目词+差异宣称词），且条目数字与 check 输出的权威
# 结果数字（actual_change/difference 等）无交集 → 判定 LLM 自编算术伪风险。
# check 名 → (科目词, 差异宣称词)
_VALIDATION_FINGERPRINTS = {
    "未分配利润一致性": ("未分配利润", ("勾稽", "差异", "不一致", "差额")),
    "现金流勾稽": ("现金流", ("勾稽", "差异", "不一致", "差额")),
    "现金流勾稽（简化）": ("现金流", ("勾稽", "差异", "不一致", "差额")),
    "资产负债表平衡": ("资产负债", ("平衡", "不平", "差异", "差额")),
}
# 权威结果数字只取校验器的结果字段（difference/actual_change 等），不取原始入账科目
# 值——实测 R003 类条目会正确引用 83993/45755/116 等输入科目数（与 check 全量 JSON
# 有交集），但自编的「差异 9789」与权威结果字段无交集；若用全量 JSON 建数字集会漏拦。
_VALIDATION_RESULT_KEYS = (
    "difference", "actual_change", "expected_change",
    "estimated_cashflow", "actual_cashflow", "difference_pct", "difference_ratio",
)


def _drop_llm_fabricated_validation_risks(report_obj: dict, vd_data: dict) -> int:
    """勾稽伪风险拦截：剔除引用已通过校验科目、但数字系 LLM 自编算术的风险条目。

    背景（50b 实测）：未分配利润勾稽实际已通过（83993-45755-116 恒等），但 LLM 把
    同一组数字自行加减编出「差异 9789」并立项为 R003——D 补丁只认「校验项：xxx」
    字面引用，自编数字的伪风险匹配不到。判定依据（保守，防误删）：
    1. check 当前 passed=True；
    2. 条目 title/evidence/data_analysis 命中该 check 的科目指纹组合
       （科目词 + 任一差异宣称词）；
    3. 条目数字指纹与 check 结果字段的权威数字无交集，且条目确含数字（无数字的
       纯文字条目保守保留，不属「自编算术」）。
    数字有交集或科目指纹不命中的条目保守保留。被剔除条目移入 excluded_items 留痕。

    Args:
        report_obj: 风险台账（就地修改 risk_details / excluded_items）
        vd_data: validate_financial_data 的解析结果

    Returns:
        剔除条数（0 表示无伪风险条目）
    """
    checks = (vd_data.get("data_validation") or {}).get("all_checks") or []
    passed_checks = [c for c in checks if isinstance(c, dict)
                     and c.get("passed") is True and str(c.get("check", "") or "").strip()]
    if not passed_checks:
        return 0
    armed = []
    for c in passed_checks:
        name = str(c.get("check", "") or "").strip()
        fp = _VALIDATION_FINGERPRINTS.get(name)
        if not fp:
            continue
        subject, claim_words = fp
        authoritative_nums = _extract_amount_numbers(
            json.dumps({k: c.get(k) for k in _VALIDATION_RESULT_KEYS if k in c},
                       ensure_ascii=False))
        armed.append((subject, claim_words, authoritative_nums, name))
    if not armed:
        return 0
    kept, dropped = [], []
    for r in report_obj.get("risk_details", []):
        if not isinstance(r, dict):
            kept.append(r)
            continue
        text = " ".join(str(r.get(k, "") or "") for k in ("title", "evidence", "data_analysis"))
        hit = False
        for subject, claim_words, authoritative_nums, name in armed:
            if subject in text and any(w in text for w in claim_words):
                entry_nums = _extract_amount_numbers(text)
                if entry_nums and not (entry_nums & authoritative_nums):
                    hit = True
                    logger.info(
                        f"勾稽伪风险拦截：{r.get('risk_id', '?')} 引用已通过的校验项「{name}」"
                        f"且数字系自编（条目数字 {sorted(entry_nums)[:5]} 均不在校验器权威"
                        f"结果中），移入备查录")
                    break
        if hit:
            dropped.append(r)
        else:
            kept.append(r)
    if dropped:
        report_obj["risk_details"] = kept
        report_obj.setdefault("excluded_items", []).extend(dropped)
    return len(dropped)


# ── 系统红旗告警锚定（50b 批次）──
# financial_calculator 的确定性告警（存贷双高/现金流为负等）是系统计算的硬信号，
# 仲裁可辩但不可被「央企背景/行业地位」等话术随意降级穿底。锚定保护分两步：
# 1. 台账标记：命中红旗词的告警在对应风险条目写 system_anchored: true（按科目指纹
#    匹配，匹配不到则按告警文本新建锚定条目）；
# 2. 回写保护：_apply_arbiter_adjustments 对 system_anchored 条目降级至「一般」时拒绝。
# 必须在辩论/仲裁回写之前调用（否则同轮仲裁的保护不生效）。
# 红旗词 → 条目匹配科目指纹
_RED_FLAG_ALERTS = (
    ("存贷双高", ("货币资金", "存贷", "短期借款", "利息支出")),
    ("现金流为负", ("现金流", "经营现金流")),
    ("应收增速显著高于", ("应收账款", "应收")),
    ("商誉", ("商誉",)),
)


def _anchor_system_alerts(report_obj: dict, tool_results: dict) -> int:
    """系统告警锚定：为红旗告警对应的风险条目写 system_anchored 标记。

    背景（50b 实测）：financial_calculator 已产出确定性红旗告警（存贷双高、
    应收-现金流背离），但无锚定保护，仲裁以「央企背景」随意降级。本函数：
    1. 解析 calculate_financial_indicators 结果的 alerts，筛出含红旗词的告警；
    2. 按科目指纹匹配现有 risk_details 条目，命中则写 system_anchored: true；
    3. 匹配不到则按告警文本新建锚定条目（财务错报维度、重要级、可追溯 source），
       保证红旗信号必然进入台账并受回写保护。

    Args:
        report_obj: 风险台账（就地修改 risk_details）
        tool_results: 工具结果字典（读取 calculate_financial_indicators）

    Returns:
        锚定条数（含新建条目数）
    """
    raw = tool_results.get("calculate_financial_indicators", "")
    try:
        fi = json.loads(raw) if isinstance(raw, str) and raw else {}
    except Exception:
        fi = {}
    alerts = fi.get("alerts") if isinstance(fi, dict) else None
    red_flags = []
    if isinstance(alerts, list) and alerts:
        red_flags = [str(a) for a in alerts
                     if any(kw in str(a) for kw, _ in _RED_FLAG_ALERTS)]
    anchored = 0
    if red_flags:
        from tools.pdf_export import _norm_dim
        details = report_obj.setdefault("risk_details", [])
        for flag in red_flags:
            subjects = next((s for kw, s in _RED_FLAG_ALERTS if kw in flag), ())
            target = None
            for r in details:
                if not isinstance(r, dict):
                    continue
                rtext = " ".join(str(r.get(k, "") or "") for k in ("title", "evidence", "data_analysis"))
                if any(sub in rtext for sub in subjects):
                    target = r
                    break
            if target is not None:
                # 50d：红旗信号最低等级=重要——实测 18:37 版 R002 被锚定但初始等级
                # 「一般」，仲裁「降级理由成立」无从谈起；强制提升后仲裁降级拦截
                # （50b）才对「重要→一般」生效。提升同时重写推理链 [风险判定]，
                # 防底稿「表头重要、链中一般」互搏。
                _touched = False
                if str(target.get("level", "") or "").strip() in ("一般", "低", ""):
                    target["original_level"] = target.get("level", "")
                    target["level"] = "重要"
                    _rewrite_risk_judgment(target, "重要", "系统红旗信号最低等级（锚定）", llm=None)
                    _touched = True
                    logger.info(f"系统告警锚定：{target.get('risk_id', '?')} 等级提升至重要（红旗信号最低等级）")
                if not target.get("system_anchored"):
                    target["system_anchored"] = True
                    _touched = True
                    logger.info(f"系统告警锚定：{target.get('risk_id', '?')} 标记为红旗锚定条目")
                if _touched:
                    anchored += 1
            else:
                _existing_ids = {str(r.get("risk_id", "")) for r in details if isinstance(r, dict)}
                _n = len(details) + 1
                new_id = f"S{_n:03d}"
                while new_id in _existing_ids:
                    _n += 1
                    new_id = f"S{_n:03d}"
                details.append({
                    "risk_id": new_id,
                    "dimension": _norm_dim("财务错报"),
                    "title": flag[:80],
                    "level": "重要",
                    "confidence": 0.8,
                    "evidence": flag,
                    "system_anchored": True,
                    "source": "系统红旗告警",
                    "audit_suggestion": "建议针对该红旗信号执行专项审计程序，取得可核验反证后方可降级",
                    # 50d：锚定新建条目补 reasoning_chain（消除与 LLM 条目的结构差异）
                    "reasoning_chain": [{"step": "系统预警",
                                          "detail": "系统确定性红旗告警触发，锚定为独立风险条目"}],
                })
                anchored += 1
                logger.info(f"系统告警锚定：无匹配条目，按告警文本新建锚定条目 {new_id}")

    # 50c：量化模型信号锚定——Z-Score 灰色/困境区、M-Score 超阈值为确定性模型输出，
    # 不得被「央企背景/集团资金池」等主观话术推翻（实测 17:59 版：Z'=1.6213 灰色预警
    # 区被「能源行业重资产特性」洗白）。锚定条目仅可辩、不可删、须正面回应；灰色区锚
    # 定为一般级（不抬升等级底线，保留 risk_scorer 对基本面健康样本的既有 Z 豁免），
    # 困境区/M-Score 超阈值锚定为重要级（计入等级底线）。
    rm_raw = tool_results.get("calculate_risk_models", "")
    try:
        rm = json.loads(rm_raw) if isinstance(rm_raw, str) and rm_raw else {}
    except Exception:
        rm = {}
    model_flags = []
    rm_models = rm.get("risk_models") if isinstance(rm, dict) else None
    if isinstance(rm_models, dict):
        z = rm_models.get("altman_z_score") or {}
        m = rm_models.get("beneish_m_score") or {}
        if z.get("available") and z.get("zone") == "财务困境区":
            model_flags.append((f"Altman Z-Score {z.get('score')} 落入财务困境区，流动性/财务困境信号须正面回应", "重要"))
        elif z.get("available") and z.get("zone") == "灰色预警区":
            model_flags.append((f"Altman Z-Score {z.get('score')} 落入灰色预警区（1.23~2.9），财务困境风险嫌疑须正面回应", "一般"))
        try:
            if m.get("available") and float(m.get("score") or -99) > -1.78:
                model_flags.append((f"Beneish M-Score {m.get('score')} 高于 -1.78 阈值，存在盈余操纵嫌疑须正面回应", "重要"))
        except (TypeError, ValueError):
            pass
    if model_flags:
        from tools.pdf_export import _norm_dim
        details = report_obj.setdefault("risk_details", [])
        _model_subjects = ("Z-Score", "Z'", "财务困境", "流动性", "M-Score", "盈余操纵", "营运资金")
        for flag_text, flag_level in model_flags:
            target = next((r for r in details if isinstance(r, dict)
                           and any(s in " ".join(str(r.get(k, "") or "") for k in ("title", "evidence", "data_analysis"))
                                  for s in _model_subjects)), None)
            if target is not None:
                # 50d：量化模型预警为重要级（困境区/M-Score 超阈值）时，命中既有
                # 「一般」条目同样强制提升（与红旗路径对称，否则底线规则少计）
                _touched = False
                if (flag_level == "重要"
                        and str(target.get("level", "") or "").strip() in ("一般", "低", "")):
                    target["original_level"] = target.get("level", "")
                    target["level"] = "重要"
                    _rewrite_risk_judgment(target, "重要", "系统量化模型预警最低等级（锚定）", llm=None)
                    _touched = True
                    logger.info(f"量化模型锚定：{target.get('risk_id', '?')} 等级提升至重要")
                if not target.get("system_anchored"):
                    target["system_anchored"] = True
                    _touched = True
                if _touched:
                    anchored += 1
                continue
            _existing_ids = {str(r.get("risk_id", "")) for r in details if isinstance(r, dict)}
            _n = len(details) + 1
            new_id = f"S{_n:03d}"
            while new_id in _existing_ids:
                _n += 1
                new_id = f"S{_n:03d}"
            details.append({
                "risk_id": new_id,
                "dimension": _norm_dim("财务错报"),
                "title": flag_text[:80],
                "level": flag_level,
                "confidence": 0.8,
                "evidence": flag_text,
                "system_anchored": True,
                "source": "系统量化模型预警",
                "audit_suggestion": "建议针对该量化预警执行专项审计程序，取得可核验反证后方可降级",
                # 50d：锚定新建条目补 reasoning_chain（消除与 LLM 条目的结构差异）
                "reasoning_chain": [{"step": "系统预警",
                                      "detail": "系统量化模型确定性预警触发，锚定为独立风险条目"}],
            })
            anchored += 1
            logger.info(f"量化模型锚定：按模型预警新建锚定条目 {new_id}（{flag_level}）")
    return anchored


# ── 风险等级底线规则（50c 批次）──
# 评分由工具结果在辩论前算出，与仲裁后最终台账零耦合——实测 17:59 版：最终台账 5 个
# 重要级风险（R001-R005），综合评分却为 10.5 分（低风险），仲裁人还书写「与低风险
# 评分相符」的荒谬闭环。底线规则：重大≥1 或 重要≥5 → 下限 51 分「高风险」；
# 重要≥3 → 下限 26 分「中等风险」。触发时强制上调综合评级（不信任 LLM 的裁定叙事）。
_FLOOR_MIN_SCORE = {"高风险": 51, "中等风险": 26}
_FLOOR_LEVEL_KEY = {"高风险": "high", "中等风险": "medium"}


def _count_risk_levels(details: list) -> tuple:
    """按 pdf_export LEVEL_ALIASES 同口径统计等级：返回 (major, important)。

    等级别名：重大/高→major，重要/中→important（防 LLM 非标准取值漏计）。
    """
    major = important = 0
    for r in details:
        if not isinstance(r, dict):
            continue
        lv = str(r.get("level", "") or "").strip()
        if lv in ("重大", "高风险", "极高风险", "严重", "高"):
            major += 1
        elif lv in ("重要", "中等风险", "中"):
            important += 1
    return major, important


def _enforce_risk_level_floor(report_obj: dict):
    """风险等级底线规则：最终台账等级分布与综合评级矛盾时强制上调。

    判定（保守）：重大≥1 或 重要≥5 → 下限 51 分（高风险）；重要≥3 → 下限 26 分
    （中等风险）。当前评分已达标时不触发。触发时覆盖 comprehensive_score /
    comprehensive_score_snapshot / overall_assessment（用新快照重刷旧分表述），
    写 level_floor_note 供 PDF/Excel/网页渲染。

    Args:
        report_obj: 风险台账（就地修改）

    Returns:
        触发时返回 (新评分 JSON 串, 警示文本)；未触发返回 None。
    """
    major, important = _count_risk_levels(report_obj.get("risk_details") or [])
    floor_level = None
    if major >= 1 or important >= 5:
        floor_level = "高风险"
    elif important >= 3:
        floor_level = "中等风险"
    if floor_level is None:
        return None
    score_json = report_obj.get("comprehensive_score")
    if not isinstance(score_json, dict):
        return None
    # 50d：score=None（评分未获取/无法判定）或 NaN 时不触发底线——
    # 「无数据」不得被改写为确定性的高风险（实测风险：-1 折叠 None 后伪造结论）
    sc = score_json.get("score")
    if not isinstance(sc, (int, float)) or sc != sc:
        return None
    cur = float(sc)
    floor_score = _FLOOR_MIN_SCORE[floor_level]
    if cur >= floor_score:
        return None
    old_level = str(score_json.get("level", "") or "") or "未知等级"
    new = dict(score_json)
    new["score"] = float(floor_score)
    new["level"] = floor_level
    new["level_key"] = _FLOOR_LEVEL_KEY[floor_level]
    # 底线同步基础分/抬升：避免 PDF KPI 卡出现「51 分但基础分 10.5」的自相矛盾
    new["base_score"] = float(floor_score)
    new["escalation"] = 0.0
    reasons = list(new.get("escalation_reasons") or [])
    reason = (f"风险等级底线规则：最终台账含重大 {major} 项、重要 {important} 项，"
              f"综合评级不得为{old_level}，系统强制上调至{floor_level}")
    if reason not in reasons:
        reasons.append(reason)
    new["escalation_reasons"] = reasons
    report_obj["comprehensive_score"] = new
    report_obj["comprehensive_score_snapshot"] = {
        "score": float(floor_score),
        "level": floor_level,
    }
    if isinstance(report_obj.get("overall_assessment"), str):
        from tools.pdf_export import _apply_score_snapshot
        report_obj["overall_assessment"] = _apply_score_snapshot(
            report_obj["overall_assessment"], report_obj)
    report_obj["level_floor_note"] = reason
    warn = (f"\n\n⚠️ {reason}（原模型评分 {cur:g} 分已作废，"
            f"请以 {floor_score:g} 分（{floor_level}）为准）")
    logger.warning(f"风险等级底线规则触发：{reason}")
    return json.dumps(new, ensure_ascii=False), warn


# ── 语义化编号（50d 引入，50e 改维度前缀）──
# R 系列编号随 LLM 每次生成漂移；50d 科目指纹方案存在顺序冲突（实测 V4：
# R005 储气库收购误命中 CASH、R006 信披误命中 AR）。50e 改为维度前缀：
# 维度唯一不冲突，与用户要求的 FIN/REL/DIS 统一编号规范一致。
_SEMANTIC_DIM_PREFIX = {
    "financial_misstatement": "FIN",
    "related_party": "REL",
    "disclosure_compliance": "DIS",
    "going_concern": "GC",
    "regulatory_penalty": "REG",
}


def _assign_semantic_ids(report_obj: dict) -> None:
    """为风险明细派生 semantic_id（50e：维度前缀，确定性无冲突）。

    主 risk_id 保持不变（仲裁 JSON 引用与历史测试依赖 R/S 系列）；semantic_id
    按维度前缀分组（FIN/REL/DIS/GC/REG），组内按台账出现顺序编号，统一废弃
    S/R 前缀展示；未知维度保留原 risk_id。供 PDF/Excel 渲染与人工追溯。

    Args:
        report_obj: 风险台账（就地写入各条目 semantic_id 字段）
    """
    from tools.pdf_export import _norm_dim
    details = [r for r in (report_obj.get("risk_details") or []) if isinstance(r, dict)]
    counters = {}
    for r in details:
        prefix = _SEMANTIC_DIM_PREFIX.get(_norm_dim(r.get("dimension")))
        if prefix is None:
            r.setdefault("semantic_id", str(r.get("risk_id", "")))
            continue
        counters[prefix] = counters.get(prefix, 0) + 1
        r["semantic_id"] = f"{prefix}-{counters[prefix]:03d}"


def _build_risk_index_md(report_obj: dict) -> str:
    """风险索引表（50e JSON 驱动渲染）——从最终定稿 risk_details 生成 Markdown 表，
    保证「JSON 里有的条目正文必然有」（修复 V4 实测：仲裁新增 R006 未写入
    正文风险明细，底稿与正文打架）。含 semantic_id 与待核实标注。"""
    from tools.pdf_export import DIM_CN, _norm_dim
    details = [r for r in (report_obj.get("risk_details") or []) if isinstance(r, dict)]
    if not details:
        return ""
    lines = [
        "### 最终风险清单（系统生成，与审计底稿同源）",
        "",
        "| 语义编号 | 风险ID | 维度 | 风险标题 | 等级 | 置信度 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in details:
        sid = str(r.get("semantic_id", "") or r.get("risk_id", ""))
        dim = DIM_CN.get(_norm_dim(r.get("dimension")), str(r.get("dimension", "") or ""))
        title = str(r.get("title", "") or "")[:60]
        pend = "【待核实】" if r.get("pending_verification") else ""
        conf = r.get("confidence")
        conf_s = f"{float(conf):.2f}" if isinstance(conf, (int, float)) else "—"
        lines.append(f"| {sid} | {r.get('risk_id', '')} | {dim} | {title}{pend} | "
                     f"{r.get('level', '')} | {conf_s} |")
    return "\n".join(lines)


def _dedupe_v_risks(vd_risks: list, details: list) -> list:
    """事实去重：V 系列校验风险若与已有 R 系列风险指向同一科目且同一差异事实，
    降级为 R 条目的证据附注（v_evidence_ref），不再作为独立风险条目并列。

    匹配规则（保守，防误并）：
    1. V 条目提取科目指纹（未分配利润/现金流/应收账款等关键词）；
    2. 候选 R 条目 = 含同一科目词的现有明细；
    3. 数字指纹有重合 → 必合并；无重合但候选唯一 → 也合并（同一科目即同一事实，
       数字差异多因单位口径不同，如 381,500,000,000 元 vs 38,150 百万元）；
    4. 无科目 / 无候选 / 多候选且数字不重合 → 保留独立条目（防误并）。

    Returns:
        去重后仍需追加为独立条目的 V 列表（命中合并的不在其中）。
    """
    kept = []
    for vr in vd_risks or []:
        if not isinstance(vr, dict):
            continue
        v_text = f"{vr.get('title', '')} {vr.get('evidence', '')}"
        v_subject = next((m for m in _SUBJECT_MARKERS if m in v_text), None)
        v_nums = _extract_amount_numbers(v_text)
        target = None
        if v_subject:
            candidates = [
                r for r in (details or []) if isinstance(r, dict)
                and v_subject in f"{r.get('title', '')} {r.get('evidence', '')} {r.get('data_analysis', '')}"
            ]
            if candidates:
                by_num = next((r for r in candidates if _extract_amount_numbers(
                    f"{r.get('title', '')} {r.get('evidence', '')}") & v_nums), None)
                target = by_num or (candidates[0] if len(candidates) == 1 else None)
        if target is not None:
            vid = vr.get("risk_id", "V?")
            note = f"审计数据校验附注：{vr.get('title', '')}"
            old_ev = str(target.get("evidence", "") or "")
            if note not in old_ev:
                target["evidence"] = f"{old_ev}；{note}" if old_ev else note
            target["v_evidence_ref"] = vid
            logger.info(f"事实去重：{vid} 已并入 {target.get('risk_id', '')} 证据附注（同一科目同一事实）")
        else:
            kept.append(vr)
    return kept


def _enforce_indicator_unit_scale(tool_results: dict, messages: list) -> None:
    """指标金额量级补偿（P10）：LLM 自行调用指标工具时单位常为百万体系。

    背景：P1 预处理提取失败时（实测两次连续失败），LLM 自行调用
    calculate_financial_indicators 传入的数据整体按报表单位（如"百万元"）抄填，
    无绝对量级锚（营收也 <1e8），normalize_financial_units 保守跳过，导致
    四维判读表渲染出"本期净利润 9.37 万元"（93666 被当元）的量级荒谬（实测缺陷）。

    补偿策略：指标结果中若存在 <1e8 的金额字段（元体系下不可能出现），
    用年报文本的金额单位声明整体换算后覆盖 tool_results（PDF/评分读取同一引用）。

    Args:
        tool_results: 工具结果字典（原地覆盖）
        messages: 消息列表（供提取年报文本）
    """
    raw = tool_results.get("calculate_financial_indicators", "")
    if not raw:
        return
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return
    ind = d.get("indicators") if isinstance(d, dict) else None
    if not isinstance(ind, dict):
        return
    # 仅在存在可疑量级金额字段时触发（元体系下净利不可能 <1e8）
    np_c = ind.get("net_profit_current")
    if not isinstance(np_c, (int, float)) or abs(np_c) >= 1e8:
        return
    try:
        from langchain_core.messages import HumanMessage
        report_text = next((str(m.content) for m in messages or []
                            if isinstance(m, HumanMessage) and m.content), "")
        from tools.financial_calculator import (_UNIT_FACTOR, _is_amount_field,
                                                detect_amount_unit)
        unit = detect_amount_unit(report_text)
        factor = _UNIT_FACTOR.get(unit) if unit else None
    except Exception:
        return
    if not factor or factor == 1.0:
        return
    for key, value in list(ind.items()):
        if isinstance(value, (int, float)) and _is_amount_field(key):
            ind[key] = value * factor
    tool_results["calculate_financial_indicators"] = json.dumps(d, ensure_ascii=False)
    logger.info(f"指标量级补偿：年报声明单位 {unit}，金额字段 ×{factor:g} 后覆盖")


def _enforce_parent_net_profit_validation(tool_results: dict, messages: list) -> bool:
    """未分配利润勾稽归母口径兜底：LLM 调用 validate 未传归母净利润时重算。

    背景：未分配利润是母公司口径科目，勾稽必须用归母净利润。P1 预处理连续多轮
    失败（提取模型未输出 net_profit_parent），LLM 自行调用 validate 时传入合并
    净利润（93666 百万），产生 20.43% 假阳性（实测缺陷：归母口径 0.34% 通过）。

    兜底策略：从年报文本规则提取归母净利润（extract_parent_net_profit，返回元），
    换算到与原校验项相同的金额体系后重算该勾稽项，并同步更新 failed/passed 统计
    与 risks 列表（被修复的勾稽风险从 V 系列移除）。

    Args:
        tool_results: 工具结果字典（原地覆盖 validate 结果）
        messages: 消息列表（供提取年报文本）

    Returns:
        True 表示发生了归母口径修正（调用方可据此清洗台账中的假阳性条目）
    """
    vd_raw = tool_results.get("validate_financial_data", "")
    if not vd_raw:
        return False
    try:
        vd = json.loads(vd_raw) if isinstance(vd_raw, str) else vd_raw
        dv = vd.get("data_validation") or {}
        re_chk = next((c for c in dv.get("all_checks", [])
                       if isinstance(c, dict) and "未分配利润" in str(c.get("check", ""))), None)
    except Exception:
        return False
    # 已用归母口径或已通过 → 无需兜底
    if re_chk is None or re_chk.get("passed") is True:
        return False
    if "归母" in str(re_chk.get("net_profit_note", "")):
        return False
    try:
        from langchain_core.messages import HumanMessage
        report_text = next((str(m.content) for m in messages or []
                            if isinstance(m, HumanMessage) and m.content), "")
        from tools.financial_calculator import extract_parent_net_profit
        np_parent = extract_parent_net_profit(report_text)
    except Exception:
        return False
    if np_parent is None:
        return False
    ab, ae, div = (re_chk.get("retained_earnings_begin"),
                   re_chk.get("retained_earnings_end"), re_chk.get("dividends"))
    if not all(isinstance(x, (int, float)) for x in (ab, ae, div)):
        return False
    # 换算归母值到原校验项的金额体系（留存收益 <1e8 视为百万体系）
    scale = 1e6 if abs(ab) < 1e8 else 1.0
    np_p = np_parent / scale if scale != 1.0 else np_parent
    actual = ae - ab
    expected = np_p - div
    if abs(expected) < 1 and abs(actual) < 1:
        return False
    diff = abs(actual - expected)
    ratio = diff / max(abs(expected), abs(actual), 1)
    passed = ratio <= 0.05
    re_chk["net_profit"] = round(np_p, 2)
    re_chk["net_profit_note"] = "(归母口径)"
    re_chk.pop("caliber", None)  # 已升级为归母口径判定，口径提示字段不再适用
    re_chk["expected_change"] = round(expected, 2)
    re_chk["difference"] = round(diff, 2)
    re_chk["difference_pct"] = f"{ratio * 100:.2f}%"
    re_chk["passed"] = passed
    re_chk["message"] = ("未分配利润变动与净利润一致（归母口径）" if passed else
                          f"未分配利润变动({actual:.2f})与归母口径净利润-分红({expected:.2f})不一致，"
                          f"差额{diff:.2f}，存在数据可靠性风险")
    # 同步更新统计与 risks（被修复的勾稽风险从 V 系列移除，防假阳性进台账）
    checks = dv.get("all_checks", []) or []
    failed = sum(1 for c in checks if c.get("passed") is False)
    passed_n = sum(1 for c in checks if c.get("passed") is True)
    dv["failed_checks"] = failed
    dv["passed_checks"] = passed_n
    dv["validation_result"] = "通过" if failed == 0 else "未通过"
    risks = dv.get("risks") or []
    dv["risks"] = [r for r in risks if "未分配利润" not in str(r.get("title", ""))]
    tool_results["validate_financial_data"] = json.dumps(vd, ensure_ascii=False)
    logger.info(f"未分配利润勾稽归母口径兜底：net_profit 修正为 {np_p:.0f}，"
                f"passed={passed}（{ratio * 100:.2f}%）")
    return True


def _drop_false_positive_reconciliation_risks(report_obj: dict) -> None:
    """清洗台账中基于合并口径勾稽失败生成的假阳性风险条目（归母口径修正后）。

    背景：LLM 看到旧 validate 输出（合并口径 20.43% 失败）生成了"未分配利润勾稽
    不一致"风险条目；归母口径修正后该证据已失效，若残留会导致假阳性继续驱动
    评分与人工复核（实测缺陷：R003 置信度 0.90 存活）。

    判定（保守，防误删）：P11 已确认归母口径下该勾稽项通过，因此凡 title/evidence
    同时含"未分配利润"与勾稽失效信号（"勾稽"/"不一致"/"差异"）的条目一律视为
    假阳性（不再要求 evidence 含"合并"字样——实测缺陷：LLM 未写"合并"时漏洗）→
    移入 excluded_items 备查录（可追溯），而非直接删除。
    """
    details = report_obj.get("risk_details") or []
    kept, excluded = [], []
    for r in details:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        text = f"{r.get('title', '')} {r.get('evidence', '')} {r.get('data_analysis', '')}"
        if ("未分配利润" in text
                and any(k in text for k in ("勾稽", "不一致", "差异", "9789", "9,789", "20.43"))):
            excluded.append(r)
            logger.info(f"假阳性清洗：{r.get('risk_id', '')}（未分配利润勾稽证据已失效）移入备查录")
        else:
            kept.append(r)
    if excluded:
        report_obj["risk_details"] = kept
        report_obj.setdefault("excluded_items", []).extend(excluded)


def _strip_system_self_audit_items(report_obj: dict) -> None:
    """系统自审条目剥离：把"系统内部状态"被误立项为发行人风险的条目移出台账。

    背景：LLM/仲裁可能把系统内部矛盾（如"系统综合评分31.5分与台账内部评分9分矛盾"）
    登记为发行人的信披风险条目（实测缺陷：0806 批 R006），属类别错误——
    评分引擎的 Bug 不是发行人的披露违规。

    判定（双通道，与措辞无关）：
    1. 自指词：title/evidence 含（系统|台账|快照|工具输出|评分快照|内部评分|综合评分）
    2. 财务锚：evidence 含 数字+（亿元|万元|百万元|千元|元|百万|%|倍）即视为有锚
       （评分"31.5分"的"分"不是财务锚）
    3. issuer 域豁免：内部控制|内部交易|内部审批|集团内部|内部人|系统性风险
       （命中即不剥离，防 issuer 高频短语误伤）

    命中条目移入 report_obj["system_quality_notes"]（从 risk_details 移除，可追溯），
    必须在四份产物导出之前对共享 report_obj 执行。
    """
    self_refs = ("系统", "台账", "快照", "工具输出", "评分快照", "内部评分", "综合评分")
    issuer_exempt = ("内部控制", "内部交易", "内部审批", "集团内部", "内部人", "系统性风险")
    anchor_pat = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|百万元|千元|元|百万|%|倍)")
    details = report_obj.get("risk_details") or []
    kept, notes = [], []
    for r in details:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        text = f"{r.get('title', '')} {r.get('evidence', '')} {r.get('data_analysis', '')}"
        if any(k in text for k in issuer_exempt):
            kept.append(r)
            continue
        has_self_ref = any(k in text for k in self_refs)
        has_anchor = bool(anchor_pat.search(str(r.get("evidence", "")) or ""))
        if has_self_ref and not has_anchor:
            notes.append(r)
            logger.info(f"系统自审剥离：{r.get('risk_id', '')}（系统内部状态误立项为发行人风险）移入质检日志")
        else:
            kept.append(r)
    if notes:
        report_obj["risk_details"] = kept
        report_obj.setdefault("system_quality_notes", []).extend(notes)


def _enforce_disclosure_reconciliation(tool_results: dict, messages: list) -> None:
    """合规勾稽扣分系统层兜底：披露检查结果未含勾稽扣分时强制覆盖。

    背景：P1 预处理已把带勾稽扣分的披露检查结果注入消息（pre_d），但 LLM 可能
    重复调用 check_disclosure_compliance 且未传 validation_json，同名消息后者
    覆盖导致 tool_results 里是"无勾稽扣分"版本（实测缺陷：勾稽差异 20.43% 未
    联动扣分，合规评分 93 分 13/14 全绿）。

    兜底策略（以 validate 结果为权威来源）：
    1. validate.failed_checks>0 而 dc.reconciliation_checks==0 → 视为被覆盖；
    2. 优先复用 P1 注入的带扣分版本（tool_call_id="pre_d"），零成本且与预处理一致；
    3. 找不到时用第一条 human 消息文本 + validation_json 重算并覆盖。

    Args:
        tool_results: 工具结果字典（兜底导出闭包读取的同一引用，原地覆盖）
        messages: 消息列表（供找回 P1 版本 / 年报原文）
    """
    vd_raw = tool_results.get("validate_financial_data", "")
    dc_raw = tool_results.get("check_disclosure_compliance", "")
    if not vd_raw or not dc_raw:
        return
    try:
        vd = json.loads(vd_raw) if isinstance(vd_raw, str) else vd_raw
        dc = json.loads(dc_raw) if isinstance(dc_raw, str) else dc_raw
        failed = int((vd.get("data_validation") or {}).get("failed_checks", 0) or 0)
        rec = int(dc.get("reconciliation_checks", 0) or 0)
    except (ValueError, TypeError, AttributeError):
        return
    if not (failed > 0 and rec == 0):
        return  # 无勾稽差异或已含勾稽扣分，无需兜底
    # 优先复用 P1 注入的带扣分版本（tool_call_id="pre_d"）
    for m in messages or []:
        if isinstance(m, ToolMessage) and m.name == "check_disclosure_compliance" \
                and str(getattr(m, "tool_call_id", "")) == "pre_d":
            content = str(m.content or "")
            if content and "reconciliation_checks" in content:
                tool_results["check_disclosure_compliance"] = content
                logger.info("披露检查勾稽扣分兜底：复用 P1 注入的带扣分版本")
                return
    # 找不到 P1 版本时，用第一条 human 消息文本重算（带 validation_json）
    try:
        from langchain_core.messages import HumanMessage
        report_text = next((str(m.content) for m in messages or []
                            if isinstance(m, HumanMessage) and m.content), "")
    except Exception:
        report_text = ""
    if not report_text:
        return
    try:
        from tools.disclosure_checker import check_disclosure_compliance
        refreshed = check_disclosure_compliance.invoke(
            {"report_text": report_text[:200000], "validation_json": vd_raw})
        tool_results["check_disclosure_compliance"] = str(refreshed)
        logger.info("披露检查勾稽扣分兜底：系统用 validation_json 重算并覆盖")
    except Exception as e:
        logger.warning(f"披露检查勾稽扣分兜底失败: {e}")


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

    def __init__(self, agent, module=None):
        """初始化包装器。

        Args:
            agent: LangGraph create_react_agent 返回的原始 agent 实例
            module: 模块标识（financial/compliance/synthesis/outlook）。用于后处理
                差异化：行业风向研判(outlook)跳过财务专用的评分/热力图/雷达图/PDF 兜底。
        """
        self._agent = agent
        self._module = module

    async def ainvoke(self, payload, config=None, post_process=True, **kw):
        """异步调用 agent 并执行兜底后处理。

        Args:
            post_process: 是否执行兜底后处理（辩论/导出/评分）。
                串跑流水线的中间阶段传 False：局部结果不应辩论、不应导出
                局部报告、更不应拿不完整数据跑综合评分（会产生误导性分数）。
        """
        result = await self._agent.ainvoke(payload, config=config, **kw)
        return self._post_process(result) if post_process else result

    def invoke(self, payload, config=None, post_process=True, **kw):
        """同步调用 agent 并执行兜底后处理（post_process 语义同 ainvoke）"""
        result = self._agent.invoke(payload, config=config, **kw)
        return self._post_process(result) if post_process else result

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

    async def astream(self, payload, config=None, post_process=True, **kw):
        """流式透传底层 agent，并在流结束后补跑 _post_process。

        设计背景：底层 create_react_agent 的 astream 不经过 _AgentWrapper 的
        后处理（_post_process 仅在 invoke/ainvoke 中调用），导致 /stream_run 丢失
        兜底 PDF/Excel 导出、多智能体辩论复核、综合评分。此处在流结束后
        手动补齐，保证 /stream_run 与 /run 行为一致。

        Args:
            post_process: 是否在流末补跑兜底后处理（辩论/导出/评分）。
                C 端轻量工具路径（投资参考/行业风向）传 False：轻量对话
                不应触发辩论、不应导出局部 PDF/Excel、更不应跑综合评分；
                缺省 True，既有调用点行为逐字节不变（语义同 ainvoke）。

        工作流程：
        1. 逐 chunk 透传给上游（前端据此追踪真实工具进度）；
        2. 同时按消息 id 去重累积消息对象（保留顺序）；
        3. 流结束后对累积消息跑 _post_process（就地修改最后一条 AIMessage）；
        4. 额外 yield 一个带 __post_processed__ 标记的 chunk，携带后处理后的消息，
           供上游用于构建含辩论/导出/评分的最终报告。
        """
        seen = {}
        order = []
        # 重建完整台账：以入参种入的台账为底（预跑三工具结果），叠加流中观察到的
        # post_model_hook 增量。astream 增量不含输入消息，若不重建并携带台账调
        # _post_process，顺序门禁/评分兜底/PDF 导出补传只能扫到裁剪后的流内
        # 消息，读不到预跑结果。
        seed_ledger = payload.get("tool_ledger") if isinstance(payload, dict) else None
        full_ledger = _merge_tool_ledger(None, seed_ledger)
        async for chunk in self._agent.astream(payload, config=config, **kw):
            yield chunk
            for m in self._iter_chunk_messages(chunk):
                mid = getattr(m, "id", None) or id(m)
                if mid not in seen:
                    seen[mid] = m
                    order.append(mid)
            for _nk, _nv in (chunk.items() if isinstance(chunk, dict) else []):
                if isinstance(_nv, dict) and isinstance(_nv.get("tool_ledger"), dict):
                    _lt = _nv["tool_ledger"]
                    if _lt.get("entries"):
                        full_ledger = _merge_tool_ledger(full_ledger, _lt)
                    elif _lt.get("seq") or _lt.get("results"):
                        full_ledger = _lt   # 已是完整台账（reducer 合并后形态）
        messages = [seen[i] for i in order]
        if not messages or not post_process:
            return
        try:
            # 先推一个「后处理开始」标记：辩论三轮 + 兜底导出可能耗时 30-60s，
            # 上游据此更新进度文案，避免前端进度条长时间静止让用户以为卡死
            yield {"__post_processing__": True}
            self._post_process({"messages": messages, "tool_ledger": full_ledger})
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

        # P11: 未分配利润勾稽归母口径兜底——P1 提取失败时 LLM 自行调用 validate
        # 用合并净利润产生假阳性，须先用年报文本提取归母净利润重算（前置干 P9，
        # 保证勾稽扣分/风险合并基于修正后的校验结果）
        p11_fixed = _enforce_parent_net_profit_validation(tool_results, messages)

        # P9: 合规勾稽扣分系统层兜底——LLM 重复调用披露检查未传 validation_json 时
        # 覆盖了 P1 注入的带扣分版本，此处强制恢复（实测缺陷：93 分 13/14 全绿）
        _enforce_disclosure_reconciliation(tool_results, messages)

        # P10: 指标金额量级补偿——P1 提取失败时 LLM 自行调用按百万体系抄填，
        # 用年报文本单位声明换算，杜绝"9.37万元"量级荒谬（实测缺陷）
        _enforce_indicator_unit_scale(tool_results, messages)

        # ─── 领域约束分级门禁：硬约束 fail-closed（含系统代跑补救），软约束降级可见警告 ───
        # 硬约束「先校验后计算」：LLM 漏调 validate 时不再直接炸掉全场（用户白等），
        # 而是系统用 calculate 的同一份入参代跑校验：通过→附提示继续；
        # 校验不通过或无法代跑→仍 fail-closed（底线：绝不在未校验数据上出报告）。
        # 其余链路次序（disclosure/search/score/export）属推荐顺序非数据依赖，
        # 乱序仅在报告末尾附可见提示。
        # 采用 tool_ledger 的完整序列而非裁剪后的 messages，避免早期消息被丢弃后误判。
        soft_order_warn = self._enforce_tool_call_order(called_seq, messages)

        # 行业风向研判是行业维度：审计 PDF/Excel、财务热力图/雷达图、综合评分均为
        # 公司财务报表审计专用，套到行业研判会产生误导，故后处理统一跳过这些兜底。
        is_outlook = self._module == "outlook"
        
        # 双保险：LLM 工具集已移除导出工具（导出收敛为系统兜底唯一路径），此判定
        # 防御历史会话续接/模块裁剪回退全量等异常场景——LLM 若调用过导出但传参不全，
        # 系统仍须兜底重导，避免产出残缺版报告（专项章缺失、评分/复核缺失）。
        pdf_llm_state = self._llm_export_was_complete(messages, "export_pdf_report", called)
        excel_llm_state = self._llm_export_was_complete(messages, "export_excel_report", called)
        need_pdf = not is_outlook and (pdf_llm_state is None or not pdf_llm_state)
        need_excel = not is_outlook and (excel_llm_state is None or not excel_llm_state)

        # 第二步：取最后一条 AI 消息
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None or not last_ai.content:
            return result

        # 第三步：从 AI 回复中提取风险台账 JSON
        risk_json = _extract_risk_json(str(last_ai.content))
        if not risk_json:
            # 无法提取风险 JSON 时，用 AI 文本构造兜底输入（确保辩论/图表/评分仍能运行）
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

        # ─── 综合评分兜底计算（前移至辩论之前）：PDF 综合评分解读章需要评分数据，
        # 辩论 prompt 也需要真实评分上下文（否则 LLM 会臆造评分数字——实测缺陷：
        # 辩论中把 6.0 分幻觉成 0 分并据此展开批判）。计算后回写 tool_results
        # （导出兜底 lambda 闭包持有 dict 引用，执行时可读到），文本追加保留在
        # 消息末尾（展示顺序不变：链接 → 复核意见 → 评分卡）。
        score_text = None
        score_context = ""
        if not is_outlook:
            try:
                from tools.risk_scorer import calculate_comprehensive_score
                financial_result = tool_results.get("calculate_financial_indicators", "{}")
                validation_result = tool_results.get("validate_financial_data", "{}")
                disclosure_result = tool_results.get("check_disclosure_compliance", "{}")
                # 补传量化模型与审计意见：评分须含模型/意见抬升逻辑（Z 豁免门控等），
                # 缺传会导致抬升缺失或与 LLM 自行调用结果不一致（实测缺陷：
                # LLM 传参不全算出 0 分写入正文，与系统 6.0 分矛盾）
                score_output = calculate_comprehensive_score.invoke({
                    "financial_analysis_json": financial_result,
                    "disclosure_check_json": disclosure_result,
                    "validation_json": validation_result,
                    "risk_models_json": tool_results.get("calculate_risk_models", ""),
                    "audit_opinion_json": tool_results.get("identify_audit_opinion", ""),
                })
                # 回写台账：LLM 未调用评分工具时，PDF 导出仍能渲染综合评分解读章
                tool_results["calculate_comprehensive_score"] = score_output
                score_text = f"\n\n<!--COMPREHENSIVE_SCORE-->\n{score_output}"
                try:
                    _sd = json.loads(score_output)
                    score_context = (
                        f"综合评分（系统计算，禁止臆造或修改）：{_sd.get('score')} 分"
                        f"（{_sd.get('level')}）；基础分 {_sd.get('base_score')}；"
                        f"模型/意见抬升 {_sd.get('escalation')}。辩论中引用评分必须使用以上数值。"
                    )
                    # L 下沉：系统评分快照回写台账（覆盖 LLM 写入的旧 comprehensive_score
                    # 与 overall_assessment 旧分数表述），使辩论/导出/图表读到同一口径——
                    # 否则辩论读到的 risk_json 仍含 LLM 原始旧分（实测 v23：辩论引用
                    # 台账 15.7 分与系统 12.6 分矛盾，生成"评分口径不一致"讨论）。
                    try:
                        _ro = json.loads(risk_json)
                        if isinstance(_ro, dict):
                            _ro["comprehensive_score"] = {k: _sd.get(k) for k in
                                ("score", "level", "level_key", "breakdown", "weights",
                                 "base_score", "escalation", "escalation_reasons", "summary")
                                if k in _sd}
                            if isinstance(_sd.get("score"), (int, float)):
                                _ro["comprehensive_score_snapshot"] = {
                                    "score": float(_sd["score"]),
                                    "level": str(_sd.get("level", "") or ""),
                                }
                            if isinstance(_ro.get("overall_assessment"), str):
                                from tools.pdf_export import _apply_score_snapshot
                                _ro["overall_assessment"] = _apply_score_snapshot(
                                    _ro["overall_assessment"], _ro)
                            risk_json = json.dumps(_ro, ensure_ascii=False)
                    except Exception:
                        pass  # 回写失败不阻断（渲染层 L 补丁仍兜底）
                    # L 下沉·消息层：同步回写 last_ai.content（前端直接展示的原文）——
                    # 此前只回写内部 risk_json 变量，前端消息仍显示 LLM 旧分（实测 v24：
                    # 正文"综合评分18.5分"与系统评分卡 14.8 同屏矛盾，用户多次反馈）。
                    # 50c：提炼为 _sync_score_into_message（L 下沉与等级底线共用），
                    # 并补上内嵌台账块 overall_assessment 的旧分重写。
                    try:
                        if _sync_score_into_message(last_ai, _sd):
                            logger.info("L 下沉·消息层：已回写前端消息中的旧评分表述")
                    except Exception:
                        pass  # 消息层回写失败不阻断（渲染层 L 补丁仍兜底）
                except Exception:
                    score_context = "综合评分（系统计算）：未能解析，辩论中不得引用具体评分数字。"
            except Exception as e:
                logger.warning(f"综合评分兜底失败: {e}")
                score_text = ("\n\n⚠️ 综合风险评分生成失败，本次结论未包含综合评分，"
                              "请结合人工判断复核并重试（详见服务日志）。")

        review_block = None
        arb_level_changed = False
        disclosure_consistency_note = ""
        # 50d：仲裁状态（辩论失败/无裁定时不标记）——须在辩论分支外初始化，
        # 防后续 try 块引用未绑定变量被外层 except 吞掉
        arb_incomplete = False
        _arb_verdict = ""
        # 50b：系统红旗告警锚定——必须在辩论/仲裁回写之前完成标记，使同轮仲裁的
        # 降级拦截（_apply_arbiter_adjustments）对 system_anchored 条目生效；
        # 锚定后重导出 risk_json，辩论与仲裁读到的台账即含锚定标记/条目。
        try:
            _anchor_obj = json.loads(risk_json)
            if isinstance(_anchor_obj, dict):
                _anchored = _anchor_system_alerts(_anchor_obj, tool_results)
                if _anchored:
                    risk_json = json.dumps(_anchor_obj, ensure_ascii=False)
                    logger.info(f"系统告警锚定完成：{_anchored} 条红旗条目已锚定")
        except Exception as e:
            logger.warning(f"系统告警锚定失败（不阻断主流程）: {e}")
        # 50e：辩论前底线预锁——锚定提升等级后，基于当时台账先算一次底线；触发则
        # 更新 risk_json/score_context/消息层，使辩论基于底线分（如 26 中等）而非
        # 工具原始分（15 低）——修复 V4 时空错位（全员基于 15 分辩论，系统后宣判 26）。
        _floor_sd = None
        _floor_note = ""
        try:
            _pre_lock_obj = json.loads(risk_json)
            if isinstance(_pre_lock_obj, dict):
                _pre_floor = _enforce_risk_level_floor(_pre_lock_obj)
                if _pre_floor:
                    _pre_floor_json, _pre_floor_warn = _pre_floor
                    risk_json = json.dumps(_pre_lock_obj, ensure_ascii=False)
                    tool_results["calculate_comprehensive_score"] = _pre_floor_json
                    _floor_sd = json.loads(_pre_floor_json)
                    _floor_note = _pre_lock_obj.get("level_floor_note", "") or ""
                    score_context = (
                        f"综合评分（系统计算，禁止臆造或修改）：{_floor_sd.get('score')} 分"
                        f"（{_floor_sd.get('level')}）。{_floor_note}——该分数已含风险等级"
                        "底线规则上调，辩论与裁定必须基于此定级。")
                    if _sync_score_into_message(last_ai, _floor_sd, floor_note=_floor_note):
                        logger.info("辩论前底线预锁：已回刷前端消息中的评分表述为底线分")
        except Exception as e:
            logger.warning(f"辩论前底线预锁失败（不阻断主流程）: {e}")
        if REVIEW_ENABLED and not skip_debate:
            debate_result = self._run_debate(risk_json, score_context)
            if debate_result:
                # 仲裁裁定回写：解析【裁定JSON】并按白名单规则修正风险等级/备注
                # 50d：同步解析 verdict——仲裁结论为「需补充/需重新分析」时降级可见
                # 状态标记（不阻断输出，adjustments 仍应用）；有裁定 JSON 且 verdict
                # 非「通过」（含变体如「裁定通过」）即标记，不依赖 adjustments 非空
                adjustments, _arb_verdict = _extract_arbiter_adjustments(debate_result)
                arb_incomplete = bool(_arb_verdict) and "通过" not in _arb_verdict
                # 50e：仲裁补全重试（限 1 次）——verdict 为「需补充/需重新分析」时
                # 用辩论 LLM 追加一轮补全；成功则以新 verdict/adjustments 为准，
                # 失败则后续以横幅替换（「需补充」字样不穿透到最终输出）。
                if arb_incomplete:
                    try:
                        _debate_llm = getattr(self, "_last_debate_llm", None)
                        if _debate_llm is not None:
                            logger.warning(f"仲裁 verdict 不完整（{_arb_verdict}），发起补全重试")
                            from langchain_core.messages import HumanMessage as _HM2, SystemMessage as _SM2
                            _retry_resp = _invoke_llm_with_retry(_debate_llm, [
                                _SM2(content="你是审计仲裁人。上一次仲裁结论不完整，请补全。"
                                           "仅输出补全后的【仲裁结论】与完整【裁定JSON】。"),
                                _HM2(content=(
                                    f"上一次仲裁结论为「{_arb_verdict}」，不完整。请基于双方辩论意见"
                                    "与风险台账，输出补全后的【仲裁结论】与完整【裁定JSON】"
                                    "（adjustments 数组 + verdict 必须为'通过'或'需重新分析'之一）。\n\n"
                                    f"风险台账（摘要）：\n{risk_json[:4000]}\n\n"
                                    f"原仲裁文本：\n{debate_result[-2500:]}")),
                            ], label="仲裁·补全重试")
                            _retry_text = str(getattr(_retry_resp, "content", "") or "")
                            _retry_adj, _retry_verdict = _extract_arbiter_adjustments(_retry_text)
                            if _retry_verdict and _retry_verdict != _arb_verdict:
                                _arb_verdict = _retry_verdict
                                arb_incomplete = "通过" not in _arb_verdict
                                if _retry_adj:
                                    adjustments = adjustments + _retry_adj
                                debate_result = debate_result + "\n\n【仲裁补全重试】\n" + _retry_text
                                logger.info(f"仲裁补全重试结果：verdict={_arb_verdict}，"
                                            f"新增裁定 {len(_retry_adj)} 条")
                    except Exception as e:
                        logger.warning(f"仲裁补全重试失败（降级横幅兑底）: {e}")
                if adjustments:
                    # 50b：仲裁前后等级快照——仲裁改级后热力图/雷达图须按仲裁后
                    # 等级重生（LLM 主运行阶段生成的图表反映仲裁前等级）
                    try:
                        _pre_levels = {
                            str(r.get("risk_id", "")): str(r.get("level", ""))
                            for r in (json.loads(risk_json).get("risk_details") or [])
                            if isinstance(r, dict)}
                    except Exception:
                        _pre_levels = {}
                    # 仲裁回写时传入辩论 LLM：等级变更须重写 [风险判定] 推理链（补丁 1）
                    risk_json, applied = _apply_arbiter_adjustments(
                        risk_json, adjustments, llm=getattr(self, "_last_debate_llm", None))
                    if applied:
                        logger.info(f"仲裁裁定已回写风险台账：{applied} 条等级/备注调整")
                        # 50b：等级实际变更检测（仅既有条目等级变化触发图表重生）
                        try:
                            _post_levels = {
                                str(r.get("risk_id", "")): str(r.get("level", ""))
                                for r in (json.loads(risk_json).get("risk_details") or [])
                                if isinstance(r, dict)}
                            arb_level_changed = any(
                                _post_levels.get(k) != v for k, v in _pre_levels.items())
                        except Exception:
                            arb_level_changed = False
                        # 50b：信披结论一致性标注——仲裁新增/调整披露类风险条目后，前文
                        # 「合规评分 100 分无缺失」表述可能自相矛盾，生成注记供 PDF
                        # 信披章节/Excel 底稿/网页回复三处渲染。
                        try:
                            _ro2 = json.loads(risk_json)
                            from tools.pdf_export import _norm_dim
                            _comp_n = sum(
                                1 for r in (_ro2.get("risk_details") or [])
                                if isinstance(r, dict)
                                and _norm_dim(r.get("dimension")) == "disclosure_compliance")
                            _dc_raw = tool_results.get("check_disclosure_compliance", "")
                            _dc = json.loads(_dc_raw) if isinstance(_dc_raw, str) and _dc_raw else {}
                            if (_comp_n and isinstance(_dc, dict)
                                    and float(_dc.get("compliance_score", -1) or -1) == 100
                                    and not (_dc.get("issues") or [])
                                    and not (_dc.get("sections_missing") or [])):
                                disclosure_consistency_note = (
                                    f"仲裁阶段补充披露类风险 {_comp_n} 条，合规结论以仲裁后口径为准")
                                logger.info("信披结论一致性注记已生成：" + disclosure_consistency_note)
                        except Exception:
                            disclosure_consistency_note = ""
                        # 仲裁回写·消息层同步：仲裁可能新增/调整风险条目（实测 v27：
                        # 新增 R007 只写入导出台账，前端消息内嵌台账仍 5 条——与
                        # L 下沉消息层回写同构的展示不一致）。重建含 risk_details 的
                        # ```json 块为最新台账，其他块保持不动。
                        try:
                            if isinstance(last_ai.content, str):
                                _latest_ledger = json.loads(risk_json)
                                def _sync_ledger_block(m):
                                    try:
                                        _obj = json.loads(m.group(1))
                                        if _obj.get("risk_details") is not None:
                                            return "```json\n" + json.dumps(
                                                _latest_ledger, ensure_ascii=False,
                                                indent=2) + "\n```"
                                    except Exception:
                                        pass
                                    return m.group(0)
                                _new_content = re.sub(
                                    r"```json\s*(\{.*\})\s*```", _sync_ledger_block,
                                    last_ai.content, flags=re.S)
                                if _new_content != last_ai.content:
                                    last_ai.content = _new_content
                                    logger.info("仲裁回写·消息层：已同步前端消息中的风险台账")
                        except Exception:
                            pass  # 消息层同步失败不阻断（导出层已含仲裁结果）
                _arb_status = _arb_verdict if _arb_verdict else "未完成"
                # 50e：补全重试仍失败时，将「需补充」占位文本替换为未完成横幅
                # （「需补充」字样不得穿透到最终输出，含标题与提示）
                _incomplete_banner = ""
                if arb_incomplete:
                    _arb_status = "仲裁未完成"
                    _incomplete_banner = ("仲裁未完成（系统补全重试失败，本报告结论不生效，"
                                          "请重新运行或人工介入）")
                    debate_result = re.sub(r"(【仲裁结论】\s*)需补充", rf"\1{_incomplete_banner}",
                                           debate_result)
                    debate_result = re.sub(r'"verdict"\s*:\s*"需补充"',
                                           '"verdict": "仲裁未完成"', debate_result)
                    debate_result = debate_result.replace("需补充", "待补全")
                _arb_title = ("### 🔍 审计合伙人复核意见"
                              + (f"（仲裁状态：{_arb_status}）" if arb_incomplete else ""))
                _arb_lead = (f"\n\n⚠️ {_incomplete_banner}" if arb_incomplete else "")
                review_block = ((f"\n\n⚠️ 披露合规一致性提示：{disclosure_consistency_note}"
                                 if disclosure_consistency_note else "")
                                + _arb_lead + f"\n\n---\n{_arb_title}\n{debate_result}")
                # C 补丁：辩论评分引用防伪——AI 辩论可能把其他公司/历史版本的评分
                # 张冠李戴（实测幻觉：引用不存在的 72 分生成信披风险）。输出后校验
                # 引用与系统评分一致，不一致附可见警示（不篡改 LLM 原文，降级可见）。
                _warn = _score_mismatch_warning(debate_result, score_context)
                if _warn:
                    review_block += _warn
                # 50b：辩论金额防伪——辩论中自编金额（未见于风险台账）附可见警示
                _amt_warn = _amount_mismatch_warning(debate_result, risk_json)
                if _amt_warn:
                    review_block += _amt_warn
            else:
                # 降级可见原则：多智能体复核失败不再静默——除 _run_debate 内写日志外，
                # 还须在报告中追加醒目提示，避免用户误以为结论已通过复核环节
                # （与 PDF/Excel/综合评分兜底失败的可见降级保持一致）。
                review_block = (
                    "\n\n---\n### 🔍 审计合伙人复核意见\n"
                    "⚠️ 多智能体复核未能完成，本次结论未经过复核环节，请结合人工判断审慎采信（详见服务日志）。"
                )

        # 复核意见回写台账：PDF 综合汇总报告「审计合伙人复核意见」章与前端消息同源
        # （原实现只追加消息，PDF 无复核内容，前后端展示不一致）
        # 同步处理：勾稽差异条目并入（F3）+ 伪风险备查录（F4）
        try:
            report_obj = json.loads(risk_json)
            if review_block:
                report_obj["review_conclusion"] = review_block
            # 50d：仲裁状态写入台账（复核意见章渲染状态标记，与网页同源）
            if arb_incomplete:
                report_obj["arbiter_verdict"] = _arb_verdict or "未完成"
                report_obj["arbiter_incomplete"] = True
            # 50b：信披结论一致性注记写入台账（PDF 信披章节/Excel 底稿渲染）
            if disclosure_consistency_note:
                report_obj["disclosure_consistency_note"] = disclosure_consistency_note
            # 50c：跨期穿透注记——应收背离告警触发时写入确定性提示，防 LLM 用当期静态
            # 现金流比率（OCF/NP）掩盖应收激增的跨期风险（实测 17:59 版：用 OCF/NP=2.42
            # 反驳应收激增 67.18% 的传导链）。同步前置到复核意见可见区。
            try:
                _fin_raw = tool_results.get("calculate_financial_indicators", "")
                _fin = json.loads(_fin_raw) if isinstance(_fin_raw, str) and _fin_raw else {}
                _alerts = _fin.get("alerts") if isinstance(_fin, dict) else None
                if isinstance(_alerts, list) and any(
                        ("应收账款增速" in str(a)) or ("应收账款占营收比" in str(a))
                        for a in _alerts):
                    _pen_text = ("应收账款增速显著高于营收增速：不得以当期静态现金流比率"
                                 "替代跨期穿透分析，须核查经营性应付项目变动、应收票据"
                                 "贴现与回款质量")
                    report_obj["cashflow_penetration_note"] = _pen_text
                    review_block = (review_block or "") + f"\n\n⚠️ 跨期穿透提示：{_pen_text}。"
                    report_obj["review_conclusion"] = review_block
                    logger.info("跨期穿透注记已生成（应收背离告警触发）")
            except Exception:
                pass
            # Q 补丁：run_id 写入 company_info，供封面/页脚版本戳展示（产物可追溯）。
            # 优先取当前请求上下文（HTTP 入口已 set），次选 result 字段（CLI/离线路径）；
            # 两者均无时保持空串，产物版本戳回退为占位符（旧实例产物一眼可辨）。
            if isinstance(report_obj.get("company_info"), dict):
                _ctx_run_id = getattr(request_context.get(), "run_id", "") or ""
                report_obj["company_info"]["run_id"] = str(
                    _ctx_run_id or result.get("run_id", "") or "")

            # P11b: 归母口径修正后，清洗台账中基于合并口径勾稽失败生成的
            # 假阳性条目（LLM 看到旧 validate 输出生成的风险，证据已失效）
            if p11_fixed:
                _drop_false_positive_reconciliation_risks(report_obj)

            # F3: 勾稽差异风险条目并入台账（data_validator 已生成 V001... 条目，
            # 合并使"数据校验维度得分"在风险明细中有对应可追溯条目，消除黑盒）
            vd_raw = tool_results.get("validate_financial_data", "")
            if vd_raw:
                try:
                    vd_data = json.loads(vd_raw) if isinstance(vd_raw, str) else vd_raw
                    vd_risks = (vd_data.get("data_validation") or {}).get("risks", [])
                    if vd_risks and isinstance(vd_risks, list):
                        existing_ids = {r.get("risk_id") for r in report_obj.get("risk_details", [])
                                        if isinstance(r, dict)}
                        # F5: 事实去重——V 系列与 R 系列指向同一科目同一差异时降级为
                        # 证据附注，避免同一事实被重复定罪（实测缺陷：R005/V001 并列）
                        vd_risks = _dedupe_v_risks(vd_risks, report_obj.get("risk_details", []))
                        for vr in vd_risks:
                            if isinstance(vr, dict) and vr.get("risk_id") and vr.get("risk_id") not in existing_ids:
                                report_obj.setdefault("risk_details", []).append(vr)
                    # D 补丁：幽灵数据清洗——剔除引用已通过校验项的过期风险条目（含 R 系列
                    # 与并入的 V 系列）。LLM 生成的风险证据可能引用历史/已修复的勾稽差异
                    # （实测：历史版本引用"现金流勾稽差异16.71%"，当前校验已通过），
                    # 不剔除则跨版本幽灵数据进入底稿。无法对应到具体校验项的保守保留。
                    _drop_stale_validation_risks(report_obj, vd_data)
                    # 50b：勾稽伪风险拦截（D 补丁姊妹函数）——剔除引用已通过校验科目
                    # 但数字系 LLM 自编算术的条目
                    _drop_llm_fabricated_validation_risks(report_obj, vd_data)
                except Exception:
                    pass

            # F4: 伪风险反向校验器（保守）——level 为一般/低 且分析结论含明确否定表述
            # （如"持续经营风险较低""已排除"）的条目移至 excluded_items 备查录，
            # 不再进入风险明细/KPI/热力图，保持风险清单纯度（实测缺陷：R006 凑数）
            _NEGATION_MARKERS = ("无风险", "风险极低", "已排除嫌疑", "不存在风险",
                                 "不构成风险", "风险较低")
            details = report_obj.get("risk_details", [])
            kept, excluded = [], []
            for r in details:
                if not isinstance(r, dict):
                    kept.append(r)
                    continue
                level = str(r.get("level", "")).strip()
                conclusion_text = " ".join(str(r.get(k, "")) for k in ("title", "data_analysis", "evidence"))
                if level in ("一般", "低") and any(nm in conclusion_text for nm in _NEGATION_MARKERS):
                    excluded.append(r)
                else:
                    kept.append(r)
            if excluded:
                report_obj["risk_details"] = kept
                report_obj["excluded_items"] = excluded

            # L 补丁：系统评分快照写入（单一数据源）——结论章/底稿整体评估渲染时
            # 用正则把 LLM 正文里的旧评分引用替换为快照值，杜绝双分矛盾（0806 批：
            # 封面 31.5 vs 结论章 9）。50d：score=None（评分未获取）时同样写入快照，
            # 保持「快照恒存在、正文恒被归一」契约（否则结论章残留 LLM 旧分）。
            try:
                _score_raw = tool_results.get("calculate_comprehensive_score", "")
                _sd = json.loads(_score_raw) if isinstance(_score_raw, str) and _score_raw else {}
                if "score" in _sd and (_sd.get("score") is None
                                        or isinstance(_sd.get("score"), (int, float))):
                    report_obj["comprehensive_score_snapshot"] = {
                        "score": _sd["score"],
                        "level": str(_sd.get("level", "") or ""),
                    }
            except Exception:
                pass

            # 50c：风险等级底线规则——最终台账等级分布与综合评级矛盾时强制上调
            # （实测 17:59 版：5 个重要级风险仍评 10.5 分低风险）。触发时同步
            # tool_results（PDF 评分章/Excel 同口径）、重建评分卡并重刷消息层表述。
            try:
                _floor = _enforce_risk_level_floor(report_obj)
                if _floor:
                    _new_score_json, _floor_warn = _floor
                    tool_results["calculate_comprehensive_score"] = _new_score_json
                    _floor_sd = json.loads(_new_score_json)
                    _floor_note = report_obj.get("level_floor_note", "") or _floor_note
                    score_text = (f"\n\n<!--COMPREHENSIVE_SCORE-->\n{_new_score_json}"
                                  f"{_floor_warn}")
                    if _sync_score_into_message(last_ai, _floor_sd, floor_note=_floor_note):
                        logger.info("等级底线·消息层：已重刷前端消息中的评分表述")
            except Exception as e:
                logger.warning(f"风险等级底线规则执行失败（不阻断主流程）: {e}")

            # 50d：语义化编号派生（主 risk_id 不动，追加 semantic_id 供渲染追溯）
            try:
                _assign_semantic_ids(report_obj)
            except Exception as e:
                logger.warning(f"语义化编号派生失败（不阻断主流程）: {e}")

            # 50e：风险索引表（JSON 驱动）——从最终定稿台账生成，保证「JSON 里
            # 有的条目正文必然有」（含仲裁新增条目）；写入台账供导出层追溯，
            # 最终装配阶段注入回复正文。
            try:
                report_obj["risk_index_md"] = _build_risk_index_md(report_obj)
            except Exception as e:
                logger.warning(f"风险索引表生成失败（不阻断主流程）: {e}")

            # 50d：证据不足待核实标记——confidence < 0.5 的条目标记
            # pending_verification（不动等级与统计，渲染层加「待核实」标签）
            try:
                _pending_ids = []
                for r in report_obj.get("risk_details", []):
                    if not isinstance(r, dict):
                        continue
                    try:
                        _conf = float(r.get("confidence"))
                    except (TypeError, ValueError):
                        # 50d：置信度表述不规范/缺失时按 0.0 处理（触发待核实更安全）
                        _conf = 0.0
                    if _conf < 0.5:
                        r["pending_verification"] = True
                        _pending_ids.append(str(r.get("risk_id", "?")))
                if _pending_ids:
                    report_obj["pending_verification_note"] = (
                        "待核实事项：以下条目置信度低于 0.50、关键证据缺失，"
                        "不得作为已确认风险结论引用：" + "、".join(_pending_ids))
                    logger.info(f"证据不足待核实标记：{_pending_ids}")
            except Exception as e:
                logger.warning(f"待核实标记失败（不阻断主流程）: {e}")

            # M 补丁：系统自审条目剥离（自指词+财务锚门禁+issuer 豁免）——
            # 必须在四份产物导出之前对共享 report_obj 执行
            _strip_system_self_audit_items(report_obj)

            # 罗马数字序号：预计算本次运行的序号并写入 company_info，
            # 供 build_file_prefix / 趋势图兜底统一使用，保证四产物序号一致。
            # 注入失败时降级为 _run_number=None（趋势图兜底自行自动计算），
            # 显式初始化避免外层 try 吞异常后引用未绑定变量；
            # company_info 缺失时先写回台账，否则序号注入游离字典不生效。
            _run_number = None
            try:
                _ci = report_obj.get("company_info")
                if not isinstance(_ci, dict):
                    _ci = {}
                    report_obj["company_info"] = _ci
                _raw_company, _raw_year = resolve_company_year(_ci)
                _co = _sanitize_fn(_raw_company or "未知公司")
                _yr = _sanitize_fn(_raw_year) if _raw_year else ""
                _run_number = count_existing_runs(_co, _yr) + 1
                _ci["run_number"] = _run_number
            except Exception as e:
                logger.warning(f"run_number 预注入失败，趋势图将回退自动计算序号: {e}")

            risk_json = json.dumps(report_obj, ensure_ascii=False)
        except Exception:
            pass
        # 50b：仲裁改级后（applied>0 且存在 level 实际变更）即使 LLM 已调用过图表，
        # 也须用仲裁后 risk_json 强制重生（LLM 主运行阶段生成的图表反映仲裁前等级）；
        # 趋势图数据来自多年对比，不受仲裁影响，不重生。
        need_heatmap = (not skip_debate and not is_outlook) and (
            "generate_risk_heatmap" not in called or arb_level_changed)
        need_radar = (not skip_debate and not is_outlook) and (
            "generate_radar_chart" not in called or arb_level_changed)
        need_trend = "generate_trend_chart" not in called and not skip_debate and not is_outlook

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

        def _backfill_trend_chart():
            """趋势折线图兜底：优先复用多年对比结果构造时序数据（LLM 未主动
            调用时仍能出图，补齐三类图表）；无多年数据时降级为可见警告而非静默缺失。"""
            try:
                raw = tool_results.get("compare_multi_year", "")
                if isinstance(raw, str) and raw:
                    my_data = json.loads(raw)
                    years = my_data.get("years_analyzed") or []
                    ind = my_data.get("indicators_by_year") or {}
                    if len(years) >= 2:
                        try:
                            comp = (json.loads(risk_json).get("company_info") or {}).get("company_name", "未知公司")
                        except Exception:
                            comp = "未知公司"
                        trend_data = {
                            "company_name": comp,
                            # run_number 透传：兜底在 ThreadPoolExecutor 线程内执行，
                            # 趋势图文件名需与 PDF/Excel/热力图/雷达图共享同一罗马数字序号
                            "run_number": _run_number,
                            "years": [{"year": y,
                                       "revenue": ind.get(y, {}).get("revenue"),
                                       "net_profit": ind.get(y, {}).get("net_profit"),
                                       "operating_cashflow": ind.get(y, {}).get("operating_cashflow")}
                                      for y in years],
                        }
                        raw_url = str(generate_trend_chart.invoke(
                            {"trend_data_json": json.dumps(trend_data, ensure_ascii=False)}))
                        url = raw_url
                        try:
                            url = json.loads(raw_url).get("download_url") or raw_url
                        except Exception:
                            pass
                        return f"\n\n📊 趋势折线图: {url}"
            except Exception as e:
                logger.warning(f"趋势折线图兜底生成失败: {e}")
            return ("\n\n⚠️ 趋势折线图生成失败（缺少多年财务数据），"
                    "本次报告未附该图表（详见服务日志）。")

        def _backfill_export(tool_obj, prefix, fail_text, extra_args=None):
            """导出兜底：保持原有文案与失败提示格式。extra_args 用于给 PDF 导出
            补传财务指标/披露检查数据源（拆分报告需据此生成专项章节）。"""
            try:
                args = {'risk_report_json': risk_json}
                if extra_args:
                    args.update(extra_args)
                return f"\n\n{prefix}{tool_obj.invoke(args)}"
            except Exception as e:
                logger.warning(f"{prefix.strip()}兜底导出失败: {e}")
                return fail_text

        jobs = {}
        _hm_label = "风险热力图" + ("（仲裁后更新版）" if arb_level_changed else "")
        _rd_label = "财务雷达图" + ("（仲裁后更新版）" if arb_level_changed else "")
        if need_heatmap:
            jobs["heatmap"] = lambda: _backfill_chart(generate_risk_heatmap, _hm_label)
        if need_radar:
            jobs["radar"] = lambda: _backfill_chart(generate_radar_chart, _rd_label)
        if need_trend:
            jobs["trend"] = lambda: _backfill_trend_chart()
        if need_pdf:
            # PDF 拆分导出：补传财务指标/披露检查/评分/量化模型/校验/多年对比/审计意见
            # 工具结果（台账不受滑窗裁剪，即使早期工具消息已被窗口丢弃，仍能读到真实
            # 结果），供专项报告章节渲染；同时透传当前模块：点单模块（financial/compliance）
            # 只出对应专项报告，避免选财务健康却收到全套三份（synthesis/未指定仍出全部三份）。
            pdf_prefix = ("📎 PDF报告: " if pdf_llm_state is None else
                          "📎 PDF报告（LLM 导出参数不完整，系统已用完整数据重新生成）: ")
            jobs["pdf"] = lambda: _backfill_export(
                export_pdf_report, pdf_prefix,
                "\n\n⚠️ PDF报告导出失败，本次结论未生成可下载的 PDF 报告，请重试或联系维护者（详见服务日志）。",
                {"financial_indicators_json": tool_results.get("calculate_financial_indicators", ""),
                 "disclosure_check_json": tool_results.get("check_disclosure_compliance", ""),
                 "comprehensive_score_json": tool_results.get("calculate_comprehensive_score", ""),
                 "risk_models_json": tool_results.get("calculate_risk_models", ""),
                 "validation_json": tool_results.get("validate_financial_data", ""),
                 "compare_multi_year_json": tool_results.get("compare_multi_year", ""),
                 "audit_opinion_json": tool_results.get("identify_audit_opinion", ""),
                 "module": self._module or ""})
        if need_excel:
            # S4 事实层注入：Excel 底稿同样携带工具 JSON，风险明细追加"系统量化事实"列
            jobs["excel"] = lambda: _backfill_export(
                export_excel_report, "📊 Excel底稿: ",
                "\n\n⚠️ Excel底稿导出失败，本次结论未生成可下载的 Excel 底稿，请重试或联系维护者（详见服务日志）。",
                {"validation_json": tool_results.get("validate_financial_data", ""),
                 "financial_indicators_json": tool_results.get("calculate_financial_indicators", ""),
                 "disclosure_check_json": tool_results.get("check_disclosure_compliance", ""),
                 "audit_opinion_json": tool_results.get("identify_audit_opinion", "")})

        links = ""
        if jobs:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = {name: pool.submit(fn) for name, fn in jobs.items()}
                outputs = {name: fut.result() for name, fut in futures.items()}
            # 展示顺序固定：图表在前、报告文件在后（与阅读动线一致）
            for key in ("heatmap", "radar", "trend", "pdf", "excel"):
                links += outputs.get(key, "")

        if links:
            if isinstance(last_ai.content, str):
                last_ai.content += links
            else:
                last_ai.content.append(links)

        # 第七步：将辩论复核意见（或失败提示）追加到回复末尾（保持链接在前的展示顺序）
        # 50e：风险索引表注入（JSON 驱动，含仲裁新增条目）——在复核意见之前，
        # 保证正文风险清单与底稿同源完备（LLM 明细文本保留为分析段落）。
        try:
            _risk_index_md = report_obj.get("risk_index_md", "") if isinstance(report_obj, dict) else ""
        except Exception:
            _risk_index_md = ""
        if _risk_index_md:
            if isinstance(last_ai.content, str):
                last_ai.content += "\n\n" + _risk_index_md
            else:
                last_ai.content.append("\n\n" + _risk_index_md)
        if review_block:
            if isinstance(last_ai.content, str):
                last_ai.content += review_block
            else:
                last_ai.content.append(review_block)

        # ─── 综合评分结果追加到消息末尾（计算已前移并回写 tool_results，
        # 此处仅保持原展示顺序：链接 → 复核意见 → 评分卡）───
        if score_text is not None:
            if isinstance(last_ai.content, str):
                last_ai.content += score_text
            else:
                last_ai.content.append(score_text)

        # 软约束顺序提示（如有）：附在报告末尾供人工复核（降级可见，不中断产出）
        if soft_order_warn:
            if isinstance(last_ai.content, str):
                last_ai.content += soft_order_warn
            else:
                last_ai.content.append(soft_order_warn)

        # AI 生成声明系统兜底：sp 要求每次回复末尾附加声明，LLM 偶发漏附时
        # 由系统补上（与 PDF/Excel 的免责声明后置校验同构，防合规声明缺失）。
        _AI_DISCLAIMER = ("\n\n⚠️ 以上分析由 AI 辅助生成，仅供审计参考，"
                          "不构成专业审计意见或投资建议，请结合人工专业判断进行复核确认。")
        _tail = last_ai.content if isinstance(last_ai.content, str) else ""
        if "辅助生成" not in _tail and "免责" not in _tail:
            if isinstance(last_ai.content, str):
                last_ai.content += _AI_DISCLAIMER
            else:
                last_ai.content.append(_AI_DISCLAIMER)

        # 50e：最终装配后回刷（修复时序缺陷）——底线规则的消息层回刷此前执行于
        # review_block 追加进 content 之前，辩论文本中的旧分引用（如「综合评分
        # 15.0分」）未被回刷；此处在所有内容追加完成后兑底回刷，保证最终消息
        # （含辩论段落）评分表述与系统底线分一致。
        if _floor_sd is not None:
            try:
                if _sync_score_into_message(last_ai, _floor_sd, floor_note=_floor_note):
                    logger.info("最终装配后回刷：辩论文本评分表述已同步为底线分")
            except Exception as e:
                logger.warning(f"最终装配后回刷失败（不阻断主流程）: {e}")

        return result

    @staticmethod
    def _llm_export_was_complete(messages, tool_name: str, called: set = None):
        """双保险：检查 LLM 是否已调用导出工具且传参完整。

        LLM 工具集已移除导出工具（导出收敛为系统兜底唯一路径），此判定防御
        历史会话续接/模块裁剪回退全量等异常场景：LLM 若调用导出但传参不全
        （专项入参为空），系统仍须兜底重导，避免产出残缺版报告。

        Args:
            messages: 消息列表（扫描 tool_calls）
            tool_name: 导出工具名
            called: 已调用工具名集合（ToolMessage 证据；消息中无 tool_calls 可查时
                以此确认 LLM 确已调用——窗口裁剪可能只保留结果消息）

        Returns:
            None: LLM 未调用该工具
            True: 已调用且入参完整（或参数不可验证但确已调用，不重复导出）
            False: 已调用但入参不完整（PDF 需 risk_report_json + 任一专项入参；
                   Excel 仅需 risk_report_json）
        """
        for m in messages:
            for tc in (getattr(m, "tool_calls", None) or []):
                if tc.get("name") != tool_name:
                    continue
                args = tc.get("args") or {}
                if not str(args.get("risk_report_json") or "").strip():
                    return False
                if tool_name == "export_pdf_report":
                    # PDF 专项章依赖财务/披露/评分入参：全空则视为不完整
                    if not any(str(args.get(k) or "").strip() for k in
                               ("financial_indicators_json", "disclosure_check_json",
                                "comprehensive_score_json")):
                        return False
                return True
        # 消息中无 tool_calls 可查：若 ToolMessage 证明已调用（滑窗裁剪场景），
        # 参数不可验证 → 不重复导出（保持旧行为，避免 LLM 已导出后系统再导一份）
        if called and tool_name in called:
            return True
        return None

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

    def _run_debate(self, risk_json: str, score_context: str = "") -> str | None:
        """多智能体辩论机制：风险关注方 -> 风险否定方 -> 裁判仲裁。

        三步辩论流程：
        1. 风险关注方(Risk Advocate)：审查风险台账，挖掘被低估或遗漏的风险
        2. 风险否定方(Risk Skeptic)：逐条反驳关注方的质疑，提供合理商业解释
        3. 裁判仲裁人(Arbiter)：综合双方意见，对每条争议风险做最终裁定

        Args:
            risk_json: 风险台账 JSON 字符串
            score_context: 系统计算的综合评分上下文（注入辩论 prompt，
                防止 LLM 臆造评分数字——实测缺陷：辩论中把 6.0 分幻觉成 0 分）

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
                HumanMessage(content=f"请审查以下审计风险分析报告的风险台账。\n\n{score_context}\n\n风险台账数据：\n{risk_json[:6000]}"),
            ], label="辩论·风险关注方")
            advocate_text = advocate_response.content or ""
            logger.info(f"风险关注方分析完成（{len(advocate_text)}字）")

            # Step 2: 风险否定方反驳
            logger.info("辩论 Step 2/3: 风险否定方反驳中...")
            skeptic_response = _invoke_llm_with_retry(debate_llm, [
                SystemMessage(content=SKEPTIC_SYSTEM_PROMPT),
                HumanMessage(content=(
                    f"请对风险关注方的意见进行逐条反驳。\n\n{score_context}\n\n"
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
                    f"请综合双方辩论意见，对争议风险进行逐条裁定。\n\n{score_context}\n\n"
                    f"风险关注方意见：\n{advocate_text[:2500]}\n\n"
                    f"风险否定方意见：\n{skeptic_text[:2500]}"
                )),
            ], label="辩论·裁判仲裁")
            arbiter_text = arbiter_response.content or ""
            logger.info(f"裁判仲裁完成（{len(arbiter_text)}字）")

            # 组装辩论结果，使用结构化标记便于前端三段式渲染
            # 同时保存辩论 LLM 引用：仲裁回写重写推理链时复用同一实例（补丁 1）
            self._last_debate_llm = debate_llm
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


def build_agent(ctx=None, model_override=None, module=None):
    """构建年报风险分析 Agent。

    完整流程：
    1. 读取 agent_llm_config.json 获取 LLM 参数（模型名、温度、top_p 等）
    2. 从环境变量获取 API Key 和 Base URL
    3. 创建 ChatOpenAI 实例（兼容 DeepSeek API）
    4. 注册全部 16 个分析工具（module 不为空时只注册该模块的工具子集）
    5. 通过 LangGraph create_react_agent 构建 ReAct 模式 Agent
    6. 用 _AgentWrapper 包装以提供兜底导出机制

    Args:
        ctx: 可选的请求上下文对象，用于传递请求头等信息
        model_override: 可选的主分析模型名覆盖（快速模式传入 FAST_MODEL；缺省时用 config
            中的 model，当前全链路 deepseek-v4-flash。普通/快速模式的差异现在主要
            在链路（图表+辩论复核 vs 精简）而非模型）
        module: 可选的任务模块标识，取值 financial / compliance / synthesis。
            传入时仅注册该模块所需工具（减少 LLM 选择空间与往返，提升单模块速度）；
            缺省（None）时注册全量工具，行为与改造前完全一致。

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

    # 注册全部分析工具列表（顺序不影响执行，Agent 自主决定调用顺序）
    tools = [
        parse_pdf_report,              # PDF 年报解析
        calculate_financial_indicators, # 财务指标计算
        search_regulations,            # 法规知识库检索
        compare_multi_year,            # 多年数据对比分析
        # 注：export_pdf_report/export_excel_report 不再注册给 LLM——导出由系统在
        # 后处理中兜底执行（_post_process），保证始终以完整数据（含仲裁回写/评分
        # 兜底）生成报告；LLM 主动调用只能传自己上下文里的参数，产出残缺版。
        validate_financial_data,       # 财务数据一致性校验
        generate_risk_heatmap,         # 风险热力图生成
        generate_radar_chart,          # 财务雷达图生成
        generate_trend_chart,          # 趋势折线图生成
        batch_analyze_companies,       # 多公司批量分析
        check_disclosure_compliance,   # 信息披露规范性检查
        calculate_comprehensive_score, # 综合风险评分
        calculate_risk_models,         # Altman Z-Score / Beneish M-Score 量化预警
        identify_audit_opinion,        # 审计意见类型识别
        investment_advisor,            # 智能投资参考卡（C 端轻量，风险提示定位）
        industry_outlook,              # 行业风向标（C 端轻量，新闻+知识库驱动）
        search_regulatory_inquiries,   # 监管问询在线查询（上交所/深交所，网络失败自动跳过）
    ]

    # 模块裁剪：单模块分析时只注册本模块所需工具，缩小 LLM 的选择空间，
    # 避免它去调无关工具（既提速也使输出更聚焦）。module 为 None 时保留全量。
    if module:
        allowed = MODULE_TOOLS.get(module) or LIGHT_MODULE_TOOLS.get(module)
        if allowed:
            tools = [t for t in tools if t.name in allowed]
            logger.info(f"模块工具裁剪：module={module}，保留 {len(tools)} 个工具")
        else:
            logger.warning(f"未知模块标识 {module}，回退全量工具")

    # 使用 LangGraph 的 create_react_agent 构建 ReAct 模式智能体
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=cfg.get("sp"),           # 系统提示词（System Prompt）
        checkpointer=get_memory_saver(), # 会话记忆存储
        state_schema=AgentState,         # 自定义状态 schema
        post_model_hook=_accumulate_tool_ledger,  # 裁剪前累积工具链台账，与滑窗解耦
    )

    # 包装为 _AgentWrapper 以提供兜底导出能力（透传 module 供后处理差异化）
    return _AgentWrapper(agent, module=module)
