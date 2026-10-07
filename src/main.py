"""本地运行入口 - 精简版 FastAPI 服务

替代 main.py 中大量 coze_coding_utils 依赖，使用 local_shims 提供兼容。
启动方式: python -m main 或 uvicorn main:app --port 5000
"""
import argparse
import asyncio
import copy
import json
import os
import re
import threading
import traceback
import logging
import uuid
import tempfile
import shutil
import hashlib
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
from maintenance import MaintenancePolicy, last_report, maintenance_worker

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

    G1 加固：模块标记只认「前端在消息首部注入的短指令消息」——同时满足
    消息长度 <= _MARKER_MSG_MAX_CHARS 且标记出现在前 _MARKER_PREFIX_CHARS
    个字符内。年报正文等不可信长文本即使包含标记字样也不会被路由，
    防止被分析对象借正文内容切换控制流。
    """
    for m in (payload or {}).get("messages", []):
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        text = str(content)
        if len(text) > _MARKER_MSG_MAX_CHARS:
            continue
        for key, marker in MODULE_MARKERS.items():
            if marker in text[:_MARKER_PREFIX_CHARS]:
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
        items.append({"key": "json", "kind": "json", "label": "TXT格式结构化风险台账（JSON内容）"})
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

    G1 加固：与 _detect_module 同款「短指令消息 + 首部前缀」限定——轻量标记
    只在短消息首部生效，正文埋标记的不可信长文本不改变路由。
    """
    msgs = (payload or {}).get("messages", [])
    if not msgs:
        return None
    m = msgs[-1]
    content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
    text = str(content)
    if len(text) > _MARKER_MSG_MAX_CHARS:
        return None
    for key, marker in LIGHT_MARKERS.items():
        if marker in text[:_MARKER_PREFIX_CHARS]:
            return key
    return None


# G1 控制流与不可信文本隔离的扫描限定：标记消息必须是短指令（前端卡片按钮 /
# 模块入口注入的文本均远小于该上限），且标记位于消息首部前缀内。
_MARKER_MSG_MAX_CHARS = 2000
_MARKER_PREFIX_CHARS = 64


def _payload_wants_fast_mode(payload) -> bool:
    """检测请求载荷是否启用快速模式。

    优先读结构化字段 payload["fast_mode"]（前端 ⚡ 开关显式传入，权威来源）；
    未携带该字段时回退到旧版关键词兼容检测，但**只扫短指令消息**
    （长度 <= _MARKER_MSG_MAX_CHARS）——年报正文等不可信长文本即使包含
    "快速模式"字样也不会触发快速模式（G1 控制流与不可信文本隔离）。

    兼容两种消息形态：前端 JSON 的 dict（{"role","content"}）与 LangChain 消息对象。
    """
    if isinstance(payload, dict) and payload.get("fast_mode"):
        return True
    for m in (payload or {}).get("messages", []):
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        text = str(content)
        if len(text) <= _MARKER_MSG_MAX_CHARS and FAST_MODE_KEYWORD in text:
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
    # 新记录优先使用结构化快照。正文 JSON 仅用于旧历史兼容，避免展示文本
    # 的截断、改写或模型措辞影响历史列表的身份和评分。
    snapshot = report.get("report_snapshot") if isinstance(report, dict) else None
    if isinstance(snapshot, dict) and snapshot.get("snapshot_id"):
        company_info = snapshot.get("company") if isinstance(snapshot.get("company"), dict) else {}
        from utils.filename import resolve_company_year
        company, year = resolve_company_year(company_info)
        fields["company_name"] = company
        fields["report_year"] = year
        score_data = snapshot.get("score") if isinstance(snapshot.get("score"), dict) else {}
        fields["score"] = score_data.get("score")
        fields["risk_level"] = str(score_data.get("level", "") or "")
        rendering = snapshot.get("rendering") if isinstance(snapshot.get("rendering"), dict) else {}
        fields["summary"] = str(
            rendering.get("overall_assessment") or score_data.get("summary") or "")
        risk_data = {"company_info": company_info, "comprehensive_score": score_data}
    try:
        if not risk_data:
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
    manifest = report.get("artifact_manifest") if isinstance(report, dict) else None
    if isinstance(manifest, list) and manifest:
        fields["files"] = [
            item.get("path", "") for item in manifest
            if isinstance(item, dict) and item.get("status") == "success" and item.get("path")
        ]
    else:
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
    if suffix == ".json" or (suffix == ".txt" and ("json" in name or "风险台账" in name)):
        return "json"
    if suffix == ".pdf":
        if "财务健康" in name:
            return "pdf_financial"
        if "合规" in name or "信息披露" in name:
            return "pdf_compliance"
        return "pdf_synthesis"
    return suffix.lstrip(".") or "file"


def _build_data_sources(tool_result_index: dict, snapshot: dict | None = None) -> list:
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
    snapshot_multi_year = snapshot.get("multi_year", {}) if isinstance(snapshot, dict) else {}
    snapshot_multi_year = snapshot_multi_year if isinstance(snapshot_multi_year, dict) else {}
    trend_used = _used_json("compare_multi_year") or len(
        snapshot_multi_year.get("years_analyzed", []) or []
    ) >= 2
    source_specs = (
        ("财务指标数据", _used_json("calculate_financial_indicators"),
         "未上传年报或文本过短（预处理跳过），未产出结构化财务指标"),
        ("披露规范性检查", _used_json("check_disclosure_compliance"),
         "未调用披露检查工具或年报文本不足以检查"),
        ("综合风险评分", _used_json("calculate_comprehensive_score"),
         "未调用评分工具且系统兜底评分失败"),
        ("多年指标趋势", trend_used,
         "未提供两个可比期间或多年财务数据"),
        ("审计意见识别", opinion_used,
         "未识别到审计意见章节或未调用识别工具"),
        ("量化模型预警", _used_json("calculate_risk_models"),
         "缺少多期报表数据（Altman Z-Score / Beneish M-Score 需多年数据）"),
        ("数据勾稽校验", _used_json("validate_financial_data"),
         "缺少结构化财务数据（勾稽校验需三大报表字段）"),
    )
    quality = snapshot.get("data_quality", {}) if isinstance(snapshot, dict) else {}
    presentation_mode = str(quality.get("presentation_mode") or "strict")

    def _status(name, used):
        if presentation_mode in {"demo", "demo_placeholder"}:
            return "demo_placeholder"
        if not used:
            return "unverified"
        if name == "财务指标数据" and quality.get("incomplete_metrics"):
            return "incomplete"
        if name == "多年指标趋势":
            try:
                my = json.loads(tool_result_index.get("compare_multi_year", "") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                my = {}
            if not my:
                my = snapshot_multi_year
            if len(my.get("years_analyzed", []) or []) < 2:
                return "incomplete"
        return "verified"

    return [{"name": name, "used": bool(used), "data_status": _status(name, used),
             "source_status": _status(name, used), "note": "" if used else note}
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
            report_snapshot=report.get("report_snapshot") or report.get("final_snapshot") or {},
            artifact_manifest=report.get("artifact_manifest") or [],
            report_metadata=report.get("report_metadata") or {},
            ai_text=report.get("ai_text", ""),
            data_status=report.get("data_status", ""),
            task_status=report.get("task_status", ""),
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
            if fast:
                # G1：快速模式经结构化字段一路传给 _AgentWrapper（self._fast），
                # 后处理跳过辩论只认该标志，不从消息文本扫描关键词。
                build_kwargs["fast"] = True
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

    # 报表关键词 -> 命中页必须进入抽取窗口（实测：仅按固定页号截取只覆盖
    # 17,790/176,691 字符，营业成本、其他应付款、商誉等关键词仅剩 1 处命中，
    # LLM 抽取随机漏项 → 毛利率/周转率/模型大量「未获取」）。
    _EXCERPT_KEYWORDS = (
        "合并资产负债表", "合并及公司资产负债表", "合并利润表", "合并及公司利润表",
        "合并现金流量表", "合并股东权益变动表",
        "流动资产合计", "流动负债合计", "非流动资产合计", "非流动负债合计",
        "资产总计", "负债合计", "股东权益合计",
        "营业收入", "营业成本", "营业利润", "利润总额", "净利润",
        "货币资金", "应收账款", "其他应收款", "其他应付款", "存货",
        "固定资产", "在建工程", "商誉", "未分配利润", "归属于母公司股东的净利润",
        "利息费用", "利息收入", "经营活动产生的现金流量净额",
    )
    _EXCERPT_HEADER_PAGES = {1, 3}
    _EXCERPT_MAX_CHARS = 80000

    @classmethod
    def _financial_extraction_excerpt(cls, text: str) -> str:
        """优先抽取中国准则合并报表所在页，并纳入所有报表关键词命中页。

        PDF 上传文本由 parse_pdf_report 按页加上该标记。样例半年报同时
        包含国际准则和中国准则报表，单纯截取全文前缀会把两套口径交叉拼接；
        但只用固定页号又会漏掉附注表，故此处：

        1. 固定保留封面/重要提示页（识别期间、单位、报告类型）；
        2. 固定保留历史 preferred 页（中国准则主表）；
        3. 追加所有报表关键词命中页（按页号去重、排序）；
        4. 在字符上限内拼装，超出时优先保留主表与靠前的命中页。
        """
        page_chunks = re.findall(
            r"(---\s*第\s*(\d+)\s*页\s*---[\s\S]*?)(?=---\s*第\s*\d+\s*页\s*---|$)",
            text,
        )
        if not page_chunks:
            return text[:cls._EXCERPT_MAX_CHARS]
        preferred_pages = {6, 7, 49, 50, 51, 52, 86, 87, 109, 120, 121, 135}
        by_page = {}
        for chunk, page_no in page_chunks:
            by_page.setdefault(int(page_no), chunk)

        def _rank(page_no: int) -> tuple:
            # 排序优先级：表头页 > 主表页 > 关键词命中页，同级按页码升序
            if page_no in cls._EXCERPT_HEADER_PAGES:
                group = 0
            elif page_no in preferred_pages:
                group = 1
            else:
                group = 2
            return (group, page_no)

        selected = set(cls._EXCERPT_HEADER_PAGES) | (preferred_pages & set(by_page))
        for page_no, chunk in by_page.items():
            if any(keyword in chunk for keyword in cls._EXCERPT_KEYWORDS):
                selected.add(page_no)
        ordered = sorted((p for p in selected if p in by_page), key=_rank)

        parts, used = [], 0
        for page_no in ordered:
            chunk = by_page[page_no]
            if used and used + len(chunk) > cls._EXCERPT_MAX_CHARS:
                continue
            parts.append(chunk)
            used += len(chunk)
        if not parts:
            return text[:cls._EXCERPT_MAX_CHARS]
        return "\n\n".join(parts)

    @staticmethod
    def _backfill_source_facts(text: str, data: dict) -> dict:
        """补回可由原文表格确定识别的字段，避免 LLM 抽取随机漏项。

        样例半年报的应收账款附注和合并权益变动表同时给出净额、账面余额、
        坏账准备及「其他」变动。这里只补有明确表格锚点的值，不按金额量级猜测。
        """
        if not isinstance(data, dict) or not text:
            return data
        page_chunks = re.findall(
            r"---\s*第\s*(\d+)\s*页\s*---([\s\S]*?)(?=---\s*第\s*\d+\s*页\s*---|$)",
            text,
        )
        # Without page markers, retain a single source chunk, but still require
        # explicit table context below. This avoids making a page locator up.
        source_chunks = page_chunks or [("", text)]

        def _dates(chunk: str) -> list[str]:
            found = re.findall(
                r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", chunk)
            result = []
            for year, month, day in found:
                value = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                if value not in result:
                    result.append(value)
            return result

        def _unit(chunk: str) -> str:
            match = re.search(
                r"(?:金额单位\s*(?:为|：|:)\s*|单位\s*(?:为|：|:)\s*)"
                r"(?:人民币\s*)?(万亿|百万元|千万元|亿元|万元|千元|元)", chunk)
            if not match:
                match = re.search(
                    r"除特别注明外[^。]{0,50}?金额单位\s*(?:为|：|:)\s*"
                    r"(?:人民币\s*)?(万亿|百万元|千万元|亿元|万元|千元|元)", chunk)
            return f"人民币{match.group(1)}" if match else ""

        def _context(predicate):
            for page_no, chunk in source_chunks:
                if not predicate(chunk):
                    continue
                dates = _dates(chunk)
                unit = _unit(chunk)
                # Numeric backfill is allowed only when the table itself declares
                # dates and a monetary unit. Scope is recorded separately.
                if len(dates) < 2 or not unit:
                    continue
                scope = "合并" if ("合并" in chunk or "本集团" in chunk) else ""
                if not scope:
                    continue
                return page_no, chunk, dates[:2], unit, scope
            return None

        def _unit_compatible(source_unit: str) -> bool:
            declared = str(data.get("amount_unit") or (data.get("_metadata") or {}).get("amount_unit") or "")
            if not declared:
                data["amount_unit"] = source_unit
            # 字段级源表元数据优先于全局单位声明。LLM 可能已经把部分金额
            # 归一化为人民币元，而原表仍以人民币百万元列示；单位不一致时仍
            # 必须回写页码、定位和字段单位，不能整组跳过坏账/权益证据。
            return True

        def _scope_compatible(source_scope: str) -> bool:
            declared = str(data.get("scope") or "").replace("中国准则", "")
            if not declared:
                return True
            if "母公司" in declared:
                return source_scope == "母公司"
            if "合并" in declared:
                return source_scope == "合并"
            return True

        def _put_metadata(fields, context, locator, method="source_table_regex"):
            page_no, excerpt, dates, source_unit, scope = context
            if not _unit_compatible(source_unit):
                return
            if not _scope_compatible(scope):
                return
            metadata = data.setdefault("_field_metadata", {})
            flow_period_fields = {"dividends", "retained_earnings_other_changes"}
            for key, value in fields.items():
                if data.get(key) is None:
                    data[key] = value
                if key == "retained_earnings_begin":
                    period = dates[0]
                elif key == "retained_earnings_end":
                    period = dates[1]
                elif key in flow_period_fields:
                    period = f"{dates[1][:4]}年1-6月"
                else:
                    period = dates[0] if key.endswith("_current") else dates[1]
                source_metadata = {
                    "period": period,
                    "scope": scope,
                    "unit": source_unit,
                    "page": page_no,
                    "locator": locator,
                    "excerpt": excerpt[:800],
                    "extraction_method": method,
                }
                # A prior LLM extraction may have left a stale page or blank unit.
                # Once the source table is positively identified, its locator and
                # unit are authoritative for the affected fields.
                existing = metadata.get(key)
                if not isinstance(existing, dict):
                    metadata[key] = source_metadata
                else:
                    existing.update(source_metadata)

        ar_context = _context(
            lambda chunk: "应收账款" in chunk and "坏账准备" in chunk
            and ("本集团" in chunk or "合并" in chunk))
        ar_match = None
        if ar_context:
            ar_match = re.search(
                r"应收账款(?:\s+\d+)?\s+([\d,]+)\s+([\d,]+)\s+[\d,]+\s+[\d,]+\s+"
                r"减：坏账准备\s+\(([\d,]+)\)\s+\(([\d,]+)\)",
                re.sub(r"\s+", " ", ar_context[1]),
            )
        if ar_match and ar_context:
            fields = {
                "accounts_receivable_gross_current": int(ar_match.group(1).replace(",", "")),
                "accounts_receivable_gross_same_period_previous": int(ar_match.group(2).replace(",", "")),
                "bad_debt_provision_current": int(ar_match.group(3).replace(",", "")),
                "bad_debt_provision_same_period_previous": int(ar_match.group(4).replace(",", "")),
            }
            _put_metadata(fields, ar_context, "附注9 应收账款：账面余额及坏账准备表")

        # 合并资产负债表明确列示其他应收款；这是关联方往来与资金占用筛查的
        # 输入，若仅依赖 LLM 抽取，容易在多列报表中漏掉。只接受同一行的四列
        # 数值（合并本期/上期、母公司本期/上期），不按金额量级推断。
        other_context = _context(
            lambda chunk: "其他应收款" in chunk
            and ("合并资产负债表" in chunk or "合并及公司资产负债表" in chunk))
        other_match = None
        if other_context:
            # 资产负债表行名后可能紧跟附注编号（如「其他应收款 12」）。
            # 按行取数并保留最后四个金额列，避免把附注编号当成本期金额。
            for line in other_context[1].splitlines():
                normalized = re.sub(r"\s+", " ", line).strip()
                if "其他应收款" not in normalized:
                    continue
                tail = normalized.split("其他应收款", 1)[1]
                values = re.findall(r"[\d,]+", tail)
                if len(values) < 4:
                    continue
                values = values[-4:]
                other_match = re.match(
                    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)$",
                    " ".join(values),
                )
                if other_match:
                    break
        if other_match and other_context:
            fields = {
                "other_receivables_current": int(other_match.group(1).replace(",", "")),
                "other_receivables_previous": int(other_match.group(2).replace(",", "")),
            }
            _put_metadata(fields, other_context, "合并资产负债表：其他应收款")

        # 合并资产负债表的非流动资产行同时给出固定资产和在建工程的
        # 期末/上年末账面价值。两者是周转率和变动率的直接输入；只接受
        # 明确带附注表头、单位和合并范围的报表行，避免把附注政策说明
        # 中的数字误当作余额。
        asset_context = _context(
            lambda chunk: ("合并资产负债表" in chunk or "合并及公司资产负债表" in chunk)
            and "固定资产" in chunk
            and "在建工程" in chunk
            and "非流动资产" in chunk
        )
        if asset_context:
            asset_fields = {}
            for raw_line in asset_context[1].splitlines():
                line = re.sub(r"\s+", " ", raw_line).strip()
                fixed_match = re.search(
                    r"固定资产\s+(?:\d+\s+)?([\d,]+)\s+([\d,]+)", line
                )
                cip_match = re.search(
                    r"在建工程\s+(?:\d+\s+)?([\d,]+)\s+([\d,]+)", line
                )
                if fixed_match:
                    asset_fields.update({
                        "fixed_assets_current": int(fixed_match.group(1).replace(",", "")),
                        "fixed_assets_previous": int(fixed_match.group(2).replace(",", "")),
                    })
                if cip_match:
                    asset_fields.update({
                        "construction_in_progress_current": int(cip_match.group(1).replace(",", "")),
                        "construction_in_progress_previous": int(cip_match.group(2).replace(",", "")),
                    })
            if asset_fields:
                _put_metadata(asset_fields, asset_context, "合并资产负债表：固定资产/在建工程")

        # ── 合并资产负债表（流动资产/负债与非流动资产）行级回填 ──
        # 只取「行首标签 + 紧邻两列金额」，列顺序固定为「本期 / 上年末」，
        # 单位/日期/合并范围仍由 _context 校验；括号表示负值（成本类转正）。
        def _row_pair(chunk: str, label: str):
            for raw_line in chunk.splitlines():
                line = re.sub(r"\s+", " ", raw_line).strip()
                # 行首标签（允许「减：」前缀与紧随其后的附注编​号）
                # 附注引用既可能是纯数字（「货币资金 7」），也可能是
                # 「59(f)」这种带括号字母的形式（现金流量表），两种都要跳过。
                m = re.match(
                    r"^(?:其中：|减：|加：)?" + re.escape(label) + r"\s+(?:\d+(?:\([a-z]\))?\s+)?\(?([\d,]+)\)?\s+\(?([\d,]+)\)?",
                    line,
                )
                if m:
                    def _val(group):
                        return int(group.replace(",", ""))
                    return _val(m.group(1)), _val(m.group(2))
            return None

        asset_row_context = _context(
            lambda chunk: ("合并资产负债表" in chunk or "合并及公司资产负债表" in chunk)
            and "流动资产合计" in chunk and "资产总计" in chunk)
        if asset_row_context:
            fields = {}
            for label, cur_key, prev_key in (
                    ("货币资金", "monetary_funds_current", "monetary_funds_previous"),
                    ("应收账款", "accounts_receivable_current", "accounts_receivable_previous"),
                    ("存货", "inventory_current", "inventory_previous"),
                    ("流动资产合计", "current_assets_current", "current_assets_previous"),
                    ("商誉", "goodwill_current", "goodwill_previous"),
                    ("资产总计", "total_assets_current", "total_assets_previous")):
                pair = _row_pair(asset_row_context[1], label)
                if pair:
                    fields[cur_key], fields[prev_key] = pair
            if fields:
                if fields.get("total_assets_current") is not None and data.get("total_assets") is None:
                    fields["total_assets"] = fields["total_assets_current"]
                _put_metadata(fields, asset_row_context, "合并资产负债表：主要资产行")

        # ── 合并资产负债表（续）：其他应付款 / 流动负债 / 负债合计 ──
        liab_row_context = _context(
            lambda chunk: ("合并资产负债表" in chunk or "合并及公司资产负债表" in chunk)
            and "流动负债合计" in chunk and "未分配利润" in chunk)
        if liab_row_context:
            fields = {}
            for label, cur_key, prev_key in (
                    ("其他应付款", "other_payables_current", "other_payables_previous"),
                    ("流动负债合计", "current_liabilities_current", "current_liabilities_previous"),
                    ("负债合计", "total_liabilities_current", "total_liabilities_previous")):
                pair = _row_pair(liab_row_context[1], label)
                if pair:
                    fields[cur_key], fields[prev_key] = pair
            if fields:
                _put_metadata(fields, liab_row_context, "合并资产负债表（续）：主要负债行")
            # 未分配利润：期末列(本期) -> end，期初列(上年末) -> begin。
            # 权益变动表（更权威）在其后覆盖；此处仅在其缺席时补位。
            pair = _row_pair(liab_row_context[1], "未分配利润")
            if pair:
                end_value, begin_value = pair
                if data.get("retained_earnings_end") is None:
                    data["retained_earnings_end"] = end_value
                if data.get("retained_earnings_begin") is None:
                    data["retained_earnings_begin"] = begin_value
                meta = data.setdefault("_field_metadata", {})
                for key, value, period in (("retained_earnings_end", end_value, liab_row_context[2][0]),
                                           ("retained_earnings_begin", begin_value, liab_row_context[2][1])):
                    meta.setdefault(key, {})
                    if isinstance(meta[key], dict):
                        meta[key].update({
                            "period": period, "scope": liab_row_context[4],
                            "unit": liab_row_context[3], "page": liab_row_context[0],
                            "locator": "合并资产负债表：未分配利润",
                            "excerpt": liab_row_context[1][:800],
                            "extraction_method": "source_table_regex"})
            # 股东权益合计 -> 净资产（所有者权益）
            pair = _row_pair(liab_row_context[1], "股东权益合计")
            if pair:
                _put_metadata({"net_assets_current": pair[0], "net_assets_previous": pair[1]},
                              liab_row_context, "合并资产负债表：股东权益合计")

        # ── 合并利润表：营收/成本/利润/归母/利息 行级回填 ──
        # 样例半年报利润表为「本期合并 / 上年同期合并 / 本期公司 / 上年同期公司」
        # 四列，只取前两列（合并口径）；营业成本以括号列示，回填为正数。
        income_context = _context(
            lambda chunk: ("合并利润表" in chunk or "合并及公司利润表" in chunk)
            and "营业收入" in chunk and "营业成本" in chunk)
        if income_context:
            fields = {}
            for label, cur_key, prev_key in (
                    ("营业收入", "revenue_current", "revenue_previous"),
                    ("营业成本", "cost_of_goods_current", "cost_of_goods_previous"),
                    ("营业利润", "operating_profit_current", "operating_profit_previous"),
                    ("利润总额", "ebit_current", "ebit_previous"),
                    ("净利润", "net_profit_current", "net_profit_previous"),
                    ("归属于母公司股东的净利润",
                     "net_profit_parent_current", "net_profit_parent_previous"),
                    ("利息费用", "interest_expense_current", "interest_expense_previous"),
                    ("利息收入", "interest_income_current", "interest_income_previous")):
                pair = _row_pair(income_context[1], label)
                if pair:
                    fields[cur_key], fields[prev_key] = pair
            if fields:
                # 营业利润/利润总额别名：计算器与模型按无后缀键读取
                if fields.get("operating_profit_current") is not None:
                    fields.setdefault("operating_profit", fields["operating_profit_current"])
                if fields.get("ebit_current") is not None and data.get("ebit") is None:
                    fields["ebit"] = fields["ebit_current"]
                if fields.get("net_profit_current") is not None and data.get("net_profit") is None:
                    fields["net_profit"] = fields["net_profit_current"]
                _put_metadata(fields, income_context, "合并利润表：主要损益行")

            # SG&A（销售费用+管理费用）是 Beneish SGAI 的输入：两行取自同一合并利润表，
            # 只在两行都定位成功时合并，避免用单边数据凑出因子。
            selling = _row_pair(income_context[1], "销售费用")
            admin = _row_pair(income_context[1], "管理费用")
            if selling and admin:
                _put_metadata({
                    "sga_expense_current": selling[0] + admin[0],
                    "sga_expense_previous": selling[1] + admin[1],
                }, income_context, "合并利润表：销售费用+管理费用（SG&A 口径）")

            # 毛利额＝营业收入-营业成本：仅当两行都是从本表确定识别的数值时派生，
            # 供 Beneish GMI 使用（与指标层毛利率口径一致）；已有披露值时不覆盖。
            derived = {}
            if (fields.get("revenue_current") is not None
                    and fields.get("cost_of_goods_current") is not None):
                derived["gross_profit_current"] = (fields["revenue_current"]
                                                  - fields["cost_of_goods_current"])
                if data.get("gross_profit") is None:
                    derived["gross_profit"] = derived["gross_profit_current"]
            if (fields.get("revenue_previous") is not None
                    and fields.get("cost_of_goods_previous") is not None):
                derived["gross_profit_previous"] = (fields["revenue_previous"]
                                                   - fields["cost_of_goods_previous"])
            if derived:
                _put_metadata(derived, income_context,
                              "合并利润表：毛利额＝营业收入-营业成本（派生）",
                              method="derived_from_source_table")

        # ── 合并现金流量表：经营活动现金流量净额（OCF/NP 质量比、趋势图直接输入）──
        # 实测缺陷：原回填只覆盖资产负债表与利润表，现金流量表行（带「59(f)」
        # 式附注引用）未被识别，导致经营现金流/净利润比、现金流趋势大面积缺口。
        cashflow_context = _context(
            lambda chunk: "现金流量表" in chunk and "经营活动产生的现金流量净额" in chunk)
        if cashflow_context:
            fields = {}
            pair = _row_pair(cashflow_context[1], "经营活动产生的现金流量净额")
            if pair:
                fields["operating_cashflow_current"] = pair[0]
                fields["operating_cashflow_previous"] = pair[1]
                # 计算器/model 同时读带后缀与无后缀键，保持两者一致
                fields["operating_cashflow"] = pair[0]
            # 折旧、折耗及摊销是间接法调整项，也是 Beneish DEPI 的输入。中国准则
            # 主表按直接法列示、不含该行，须单独定位间接法调整表所在页；期间、
            # 单位与合并范围仍由 _context 校验，避免把附注文字当作表内数据。
            dep_context = _context(
                lambda chunk: "现金流量表" in chunk and "折旧、折耗及摊销" in chunk)
            if dep_context:
                dep_pair = _row_pair(dep_context[1], "折旧、折耗及摊销")
                if dep_pair:
                    _put_metadata({"depreciation_current": dep_pair[0],
                                   "depreciation_previous": dep_pair[1]},
                                  dep_context,
                                  "合并现金流量表：折旧、折耗及摊销（间接法调整项）")
            if fields:
                _put_metadata(fields, cashflow_context,
                              "合并现金流量表：经营活动产生的现金流量净额")

        # 合并股东权益变动表中「其他」行的第六列为未分配利润变动；
        # 仅接受同时出现未分配利润标题和明确括号数值的表格行。
        equity_context = _context(
            lambda chunk: "合并股东权益变动表" in chunk
            and re.search(r"未分配\s*利润", chunk) is not None)
        if equity_context:
            # 页面同时包含 2024 年比较行和 2025 年本期行，不能使用整页
            # 日期出现顺序。以「本期 6 月 30 日余额」对应年份重建期间。
            equity_excerpt = equity_context[1]
            year_matches = re.findall(
                r"(20\d{2})\s*年\s*6\s*月\s*30\s*日余额", equity_excerpt)
            current_year = max((int(year) for year in year_matches), default=int(equity_context[2][1][:4]))
            if year_matches:
                equity_context = (
                    equity_context[0], equity_excerpt,
                    [f"{current_year:04d}-01-01", f"{current_year:04d}-06-30"],
                    equity_context[3], equity_context[4],
                )
            # 必须按原始换行定位「其他」行；整页压成单空格后无法区分
            # 行边界，容易把前面其他项目的括号数字误当成目标列。
            candidates = []
            for raw_line in equity_context[1].splitlines():
                line = re.sub(r"\s+", " ", raw_line).strip()
                if not re.match(r"^其他\s+", line):
                    continue
                parens = re.findall(r"\(([\d,]+)\)", line)
                if len(parens) < 3:
                    continue
                candidates.append(parens)
            if candidates and data.get("retained_earnings_other_changes") is None:
                # 未分配利润是该行第三个括号值；同页可能有上年比较行，
                # 取最后一个合格「其他」行对应本期披露。
                value = -int(candidates[-1][2].replace(",", ""))
                _put_metadata({"retained_earnings_other_changes": value}, equity_context,
                              "合并股东权益变动表：其他行、未分配利润列")

            # The same table is the source for the opening/closing retained
            # earnings and shareholder distributions.  Extract the sixth value
            # after the date (the 未分配利润 column), preserving parentheses as
            # negatives.  This also repairs stale LLM locators such as note 39.
            def _row_value(prefix: str):
                matches = []
                for raw_line in equity_context[1].splitlines():
                    line = re.sub(r"\s+", " ", raw_line).strip()
                    if not re.search(prefix, line):
                        continue
                    tail = re.split(prefix, line, maxsplit=1)[-1]
                    tokens = re.findall(r"-+|\([\d,]+\)|[\d,]+", tail)
                    if len(tokens) < 6:
                        continue
                    token = tokens[5]
                    matches.append(
                        -int(token[1:-1].replace(",", ""))
                        if token.startswith("(") else int(token.replace(",", "")))
                return matches[-1] if matches else None

            equity_fields = {}
            begin = _row_value(fr"{current_year}\s*年\s*1\s*月\s*1\s*日余额")
            end = _row_value(fr"{current_year}\s*年\s*6\s*月\s*30\s*日余额")
            dividends = _row_value(r"对股东的分配")
            # 同一页保留 2024 年比较期间的分配行；_row_value 返回本表
            # 最后一个匹配行，即当前 2025 年半年度披露。
            if dividends is not None:
                # 权益变动表用括号表示分配对留存收益的扣减；字段本身
                # 表示分红金额，沿用校验器和工具的正数约定。
                dividends = abs(dividends)
            if begin is not None:
                equity_fields["retained_earnings_begin"] = begin
            if end is not None:
                equity_fields["retained_earnings_end"] = end
            if dividends is not None:
                equity_fields["dividends"] = dividends
            if equity_fields:
                _put_metadata(equity_fields, equity_context,
                              "合并股东权益变动表：未分配利润列")
        # 报告身份（公司名/股票代码/期间/行业/审计意见）：原文确定性识别，
        # 供文件命名、期间勾稽与量化模型适用性判定使用；不覆盖已有非空值。
        try:
            from utils.report_identity import extract_report_identity
            identity = extract_report_identity(text)
            for key, value in identity.items():
                if value and not str(data.get(key) or "").strip():
                    data[key] = value
        except Exception:
            pass
        return data

    async def _extract_financial_json(self, text: str):
        """用快速模型从年报文本提取结构化财务数据 JSON；失败返回 None。"""
        from langchain_openai import ChatOpenAI
        from utils.llm import thinking_extra_body
        llm = ChatOpenAI(
            model=FAST_MODEL,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
            temperature=0,
            max_tokens=3000,
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
             "accounts_receivable_current, accounts_receivable_previous, "
             "accounts_receivable_gross_current, accounts_receivable_gross_same_period_previous, "
             "bad_debt_provision_current, bad_debt_provision_same_period_previous, "
             "other_receivables, other_receivables_current, other_receivables_previous, other_payables, other_payables_current, inventory_current, "
             "inventory_previous, operating_cost_current, operating_cost_previous, current_assets, current_liabilities, "
             "fixed_assets_current, fixed_assets_previous, construction_in_progress_current, construction_in_progress_previous, "
             "net_profit_parent_current, net_profit_parent_previous, net_profit_parent_deducted_current, "
             "net_profit_parent_deducted_previous, retained_earnings_other_changes, goodwill, monetary_funds, cash_and_equivalents, "
             "short_term_loans, industry, period, scope, accounting_standard\n"
             "（industry 为字符串，取：制造业/房地产/互联网/医药/金融/零售/能源/农业/军工/传媒 之一；"
             "period 为本次报告期，例如 2025年半年度/2025年度；scope 取合并或母公司）\n"
             "同一组计算只使用同一会计准则和报表范围。未指定时优先中国企业会计准则合并报表；"
             "不得将国际准则数值、母公司数据或归母净利润混入合并口径。\n"
             "资产负债表_previous为期初余额，利润/现金流_previous为上年同期流量，两者不得都标同比。"
             "为提取字段附_field_metadata对象，逐字段记录period（实际日期或期间）、page、locator和简短excerpt。"
             "应收上年同期期末仅在原文明确提供时放入accounts_receivable_same_period_previous。\n"
             "货币资金放monetary_funds；现金及现金等价物只取现金流量表附注的对应合计，不得互换。"
             "原披露毛利率、按营业收入减营业成本重算毛利率可能定义不同，保留各自名称和公式。"
             "销管费用等合计字段缺失时不要心算补值，只提取其组成项。\n\n"
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
            data_json = await self._extract_financial_json(self._financial_extraction_excerpt(text))
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
                    _raw = self._backfill_source_facts(text, _raw)
                    # 报告身份兜底：LLM 常漏掉封面公司名与报告期，导致文件名为
                    #「未知公司」且 Altman/Beneish 被判「缺少本期期间」。此处用
                    # 原文确定性识别补齐 period/report_period/industry 等上下文。
                    try:
                        from utils.report_identity import extract_report_identity
                        _identity = extract_report_identity(text)
                        for _k, _v in _identity.items():
                            if _v and not str(_raw.get(_k) or "").strip():
                                _raw[_k] = _v
                    except Exception:
                        pass
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
            # 每次预处理必须使用新消息 ID。LangGraph 的 add_messages reducer 会按
            # 消息 ID 去重；若同一会话重复分析仍使用固定 ID，上一轮的 ToolMessage
            # 会被复用到新 AIMessage 之前，DeepSeek 就会收到悬空 tool_calls。
            pre_token = uuid.uuid4().hex
            call_ids = {
                "validate_financial_data": f"pre_v_{pre_token}",
                "calculate_financial_indicators": f"pre_c_{pre_token}",
                "check_disclosure_compliance": f"pre_d_{pre_token}",
                "calculate_risk_models": f"pre_m_{pre_token}",
                "calculate_comprehensive_score": f"pre_s_{pre_token}",
            }
            calls = [
                {"name": "validate_financial_data", "args": {"financial_data_json": data_json},
                 "id": call_ids["validate_financial_data"]},
                {"name": "calculate_financial_indicators", "args": {"financial_data_json": data_json},
                 "id": call_ids["calculate_financial_indicators"]},
                {"name": "check_disclosure_compliance", "args": {"report_text": "（见上文年报文本）"},
                 "id": call_ids["check_disclosure_compliance"]},
                {"name": "calculate_risk_models", "args": {"financial_data_json": data_json},
                 "id": call_ids["calculate_risk_models"]},
                {"name": "calculate_comprehensive_score", "args": {"financial_analysis_json": "（见预处理结果）",
                                                                       "disclosure_check_json": "（见预处理结果）",
                                                                       "validation_json": "（见预处理结果）",
                                                                       "risk_models_json": "（见预处理结果）"},
                 "id": call_ids["calculate_comprehensive_score"]},
            ]
            logger.info("预处理注入完成：校验/指标/披露/量化模型/综合评分五工具已预跑")
            # 显式且唯一的消息 id：既保证 tool_calls 应答配对，也供 tool_ledger
            # 种入去重（与 _accumulate_tool_ledger 的 mid 口径一致）。
            return [
                AIMessage(content="", tool_calls=calls),
                ToolMessage(content=str(v_res), name="validate_financial_data",
                            tool_call_id=call_ids["validate_financial_data"],
                            id=f"pre_tv_{pre_token}"),
                ToolMessage(content=str(c_res), name="calculate_financial_indicators",
                            tool_call_id=call_ids["calculate_financial_indicators"],
                            id=f"pre_tc_{pre_token}"),
                ToolMessage(content=str(d_res), name="check_disclosure_compliance",
                            tool_call_id=call_ids["check_disclosure_compliance"],
                            id=f"pre_td_{pre_token}"),
                ToolMessage(content=str(m_res), name="calculate_risk_models",
                            tool_call_id=call_ids["calculate_risk_models"],
                            id=f"pre_tm_{pre_token}"),
                ToolMessage(content=str(s_res), name="calculate_comprehensive_score",
                            tool_call_id=call_ids["calculate_comprehensive_score"],
                            id=f"pre_ts_{pre_token}"),
            ]
        except Exception as e:  # noqa: BLE001 — fail-open：预处理失败只意味着回到原速度
            logger.warning(f"预处理注入失败，回退原链路: {e}")
            return None

    async def run(self, payload: Dict[str, Any], ctx=None) -> Dict[str, Any]:
        if ctx is None:
            ctx = new_context("run")
        run_id = ctx.run_id
        # 开启新产物批次：本次分析的所有产物落 local_storage/<YYYYMMDD_HHMMSS>/，
        # 与旧批次隔离（精确到秒的独立文件夹），下次运行生成全新目录。
        from local_storage import begin_batch
        begin_batch()
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
            "calculate_risk_models",
            "search_regulations",
            "calculate_comprehensive_score",
        }

        # ── 综合研判模式同样必须先做 P1 预处理 ──
        # 实测缺陷：synthesis 分支原本完全跳过 _preprocess_inject，财务数据全靠 LLM
        # 从 17 万字符原文自行抽取，导致指标/模型/数据源大面积「未获取」。
        # 预处理只在串跑编排器内部跑一次（stream_sse 的单 Agent 守卫对 synthesis
        # 为假，不会重复执行）；预跑结果拆分后同时交给：
        #   ① 财务阶段（消息尾部追加预跑轨迹，避免重复抽取）；
        #   ② 综合研判阶段（合并进 tool_ledger 与 prior_tool_results）。
        pre_msgs, pre_entries, pre_tool_results = [], [], {}
        if isinstance(payload, dict) and len(self._last_user_text(payload)) >= self._PREPROCESS_MIN_CHARS:
            yield ("progress", "预提取财务数据并行预跑工具", 8)
            try:
                pre_msgs = await self._preprocess_inject(payload) or []
            except Exception as e:  # noqa: BLE001 - fail-open：预处理失败回退原链路
                logger.warning(f"综合研判预处理失败，回退原链路: {e}")
                pre_msgs = []
            from langchain_core.messages import ToolMessage
            for m in pre_msgs:
                if not isinstance(m, ToolMessage):
                    continue
                content = (m.content if isinstance(m.content, str)
                           else json.dumps(m.content, ensure_ascii=False))
                pre_entries.append([getattr(m, "id", None) or "", getattr(m, "name", "") or "", content])
                pre_tool_results[getattr(m, "name", "") or ""] = content
            if pre_entries:
                # 交回 stream_sse 登记进 seed_tool_results：预跑结果位于图输入侧，
                # 不出现在 astream 增量里，不登记则数据源清单/指标视图读不到。
                yield ("tool_results", pre_entries)
                yield ("progress", "预处理完成（校验/指标/披露）", 42)

        async def _run_one_stage(module: str, stage_name: str):
            """运行单个前置阶段（financial/compliance），返回摘要、工具结果和工具调用名序列。"""
            stage_payload = build_stage_payload(
                payload, module, MODULE_MARKERS[module], [])
            # 财务阶段吸收预跑轨迹：build_stage_payload 已用原始请求重写为末条
            # user 消息，此处再追加 ToolMessage，形态与单 Agent 预处理注​入一致。
            if module == "financial" and pre_msgs:
                stage_payload = {**stage_payload,
                                 "messages": list(stage_payload.get("messages", [])) + list(pre_msgs)}
                if pre_entries:
                    stage_payload = {**stage_payload,
                                     "tool_ledger": {"entries": pre_entries}}
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

        # 预跑结果先于两阶段结果入账：同名工具以确定性预跑为准（与 stream_sse
        # 的 seed_tool_results 优先级一致）。
        stage_ledger_entries.extend(pre_entries)
        prior_tool_results.update(pre_tool_results)
        # 按固定顺序整理前两阶段结果，保证第三阶段摘要顺序稳定
        for module, stage_name, _pct_start, _pct_end in SYNTHESIS_STAGES:
            if module == "synthesis":
                continue
            res = next((r for r in stage_results if r["module"] == module), None)
            if res:
                # 前两阶段后台并行，但对外按业务流程顺序发布阶段锚点。
                # 前端据此把并行执行结果整理成稳定的财务 → 合规 → 综合顺序。
                yield ("progress", stage_name, _pct_start)
                summaries.append((res["stage_name"], res["summary"]))
                stage_ledger_entries.extend(res["entries"])
                for _name, _content in res["tool_results"].items():
                    prior_tool_results.setdefault(_name, _content)
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
        # HTTP 入口会提前设置 request_context，但 CLI/测试/脚本可能直接调用
        # stream_sse。统一在服务层绑定当前上下文，保证最终快照能拿到同一个
        # run_id，避免真实演示产物的 analysis_id 为空而无法批次隔离。
        request_context.set(ctx)
        run_id = ctx.run_id
        # 开启新产物批次：三段串跑共享同一时间戳子目录，下次运行生成全新目录
        from local_storage import begin_batch
        begin_batch()
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
        final_snapshot = None  # 后处理产出的结构化最终快照，正常发布路径的事实源
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
                    final_snapshot = chunk.get("final_snapshot")
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
                report = self._build_final_report_from_messages(
                    seed_tool_results + processed, final_snapshot=final_snapshot)
            else:
                report = self._build_final_report_from_messages(
                    all_messages, final_snapshot=final_snapshot)

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

    def _build_final_report_from_messages(self, all_messages, final_snapshot=None):
        """从消息列表构建最终报告。

        链接来源有两处，必须都扫：
        - ToolMessage 内容（LLM 主动调用图表/导出工具的返回）；
        - 最后一条 AI 正文（兜底导出的 PDF/Excel 链接由 _post_process 追加在正文，
          不产生 ToolMessage；导出改全兜底后这是主要来源，漏扫会导致文件卡/历史无文件）。
        同一路径去重，避免两源重复。
        """
        ai_text = ""
        tool_results = []
        # 原文文本：报告身份兜底的公司名/报告期只能从年报正文确定性提取，
        # 这里取最长的一条 human 消息（即上传的年报解析文本）作为识别语料。
        report_text = ""

        for d in all_messages:
            msg_type = d.get("type", "")

            if msg_type in ("human", "HumanMessage"):
                content = d.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        p if isinstance(p, str) else p.get("text", "")
                        for p in content
                    )
                if isinstance(content, str) and len(content) > len(report_text):
                    report_text = content

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
            for m in re.finditer(r'(/local_storage/[^\s"\'<>）)\]，,]+\.(?:pdf|xlsx|json|txt))', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})
            # 兼容旧格式 file://... 路径
            for m in re.finditer(r'file://([^\s"\'<>]+\.png)', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})
            for m in re.finditer(r'file://([^\s"\'<>]+\.(?:pdf|xlsx|json|txt))', text):
                if m.group(1) not in seen_paths:
                    seen_paths.add(m.group(1))
                    artifact_candidates.append({"tool": source_name, "path": m.group(1)})

        for tr in tool_results:
            c = tr["content"] if isinstance(tr["content"], str) else str(tr["content"])
            _collect(tr["name"], c)
        # 兜底导出的链接在 AI 正文里（📎 PDF报告: .../📊 Excel底稿: ...）
        _collect("fallback_export", str(ai_text))

        # 正常新链路直接消费 Agent 产出的最终快照。ai_text 中的 JSON 只作为
        # 旧历史/异常链路兼容输入，不能再作为网页/API 的事实来源。
        risk_ledger = {}
        risk_json = ""
        snapshot_from_agent = isinstance(final_snapshot, dict) and bool(final_snapshot)
        if snapshot_from_agent:
            from core.report_publication import ensure_snapshot_identity
            from core.report_snapshot import snapshot_as_legacy_payload
            current_run_id = getattr(request_context.get(), "run_id", "") or ""
            snapshot_meta = ensure_snapshot_identity(final_snapshot, run_id=current_run_id)
            risk_ledger = snapshot_as_legacy_payload(snapshot_meta, {})
            risk_ledger["report_snapshot"] = snapshot_meta
        else:
            try:
                from agents.agent import _extract_risk_json
                risk_json = _extract_risk_json(str(ai_text)) or ""
                if risk_json:
                    parsed = json.loads(risk_json)
                    if isinstance(parsed, dict):
                        risk_ledger = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                risk_ledger = {}

            snapshot_meta = risk_ledger.get("report_snapshot") if isinstance(risk_ledger, dict) else None
            if isinstance(snapshot_meta, dict) and isinstance(snapshot_meta.get("risks"), dict):
                # 旧台账若已嵌入快照，也统一通过集中适配器展开兼容字段。
                from core.report_snapshot import snapshot_as_legacy_payload
                risk_ledger = snapshot_as_legacy_payload(snapshot_meta, risk_ledger)
                risk_ledger["report_snapshot"] = snapshot_meta

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

        from core.result_contract import DATA_VERSION, RULE_VERSION
        company_info = risk_ledger.get("company_info") if isinstance(risk_ledger, dict) else {}
        # 报告身份确定性兜底：LLM 漏抽公司名/报告期时，用原文（封面/股票代码/期间表述）
        # 补齐 company_info，保证 report_metadata 与产物文件名不退化为「未知公司」。
        # 仅补空字段，绝不覆盖模型已给出的非空值。
        if report_text:
            try:
                from utils.report_identity import apply_company_info_fallback
                company_info = apply_company_info_fallback(company_info, report_text)
                if isinstance(risk_ledger, dict):
                    risk_ledger["company_info"] = company_info
            except Exception as _identity_err:
                logger.warning("报告身份兜底识别失败（不阻断导出）: %s", _identity_err)
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
                {"key": "json", "kind": "json", "label": "TXT格式结构化风险台账（JSON内容）"},
            ]
        is_outlook = "INDUSTRY_OUTLOOK" in str(ai_text)
        if isinstance(expectations, list) and risk_ledger and not is_outlook \
                and not any(str(item.get("key", "")) == "json"
                            for item in expectations if isinstance(item, dict)):
            expectations = [*expectations, {
                "key": "json", "kind": "json", "label": "TXT格式结构化风险台账（JSON内容）"}]
        expectations = expectations if isinstance(expectations, list) else []

        # 结构化 JSON 以 TXT 独立产物交付，不再依赖 ai_text 的长度和 Markdown 渲染。
        # 去掉 manifest 是为了避免文件内容与自身的 content_hash 形成循环引用。
        if risk_ledger and not is_outlook and any(
                isinstance(item, dict) and item.get("key") == "json"
                for item in expectations):
            json_payload = copy.deepcopy(risk_ledger)
            json_payload.pop("artifact_manifest", None)
            json_snapshot = json_payload.get("report_snapshot")
            if isinstance(json_snapshot, dict):
                json_snapshot.pop("artifact_manifest", None)
            from core.report_publication import write_txt_artifact
            json_url = write_txt_artifact(json_payload, risk_ledger)
            if json_url:
                json_candidate = {"tool": "txt_artifact", "path": json_url}
                artifact_candidates.append(json_candidate)
                # PDF/Excel/图表候选已在上方完成扫描；TXT 是此处新生成的，
                # 立即通过同一可访问性门禁加入前端下载列表和 manifest。
                if _artifact_is_accessible(json_url):
                    accessible_candidates.append(json_candidate)
                    files.append(json_candidate)
        artifact_manifest = []

        snapshot_meta = risk_ledger.get("report_snapshot") if isinstance(risk_ledger, dict) else None
        snapshot_meta = snapshot_meta if isinstance(snapshot_meta, dict) else {}
        snapshot_quality = snapshot_meta.get("data_quality", {}) if isinstance(snapshot_meta, dict) else {}
        snapshot_present = bool(snapshot_meta.get("snapshot_id"))
        presentation_mode = str(snapshot_quality.get("presentation_mode") or
                                 (risk_ledger.get("presentation_mode", "") if isinstance(risk_ledger, dict) else "") or
                                 "strict")
        if presentation_mode in {"demo", "demo_placeholder"}:
            artifact_data_status = "demo_placeholder"
        elif snapshot_present and (snapshot_quality.get("pending_risks") or
                                    snapshot_quality.get("incomplete_facts") or
                                    snapshot_quality.get("incomplete_metrics")):
            artifact_data_status = "incomplete"
        else:
            artifact_data_status = "verified"
        artifact_status_labels = {
            "verified": "已核验",
            "incomplete": "数据源不完整，仅供参考",
            "unverified": "来源未核验，仅供人工复核",
            "demo_placeholder": "演示占位数据，不代表公司实际数据",
        }

        # 产物清单由集中发布模块生成。这里传入已通过文件可访问性门禁的候选项，
        # 兼容旧测试对 _artifact_is_accessible 的替换，同时避免发布层自行维护
        # 第二套 key 匹配和哈希逻辑。
        from core.report_publication import finalize_artifact_manifest
        artifact_warning = "" if artifact_data_status == "verified" else (
            f"{artifact_status_labels.get(artifact_data_status, artifact_data_status)}；"
            "请勿将该产物视为完整审计结论")
        artifact_manifest = finalize_artifact_manifest(
            expectations, accessible_candidates, analysis_id,
            str(snapshot_meta.get("snapshot_id", "") or ""),
            storage_root=os.getcwd(), accessible_fn=lambda _path: True,
            data_status=artifact_data_status,
            warning=artifact_warning)

        # 发布层才能确定文件是否真实存在，因此 manifest 在这里回写最终快照。
        # snapshot_digest 明确排除了 artifact_manifest，回写不会改变共享 snapshot_id。
        if isinstance(snapshot_meta, dict) and snapshot_meta.get("snapshot_id"):
            snapshot_meta["artifact_manifest"] = copy.deepcopy(artifact_manifest)
            if isinstance(risk_ledger, dict):
                # Rebuild the compatibility shell after the manifest is final.
                # This keeps persisted AI JSON and old history readers aligned
                # with the same snapshot-derived gate/status fields.
                from core.report_snapshot import snapshot_as_legacy_payload
                risk_ledger = snapshot_as_legacy_payload(snapshot_meta, risk_ledger)
                risk_ledger["report_snapshot"] = snapshot_meta

        # ai_text 会随会话历史持久化，不能继续保留模型生成的旧台账。
        # 否则网页顶层清单虽已正确，历史正文/API 再次提取时仍会得到空清单。
        # 只替换已被 _extract_risk_json 确认的首个结构化台账，保留正文和评分 marker。
        if risk_ledger:
            try:
                if snapshot_from_agent:
                    from core.report_publication import sync_ai_text_with_snapshot
                    ai_text = sync_ai_text_with_snapshot(ai_text, risk_ledger)
                elif risk_json:
                    updated_risk_json = json.dumps(risk_ledger, ensure_ascii=False)
                    ai_text = str(ai_text).replace(risk_json, updated_risk_json, 1)
            except (TypeError, ValueError):
                logger.warning("发布层回写结构化台账失败，保留原始 AI 正文")

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

        data_sources = _build_data_sources(tool_result_index, snapshot_meta)
        company = company_info if isinstance(company_info, dict) else {}
        gate = risk_ledger.get("review_gate") if isinstance(risk_ledger, dict) else {}
        gate = gate if isinstance(gate, dict) else {}
        snapshot_meta = risk_ledger.get("report_snapshot") if isinstance(risk_ledger, dict) else None
        snapshot_meta = snapshot_meta if isinstance(snapshot_meta, dict) else {}
        snapshot_validation = snapshot_meta.get("validation") if isinstance(snapshot_meta, dict) else {}
        if not isinstance(snapshot_validation, dict):
            snapshot_validation = {}
        if isinstance(snapshot_validation.get("data_validation"), dict):
            snapshot_validation = snapshot_validation["data_validation"]
        snapshot_source = snapshot_meta.get("source") if isinstance(snapshot_meta, dict) else {}
        if not isinstance(snapshot_source, dict):
            snapshot_source = {}
        report_metadata = {
            "analysis_id": analysis_id,
            "data_version": data_version,
            "rule_version": rule_version,
            "result_schema_version": str(risk_ledger.get("result_schema_version", "") or
                                           snapshot_meta.get("schema_version", ""))
                if isinstance(risk_ledger, dict) else "",
            "snapshot_id": str(snapshot_meta.get("snapshot_id", "") or "")
                or str(risk_ledger.get("snapshot_id", "") or ""),
            "validation_status": str(snapshot_validation.get("validation_result", "") or
                                      snapshot_meta.get("validation_status", "") or
                                      (risk_ledger.get("data_validation") or {}).get("validation_result", "") or "")
                if isinstance(risk_ledger, dict) else "",
            "source_hash": str(snapshot_source.get("source_hash", "") or
                                snapshot_meta.get("source_hash", "") or ""),
            "data_status": artifact_data_status,
            "warning": "" if artifact_data_status == "verified" else artifact_status_labels.get(artifact_data_status, artifact_data_status),
            "company_name": str(company.get("company_name", "") or ""),
            "stock_code": str(company.get("stock_code", "") or ""),
            "report_year": str(company.get("report_year", "") or ""),
            "report_period": str(company.get("report_period") or company.get("period") or ""),
            "industry": str(company.get("industry", "") or ""),
            "audit_opinion": str(company.get("audit_opinion", "") or ""),
            "accounting_standard": str(company.get("accounting_standard", "") or ""),
            "review_gate_status": str(gate.get("status", "not_run") or "not_run"),
            "human_review_required": bool(gate.get("human_review_required")),
            "data_sources": data_sources,
            "source_used_count": sum(1 for item in data_sources if item["used"]),
            "source_total": len(data_sources),
        }
        # 新链路的元数据以快照和最终 manifest 为准；data_sources 是网页特有的
        # 能力覆盖说明，保留在发布对象中但不反向影响身份、门禁和版本字段。
        if snapshot_present and isinstance(snapshot_meta.get("risks"), dict):
            from core.report_publication import publication_metadata
            report_metadata.update(publication_metadata(snapshot_meta, artifact_manifest))

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
            "report_snapshot": snapshot_meta if snapshot_present else {},
            "final_snapshot": copy.deepcopy(snapshot_meta) if snapshot_present else None,
            "snapshot_id": report_metadata["snapshot_id"],
            "risk_summary": (risk_ledger.get("risk_summary", {}) if isinstance(risk_ledger, dict) else {}),
            "data_status": artifact_data_status,
            "warning": report_metadata["warning"],
            "indicator_view": build_indicator_view(
                tool_result_index.get("calculate_financial_indicators", "")),
        }

    def _build_final_report(self, result):
        """兼容旧调用"""
        messages = result.get("messages", []) if result else []
        all_msgs = [self._msg_to_dict(m) for m in messages]
        return self._build_final_report_from_messages(
            all_msgs, final_snapshot=(result or {}).get("final_snapshot"))

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
    maintenance_task = None
    try:
        maintenance_policy = MaintenancePolicy.from_env()
        if maintenance_policy.enabled:
            maintenance_task = asyncio.create_task(maintenance_worker(maintenance_policy))
            logger.info(
                "定期回收已启动：每 %.1f 小时一次，检查点保留最近 %d 个线程",
                maintenance_policy.interval_hours,
                maintenance_policy.checkpoint_keep_threads,
            )
    except Exception as exc:  # noqa: BLE001 - 回收配置异常不应阻断启动
        logger.warning("定期回收启动失败（不影响主服务）: %s", exc)
    # 业务库建表（用户/会话/分析历史）：幂等 create_all；失败不阻断启动，
    # 仅登录/历史功能降级不可用（接口会返回可见错误），分析主流程不受影响。
    try:
        from storage.database.db import init_tables
        init_tables()
        logger.info("业务库表结构就绪（users / session_tokens / analysis_history）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"业务库初始化失败（登录/历史功能不可用）: {e}")
    service._get_agent()
    try:
        yield
    finally:
        # 先停止周期任务，再释放 checkpointer 连接
        if maintenance_task is not None:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
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

# ── 最小鉴权（可选）：配置 APP_API_KEY 后对写操作/敏感读接口强制校验 X-API-Key ──
# 默认不配置即完全放行，保证本地演示零摩擦；对外暴露服务时在 .env 中设置。
# S1 加固：/api/files（枚举全部分析产物）与 /api/reload_kb（重建知识库向量索引，
# 可被外部语料投毒）纳入保护；比较改用 hmac.compare_digest 防时序侧信道。
_PROTECTED_PREFIXES = (
    "/run",
    "/stream_run",
    # OpenAI 兼容端点同样会真实调用大模型并写产物，必须与 /run 同级保护
    "/v1/chat/completions",
    "/upload",
    "/api/upload_kb",
    "/api/evaluate/run",
    "/api/files",
    "/api/reload_kb",
)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """写操作接口的 X-API-Key 校验中间件（APP_API_KEY 未配置时直接放行）。"""
    expected = os.getenv("APP_API_KEY", "")
    if expected and request.url.path.startswith(_PROTECTED_PREFIXES):
        import hmac as _hmac
        if not _hmac.compare_digest(request.headers.get("X-API-Key", ""), expected):
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
# S3 登录防爆破限速器（内存滑动窗口，进程级；无新依赖）。
# 按「IP + 用户名」双键计数：任一键在窗口内超限即 429。
# 本地单机演示默认零摩擦；对外部署时可按需调严。
# ══════════════════════════════════════════════════════
_AUTH_RATE_WINDOW_SEC = 300        # 5 分钟窗口
_AUTH_RATE_MAX_ATTEMPTS = 10       # 窗口内最多 10 次失败尝试
_AUTH_RATE_MAX_PER_IP = 30         # 窗口内单 IP 总上限（防换用户名绕过）
_auth_attempts: Dict[str, list] = {}
_auth_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取客户端 IP（优先代理头，防直连伪造仅作尽力而为）。"""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _auth_rate_check(request: Request, username: str) -> str | None:
    """检查登录/注册尝试是否超限；超限返回原因文本，未超限返回 None。"""
    import time as _time
    ip = _client_ip(request)
    now = _time.time()
    cutoff = now - _AUTH_RATE_WINDOW_SEC
    with _auth_lock:
        # 惰性清理过期记录
        stale = [k for k, ts in _auth_attempts.items()
                 if not ts or ts[-1] < cutoff]
        for k in stale:
            _auth_attempts.pop(k, None)
        user_key = f"u:{ip}:{str(username or '')[:64]}"
        ip_key = f"ip:{ip}"
        user_ts = [t for t in _auth_attempts.get(user_key, []) if t >= cutoff]
        ip_ts = [t for t in _auth_attempts.get(ip_key, []) if t >= cutoff]
        if len(user_ts) >= _AUTH_RATE_MAX_ATTEMPTS:
            return "登录尝试过于频繁，请 5 分钟后重试"
        if len(ip_ts) >= _AUTH_RATE_MAX_PER_IP:
            return "该地址请求过于频繁，请稍后重试"
        _auth_attempts[user_key] = user_ts + [now]
        _auth_attempts[ip_key] = ip_ts + [now]
    return None


# S5 分析并发上限（进程级信号量）：/run 与 /stream_run 共享，超限 429 排队拒绝。
# 每个分析任务内部还有导出线程池/matplotlib/chroma/LLM 调用，无界并发会放大
# 内存与 API 账单，默认 4 路（本地单机场景足够；可用 env 覆盖）。
try:
    _ANALYSIS_MAX_CONCURRENT = max(1, int(os.getenv("ANALYSIS_MAX_CONCURRENT", "4")))
except (TypeError, ValueError):
    _ANALYSIS_MAX_CONCURRENT = 4
_analysis_semaphore = asyncio.Semaphore(_ANALYSIS_MAX_CONCURRENT)


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
    """口令登录，签发会话令牌（用户名不存在与口令错误统一提示，防枚举）。
    S3：按 IP+用户名限速防在线爆破；PBKDF2 用 to_thread 下放，不阻塞事件循环。"""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"status": "error", "message": "请求体不是合法 JSON"}, status_code=400)
    _username = str(body.get("username", ""))[:64]
    limited = _auth_rate_check(request, _username)
    if limited:
        logger.warning(f"登录限速触发: ip={_client_ip(request)}, user={_username}")
        return JSONResponse({"status": "error", "message": limited}, status_code=429)
    try:
        from storage.database.user_service import login_user
        # S3：PBKDF2 120k 迭代约 50-100ms，放事件循环内会阻塞所有并发请求
        session_info = await asyncio.to_thread(
            login_user, body.get("username", ""), body.get("password", ""))
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
        # S5 并发配额：超过上限直接 429（不排队，快速失败让前端提示重试），
        # 防止无界并发放大内存与 LLM 账单
        if _analysis_semaphore.locked():
            return JSONResponse(
                {"status": "busy", "message": f"当前已有 {_ANALYSIS_MAX_CONCURRENT} 个分析在执行，请稍后重试"},
                status_code=429)
        async with _analysis_semaphore:
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
        # S7：/run 内部错误不回显异常串（含内部路径/库细节），只给泛化文案
        logger.error(f"Error in /run: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail={"error": "服务内部错误，请稍后重试（详情见服务日志）"})
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

    # S5 并发配额：与 /run 共享信号量，超限 429 快速失败
    if _analysis_semaphore.locked():
        return JSONResponse(
            {"status": "busy", "message": f"当前已有 {_ANALYSIS_MAX_CONCURRENT} 个分析在执行，请稍后重试"},
            status_code=429)

    async def _guarded_stream():
        async with _analysis_semaphore:
            generator = service.stream_sse(payload, ctx, user=_current_user(request))
            async for chunk in generator:
                yield chunk

    return StreamingResponse(_guarded_stream(), media_type="text/event-stream")


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


@app.get("/api/maintenance/status")
async def maintenance_status():
    """只读查询最近一次定期回收报告（不触发回收）。"""
    report = last_report()
    if not report:
        return {"status": "idle", "message": "尚无回收记录（首次回收将在启动后调度）"}
    return {"status": "ok", "report": report}


def _process_uploads(files) -> list:
    """S4：/upload 的同步处理主体（保存落盘 + 多格式解析）。

    100MB 扫描版 PDF 的解析可达数十秒，放在 async 路由内联执行会阻塞
    事件循环、冻结全站；抽成同步函数由调用方 to_thread 下放。
    Args:
        files: [(filename, BytesIO) 元组]——async 层已预读的文件内容。
    """
    results = []
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    for filename, bio in files:
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            results.append({
                "filename": filename,
                "status": "error",
                "error": f"不支持的文件格式: {ext}，仅支持 {', '.join(ALLOWED_EXTENSIONS)}",
            })
            continue

        bio.seek(0, os.SEEK_END)
        f_size = bio.tell()
        if f_size > MAX_UPLOAD_SIZE:
            results.append({
                "filename": filename,
                "status": "error",
                "error": f"文件大小 ({f_size} bytes) 超过限制 100MB",
            })
            continue

        # 路径穿越防护：剥离路径成分并净化非法字符（与 /api/upload_kb 的净化逻辑对齐），
        # 防止构造 "../../../evil" 之类文件名逃逸出 UPLOAD_DIR
        import re as _re
        safe_name = _re.sub(r'[<>:"/\\|?*]', '_', os.path.basename(filename or "file"))
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        save_path = os.path.join(UPLOAD_DIR, unique_name)

        try:
            # 流式落盘：分块读取边写边计数，内存峰值仅一个 chunk（1MB）；
            # 超限立即中断并删除半成品文件，避免全量读内存导致并发 OOM。
            # S4：本函数已改为同步（to_thread 下放），UploadFile 的异步读
            # 需在此先行读入内存分块再落盘——由调用方先按 1MB 分块读齐。
            size = 0
            oversize = False
            source_digest = hashlib.sha256()
            with open(save_path, "wb") as out:
                while True:
                    chunk = bio.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_SIZE:
                        oversize = True
                        break
                    out.write(chunk)
                    source_digest.update(chunk)
            if oversize:
                os.remove(save_path)
                results.append({
                    "filename": filename,
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
                "filename": filename,
                "saved_path": save_path,
                "status": "ok",
                "file_size": size,
                "extracted_text": extracted_text,
                "page_count": page_count,
                # 上传层计算的源文件指纹随结构化元数据传给前端和终局快照；
                # 不把哈希埋在解析文本中，避免模型改写或截断来源信息。
                "source_document": filename or "",
                "source_hash": source_digest.hexdigest(),
            })
            logger.info(f"文件上传成功: {filename} → {save_path} ({size} bytes)")

        except Exception as e:
            # S7：对外只回泛化文案——异常串可能含服务器内部绝对路径/库细节；
            # 完整原因已入服务日志（logger.error）。
            logger.error(f"文件上传失败: {filename}: {e}\n{traceback.format_exc()}")
            results.append({
                "filename": filename,
                "status": "error",
                "error": "文件处理失败，请确认文件未损坏后重试（详情见服务日志）",
            })

    return results


@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """接收上传文件，保存到临时目录并解析提取文本内容。

    支持多种文件格式：PDF/Word/Excel/CSV/HTML/PPT/TXT/Markdown。
    每种格式使用对应的解析器提取纯文本，供后续 AI 分析使用。
    提取文本超过 20 万字符时自动截断，防止 LLM 上下文溢出。
    S4：async 层只做网络读取（UploadFile 必须在事件循环内读），
    解析主体同步执行（_process_uploads）经 to_thread 下放，
    100MB 级扫描件解析期间事件循环照常服务其他请求。
    """
    # 预读：UploadFile 是异步文件对象，只能在事件循环内读取；
    # 按 1MB 分块读入 BytesIO（内存峰值 ~文件大小，100MB 上限可控）
    import io as _io
    preloaded = []
    for f in files:
        buf = _io.BytesIO()
        while chunk := await f.read(1024 * 1024):
            buf.write(chunk)
        preloaded.append((f.filename, buf))
    results = await asyncio.to_thread(_process_uploads, preloaded)
    return {"files": results}


@app.post("/api/reload_kb")
async def reload_knowledge_base():
    """热重载知识库，无需重启服务（S1：改为 POST——重建索引是状态变更操作，
    GET 形式可被预取/CSRF 触发；配合 APP_API_KEY 保护清单防检索投毒）。

    清空当前知识库缓存（文档列表 + ChromaDB collection），
    然后重新扫描 knowledge_base/ 目录并重建向量索引。
    适用于用户上传新法规文件后刷新知识库。
    """
    try:
        from local_knowledge import get_knowledge_base

        def _do_reload():
            kb = get_knowledge_base()
            kb.documents.clear()
            kb._collection = None
            kb._loaded = False
            kb.load()
            return kb

        # S4：重建向量索引可达分钟级（嵌入式 ONNX 逐块 embedding），
        # 必须下放线程池，否则期间全站（登录/历史/SSE）无响应
        kb = await asyncio.to_thread(_do_reload)
        return {
            "status": "ok",
            "message": f"知识库重载完成，共 {len(kb.documents)} 个文档块",
            "file_count": len(kb.documents),
        }
    except Exception as e:
        # S7：重载失败原因只进日志，不回显内部细节
        logger.error(f"知识库重载失败: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": "知识库重载失败，请查看服务日志后重试"}


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
        return {"status": "error", "message": "读取评估结果失败，请稍后重试（详见服务日志）", "data": None}


@app.post("/api/evaluate/run")
async def run_evaluation(mode: str = "tool", output_path: str | None = None):
    """运行效果评估（工具模式或全链路 Agent 模式）。

    工具模式：直接调用本地确定性评估逻辑计算 22 个测试用例，通常数秒内完成。
    Agent 模式：调用完整 LLM + RAG + 辩论链路，每个用例约 30-60 秒。

    Args:
        mode: 评估模式，"tool"=工具级（快速）, "agent"/"all"=全链路
        output_path: 结果落盘路径；缺省写 tests/evaluation_results.json。
            单元测试必须传 tmp_path（T5：禁止测试覆写版本库跟踪的评估结果）。
    """
    try:
        # 评估脚本与服务使用同一项目根目录，避免 COZE_WORKSPACE_PATH 指向父目录时
        # 产生错误的 cwd/PYTHONPATH。工具评估本身是同步计算，放到线程中避免阻塞
        # FastAPI 事件循环；CLI 与接口共用 tests.evaluation_report.run_evaluation。
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "tests" / "evaluation_report.py"
        if not os.path.exists(script_path):
            return {"status": "error", "message": f"评估脚本不存在: {script_path}"}

        if mode == "tool":
            import importlib.util

            def _run_tool():
                spec = importlib.util.spec_from_file_location("evaluation_report_service", script_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"无法加载评估模块: {script_path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                output_path_final = output_path or str(project_root / "tests" / "evaluation_results.json")
                return module.run_evaluation("tool", output_path_final)

            data = await asyncio.wait_for(asyncio.to_thread(_run_tool), timeout=30)
            return {"status": "ok", "mode": "tool", "data": data}

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

    except asyncio.TimeoutError:
        return {"status": "error", "message": "评估超时（30秒），请检查系统状态"}
    except Exception as e:
        logger.error(f"运行评估失败: {e}")
        return {"status": "error", "message": "评估执行失败，请查看服务日志后重试"}


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


def _pick_free_port(host: str, port: int, attempts: int = 20) -> int:
    """端口自愈：若目标端口不可 bind，则向后顺延到空闲端口。

    Windows 上加速器/代理软件（如 Watt Toolkit / Steam++）会以 Bound 状态长期占用
    5000-5002 等端口，uvicorn 直接启会报 WinError 10013 退出。这里用真实 bind 探测，
    避免用户双击启动却看不到任何有效提示。
    """
    import socket

    for candidate in range(port, port + attempts + 1):
        for addr in (host, "0.0.0.0"):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((addr, candidate))
            except OSError:
                break
            finally:
                s.close()
        else:
            return candidate
    return port


def start_http_server(port):
    reload = os.getenv("ENV", "dev") == "dev"
    host = os.getenv("HOST", "127.0.0.1")
    picked = _pick_free_port(host, port)
    if picked != port:
        logger.warning(f"端口 {port} 不可用（可能被加速器/代理软件占用），已自动改用 {picked}")
        port = picked
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

