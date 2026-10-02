"""本地兼容层 - 替代 coze_coding_utils / coze_coding_dev_sdk 的平台绑定部分

在本地 Windows 环境下提供最小可运行的替代实现。
"""
import os
import re
import uuid
import logging
import threading
import json
import traceback
from typing import Any, Dict, Optional, Iterable, AsyncIterable
from dataclasses import dataclass, field
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 合法 run_id 格式：16-64 位十六进制字符或连字符（覆盖 uuid4.hex 与带连字符 UUID），
# 用于校验客户端 x-run-id，拒绝可推测/畸形值防会话固定劫持
_re_runid = re.compile(r"[0-9a-fA-F-]{16,64}")


# ─────────────────────────────────────────────────────────
# 1. Context 替代 (coze_coding_utils.runtime_ctx.context)
# ─────────────────────────────────────────────────────────

@dataclass
class Context:
    run_id: str = ""
    method: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.run_id:
            self.run_id = uuid.uuid4().hex


def normalize_run_id(raw: str) -> str:
    """校验客户端 run_id：合法（强格式）则原样返回，否则服务端重新生成。

    供 HTTP 入口与 new_context 共用，避免入口层直接信任原始头部而绕过校验（会话固定防护）。
    """
    raw = (raw or "").strip()
    return raw if _re_runid.fullmatch(raw) else uuid.uuid4().hex


def new_context(method: str = "", headers: Any = None) -> Context:
    hdrs = dict(headers) if headers else {}
    # 会话固定/劫持防护：客户端 x-run-id 会被直接用作 LangGraph checkpointer 的
    # thread_id（会话隔离边界）。仅接受强格式的 run_id（16-64 位十六进制/连字符），
    # 拒绝可推测或畸形值（如他人知晓的短字串），不合法则服务端重新生成，
    # 避免攻击者伪造 thread_id 读取或注入他人会话（用户级隔离由上层登录体系叠加）。
    run_id = normalize_run_id(hdrs.get("x-run-id", ""))
    return Context(run_id=run_id, method=method, headers=hdrs)


def default_headers(ctx=None) -> dict:
    return {}


# request_context：线程安全的全局上下文变量
_request_context_var: ContextVar[Optional[Context]] = ContextVar("request_context", default=None)


class _RequestContextProxy:
    """模拟 request_context.get() / request_context.set()"""
    def get(self) -> Optional[Context]:
        return _request_context_var.get()

    def set(self, ctx: Context):
        _request_context_var.set(ctx)


request_context = _RequestContextProxy()


# ─────────────────────────────────────────────────────────
# 2. 日志替代 (coze_coding_utils.log.*)
# ─────────────────────────────────────────────────────────

LOG_FILE = os.path.join(os.getcwd(), "app.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# 捕获原始 LogRecord 工厂（仅一次），保证重复安装不会层层包裹
_BASE_LOG_RECORD_FACTORY = logging.getLogRecordFactory()


def _run_id_log_record_factory(*args, **kwargs):
    """LogRecord 工厂：从 request_context 注入当前 run_id。

    每条日志记录都会带上 run_id 属性（无上下文时为 '-'），从而保证格式串中的
    %(run_id)s 永不缺失，同一次分析（run）内的工具链日志可按 run 关联。
    """
    record = _BASE_LOG_RECORD_FACTORY(*args, **kwargs)
    run_id = "-"
    try:
        ctx = request_context.get()
        if ctx is not None and getattr(ctx, "run_id", ""):
            run_id = ctx.run_id
    except Exception:
        run_id = "-"
    record.run_id = run_id
    return record


def _install_run_id_log_record_factory():
    """幂等安装 run_id 工厂：始终以捕获的原始工厂为基准，避免重复包裹。"""
    logging.setLogRecordFactory(_run_id_log_record_factory)


def setup_logging(log_file=None, max_bytes=100*1024*1024, backup_count=5,
                  log_level="INFO", use_json_format=False, console_output=True):
    _install_run_id_log_record_factory()
    handlers = []
    fmt = "%(asctime)s [%(levelname)s] [run=%(run_id)s] %(name)s - %(message)s"
    formatter = logging.Formatter(fmt)
    if console_output:
        # Windows 控制台默认代码页常为 GBK/CP936；显式让输出流使用 UTF-8，
        # 避免中文日志在部署/排查时显示为乱码（仅影响控制台，不改文件日志）。
        import sys
        stream = sys.stdout
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        ch = logging.StreamHandler(stream)
        ch.setFormatter(formatter)
        handlers.append(ch)
    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(formatter)
        handlers.append(fh)
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), handlers=handlers, force=True)


# ─────────────────────────────────────────────────────────
# 3. 错误分类替代 (coze_coding_utils.error.classifier)
# ─────────────────────────────────────────────────────────

@dataclass
class ClassifiedError:
    code: int = 500
    message: str = ""
    category: Any = None


class _ErrorCategory:
    def __init__(self, name="unknown"):
        self.name = name


class ErrorClassifier:
    def classify(self, exc: Exception, meta: dict = None) -> ClassifiedError:
        return ClassifiedError(code=500, message=str(exc), category=_ErrorCategory("runtime"))

    def get_error_response(self, exc: Exception, meta: dict = None) -> dict:
        return {
            "error_code": 500,
            "error_message": str(exc),
        }


def classify_error(exc: Exception, meta: dict = None) -> ClassifiedError:
    return ErrorClassifier().classify(exc, meta)


# ─────────────────────────────────────────────────────────
# 4. Stream Runner 替代 (coze_coding_utils.helper.stream_runner)
# ─────────────────────────────────────────────────────────

@dataclass
class RunOpt:
    workflow_debug: bool = False


class AgentStreamRunner:
    def stream(self, payload, graph, run_config, ctx) -> Iterable:
        for chunk in graph.stream(payload, config=run_config):
            yield chunk

    async def astream(self, payload, graph, run_config, ctx, run_opt=None) -> AsyncIterable:
        async for chunk in graph.astream(payload, config=run_config):
            yield chunk


class WorkflowStreamRunner:
    def stream(self, payload, graph, run_config, ctx) -> Iterable:
        for chunk in graph.stream(payload, config=run_config):
            yield chunk

    async def astream(self, payload, graph, run_config, ctx, run_opt=None) -> AsyncIterable:
        async for chunk in graph.astream(payload, config=run_config, stream_mode="updates"):
            yield chunk


async def agent_stream_handler(payload, ctx, run_id, stream_sse_func, sse_event_func,
                                error_classifier, register_task_func, **kw):
    import asyncio
    async def _gen():
        try:
            async for event in stream_sse_func(payload, ctx):
                yield event
        except Exception as e:
            logger.error(f"agent_stream_handler error: {e}")
            # 不向客户端回传原始异常文本（可能含内部路径/密钥片段），只回通用提示
            yield sse_event_func({"error": "内部处理错误，请稍后重试（详见服务日志）"})
    return _gen()


async def workflow_stream_handler(payload, ctx, run_id, stream_sse_func, sse_event_func,
                                   error_classifier, register_task_func, run_opt=None, **kw):
    async def _gen():
        try:
            async for event in stream_sse_func(payload, ctx, run_opt=run_opt):
                yield event
        except Exception as e:
            logger.error(f"workflow_stream_handler error: {e}")
            # 同 agent_stream_handler：异常详情仅入服务端日志，客户端只得通用提示
            yield sse_event_func({"error": "内部处理错误，请稍后重试（详见服务日志）"})
    return _gen()


# ─────────────────────────────────────────────────────────
# 5. Graph Helper 替代 (coze_coding_utils.helper.graph_helper)
# ─────────────────────────────────────────────────────────

class graph_helper:
    @staticmethod
    def is_agent_proj() -> bool:
        return True  # 本项目固定为 agent 模式

    @staticmethod
    def get_agent_instance(module_path: str, ctx=None, **build_kwargs):
        # 透传额外构建参数（如 model_override）给 build_agent，保持无参调用向后兼容
        import importlib
        mod = importlib.import_module(module_path)
        return mod.build_agent(ctx, **build_kwargs)

    @staticmethod
    def get_graph_instance(module_path: str):
        import importlib
        mod = importlib.import_module(module_path)
        return mod.build_graph()

    @staticmethod
    def is_dev_env() -> bool:
        return os.getenv("ENV", "dev") == "dev"

    @staticmethod
    def get_graph_node_func_with_inout(graph, node_id):
        node = graph.nodes.get(node_id)
        if node is None:
            return None, None, None
        return node, None, None


# ─────────────────────────────────────────────────────────
# 6. Agent Helper 替代
# ─────────────────────────────────────────────────────────

def to_stream_input(client_msg):
    return {"messages": [client_msg]}


def to_client_message(payload):
    from langchain_core.messages import HumanMessage
    if isinstance(payload, dict):
        text = payload.get("text", "") or payload.get("message", "") or json.dumps(payload, ensure_ascii=False)
    else:
        text = str(payload)
    msg = HumanMessage(content=text)
    return msg, None


# ─────────────────────────────────────────────────────────
# 7. OpenAI Handler 替代 (简化版)
# ─────────────────────────────────────────────────────────

class OpenAIChatHandler:
    def __init__(self, service):
        self.service = service

    async def handle(self, payload: dict, ctx) -> dict:
        from langchain_core.messages import HumanMessage, AIMessage
        messages = payload.get("messages", [])
        if not messages:
            last_msg = ""
        else:
            last = messages[-1]
            last_msg = last.get("content", "") if isinstance(last, dict) else str(last)
        result = await self.service.run({"messages": [HumanMessage(content=last_msg)]}, ctx)
        ai_msgs = [m for m in result.get("messages", []) if isinstance(m, AIMessage)]
        content = ai_msgs[-1].content if ai_msgs else ""
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ─────────────────────────────────────────────────────────
# 8. LangGraph Parser 替代 (简化)
# ─────────────────────────────────────────────────────────

class LangGraphParser:
    def __init__(self, graph):
        self.graph = graph

    def get_node_metadata(self, node_id: str) -> Optional[dict]:
        return None


# ─────────────────────────────────────────────────────────
# 9. Trace / Loop Trace 替代
# ─────────────────────────────────────────────────────────

def extract_core_stack() -> str:
    return traceback.format_exc()


def init_run_config(graph, ctx) -> dict:
    return {
        "configurable": {"thread_id": getattr(ctx, "run_id", uuid.uuid4().hex)},
        "recursion_limit": 50,
    }


def init_agent_config(graph, ctx) -> dict:
    return init_run_config(graph, ctx)


# ─────────────────────────────────────────────────────────
# 10. cozeloop 替代（空操作）
# ─────────────────────────────────────────────────────────

class cozeloop:
    @staticmethod
    def flush():
        pass


# ─────────────────────────────────────────────────────────
# 11. AsyncTask 替代（简化，仅保留接口）
# ─────────────────────────────────────────────────────────

class AsyncTaskStorageError(Exception):
    pass


class async_task_config:
    RECURSION_LIMIT = 50


HEADER_X_RUN_ID = "x-run-id"


def parse_deadline_sec(headers) -> int:
    return 3600


def extract_biz_context(headers) -> dict:
    return {}


class AsyncTaskRuntime:
    def __init__(self, session_factory, engine, graph, checkpointer):
        self.graph = graph
        self.checkpointer = checkpointer

    async def submit(self, task_id, payload, biz_context, deadline_sec, run_config, ctx) -> dict:
        import asyncio
        result = await self.graph.ainvoke(payload, config=run_config)
        return {"task_id": task_id, "status": "completed", "result": result}

    async def get(self, task_id) -> Optional[dict]:
        return None

    async def shutdown(self):
        pass
