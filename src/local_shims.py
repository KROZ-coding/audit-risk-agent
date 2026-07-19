"""本地兼容层 - 替代 coze_coding_utils / coze_coding_dev_sdk 的平台绑定部分

在本地 Windows 环境下提供最小可运行的替代实现。
"""
import os
import uuid
import logging
import threading
import json
import traceback
from typing import Any, Dict, Optional, Iterable, AsyncIterable
from dataclasses import dataclass, field
from contextvars import ContextVar

logger = logging.getLogger(__name__)


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


def new_context(method: str = "", headers: Any = None) -> Context:
    hdrs = dict(headers) if headers else {}
    run_id = hdrs.get("x-run-id", "") or uuid.uuid4().hex
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


def setup_logging(log_file=None, max_bytes=100*1024*1024, backup_count=5,
                  log_level="INFO", use_json_format=False, console_output=True):
    handlers = []
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    formatter = logging.Formatter(fmt)
    if console_output:
        ch = logging.StreamHandler()
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
            yield sse_event_func({"error": str(e)})
    return _gen()


async def workflow_stream_handler(payload, ctx, run_id, stream_sse_func, sse_event_func,
                                   error_classifier, register_task_func, run_opt=None, **kw):
    async def _gen():
        try:
            async for event in stream_sse_func(payload, ctx, run_opt=run_opt):
                yield event
        except Exception as e:
            logger.error(f"workflow_stream_handler error: {e}")
            yield sse_event_func({"error": str(e)})
    return _gen()


# ─────────────────────────────────────────────────────────
# 5. Graph Helper 替代 (coze_coding_utils.helper.graph_helper)
# ─────────────────────────────────────────────────────────

class graph_helper:
    @staticmethod
    def is_agent_proj() -> bool:
        return True  # 本项目固定为 agent 模式

    @staticmethod
    def get_agent_instance(module_path: str, ctx=None):
        import importlib
        mod = importlib.import_module(module_path)
        return mod.build_agent(ctx)

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
