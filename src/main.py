"""本地运行入口 - 精简版 FastAPI 服务

替代 main.py 中大量 coze_coding_utils 依赖，使用 local_shims 提供兼容。
启动方式: python -m main 或 uvicorn main:app --port 5000
"""
import argparse
import asyncio
import json
import os
import re
import traceback
import logging
import uuid
import tempfile
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Any, Dict, Optional, List

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage

from agents.pipeline import SYNTHESIS_STAGES, build_stage_payload, extract_stage_summary

from local_shims import (
    new_context, Context, request_context,
    setup_logging, LOG_FILE, LOG_LEVEL,
    ErrorClassifier, graph_helper,
    init_run_config, init_agent_config, extract_core_stack,
    cozeloop, OpenAIChatHandler,
    AsyncTaskRuntime, AsyncTaskStorageError, async_task_config,
    extract_biz_context, parse_deadline_sec, HEADER_X_RUN_ID, normalize_run_id,
)
from storage.memory.memory_saver import get_memory_saver

setup_logging(
    log_file=LOG_FILE,
    max_bytes=100 * 1024 * 1024,
    backup_count=5,
    log_level=LOG_LEVEL,
    console_output=True,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 900

TOOL_PIPELINE = [
    ("parse_pdf_report",                  "解析年报文件",     8),
    ("validate_financial_data",           "校验财务数据",    18),
    ("calculate_financial_indicators",    "计算财务指标",    30),
    ("calculate_risk_models",             "量化风险模型预警", 36),
    ("check_disclosure_compliance",       "检查披露合规",    42),
    ("identify_audit_opinion",            "识别审计意见",    48),
    ("search_regulations",                "检索法规条文",    55),
    ("search_regulatory_inquiries",        "查询监管问询",    56),
    ("industry_outlook",                  "研判行业风向",    58),
    ("compare_multi_year",               "多年数据对比",    60),
    ("calculate_comprehensive_score",     "综合风险评分",    68),
    ("generate_risk_heatmap",             "生成风险热力图",  76),
    ("generate_radar_chart",              "生成财务雷达图",  84),
    ("generate_trend_chart",              "生成趋势折线图",  87),
    ("investment_advisor",                "生成投资参考卡",  90),
    ("export_pdf_report",                 "导出 PDF 报告",   92),
    ("export_excel_report",               "导出 Excel 底稿", 95),
]

TOOL_NAME_TO_STEP = {name: (label, pct) for name, label, pct in TOOL_PIPELINE}

# ── 分级提速：全接口均用 deepseek-v4-flash（config 主模型）。普通模式跑完整链路
# （图表 + 三方辩论复核，目标 ≤3 分钟）；快速模式（前端 ⚡ 开关在用户消息末尾
# 追加“快速模式”关键词）走精简链路：跳图表/跳辩论/限 3 条风险，目标 ≤1 分钟。
# FAST_MODEL 保留独立配置位，便于未来把普通模式单独切回更强模型。
FAST_MODE_KEYWORD = "快速模式"
FAST_MODEL = os.getenv("FAST_MODEL", "deepseek-v4-flash")

# ── 三模块路由（架构定稿的三层任务单元）──
# 复用与 FAST_MODE_KEYWORD 同款的「消息关键词标记」方案：前端在发送内容首部注入
# 标记，后端据此选择工具子集与提示词，零协议改动、与现有链路完全兼容。
# 混合运行：financial / compliance 单模块快跑；synthesis 串跑①→②→③。
MODULE_MARKERS = {
    "financial": "【模块:财务健康度诊断】",
    "compliance": "【模块:合规与经营风险扫描】",
    "synthesis": "【模块:综合研判】",
    "outlook": "【模块:行业风向研判】",
}


def _detect_module(payload) -> Optional[str]:
    """检测请求载荷中的模块标记，返回模块标识或 None（与 _payload_wants_fast_mode 同构）。

    未命中任何标记时返回 None，走全量工具链路（兼容直接输入文字的传统用法）。
    """
    for m in (payload or {}).get("messages", []):
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        text = str(content)
        for key, marker in MODULE_MARKERS.items():
            if marker in text:
                return key
    return None


def expected_artifacts(module: Optional[str], fast: bool) -> list:
    """本次运行应生成的产物类型清单（与 Agent 的 artifact_expectations 同构）。

    仅登记类型、不代表文件已落盘：用于运行开始时向前端声明「生成中」的产物，
    最终以真实存在的文件覆盖为成功或失败，避免用户中途无从判断产物范围。
    快速模式跳图表、outlook 模块只出卡片，均与 Agent 侧装配保持一致。
    """
    is_outlook = module == "outlook"
    items = []
    if not is_outlook and not fast:
        items += [
            {"key": "heatmap", "kind": "chart", "label": "风险热力图"},
            {"key": "radar", "kind": "chart", "label": "财务雷达图"},
            {"key": "trend", "kind": "chart", "label": "趋势折线图"},
        ]
    if not is_outlook:
        if module in (None, "synthesis"):
            items += [
                {"key": "pdf_financial", "kind": "pdf", "label": "财务健康诊断报告"},
                {"key": "pdf_compliance", "kind": "pdf", "label": "合规与信息披露报告"},
                {"key": "pdf_synthesis", "kind": "pdf", "label": "综合汇总报告"},
            ]
        elif module == "financial":
            items.append({"key": "pdf_financial", "kind": "pdf", "label": "财务健康诊断报告"})
        else:
            items.append({"key": "pdf_compliance", "kind": "pdf", "label": "合规与信息披露报告"})
        items.append({"key": "excel", "kind": "xlsx", "label": "Excel审计底稿"})
    return items


# ── C 端轻量工具路由（与 MODULE_MARKERS 同构的消息标记方案）──
# 命中后走独立工具子集（agent.py 的 LIGHT_MODULE_TOOLS）且跳过 _post_process
# 兜底（不辩论/不导出 PDF·Excel/不跑综合评分）：轻量对话产物是卡片不是审计报告。
LIGHT_MARKERS = {
    "advisor": "【工具:投资参考】",
    "industry": "【工具:行业风向】",
}

# 轻量工具名 → 前端专属卡片渲染 marker（与 index.html 的提取正则字面一致）。
# 服务端从 ToolMessage 原文确定性注入，不依赖 LLM 忠实复制 JSON（复刻评分卡
# <!--COMPREHENSIVE_SCORE--> 契约），且随 ai_text 持久化到会话历史。
LIGHT_TOOL_MARKERS = {
    "investment_advisor": "<!--INVESTMENT_CARD-->",
    "industry_outlook": "<!--INDUSTRY_OUTLOOK-->",
}


def _detect_light_module(payload) -> Optional[str]:
    """检测 C 端轻量工具标记，返回轻量模块键或 None。

    与 _detect_module 不同：只扫最后一条消息（当前请求）。前端 payload 携带
    会话历史，若扫全部消息，历史里的旧轻量标记会把后续普通提问也误路由到
    轻量链路；反之历史里的模块标记也会压过用户刚点的轻量卡片。
    最后一条消息代表最新用户意图，以它为准。
    """
    msgs = (payload or {}).get("messages", [])
    if not msgs:
        return None
    m = msgs[-1]
    content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
    text = str(content)
    for key, marker in LIGHT_MARKERS.items():
        if marker in text:
            return key
    return None


def _payload_wants_fast_mode(payload) -> bool:
    """检测请求载荷是否启用快速模式（与 agent.py 跳过辩论的关键词保持一致）。

    兼容两种消息形态：前端 JSON 的 dict（{"role","content"}）与 LangChain 消息对象。
    """
    for m in (payload or {}).get("messages", []):
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if FAST_MODE_KEYWORD in str(content):
            return True
    return False


# ── 多用户登录与分析历史：认证头解析 + 历史落库辅助 ──
AUTH_TOKEN_HEADER = "X-Auth-Token"


def _current_user(request: Request):
    """从请求头解析当前登录用户；未登录/令牌无效返回 None（不阻断分析）。"""
    token = request.headers.get(AUTH_TOKEN_HEADER, "")
    if not token:
        return None
    try:
        from storage.database.user_service import resolve_user
        return resolve_user(token)
    except Exception as e:  # noqa: BLE001 - 认证存储异常时降级为游客，不阻断主功能
        logger.warning(f"解析登录态失败（按游客处理）: {e}")
        return None


def _extract_history_fields(report: dict) -> dict:
    """从最终报告中抽取历史记录字段（公司/年度/评分/等级/文件）。

    数据来源：
    - 公司名/年度：复用 agents.agent._extract_risk_json 从 AI 回复抠风险台账
    - 评分/等级：<!--COMPREHENSIVE_SCORE--> 标记后的综合评分 JSON
    - 文件：报告的 files + images 路径列表
    任一段解析失败均降级为缺省值，不抛异常。
    """
    ai_text = str(report.get("ai_text", "") or "")
    fields = {"company_name": "", "report_year": "", "score": None, "risk_level": "", "summary": ""}
    risk_data = {}
    try:
        from agents.agent import _extract_risk_json
        risk_json = _extract_risk_json(ai_text)
        if risk_json:
            risk_data = json.loads(risk_json)
            # 别名兼容：LLM 可能用 name/report_period 等键名（与导出工具同源逻辑）
            from utils.filename import resolve_company_year
            company, year = resolve_company_year(risk_data.get("company_info", {}) or {})
            fields["company_name"] = company
            fields["report_year"] = year
    except Exception:
        pass
    try:
        marker = "<!--COMPREHENSIVE_SCORE-->"
        if marker in ai_text:
            score_data = json.loads(ai_text.split(marker, 1)[1].strip().split("\n\n", 1)[0])
            fields["score"] = score_data.get("score")
            fields["risk_level"] = str(score_data.get("level", "") or "")
            fields["summary"] = str(score_data.get("summary", "") or "")
    except Exception:
        pass
    # 兜底：无 marker（或解析失败）时，从风险台账内嵌的 comprehensive_score 对象补齐
    if fields["score"] is None and isinstance(risk_data.get("comprehensive_score"), dict):
        cs = risk_data["comprehensive_score"]
        fields["score"] = cs.get("score")
        fields["risk_level"] = fields["risk_level"] or str(cs.get("level", "") or "")
        fields["summary"] = fields["summary"] or str(cs.get("summary", "") or cs.get("note", "") or "")
    fields["files"] = (
        [f.get("path", "") for f in report.get("files", []) if f.get("path")]
        + [i.get("path", "") for i in report.get("images", []) if i.get("path")]
    )
    return fields


def _artifact_local_path(path: str) -> str:
    """将本地存储 URL 或 file URL 解析为服务进程可访问的文件路径。"""
    raw = str(path or "").strip()
    if not raw:
        return ""
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        candidate = unquote(parsed.path or "")
        # Windows file URL 常见形式为 file:///C:/path 或 file://C:/path。
        if re.match(r"^/[A-Za-z]:[\\/]", candidate):
            candidate = candidate[1:]
        elif parsed.netloc and re.match(r"^[A-Za-z]:$", parsed.netloc):
            candidate = parsed.netloc + candidate
        return os.path.abspath(candidate) if candidate else ""
    if raw.startswith("/local_storage/"):
        relative = raw[len("/local_storage/"):].replace("/", os.sep)
        return os.path.abspath(os.path.join(os.getcwd(), "local_storage", relative))
    if raw.startswith("local_storage/"):
        relative = raw[len("local_storage/"):].replace("/", os.sep)
        return os.path.abspath(os.path.join(os.getcwd(), "local_storage", relative))
    return os.path.abspath(raw) if os.path.isabs(raw) else ""


def _artifact_is_accessible(path: str) -> bool:
    """仅接受真实存在且可读的文件作为下载产物。"""
    local_path = _artifact_local_path(path)
    return bool(local_path and os.path.isfile(local_path) and os.access(local_path, os.R_OK))


def _artifact_key(path: str) -> str:
    """按文件名将真实产物映射到结构化期望键。"""
    name = Path(str(path or "")).name.lower()
    suffix = Path(name).suffix
    if suffix in {".png", ".jpg", ".jpeg"}:
        if "热力" in name:
            return "heatmap"
        if "雷达" in name:
            return "radar"
        if "趋势" in name:
            return "trend"
        return "chart_unknown"
    if suffix == ".xlsx":
        return "excel"
    if suffix == ".pdf":
        if "财务健康" in name:
            return "pdf_financial"
        if "合规" in name or "信息披露" in name:
            return "pdf_compliance"
        return "pdf_synthesis"
    return suffix.lstrip(".") or "file"


def _build_data_sources(tool_result_index: dict) -> list:
    """构造「数据来源与完整性说明」清单（网页端）。

    未获取时只写原因本身：前端表格另有「状态」列显示「未获取」，原因再带
    「未获取：」前缀会在同一句里重复两次（实测缺陷）。

    审计意见识别与 PDF 端同为双源判定：识别工具未调用时，披露检查已输出的
    audit_opinion 仍算已获取，避免封面显示意见而来源说明标未获取的「薛定谔状态」。
    """
    def _used_json(name):
        raw = tool_result_index.get(name, "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(parsed, dict) and "error" not in parsed

    disclosure_data = {}
    try:
        _dc = json.loads(tool_result_index.get("check_disclosure_compliance", "{}") or "{}")
        disclosure_data = _dc if isinstance(_dc, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        disclosure_data = {}
    opinion_used = _used_json("identify_audit_opinion") or bool(disclosure_data.get("audit_opinion"))
    source_specs = (
        ("财务指标数据", _used_json("calculate_financial_indicators"),
         "未上传年报或文本过短（预处理跳过），未产出结构化财务指标"),
        ("披露规范性检查", _used_json("check_disclosure_compliance"),
         "未调用披露检查工具或年报文本不足以检查"),
        ("综合风险评分", _used_json("calculate_comprehensive_score"),
         "未调用评分工具且系统兜底评分失败"),
        ("多年指标趋势", _used_json("compare_multi_year"),
         "未提供多年财务数据（需至少 2 个年度）"),
        ("审计意见识别", opinion_used,
         "未识别到审计意见章节或未调用识别工具"),
        ("量化模型预警", _used_json("calculate_risk_models"),
         "缺少多期报表数据（Altman Z-Score / Beneish M-Score 需多年数据）"),
        ("数据勾稽校验", _used_json("validate_financial_data"),
         "缺少结构化财务数据（勾稽校验需三大报表字段）"),
    )
    return [{"name": name, "used": bool(used), "note": "" if used else note}
            for name, used, note in source_specs]


def _record_history(user, run_id: str, report: dict, fast: bool):
    """分析完成后落历史（仅登录用户；失败只记日志，绝不影响主流程）。"""
    if not user:
        return
    try:
        from storage.database.user_service import save_history
        fields = _extract_history_fields(report or {})
        save_history(
            user["user_id"], run_id,
            company_name=fields["company_name"], report_year=fields["report_year"],
            score=fields["score"], risk_level=fields["risk_level"],
            mode="flash" if fast else "pro", files=fields["files"], summary=fields["summary"],
        )
        logger.info(f"分析历史已记录: user={user['username']} run_id={run_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"分析历史记录失败（不影响主报告）: {e}")


class GraphService:
    """核心服务：封装 Agent 调用、流式推送、任务生命周期管理。

    负责处理前端通过 /run 和 /stream_run 发来的审计分析请求，
    将用户输入转发给 LangGraph ReAct Agent，并管理异步任务取消和超时控制。
    """
    def __init__(self):
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self.error_classifier = ErrorClassifier()
        # 按模式缓存双 Agent 实例："pro"=普通模式（config 主模型），"flash"=快速模式（FAST_MODEL）
        # 两实例共享同一 checkpointer（get_memory_saver 单例），同 thread_id 会话可跨模式续接
        self._agents: Dict[str, Any] = {}

    def _get_agent(self, ctx=None, fast: bool = False, module: Optional[str] = None):
        """按「模式 + 模块」组合缓存 Agent 实例。

        缓存键从原来的 pro/flash 扩展为 {mode}:{module}，因为不同模块注册的工具
        子集不同，必须分别构建；所有实例共享同一 checkpointer（单例），
        因此同一 thread_id 的会话可跳模式/跳模块续接。
        """
        key = f"{'flash' if fast else 'pro'}:{module or 'all'}"
        if key not in self._agents:
            build_kwargs = {"model_override": FAST_MODEL} if fast else {}
            if module:
                build_kwargs["module"] = module
            self._agents[key] = graph_helper.get_agent_instance("agents.agent", ctx, **build_kwargs)
            logger.info(f"Agent 实例已构建：key={key}"
                        + (f", model={FAST_MODEL}" if fast else "（config 主模型）"))
        return self._agents[key]

    # ── P1 预处理并行注入：进 Agent 前由系统先提取财务数据，并行预跑
    # 校验/指标/披露三工具，把结果以「已完成工具轨迹」（AIMessage.tool_calls +
    # ToolMessage）注入上下文。LLM 看到历史里已调过这三步就不会重复调用，
    # 省 2-3 次 LLM 往返（每次 5-15s）；台账/门禁/前端工具链展示/评分兜底
    # 全部自动兼容（轨迹与真实调用形态一致）。
    # fail-open：任一环节失败/超时都静默回退原链路，预处理只是加速器。
    _PREPROCESS_MIN_CHARS = 800   # 短文本（无数据密度）不值得多一次提取调用
    _EXTRACT_TIMEOUT = 25         # 提取超时即放弃，不阻塞主链路

    @staticmethod
    def _last_user_text(payload) -> str:
        """取 payload 中最后一条 user 消息文本（预处理输入源）。"""
        try:
            for m in reversed(payload.get("messages") or []):
                if isinstance(m, dict) and m.get("role") == "user":
                    return str(m.get("content", "") or "")
        except Exception:
            pass
        return ""

    async def _extract_financial_json(self, text: str):
        """用快速模型从年报文本提取结构化财务数据 JSON；失败返回 None。"""
        from langchain_openai import ChatOpenAI
        from utils.llm import thinking_extra_body
        llm = ChatOpenAI(
            model=FAST_MODEL,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
            temperature=0,
            max_tokens=1200,
            timeout=self._EXTRACT_TIMEOUT,
            max_retries=0,
            extra_body=thinking_extra_body(),  # 仅 DeepSeek 下发禁用思考模式，其它 provider 保持纯 OpenAI 协议
        )
        prompt = (
            "从下面的年报/财务文本中提取财务数据，仅输出一个 JSON 对象，不要任何解释。\n"
            "尽量提取以下字段（数值型；金额字段严格按原文报表明确单位保留原值，"
            "例如原文单位为亿元且表内为840则写840，不要自行换算为元；比率/次数按原值；"
            "文本中缺失的字段直接省略，禁止编造）：\n"
            "total_assets, total_liabilities, net_assets, net_profit（合并口径净利润）, "
            "net_profit_parent（归属于母公司股东的净利润）, operating_cashflow, "
            "depreciation, amortization, working_capital_change, retained_earnings_begin, "
            "retained_earnings_end, dividends, revenue_current, revenue_previous, "
            "net_profit_current, net_profit_previous, operating_cashflow_current, "
            "operating_cashflow_previous, total_assets_current, total_liabilities_current, "
             "accounts_receivable_current, accounts_receivable_previous, inventory_current, "
             "inventory_previous, goodwill, monetary_funds, short_term_loans, industry, period, scope\n"
             "（industry 为字符串，取：制造业/房地产/互联网/医药/金融/零售/能源/农业/军工/传媒 之一；"
             "period 为本次报告期，例如 2025年度/2025-12-31；scope 取合并或母公司）\n\n"
            f"文本：\n{text}"
        )
        resp = await asyncio.wait_for(llm.ainvoke(prompt), timeout=self._EXTRACT_TIMEOUT + 5)
        raw = str(resp.content).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or len(data) < 3:
            # 提取不出足够字段说明文本无数据密度，预跑无意义
            return None
        return json.dumps(data, ensure_ascii=False)

    async def _preprocess_inject(self, payload):
        """构造预跑工具轨迹消息列表；不可用时返回 None（走原链路）。"""
        try:
            text = self._last_user_text(payload)
            if len(text) < self._PREPROCESS_MIN_CHARS:
                return None
            data_json = await self._extract_financial_json(text[:30000])
            if not data_json:
                return None
            # P8: 单位口径——只接受年报明确声明的统一单位并原样记录，
            # 不根据金额量级猜测或再次换算。缺少声明时保留原值并标记待复核。
            from tools.financial_calculator import (detect_amount_unit,
                                                    extract_parent_net_profit,
                                                    normalize_financial_units)
            try:
                _raw = json.loads(data_json)
                if isinstance(_raw, dict):
                    _unit = detect_amount_unit(text)
                    if _unit:
                        _raw["amount_unit"] = _unit
                    data_json = json.dumps(normalize_financial_units(_raw), ensure_ascii=False)
                    # P11: 归母净利润规则兜底——提取模型未输出 net_profit_parent 时，
                    # 用确定性正则从年报文本提取（未分配利润勾稽必须用归母口径，
                    # 否则产生假阳性，实测缺陷：20.43% vs 0.34%）
                    if not isinstance(_raw.get("net_profit_parent"), (int, float)) and _unit:
                        _np_parent = extract_parent_net_profit(text)
                        if _np_parent is not None:
                            _factor = {"元": 1, "千元": 1000, "万元": 10000,
                                       "百万元": 1000000, "亿元": 100000000}.get(_unit)
                            if _factor:
                                # 正则提取返回元；转回提取数据的明确原文单位，
                                # 与其他金额事实保持同一口径，不丢失原始单位。
                                _raw["net_profit_parent"] = _np_parent / _factor
                            data_json = json.dumps(_raw, ensure_ascii=False)
            except Exception:
                pass
            from tools.data_validator import validate_financial_data
            from tools.financial_calculator import calculate_financial_indicators
            from tools.disclosure_checker import check_disclosure_compliance
            from tools.risk_models import calculate_risk_models
            # 校验/指标/量化模型无相互依赖，线程并行；量化模型与指标同源
            # （同一份提取数据），保证 Z/M-Score 可追溯且不因 LLM 自行传参而漂移
            v_res, c_res, m_res = await asyncio.gather(
                asyncio.to_thread(validate_financial_data.invoke, {"financial_data_json": data_json}),
                asyncio.to_thread(calculate_financial_indicators.invoke, {"financial_data_json": data_json}),
                asyncio.to_thread(calculate_risk_models.invoke, {"financial_data_json": data_json}),
            )
            # 披露检查依赖 validate 结果（勾稽差异联动扣披露分，P4），串行传入 v_res
            d_res = await asyncio.to_thread(
                check_disclosure_compliance.invoke, {"report_text": text, "validation_json": v_res})
            # 综合评分依赖前三工具结果，串行计算（与兜底评分同参，保证正文/PDF 一致）
            from tools.risk_scorer import calculate_comprehensive_score
            s_res = await asyncio.to_thread(
                calculate_comprehensive_score.invoke,
                {"financial_analysis_json": c_res, "disclosure_check_json": d_res,
                 "validation_json": v_res, "risk_models_json": m_res},
            )
            from langchain_core.messages import AIMessage, ToolMessage
            # args 中的年报全文用占位符替代，避免上下文里同一段长文本出现两遍
            calls = [
                {"name": "validate_financial_data", "args": {"financial_data_json": data_json}, "id": "pre_v"},
                {"name": "calculate_financial_indicators", "args": {"financial_data_json": data_json}, "id": "pre_c"},
                {"name": "check_disclosure_compliance", "args": {"report_text": "（见上文年报文本）"}, "id": "pre_d"},
                {"name": "calculate_risk_models", "args": {"financial_data_json": data_json}, "id": "pre_m"},
                {"name": "calculate_comprehensive_score", "args": {"financial_analysis_json": "（见预处理结果）",
                                                                       "disclosure_check_json": "（见预处理结果）",
                                                                       "validation_json": "（见预处理结果）",
                                                                       "risk_models_json": "（见预处理结果）"}, "id": "pre_s"},
            ]
            logger.info("预处理注入完成：校验/指标/披露/量化模型/综合评分五工具已预跑")
            # 显式 id：既保证 tool_calls 应答配对，也供 tool_ledger 种入去重（与
            # _accumulate_tool_ledger 的 mid 口径一致，避免后续重复记账）
            return [
                AIMessage(content="", tool_calls=calls),
                ToolMessage(content=str(v_res), name="validate_financial_data", tool_call_id="pre_v", id="pre_tv"),
                ToolMessage(content=str(c_res), name="calculate_financial_indicators", tool_call_id="pre_c", id="pre_tc"),
                ToolMessage(content=str(d_res), name="check_disclosure_compliance", tool_call_id="pre_d", id="pre_td"),
                ToolMessage(content=str(m_res), name="calculate_risk_models", tool_call_id="pre_m", id="pre_tm"),
                ToolMessage(content=str(s_res), name="calculate_comprehensive_score", tool_call_id="pre_s", id="pre_ts"),
            ]
        except Exception as e:  # noqa: BLE001 — fail-open：预处理失败只意味着回到原速度
            logger.warning(f"预处理注入失败，回退原链路: {e}")
            return None

    async def run(self, payload: Dict[str, Any], ctx=None) -> Dict[str, Any]:
        if ctx is None:
            ctx = new_context("run")
        run_id = ctx.run_id
        logger.info(f"Starting run with run_id: {run_id}")
        try:
            # 与 stream_sse 同构：轻量模块标记（投资参考/行业风向）优先于历史模块标记，
            # 避免 /run 入口传轻量标记时以全量工具集运行并误触发辩论/PDF/评分兑底。
            light = _detect_light_module(payload)
            module = None if light else _detect_module(payload)
            agent = self._get_agent(ctx, fast=_payload_wants_fast_mode(payload),
                                     module=(module or light))
            run_config = init_run_config(agent, ctx)
            result = await agent.ainvoke(payload, config=run_config)
            return result
        except asyncio.CancelledError:
            logger.info(f"Run {run_id} was cancelled")
            return {"status": "cancelled", "run_id": run_id}
        except Exception as e:
            logger.error(f"Error in run: {e}\n{traceback.format_exc()}")
            raise
        finally:
            self.running_tasks.pop(run_id, None)

    async def _run_synthesis_stages(self, payload, ctx, fast: bool):
        """综合研判串跑：①财务健康度 ∥ ②合规风险（并行） → ③交叉验证。

        前两段互不依赖（仅依赖原始 payload），改为并行执行以压缩总耗时。
        第三段用 astream 保留工具级进度与后处理（辩论/兜底导出/评分）。

        容错：任一前置段失败只记日志并以空摘要继续（pipeline 会把该段标为
        “未产出有效结论”），不中断整条链路。

        Yields:
            ("progress", step, pct) / ("chunk", chunk) / ("tool_progress", tool_name)
            或 ("tool_results", [[msg_id, tool_name, content], ...])，
            统一由调用方转为 SSE。
        """

        summaries = []
        stage_ledger_entries = []   # 前序阶段工具结果（ToolMessage 级，供第三阶段兜底导出/评分复用）
        prior_tool_results = {}     # 前两阶段核心工具结果（供第三阶段提示词引用真实证据）
        _CORE_TOOL_NAMES = {
            "calculate_financial_indicators",
            "validate_financial_data",
            "check_disclosure_compliance",
            "identify_audit_opinion",
        }

        async def _run_one_stage(module: str, stage_name: str):
            """运行单个前置阶段（financial/compliance），返回摘要、工具结果和工具调用名序列。"""
            stage_payload = build_stage_payload(
                payload, module, MODULE_MARKERS[module], [])
            agent = self._get_agent(ctx, fast=fast, module=module)
            run_config = self._stage_run_config(agent, ctx, module)
            try:
                # 中间阶段跳过兜底后处理（post_process=False）：辩论/导出/综合评分
                # 只在第三阶段跑，避免每段多耗约 1 分钟且产出误导性局部报告。
                result = await agent.ainvoke(stage_payload, config=run_config, post_process=False)
                summary = extract_stage_summary(result.get("messages", []))
                # 收集本阶段工具结果（ToolMessage 形态，与 _merge_tool_ledger 契约一致）
                local_entries = []
                local_tool_results = {}
                local_tool_calls = []  # 供前端工具调用链展示
                from langchain_core.messages import ToolMessage, AIMessage
                for m in result.get("messages", []):
                    # 记录 LLM 发起的 tool_calls，让前端工具链能看到前两阶段工具
                    if isinstance(m, AIMessage):
                        for tc in (getattr(m, "tool_calls", None) or []):
                            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                            if tc_name:
                                local_tool_calls.append(tc_name)
                        continue
                    if not isinstance(m, ToolMessage):
                        continue
                    try:
                        content = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        content = str(m.content)
                    local_entries.append([getattr(m, "id", None) or "", getattr(m, "name", "") or "", content])
                    tool_name = getattr(m, "name", "") or ""
                    if tool_name in _CORE_TOOL_NAMES:
                        local_tool_results[tool_name] = content
                return {"module": module, "stage_name": stage_name,
                        "summary": summary, "entries": local_entries,
                        "tool_results": local_tool_results,
                        "tool_calls": local_tool_calls, "error": None}
            except Exception as e:  # noqa: BLE001 - 单段失败降级继续
                logger.warning(f"串跑阶段失败（{stage_name}），以空摘要继续: {e}")
                return {"module": module, "stage_name": stage_name,
                        "summary": "", "entries": [], "tool_results": {},
                        "tool_calls": [], "error": str(e)}

        # 方案 A：前两阶段并行执行，预计节省 30-50% 的前置时间
        yield ("progress", "并行运行财务诊断与合规扫描", 5)
        stage_results = await asyncio.gather(
            _run_one_stage("financial", "第一阶段 · 财务健康度诊断"),
            _run_one_stage("compliance", "第二阶段 · 合规与经营风险扫描"),
        )

        # 按固定顺序整理前两阶段结果，保证第三阶段摘要顺序稳定
        for module, stage_name, _pct_start, _pct_end in SYNTHESIS_STAGES:
            if module == "synthesis":
                continue
            res = next((r for r in stage_results if r["module"] == module), None)
            if res:
                summaries.append((res["stage_name"], res["summary"]))
                stage_ledger_entries.extend(res["entries"])
                prior_tool_results.update(res["tool_results"])
                # 把前两阶段的 tool_calls 以独立事件推给前端，补全工具调用链展示
                for tool_name in res.get("tool_calls", []):
                    yield ("tool_progress", tool_name)

        # 前两阶段的工具结果回传调用方：它们只存在于段内图状态（输入侧），
        # 不会出现在第三阶段的 astream 增量里，不登记则最终报告的
        # 「数据源与完整性」清单会把已跑过的工具全部误判为「未获取」。
        if stage_ledger_entries:
            yield ("tool_results", stage_ledger_entries)

        # 第三阶段：综合研判与交叉验证
        yield ("progress", "第三阶段 · 综合研判与交叉验证", 65)
        stage_payload = build_stage_payload(
            payload, "synthesis", MODULE_MARKERS["synthesis"], summaries,
            prior_tool_results=prior_tool_results)
        agent = self._get_agent(ctx, fast=fast, module="synthesis")
        run_config = self._stage_run_config(agent, ctx, "synthesis")
        # 前两阶段工具结果透传进第三阶段 tool_ledger：综合研判工具子集不含
        # 财务计算/披露检查/数据校验工具，不种入则兜底导出的财务/合规专项
        # 章节拿不到数据源（PDF 缺「财务指标四维判读」「披露规范性检查结果」章）。
        # 种入格式与 P1 预处理注入一致（[[msg_id, tool_name, content], ...]），
        # 由 _merge_tool_ledger 合并进台账，不受消息滑窗裁剪影响。
        if stage_ledger_entries:
            stage_payload = {**stage_payload, "tool_ledger": {"entries": stage_ledger_entries}}
        # 最后一段透传 astream chunk，保留工具进度与后处理标记
        async for chunk in agent.astream(stage_payload, config=run_config):
            yield ("chunk", chunk)

    def _stage_run_config(self, agent, ctx, module: str) -> dict:
        """为串跑的每一段派生独立的 checkpoint thread。

        三段共用 thread_id（= ctx.run_id）时，前一段异常中断遗留的悬空
        AIMessage.tool_calls（无对应 ToolMessage）会被下一段从 checkpointer 加载进
        历史，直接撞上 langgraph 的 INVALID_CHAT_HISTORY 校验——实测下一段与第三段
        连锁报错，单段失败直接拖垮整条流水线（与「单段失败降级继续」相矛盾）。
        段间本就只靠「已完成分析摘要」文本传递，不需要共享 LangGraph 状态。
        """
        run_config = init_agent_config(agent, ctx)
        configurable = dict(run_config.get("configurable") or {})
        base = configurable.get("thread_id") or getattr(ctx, "run_id", "run")
        configurable["thread_id"] = f"{base}-{module}"
        return {**run_config, "configurable": configurable}

    async def stream_sse(self, payload, ctx=None, run_opt=None, user=None):
        """用 astream 跟踪真实工具进度，最后一次性推送完整报告；登录态下顺带落历史"""
        if ctx is None:
            ctx = new_context("stream_sse")
        run_id = ctx.run_id
        fast = _payload_wants_fast_mode(payload)
        # C 端轻量工具路由：以最后一条消息（最新用户意图）为准，命中时优先于
        # 历史消息里残留的模块标记（否则同会话跑过综合研判后轻量卡片永远进不去）
        light = _detect_light_module(payload)
        module = None if light else _detect_module(payload)
        # 综合研判走三段串跑；其余情况（单模块/轻量/无标记）走单次 Agent
        pipeline_mode = module == "synthesis"
        agent = None if pipeline_mode else self._get_agent(ctx, fast=fast, module=(module or light))
        run_config = init_agent_config(agent, ctx) if agent is not None else None

        seen_ids = set()
        current_step = "初始化分析环境"
        current_pct = 2
        all_messages = []
        final_messages = None  # 后处理（辩论/兜底导出/评分）后的完整消息，优先用于构建最终报告
        # 预跑/前序阶段的工具结果：它们位于图输入侧，astream 增量里不会出现，
        # 必须单独登记，否则最终报告的数据源清单与指标视图会全部判为「未获取」。
        seed_tool_results = []

        yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})

        # 产物登记：运行开始即声明本次应生成的 PDF/Excel/图表，并标记「生成中」。
        # 只登记类型、不表示文件已落盘；最终报告会以成功/失败逐项覆盖，任务级状态
        # 由 derive_task_status 汇总（部分完成可显示），避免中间态与失败态混淆。
        _expected = expected_artifacts(module, fast) if light is None else []
        if _expected:
            from core.result_contract import ARTIFACT_GENERATING, TASK_RUNNING
            yield self._sse({
                "type": "artifact_status",
                "task_status": TASK_RUNNING,
                "artifacts": [{**item, "status": ARTIFACT_GENERATING}
                              for item in _expected],
            })

        # ── P1 预处理并行注入（详见 _preprocess_inject；失败静默回退原链路）
        # 仅对「全量链路」与「财务健康度模块」生效：合规模块不含校验/指标工具，
        # 预跑它们只会白花一次提取耗时；串跑模式由各阶段自行负责；
        # 轻量工具路径（light）产物是卡片，不需要预跑审计三工具。
        if (module in (None, "financial") and light is None and isinstance(payload, dict)
                and len(self._last_user_text(payload)) >= self._PREPROCESS_MIN_CHARS):
            current_step, current_pct = "预提取财务数据并行预跑工具", 8
            yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})
            pre_msgs = await self._preprocess_inject(payload)
            if pre_msgs:
                payload = {**payload, "messages": list(payload["messages"]) + pre_msgs}
                # 预跑结果同步种入 tool_ledger 台账：注入的 ToolMessage 位于消息序列
                # 头部，会被滑窗在 post_model_hook 首次记账前裁掉（实测：指标结果丢
                # 失导致 PDF 四维判读章与评分兜底读不到数据）。台账不受滑窗影响，
                # 种入后顺序门禁/评分兜底/PDF 导出补传均能读到预跑结果。
                from langchain_core.messages import ToolMessage
                ledger_entries = [
                    [m.id, m.name, str(m.content)]
                    for m in pre_msgs if isinstance(m, ToolMessage)
                ]
                if ledger_entries:
                    payload = {**payload, "tool_ledger": {"entries": ledger_entries}}
                # 注入轨迹登记进最终报告的工具链：astream 增量里不含输入消息，
                # 不手动登记的话预跑的三工具不会出现在前端工具调用链展示中
                for m in pre_msgs:
                    name = getattr(m, "name", None)
                    if name:
                        entry = {"type": "tool", "name": name, "content": str(m.content)}
                        seed_tool_results.append(entry)
                        all_messages.append(entry)
                # 三工具已完成，进度直接推进到披露检查锚点
                current_step, current_pct = "预处理完成（校验/指标/披露）", 42
                yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})

        try:
            # 统一 chunk 源：串跑模式从三阶段编排器取（带阶段进度），否则直接 astream。
            # 它们共用后续整套 chunk 处理逻辑（工具进度/后处理标记/消息提取）。
            if pipeline_mode:
                async def _chunk_source():
                    async for item in self._run_synthesis_stages(payload, ctx, fast):
                        yield item
            else:
                async def _chunk_source():
                    # 轻量工具路径跳过流末兜底后处理（post_process=False）：
                    # 不辩论、不导出局部 PDF/Excel、不跑综合评分
                    async for c in agent.astream(payload, config=run_config,
                                                 post_process=(light is None)):
                        yield ("chunk", c)

            async for kind, *rest in _chunk_source():
                # 阶段进度事件（仅串跑模式会发）：直接推送并推进百分比
                if kind == "progress":
                    step, pct = rest[0], rest[1]
                    current_step, current_pct = step, max(current_pct, pct)
                    yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})
                    continue
                # 前两阶段并行执行时，其 tool_calls 不会通过 astream chunk 自然透出，
                # 由 _run_synthesis_stages 以独立事件补发，供前端工具调用链完整展示。
                if kind == "tool_progress":
                    tool_name = rest[0]
                    if tool_name in TOOL_NAME_TO_STEP:
                        label, pct = TOOL_NAME_TO_STEP[tool_name]
                        current_step = label
                        current_pct = max(current_pct, pct)
                        yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})
                    continue
                # 串跑前两阶段的工具结果：同为输入侧内容，登记进最终报告的数据源清单
                if kind == "tool_results":
                    for _mid, _name, _content in rest[0]:
                        seed_tool_results.append(
                            {"type": "tool", "name": _name, "content": _content})
                    continue
                chunk = rest[0]
                # _AgentWrapper.astream 流末会依次推送：
                # ① __post_processing__ 标记：辩论复核 + 兜底导出开始（可能耗时 30-60s），
                #    据此更新进度文案避免长时间静止；
                # ② __post_processed__ 标记：携带含辩论/导出/评分的完整消息。
                if isinstance(chunk, dict) and chunk.get("__post_processing__"):
                    current_pct = max(current_pct, 90)
                    current_step = "辩论复核与报告导出中"
                    yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})
                    continue
                if isinstance(chunk, dict) and chunk.get("__post_processed__"):
                    final_messages = chunk.get("messages") or None
                    continue

                new_msgs = self._extract_new_messages(chunk, seen_ids)
                all_messages.extend(new_msgs)

                for msg in new_msgs:
                    msg_type = msg.get("type", "")

                    if msg_type in ("ai", "AIMessage") and msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            tc_name = tc.get("name", "")
                            if tc_name in TOOL_NAME_TO_STEP:
                                label, pct = TOOL_NAME_TO_STEP[tc_name]
                                current_step = label
                                # 锚点取单调不递减：Agent 实际调用顺序由 LLM 自主决定，
                                # 不一定与 TOOL_PIPELINE 声明顺序一致（实测出现过
                                # 60% → 36% 的回退）。此处只抬升不回落，
                                # 文案照旧切换到当前工具，避免进度条倒退的观感崩塌。
                                current_pct = max(current_pct, pct)
                                yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})

                    elif msg_type in ("ai", "AIMessage") and msg.get("content"):
                        if current_pct < 96:
                            current_pct = min(current_pct + 1, 96)
                            yield self._sse({"type": "progress", "step": current_step, "percent": current_pct})

            current_pct = 98
            yield self._sse({"type": "progress", "step": "汇总分析结论", "percent": current_pct})

            # 优先使用后处理后的完整消息构建报告（含辩论/导出链接/评分），
            # 回退到流式累积的 all_messages（兼容未提供后处理的底层 agent）
            if final_messages:
                processed = [self._msg_to_dict(m) for m in final_messages]
                # 预跑/前序阶段工具结果补在流内消息之前：它们真实发生在最前，且同名
                # 工具以首次结果为准（预跑是确定性计算，优先于 LLM 自行传参的重复
                # 调用）。缺此补入，正文/PDF 有数据而数据源清单显示「0/7 已获取」。
                report = self._build_final_report_from_messages(seed_tool_results + processed)
            else:
                report = self._build_final_report_from_messages(all_messages)

            # 轻量工具路径：从 ToolMessage 原文确定性注入专属卡片 marker（不依赖
            # LLM 复制 JSON），前端据此渲染投资参考卡/行业风向标，并随历史持久化
            if light:
                report = self._inject_light_card_markers(report)

            # 登录态下将本次分析写入用户历史（游客跳过；失败不影响推送）
            _record_history(user, run_id, report, fast)
            yield self._sse({"type": "final_report", "percent": 100, **report})

        except Exception as e:
            logger.error(f"stream_sse error: {e}\n{traceback.format_exc()}")
            yield self._sse({"type": "error", "content": "内部处理错误，请稍后重试（详见服务日志）"})

    @staticmethod
    def _sse(data: dict) -> str:
        return f"event: message\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def _extract_new_messages(self, chunk, seen_ids):
        """从 chunk 中提取未见过的消息"""
        new_msgs = []
        raw_messages = []

        if isinstance(chunk, dict):
            if "messages" in chunk:
                raw_messages = chunk["messages"]
            for node_key in ("agent", "tools"):
                if node_key in chunk and isinstance(chunk[node_key], dict):
                    raw_messages = raw_messages + chunk[node_key].get("messages", [])

        for msg in raw_messages:
            d = self._msg_to_dict(msg)
            mid = d.get("id") or id(msg)
            if mid not in seen_ids:
                seen_ids.add(mid)
                new_msgs.append(d)

        return new_msgs

    @staticmethod
    def _inject_light_card_markers(report: dict) -> dict:
        """轻量工具路径：把工具输出 JSON 以专属 marker 确定性追加到 ai_text。

        从 tool_results（ToolMessage 原文）取数，不依赖 LLM 忠实复制 JSON；
        前端用与评分卡 <!--COMPREHENSIVE_SCORE--> 同构的正则提取渲染专属卡片。
        同名工具多次调用时取最后一次（与“最终结论”语义一致）；非 JSON 输出跳过。
        """
        try:
            ai_text = report.get("ai_text", "") or ""
            # 先收集：同名工具多次调用时后者覆盖前者（取最后一次结果）
            latest = {}
            for tr in report.get("tool_results", []):
                marker = LIGHT_TOOL_MARKERS.get(tr.get("name", ""))
                if not marker:
                    continue
                content = str(tr.get("content", "")).strip()
                try:
                    json.loads(content)  # 只注入合法 JSON，防前端解析失败残留乱码
                except (json.JSONDecodeError, TypeError):
                    continue
                latest[marker] = content
            # 再注入：ai_text 已含该 marker（如 LLM 自行复制过）则不重复追加
            for marker, content in latest.items():
                if marker not in ai_text:
                    ai_text = ai_text + f"\n\n{marker}\n{content}"
            report["ai_text"] = ai_text
        except Exception as e:  # noqa: BLE001 - 注入失败不影响主报告推送
            logger.warning(f"轻量卡片 marker 注入失败: {e}")
        return report

    def _build_final_report_from_messages(self, all_messages):
        """从消息列表构建最终报告。

        链接来源有两处，必须都扫：
        - ToolMessage 内容（LLM 主动调用图表/导出工具的返回）；
        - 最后一条 AI 正文（兜底导出的 PDF/Excel 链接由 _post_process 追加在正文，
          不产生 ToolMessage；导出改全兜底后这是主要来源，漏扫会导致文件卡/历史无文件）。
        同一路径去重，避免两源重复。
        """
        ai_text = ""
        tool_results = []

        for d in all_messages:
            msg_type = d.get("type", "")

            if msg_type in ("ai", "AIMessage"):
                content = d.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        p if isinstance(p, str) else p.get("text", "")
                        for p in content
                    )
                if content and not d.get("tool_calls"):
                    ai_text = content

            elif msg_type in ("tool", "ToolMessage"):
                name = d.get("name", "tool")
                content = d.get("content", "")
                tool_results.append({"name": name, "content": content})

        images = []
        files = []
        artifact_candidates = []
        seen_paths = set()
        import re

        def _collect(source_name, text):
            """从一段文本中提取图片/文件链接，按路径去重后归档。"""
            for m in re.finditer(r'(/local_storage/[^\s"\'<>）)\]，,]+\.png)', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})
            for m in re.finditer(r'(/local_storage/[^\s"\'<>）)\]，,]+\.(?:pdf|xlsx))', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})
            # 兼容旧格式 file://... 路径
            for m in re.finditer(r'file://([^\s"\'<>]+\.png)', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})
            for m in re.finditer(r'file://([^\s"\'<>]+\.(?:pdf|xlsx))', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})

        for tr in tool_results:
            c = tr["content"] if isinstance(tr["content"], str) else str(tr["content"])
            _collect(tr["name"], c)
        # 兜底导出的链接在 AI 正文里（📎 PDF报告: .../📊 Excel底稿: ...）
        _collect("fallback_export", str(ai_text))

        # 结构化台账是网页/API 的权威来源，避免前端从模型散文反推风险数。
        risk_ledger = {}
        try:
            from agents.agent import _extract_risk_json
            risk_json = _extract_risk_json(str(ai_text))
            if risk_json:
                parsed = json.loads(risk_json)
                if isinstance(parsed, dict):
                    risk_ledger = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            risk_ledger = {}

        # 仅将真实存在且可读的文件交给前端下载；失效路径只保留在日志中，
        # 并由 ArtifactManifest 对应期望项记录失败状态。
        accessible_candidates = []
        for item in artifact_candidates:
            path = str(item.get("path", "") or "")
            if _artifact_is_accessible(path):
                accessible_candidates.append(item)
            else:
                logger.warning("忽略不可访问产物路径：%s", path)
        for item in accessible_candidates:
            path = str(item.get("path", "") or "")
            suffix = Path(path).suffix.lower().lstrip(".")
            target = images if suffix in {"png", "jpg", "jpeg"} else files
            target.append(item)

        from core.result_contract import ArtifactManifest, DATA_VERSION, RULE_VERSION
        company_info = risk_ledger.get("company_info") if isinstance(risk_ledger, dict) else {}
        analysis_id = str((risk_ledger or {}).get("analysis_id", "") or "")
        if not analysis_id and isinstance(company_info, dict):
            analysis_id = str(company_info.get("run_id", "") or "")
        data_version = str((risk_ledger or {}).get("data_version", DATA_VERSION) or DATA_VERSION)
        rule_version = str((risk_ledger or {}).get("rule_version", RULE_VERSION) or RULE_VERSION)
        expectations = (risk_ledger.get("artifact_expectations")
                        if isinstance(risk_ledger, dict) else None)
        if not isinstance(expectations, list) and risk_ledger and "INDUSTRY_OUTLOOK" not in str(ai_text):
            # 兼容旧台账：默认按全量报告登记三份 PDF、三类图表和 Excel。
            expectations = [
                {"key": "heatmap", "kind": "chart", "label": "风险热力图"},
                {"key": "radar", "kind": "chart", "label": "财务雷达图"},
                {"key": "trend", "kind": "chart", "label": "趋势折线图"},
                {"key": "pdf_financial", "kind": "pdf", "label": "财务健康诊断报告"},
                {"key": "pdf_compliance", "kind": "pdf", "label": "合规与信息披露报告"},
                {"key": "pdf_synthesis", "kind": "pdf", "label": "综合汇总报告"},
                {"key": "excel", "kind": "xlsx", "label": "Excel审计底稿"},
            ]
        expectations = expectations if isinstance(expectations, list) else []
        used_paths = set()
        artifact_manifest = []

        def _manifest_record(item, expectation, success, error=""):
            path = str(item.get("path", "") or "") if item else ""
            name = Path(path).name if path else str(expectation.get("label", ""))
            suffix = Path(name).suffix.lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(suffix, "")
            return ArtifactManifest(
                artifact_id=f"artifact-{len(artifact_manifest) + 1:03d}",
                kind=str(expectation.get("kind", "file")),
                status="success" if success else "failed",
                path=path if success else "",
                url=path if success else "",
                name=name,
                mime_type=mime,
                exists=bool(success),
                accessible=bool(success),
                source=str(item.get("tool", "")) if item else "",
                analysis_id=analysis_id,
                data_version=data_version,
                rule_version=rule_version,
                error=error,
            ).to_dict()

        for expectation in expectations:
            if not isinstance(expectation, dict):
                continue
            key = str(expectation.get("key", "") or "")
            match = next((item for item in accessible_candidates
                          if item.get("path") not in used_paths
                          and _artifact_key(item.get("path")) == key), None)
            if match:
                used_paths.add(match.get("path"))
                artifact_manifest.append(_manifest_record(match, expectation, True))
            else:
                artifact_manifest.append(_manifest_record(
                    None, expectation, False, "未发现实际生成且可访问的产物链接"))
        # 记录期望清单之外的真实文件，防止额外产物被静默丢弃。
        for item in accessible_candidates:
            if item.get("path") in used_paths:
                continue
            kind = "chart" if Path(str(item.get("path", ""))).suffix.lower() in {".png", ".jpg", ".jpeg"} else Path(str(item.get("path", ""))).suffix.lower().lstrip(".") or "file"
            artifact_manifest.append(_manifest_record(
                item, {"kind": kind, "label": Path(str(item.get("path", ""))).name}, True))

        # ── 报告元数据与完整性：版本、运行标识与各数据源可得性 ──
        # 前端「报告元数据与完整性」分区直接渲染本节，避免从模型散文推断覆盖率；
        # 未获取的数据源逐项给出原因，与三份 PDF 的「数据来源与完整性说明」同源口径。
        from core.result_contract import derive_task_status
        from tools.indicator_view import build_indicator_view

        tool_result_index = {}
        for item in tool_results:
            name = str(item.get("name", "") or "")
            content = item.get("content")
            if name and name not in tool_result_index:
                tool_result_index[name] = (content if isinstance(content, str)
                                           else json.dumps(content, ensure_ascii=False))

        def _used_json(name):
            raw = tool_result_index.get(name, "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            return isinstance(parsed, dict) and "error" not in parsed

        data_sources = _build_data_sources(tool_result_index)
        company = company_info if isinstance(company_info, dict) else {}
        gate = risk_ledger.get("review_gate") if isinstance(risk_ledger, dict) else {}
        gate = gate if isinstance(gate, dict) else {}
        report_metadata = {
            "analysis_id": analysis_id,
            "data_version": data_version,
            "rule_version": rule_version,
            "company_name": str(company.get("company_name", "") or ""),
            "stock_code": str(company.get("stock_code", "") or ""),
            "report_year": str(company.get("report_year", "") or ""),
            "industry": str(company.get("industry", "") or ""),
            "audit_opinion": str(company.get("audit_opinion", "") or ""),
            "review_gate_status": str(gate.get("status", "not_run") or "not_run"),
            "human_review_required": bool(gate.get("human_review_required")),
            "data_sources": data_sources,
            "source_used_count": sum(1 for item in data_sources if item["used"]),
            "source_total": len(data_sources),
        }

        return {
            "ai_text": ai_text,
            "tool_results": tool_results,
            "images": images,
            "files": files,
            "risk_ledger": risk_ledger,
            "accepted_risk_details": risk_ledger.get("accepted_risk_details", []) if isinstance(risk_ledger, dict) else [],
            "pending_items": risk_ledger.get("pending_items", []) if isinstance(risk_ledger, dict) else [],
            "review_gate": risk_ledger.get("review_gate", {"status": "not_run"}) if isinstance(risk_ledger, dict) else {"status": "not_run"},
            "semantic_review": risk_ledger.get("semantic_review", {}) if isinstance(risk_ledger, dict) else {},
            "c1_review": risk_ledger.get("c1_review", {}) if isinstance(risk_ledger, dict) else {},
            "data_version": risk_ledger.get("data_version", "") if isinstance(risk_ledger, dict) else "",
            "analysis_id": risk_ledger.get("analysis_id", "") if isinstance(risk_ledger, dict) else "",
            "artifact_manifest": artifact_manifest,
            "task_status": derive_task_status(artifact_manifest),
            "report_metadata": report_metadata,
            "indicator_view": build_indicator_view(
                tool_result_index.get("calculate_financial_indicators", "")),
        }

    def _build_final_report(self, result):
        """兼容旧调用"""
        messages = result.get("messages", []) if result else []
        all_msgs = [self._msg_to_dict(m) for m in messages]
        return self._build_final_report_from_messages(all_msgs)

    @staticmethod
    def _msg_to_dict(msg):
        """将 LangChain Message 对象或 dict 转为统一 dict"""
        if isinstance(msg, dict):
            return msg
        try:
            d = {"type": getattr(msg, "type", type(msg).__name__)}
            content = getattr(msg, "content", "")
            d["content"] = content
            d["name"] = getattr(msg, "name", "") or ""
            d["id"] = getattr(msg, "id", None) or id(msg)
            tc = getattr(msg, "tool_calls", None)
            if tc:
                d["tool_calls"] = [{"name": t.get("name", ""), "args": t.get("args", {})} for t in tc]
            return d
        except Exception:
            return {"type": "unknown", "content": str(msg)}


service = GraphService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 本地模式：先在事件循环内初始化持久化检查点（AsyncSqliteSaver），
    # 再预加载 agent（确保 build_agent 拿到的是持久化 saver）
    from storage.memory.memory_saver import init_memory_saver, close_memory_saver
    await init_memory_saver()
    # 业务库建表（用户/会话/分析历史）：幂等 create_all；失败不阻断启动，
    # 仅登录/历史功能降级不可用（接口会返回可见错误），分析主流程不受影响。
    try:
        from storage.database.db import get_engine
        from storage.database.shared.model import Base
        Base.metadata.create_all(get_engine())
        logger.info("业务库表结构就绪（users / session_tokens / analysis_history）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"业务库初始化失败（登录/历史功能不可用）: {e}")
    service._get_agent()
    yield
    # 关闭时释放 checkpointer 连接
    await close_memory_saver()


app = FastAPI(lifespan=lifespan)

# ── 静态文件服务（local_storage 目录）──
LOCAL_STORAGE = os.path.join(os.getcwd(), "local_storage")
os.makedirs(LOCAL_STORAGE, exist_ok=True)
app.mount("/local_storage", StaticFiles(directory=LOCAL_STORAGE), name="local_storage")

# ── CORS（允许前端跨域调用）──
# 安全说明：按 CORS 规范，allow_credentials=True 与通配符 origins 互斥且危险。
# 本服务为本地工具、前端同源调用，不依赖跨域凭证，故关闭 credentials 保留通配符；
# 若未来需要凭证，必须将 allow_origins 改为显式白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 最小鉴权（可选）：配置 APP_API_KEY 后对写操作接口强制校验 X-API-Key ──
# 默认不配置即完全放行，保证本地演示零摩擦；对外暴露服务时在 .env 中设置。
_PROTECTED_PREFIXES = ("/run", "/stream_run", "/upload", "/api/upload_kb", "/api/evaluate/run")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """写操作接口的 X-API-Key 校验中间件（APP_API_KEY 未配置时直接放行）。"""
    expected = os.getenv("APP_API_KEY", "")
    if expected and request.url.path.startswith(_PROTECTED_PREFIXES):
        if request.headers.get("X-API-Key") != expected:
            return JSONResponse({"detail": "unauthorized: 缺少或错误的 X-API-Key"}, status_code=401)
    return await call_next(request)

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "zhinengti_uploads")
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".pptx", ".html", ".htm", ".txt", ".md"}

openai_handler = OpenAIChatHandler(service)

# ── Web UI 路由 ──
WEB_DIR = Path(__file__).parent / "web"

# 首页 HTML 强制不缓存：前端为单文件实时读取，禁用浏览器缓存可确保
# 每次改动 index.html（如快捷上传自动发送逻辑）后刷新即生效，避免命中旧版页面。
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

# ── 前端第三方库静态目录（ECharts 本地内置）──
# 单机断网场景不能依赖 CDN，因此 echarts.min.js 必须本地提供。
# 空目录不被 git 跟踪，故先 makedirs 再 mount，否则新克隆的仓库启动即报错。
# 文件缺失时前端会自动降级为表格展示（不白屏），因此不阻塞启动。
VENDOR_DIR = WEB_DIR / "vendor"
os.makedirs(VENDOR_DIR, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


@app.get("/", response_class=HTMLResponse)
async def serve_web_ui():
    """提供可视化 Web 界面（禁用缓存，确保前端改动即时生效）"""
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            content="<h1>Web UI 未安装</h1><p>请确认 src/web/index.html 文件存在</p>",
            status_code=404,
        )
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"), headers=_NO_CACHE_HEADERS)


@app.get("/baka", response_class=HTMLResponse)
async def serve_baka_readme():
    """提供冰之妖精部署指南"""
    baka_path = Path(__file__).parent.parent / "baka专用readme.html"
    if not baka_path.exists():
        return HTMLResponse(content="<h1>⑨ 飞走了...</h1>", status_code=404)
    return HTMLResponse(content=baka_path.read_text(encoding="utf-8"))


@app.get("/readme", response_class=HTMLResponse)
async def serve_professional_readme():
    """提供年报风险识别系统部署指南（正式版）"""
    readme_path = Path(__file__).parent.parent / "年报风险识别系统专用readme.html"
    if not readme_path.exists():
        return HTMLResponse(content="<h1>页面未找到</h1>", status_code=404)
    return HTMLResponse(content=readme_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def get_system_status():
    """返回系统各组件详细状态"""
    status = {
        "server": "ok",
        "server_detail": {
            "name": "FastAPI 后端服务",
            "port": int(os.getenv("PORT", "5000")),
            "mode": "local",
            "framework": "FastAPI + LangGraph",
            "python": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
        },
        "llm": "unknown",
        "llm_detail": {},
        "knowledge_base": "unknown",
        "kb_detail": {},
        "storage": "local",
        "storage_detail": {},
    }

    try:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        config_path = os.path.join(
            os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), "..")),
            "config", "agent_llm_config.json"
        )
        model_name = "unknown"
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            model_name = cfg.get("config", {}).get("model", "unknown")
        except Exception:
            pass

        if api_key and not api_key.startswith("sk-your"):
            status["llm"] = "ok"
        else:
            status["llm"] = "not_configured"

        status["llm_detail"] = {
            "name": "大语言模型",
            "model": model_name,
            "provider": urlparse(base_url).hostname or base_url,
            "api_key_set": bool(api_key and not api_key.startswith("sk-your")),
            "streaming": True,
        }
    except Exception:
        status["llm"] = "error"
        status["llm_detail"] = {"name": "大语言模型", "error": "检测异常"}

    try:
        kb_dir = Path(os.getenv("COZE_WORKSPACE_PATH", ".")) / "knowledge_base"
        txt_files = sorted(kb_dir.glob("*.txt")) if kb_dir.exists() else []
        status["knowledge_base"] = "ok" if txt_files else "empty"
        # 不暴露服务器内部绝对路径（未认证接口），仅回中文展示名与计数
        status["kb_detail"] = {
            "name": "审计法规知识库",
            "file_count": len(txt_files),
            "files": [f.name for f in txt_files[:20]],
        }
    except Exception:
        status["knowledge_base"] = "error"
        status["kb_detail"] = {"name": "审计法规知识库", "error": "检测异常"}

    try:
        storage_dir = Path(LOCAL_STORAGE)
        file_count = sum(1 for _ in storage_dir.rglob("*") if _.is_file()) if storage_dir.exists() else 0
        subdirs = [d.name for d in storage_dir.iterdir() if d.is_dir()] if storage_dir.exists() else []
        status["storage"] = "ok" if storage_dir.exists() else "not_created"
        # 同上：不暴露绝对路径，仅保留计数与子目录名（reports/charts）
        status["storage_detail"] = {
            "name": "本地文件存储",
            "file_count": file_count,
            "subdirs": subdirs,
        }
    except Exception:
        status["storage_detail"] = {"name": "本地文件存储", "error": "检测异常"}

    return status


# ══════════════════════════════════════════════════════
# 多用户认证与分析历史 API
# 设计：未登录不影响分析（演示零摩擦），但历史仅在登录态记录/查询；
# 历史接口强制按 token 对应的 user_id 隔离，无法跨用户读取。
# ══════════════════════════════════════════════════════

@app.post("/api/auth/register")
async def auth_register(request: Request):
    """注册新用户并自动登录（返回会话令牌，免二次登录）。"""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"status": "error", "message": "请求体不是合法 JSON"}, status_code=400)
    try:
        from storage.database.user_service import login_user, register_user
        register_user(body.get("username", ""), body.get("password", ""))
        session_info = login_user(body.get("username", ""), body.get("password", ""))
        return {"status": "ok", **session_info}
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.error(f"注册失败: {e}")
        return JSONResponse({"status": "error", "message": "注册失败，请稍后重试（详见服务日志）"}, status_code=500)


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """口令登录，签发会话令牌（用户名不存在与口令错误统一提示，防枚举）。"""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"status": "error", "message": "请求体不是合法 JSON"}, status_code=400)
    try:
        from storage.database.user_service import login_user
        session_info = login_user(body.get("username", ""), body.get("password", ""))
    except Exception as e:  # noqa: BLE001
        logger.error(f"登录异常: {e}")
        return JSONResponse({"status": "error", "message": "登录服务异常，请稍后重试"}, status_code=500)
    if session_info is None:
        return JSONResponse({"status": "error", "message": "用户名或口令错误"}, status_code=401)
    return {"status": "ok", **session_info}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """登出：删除当前会话令牌（幂等）。"""
    try:
        from storage.database.user_service import logout_user
        logout_user(request.headers.get(AUTH_TOKEN_HEADER, ""))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"登出异常（忽略）: {e}")
    return {"status": "ok"}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """查询当前登录态（前端启动时校验本地 token 是否仍有效）。"""
    user = _current_user(request)
    if user is None:
        return {"status": "anonymous"}
    return {"status": "ok", **user}


@app.get("/api/history")
async def history_list(request: Request):
    """当前用户的分析历史列表（倒序，最多 50 条；未登录返回 401）。"""
    user = _current_user(request)
    if user is None:
        return JSONResponse({"status": "error", "message": "请先登录后查看分析历史"}, status_code=401)
    try:
        from storage.database.user_service import list_history
        return {"status": "ok", "username": user["username"], "records": list_history(user["user_id"])}
    except Exception as e:  # noqa: BLE001
        logger.error(f"历史查询失败: {e}")
        return JSONResponse({"status": "error", "message": "历史查询失败（详见服务日志）"}, status_code=500)


@app.get("/api/history/{record_id}")
async def history_detail(record_id: int, request: Request):
    """单条历史详情；他人记录与不存在统一返回 404（防探测）。"""
    user = _current_user(request)
    if user is None:
        return JSONResponse({"status": "error", "message": "请先登录"}, status_code=401)
    from storage.database.user_service import get_history_detail
    record = get_history_detail(user["user_id"], record_id)
    if record is None:
        return JSONResponse({"status": "error", "message": "记录不存在"}, status_code=404)
    return {"status": "ok", "record": record}


@app.post("/run")
async def http_run(request: Request) -> Dict[str, Any]:
    """同步执行审计分析（阻塞等待完整结果）。

    接收前端 JSON 载荷，创建后台异步任务并等待完成，
    超时时间默认 900 秒。返回包含风险分析报告和导出文件链接的完整结果。
    """
    raw_body = await request.body()
    try:
        body_text = raw_body.decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid body: {e}")

    ctx = new_context(method="run", headers=request.headers)
    upstream_run_id = request.headers.get(HEADER_X_RUN_ID)
    if upstream_run_id:
        # 走同一强校验，避免入口直接信任原始头部而绕过会话固定防护
        ctx.run_id = normalize_run_id(upstream_run_id)
    run_id = ctx.run_id
    request_context.set(ctx)

    logger.info(f"Received /run: run_id={run_id}, body={body_text[:500]}")

    try:
        payload = await request.json()
        task = asyncio.create_task(service.run(payload, ctx))
        service.running_tasks[run_id] = task
        try:
            result = await asyncio.wait_for(task, timeout=float(TIMEOUT_SECONDS))
        except asyncio.TimeoutError:
            task.cancel()
            return {"status": "timeout", "run_id": run_id}

        if not result:
            result = {}
        if isinstance(result, dict):
            result["run_id"] = run_id
        # 登录态下落历史（与 /stream_run 行为一致；游客/失败均不影响返回）
        user = _current_user(request)
        if user and isinstance(result, dict) and result.get("messages"):
            report = service._build_final_report(result)
            _record_history(user, run_id, report, _payload_wants_fast_mode(payload))
        return result
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    except Exception as e:
        logger.error(f"Error in /run: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail={"error": str(e)})
    finally:
        cozeloop.flush()


@app.post("/stream_run")
async def http_stream_run(request: Request):
    """流式执行审计分析（SSE 事件流推送）。

    使用 LangGraph astream 逐节点推送进度，前端实时展示分析步骤和中间结果，
    适用于需要即时反馈的交互场景。
    """
    ctx = new_context(method="stream_run", headers=request.headers)
    upstream_run_id = request.headers.get(HEADER_X_RUN_ID)
    if upstream_run_id:
        # 走同一强校验，避免入口直接信任原始头部而绕过会话固定防护
        ctx.run_id = normalize_run_id(upstream_run_id)
    request_context.set(ctx)

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    generator = service.stream_sse(payload, ctx, user=_current_user(request))
    return StreamingResponse(generator, media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """OpenAI 兼容的对话补全接口。

    支持外部系统通过标准 Chat Completions API 格式调用审计分析 Agent，
    实现与第三方应用的无缝集成。
    """
    ctx = new_context(method="openai_chat", headers=request.headers)
    request_context.set(ctx)
    try:
        payload = await request.json()
        return await openai_handler.handle(payload, ctx)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    finally:
        cozeloop.flush()


@app.get("/api/files")
async def list_generated_files():
    """列出 local_storage 中已生成的文件"""
    files = []
    storage_dir = Path(LOCAL_STORAGE)
    if not storage_dir.exists():
        return {"files": files}
    for f in sorted(storage_dir.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix in {".pdf", ".xlsx", ".png", ".jpg"}:
            rel = f.relative_to(storage_dir).as_posix()
            url = f"/local_storage/{rel}"
            files.append({
                "name": f.name,
                "path": rel,
                "url": url,
                "size": f.stat().st_size,
                "type": f.suffix.lstrip("."),
                "created": f.stat().st_mtime,
            })
    return {"files": files[:50]}


@app.get("/health")
async def health_check():
    """服务健康检查接口，用于监控和就绪探测。"""
    return {"status": "ok", "message": "Service is running (local mode)"}


@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """接收上传文件，保存到临时目录并解析提取文本内容。

    支持多种文件格式：PDF/Word/Excel/CSV/HTML/PPT/TXT/Markdown。
    每种格式使用对应的解析器提取纯文本，供后续 AI 分析使用。
    提取文本超过 20 万字符时自动截断，防止 LLM 上下文溢出。
    """
    results = []
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            results.append({
                "filename": f.filename,
                "status": "error",
                "error": f"不支持的文件格式: {ext}，仅支持 {', '.join(ALLOWED_EXTENSIONS)}",
            })
            continue

        if f.size and f.size > MAX_UPLOAD_SIZE:
            results.append({
                "filename": f.filename,
                "status": "error",
                "error": f"文件大小 ({f.size} bytes) 超过限制 100MB",
            })
            continue

        # 路径穿越防护：剥离路径成分并净化非法字符（与 /api/upload_kb 的净化逻辑对齐），
        # 防止构造 "../../../evil" 之类文件名逃逸出 UPLOAD_DIR
        import re as _re
        safe_name = _re.sub(r'[<>:"/\\|?*]', '_', os.path.basename(f.filename or "file"))
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        save_path = os.path.join(UPLOAD_DIR, unique_name)

        try:
            # 流式落盘：分块读取边写边计数，内存峰值仅一个 chunk（1MB）；
            # 超限立即中断并删除半成品文件，避免全量读内存导致并发 OOM
            size = 0
            oversize = False
            with open(save_path, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_SIZE:
                        oversize = True
                        break
                    out.write(chunk)
            if oversize:
                os.remove(save_path)
                results.append({
                    "filename": f.filename,
                    "status": "error",
                    "error": "文件大小超过限制 100MB（已中断接收并清理半成品文件）",
                })
                continue

            extracted_text = ""
            page_count = None
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(save_path)
                    page_count = len(reader.pages)
                    text_parts = []
                    for i, page in enumerate(reader.pages):
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(f"--- 第 {i + 1} 页 ---\n{page_text}")
                    extracted_text = "\n\n".join(text_parts)
                    if len(extracted_text) > 200_000:
                        extracted_text = extracted_text[:200_000]
                except Exception as parse_err:
                    logger.warning(f"PDF 解析失败: {parse_err}")
                    extracted_text = f"[PDF 解析失败: {parse_err}]"

            elif ext in (".docx", ".doc"):
                # Word 文档解析：使用 docx2python 提取全文文本
                try:
                    from docx2python import docx2python
                    doc = docx2python(save_path)
                    extracted_text = doc.text
                except Exception as parse_err:
                    logger.warning(f"Word 解析失败: {parse_err}")
                    extracted_text = f"[Word 解析失败: {parse_err}]"

            elif ext in (".xlsx", ".xls", ".csv"):
                # Excel/CSV 解析：使用 pandas 读取并转为文本
                try:
                    import pandas as pd
                    if ext == ".csv":
                        import chardet
                        # 编码探测仅取文件头 256KB 采样，避免整文件读入内存
                        with open(save_path, "rb") as rf:
                            sample = rf.read(256 * 1024)
                        detected = chardet.detect(sample)
                        encoding = detected.get("encoding", "utf-8") or "utf-8"
                        df = pd.read_csv(save_path, encoding=encoding)
                    else:
                        df = pd.read_excel(save_path, engine="openpyxl")
                    lines = []
                    for _, row in df.iterrows():
                        parts = [str(v).strip() for v in row.values if pd.notna(v) and str(v).strip()]
                        if parts:
                            lines.append(" ".join(parts))
                    extracted_text = "\n".join(lines)
                except Exception as parse_err:
                    logger.warning(f"Excel 解析失败: {parse_err}")
                    extracted_text = f"[Excel 解析失败: {parse_err}]"

            elif ext in (".html", ".htm"):
                # HTML 解析：去除标签后提取纯文本
                try:
                    import chardet
                    with open(save_path, "rb") as rf:
                        raw_bytes = rf.read()
                    detected = chardet.detect(raw_bytes[:256 * 1024])
                    encoding = detected.get("encoding", "utf-8") or "utf-8"
                    raw_text = raw_bytes.decode(encoding, errors="replace")
                    extracted_text = _re.sub(r'<[^>]+>', ' ', raw_text)
                    extracted_text = _re.sub(r'\s+', ' ', extracted_text).strip()
                except Exception as parse_err:
                    logger.warning(f"HTML 解析失败: {parse_err}")
                    extracted_text = f"[HTML 解析失败: {parse_err}]"

            elif ext == ".pptx":
                # PPT 解析：提取所有幻灯片的文本框内容
                try:
                    from pptx import Presentation
                    prs = Presentation(save_path)
                    slides_text = []
                    for slide in prs.slides:
                        parts = []
                        for shape in slide.shapes:
                            if shape.has_text_frame:
                                parts.append(shape.text_frame.text)
                        if parts:
                            slides_text.append("\n".join(parts))
                    extracted_text = "\n\n".join(slides_text)
                except Exception as parse_err:
                    logger.warning(f"PPT 解析失败: {parse_err}")
                    extracted_text = f"[PPT 解析失败: {parse_err}]"

            elif ext in (".txt", ".md"):
                # 纯文本/Markdown：直接解码
                try:
                    import chardet
                    with open(save_path, "rb") as rf:
                        raw_bytes = rf.read()
                    detected = chardet.detect(raw_bytes[:256 * 1024])
                    encoding = detected.get("encoding", "utf-8") or "utf-8"
                    extracted_text = raw_bytes.decode(encoding, errors="replace")
                except Exception:
                    with open(save_path, "rb") as rf:
                        extracted_text = rf.read().decode("utf-8", errors="ignore")

            results.append({
                "filename": f.filename,
                "saved_path": save_path,
                "status": "ok",
                "file_size": size,
                "extracted_text": extracted_text,
                "page_count": page_count,
            })
            logger.info(f"文件上传成功: {f.filename} → {save_path} ({size} bytes)")

        except Exception as e:
            logger.error(f"文件上传失败: {f.filename}: {e}")
            results.append({
                "filename": f.filename,
                "status": "error",
                "error": str(e),
            })

    return {"files": results}


@app.get("/api/reload_kb")
async def reload_knowledge_base():
    """热重载知识库，无需重启服务。

    清空当前知识库缓存（文档列表 + ChromaDB collection），
    然后重新扫描 knowledge_base/ 目录并重建向量索引。
    适用于用户上传新法规文件后刷新知识库。
    """
    try:
        from local_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        kb.documents.clear()
        kb._collection = None
        kb._loaded = False
        kb.load()
        return {
            "status": "ok",
            "message": f"知识库重载完成，共 {len(kb.documents)} 个文档块",
            "file_count": len(kb.documents),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/upload_kb")
async def upload_knowledge_base(files: List[UploadFile] = File(...)):
    """上传文件到知识库，自动转换为 txt 格式并热重载"""
    import re as _re
    import chardet

    workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), ".."))
    kb_dir = os.path.join(workspace, "knowledge_base")
    os.makedirs(kb_dir, exist_ok=True)

    results = []

    for f in files:
        original_name = f.filename or "unknown"
        ext = os.path.splitext(original_name)[1].lower()
        base_name = os.path.splitext(original_name)[0]
        safe_name = _re.sub(r'[<>:"/\\|?*]', '_', base_name)
        txt_filename = f"{safe_name}.txt"
        txt_path = os.path.join(kb_dir, txt_filename)

        try:
            # 与 /upload 一致的大小上限：流式分块读取并强制 MAX_UPLOAD_SIZE，
            # 防止未认证用户上传超大文件一次性读入内存导致耗尽（DoS）
            raw = b""
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MAX_UPLOAD_SIZE:
                    results.append({"filename": original_name, "status": "error",
                                    "error": f"文件超过大小上限 {MAX_UPLOAD_SIZE // (1024*1024)}MB"})
                    raw = None
                    break
            if raw is None:
                continue
            text = ""

            if ext == ".txt":
                detected = chardet.detect(raw)
                encoding = detected.get("encoding", "utf-8") or "utf-8"
                text = raw.decode(encoding, errors="replace")

            elif ext == ".pdf":
                try:
                    import pdfplumber
                    import io
                    with pdfplumber.open(io.BytesIO(raw)) as pdf:
                        pages = []
                        for page in pdf.pages:
                            page_text = page.extract_text()
                            if page_text:
                                pages.append(page_text)
                        text = "\n\n".join(pages)
                except Exception:
                    from pypdf import PdfReader
                    import io
                    reader = PdfReader(io.BytesIO(raw))
                    pages = []
                    for page in reader.pages:
                        page_text = page.extract_text()
                        if page_text:
                            pages.append(page_text)
                    text = "\n\n".join(pages)

            elif ext in (".docx", ".doc"):
                import tempfile
                tmp = os.path.join(tempfile.gettempdir(), f"kb_{uuid.uuid4().hex[:8]}{ext}")
                with open(tmp, "wb") as wf:
                    wf.write(raw)
                try:
                    from docx2python import docx2python
                    doc = docx2python(tmp)
                    text = doc.text
                finally:
                    os.remove(tmp)

            elif ext in (".xlsx", ".xls", ".csv"):
                import io
                import pandas as pd
                if ext == ".csv":
                    detected = chardet.detect(raw)
                    encoding = detected.get("encoding", "utf-8") or "utf-8"
                    df = pd.read_csv(io.BytesIO(raw), encoding=encoding)
                else:
                    df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                lines = []
                for _, row in df.iterrows():
                    parts = [str(v).strip() for v in row.values if pd.notna(v) and str(v).strip()]
                    if parts:
                        lines.append(" ".join(parts))
                text = "\n\n".join(lines)

            elif ext == ".pptx":
                import tempfile
                tmp = os.path.join(tempfile.gettempdir(), f"kb_{uuid.uuid4().hex[:8]}.pptx")
                with open(tmp, "wb") as wf:
                    wf.write(raw)
                try:
                    from pptx import Presentation
                    prs = Presentation(tmp)
                    slides_text = []
                    for slide in prs.slides:
                        parts = []
                        for shape in slide.shapes:
                            if shape.has_text_frame:
                                parts.append(shape.text_frame.text)
                        if parts:
                            slides_text.append("\n".join(parts))
                    text = "\n\n".join(slides_text)
                finally:
                    os.remove(tmp)

            elif ext in (".md", ".json", ".log", ".xml", ".html", ".htm"):
                detected = chardet.detect(raw)
                encoding = detected.get("encoding", "utf-8") or "utf-8"
                text = raw.decode(encoding, errors="replace")
                if ext in (".html", ".htm"):
                    text = _re.sub(r'<[^>]+>', ' ', text)
                    text = _re.sub(r'\s+', ' ', text).strip()

            else:
                detected = chardet.detect(raw)
                encoding = detected.get("encoding", "utf-8") or "utf-8"
                try:
                    text = raw.decode(encoding, errors="replace")
                except Exception:
                    text = raw.decode("utf-8", errors="ignore")

            if not text or not text.strip():
                results.append({
                    "filename": original_name,
                    "status": "error",
                    "error": "未能从文件中提取到文本内容",
                })
                continue

            text = text.strip()
            with open(txt_path, "w", encoding="utf-8") as wf:
                wf.write(text)

            chunk_count = len([c for c in text.split("\n\n") if c.strip()])
            results.append({
                "filename": original_name,
                "saved_as": txt_filename,
                "status": "ok",
                "char_count": len(text),
                "chunk_count": chunk_count,
            })
            logger.info(f"知识库文件已添加: {original_name} → {txt_filename} ({len(text)} 字符, {chunk_count} 块)")

        except Exception as e:
            logger.error(f"知识库文件上传失败: {original_name}: {e}")
            results.append({
                "filename": original_name,
                "status": "error",
                "error": str(e),
            })

    # 自动重载知识库
    try:
        from local_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        kb.documents.clear()
        kb._collection = None
        kb._loaded = False
        kb.load()
        total_chunks = len(kb.documents)
    except Exception:
        total_chunks = -1

    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "status": "ok" if ok_count > 0 else "error",
        "message": f"成功添加 {ok_count} 个文件，知识库共 {total_chunks} 个文档块",
        "files": results,
        "total_chunks": total_chunks,
    }


# ══════════════════════════════════════════════════════════
# 效果评估 API（竞赛手册 7.1 评分维度「效果评估」15分）
# ══════════════════════════════════════════════════════════

@app.get("/api/evaluate/status")
async def get_evaluation_status():
    """获取评估结果状态（从缓存的 JSON 文件读取）。

    返回工具模式评估结果和 Agent 模式评估结果（如已运行）。
    前端效果评估面板加载时自动调用此接口。
    """
    try:
        # 查找 evaluation_results.json（优先项目根目录，其次 tests 目录）
        candidates = [
            os.path.join(os.getenv("COZE_WORKSPACE_PATH", os.path.dirname(__file__)), "..", "tests", "evaluation_results.json"),
            os.path.join(os.path.dirname(__file__), "..", "tests", "evaluation_results.json"),
        ]
        eval_path = None
        for c in candidates:
            norm = os.path.normpath(c)
            if os.path.exists(norm):
                eval_path = norm
                break

        if not eval_path:
            return {
                "status": "not_run",
                "message": "尚未运行评估，请点击「运行评估」按钮",
                "data": None,
            }

        with open(eval_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 判断是否有 tool 和 agent 模式结果
        results = data.get("results", {})
        has_tool = "tool" in results
        has_agent = "agent" in results

        modes_available = []
        if has_tool:
            modes_available.append("tool")
        if has_agent:
            modes_available.append("agent")

        return {
            "status": "ok",
            "generated_at": data.get("generated_at", ""),
            "modes_available": modes_available,
            "data": data,
        }
    except Exception as e:
        logger.warning(f"读取评估结果失败: {e}")
        return {"status": "error", "message": str(e), "data": None}


@app.post("/api/evaluate/run")
async def run_evaluation(mode: str = "tool"):
    """运行效果评估（工具模式或全链路 Agent 模式）。

    工具模式：直接调用 financial_calculator 计算 18 个测试用例，<1秒完成。
    Agent 模式：调用完整 LLM + RAG + 辩论链路，每个用例约 30-60 秒。

    Args:
        mode: 评估模式，"tool"=工具级（快速）, "agent"/"all"=全链路

    Returns:
        评估完成状态和结果数据（含 Precision/Recall/F1/基线对比）
    """
    try:
        import subprocess
        import sys

        # 定位 evaluation_report.py 脚本路径
        workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), ".."))
        script_path = os.path.normpath(os.path.join(workspace, "..", "tests", "evaluation_report.py"))
        if not os.path.exists(script_path):
            script_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tests", "evaluation_report.py"))

        if not os.path.exists(script_path):
            return {"status": "error", "message": f"评估脚本不存在: {script_path}"}

        # 工具模式：直接运行（<1秒）
        if mode == "tool":
            result = subprocess.run(
                [sys.executable, script_path, "--mode", "tool"],
                capture_output=True, text=True, timeout=30,
                cwd=workspace,
                env={**os.environ, "PYTHONPATH": os.path.join(workspace, "..", "src")},
            )
            if result.returncode == 0:
                # 读取生成的 JSON 结果
                eval_json_path = os.path.normpath(os.path.join(os.path.dirname(script_path), "evaluation_results.json"))
                if os.path.exists(eval_json_path):
                    with open(eval_json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return {"status": "ok", "mode": "tool", "data": data}
                return {"status": "ok", "mode": "tool", "message": "评估完成", "stdout": result.stdout[-1000:]}
            else:
                return {"status": "error", "message": result.stderr[:500]}

        # Agent 模式：返回提示（因为耗时较长，建议通过命令行的 --mode agent 运行）
        elif mode == "agent" or mode == "all":
            return {
                "status": "info",
                "message": (
                    "Agent 全链路评估预计耗时约 2-3 分钟（每个用例约 30-60 秒），"
                    "请在终端执行：uv run python tests/evaluation_report.py --mode agent"
                ),
                "hint": "终端运行以查看实时进度，完成后刷新本页面即可看到结果",
            }

        return {"status": "error", "message": f"未知评估模式: {mode}"}

    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "评估超时（30秒），请检查系统状态"}
    except Exception as e:
        logger.error(f"运行评估失败: {e}")
        return {"status": "error", "message": str(e)}


def parse_args():
    parser = argparse.ArgumentParser(description="Start local FastAPI server")
    parser.add_argument("-m", type=str, default="http", help="Run mode")
    parser.add_argument("-p", type=int, default=5000, help="HTTP server port")
    parser.add_argument("-i", type=str, default="", help="Input JSON for CLI mode")
    return parser.parse_args()


def parse_input(input_str: str) -> Dict[str, Any]:
    if not input_str:
        return {"messages": [HumanMessage(content="你好")]}
    try:
        data = json.loads(input_str)
        if "messages" not in data:
            data = {"messages": [HumanMessage(content=input_str)]}
        return data
    except json.JSONDecodeError:
        return {"messages": [HumanMessage(content=input_str)]}


def start_http_server(port):
    reload = os.getenv("ENV", "dev") == "dev"
    logger.info(f"Start HTTP Server, Port: {port}, Reload: {reload}")
    # 白名单模式：只监控 .py 文件变化，避免 app.log/数据库/向量库写入触发热重载死循环
    # （HTML/CSS/JS 为每次请求实时读取，修改后无需重载即可生效）
    # 安全收敛：默认仅监听本机回环地址；需局域网/容器访问时在 .env 设 HOST=0.0.0.0 显式放开
    uvicorn.run(
        "main:app", host=os.getenv("HOST", "127.0.0.1"), port=port, reload=reload, workers=1,
        reload_includes=["*.py"],
        reload_excludes=["*.log", ".chroma_db/*", "local_storage/*", "*.pyc", "__pycache__/*", "*.db"],
    )


if __name__ == "__main__":
    args = parse_args()
    if args.m == "http":
        start_http_server(args.p)
    elif args.m == "flow":
        payload = parse_input(args.i)
        result = asyncio.run(service.run(payload))
        # 提取最后一条 AI 消息
        msgs = result.get("messages", [])
        ai_msgs = [m for m in msgs if isinstance(m, AIMessage)]
        if ai_msgs:
            print(ai_msgs[-1].content)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
