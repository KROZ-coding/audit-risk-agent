"""会话检查点存储器 - 管理 Agent 会话状态持久化

本模块提供 LangGraph Agent 会话的检查点（Checkpoint）管理功能，用于保存和恢复对话状态。

持久化策略（v3.1 起）：
1. 优先使用 AsyncSqliteSaver（基于 aiosqlite）将检查点落到本地 SQLite 文件，
   解决原 MemorySaver「重启丢状态 + 内存无限增长（泄漏）+ 锁死单 worker」三大问题；
2. AsyncSqliteSaver 的构造要求处于运行中的事件循环内，因此由 FastAPI lifespan
   在启动时 await init_memory_saver() 完成初始化，再构建 Agent；
3. 任一环节失败（缺依赖 / 非异步上下文 / IO 异常）自动回退到内存版 MemorySaver，
   保证服务始终可用（如 CLI flow 模式未触发 lifespan 时即走回退）。

核心功能：
1. MemoryManager 单例：全局唯一管理器，避免重复初始化
2. init_memory_saver()：异步入口，在事件循环内创建持久化 saver
3. get_memory_saver()：同步入口，被 build_agent 调用，返回已初始化的 saver 或回退
4. 通过 atexit 在程序退出时清理资源
"""
import os
import atexit
import logging
from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)


def _checkpoint_db_path() -> str:
    """确定检查点 SQLite 文件路径。

    优先级：
    1. 环境变量 CHECKPOINT_DB_PATH（显式指定）
    2. 工作目录下的 checkpoints.sqlite（零配置默认）

    注：检查点库与业务库（db.py）刻意分离——检查点是 LangGraph 内部状态，
    与业务数据生命周期不同，独立文件便于单独清理/迁移。
    """
    explicit = os.getenv("CHECKPOINT_DB_PATH")
    if explicit:
        return explicit
    return os.path.join(os.getcwd(), "checkpoints.sqlite")


class MemoryManager:
    """MemoryManager 单例类 - 管理会话检查点（Checkpoint）的创建和获取。

    设计要点：
    - 使用 __new__ 实现经典单例模式，确保全局只有一个管理器实例
    - init_persistent() 在事件循环内创建 AsyncSqliteSaver（持久化）
    - get_checkpointer() 同步返回已初始化的 saver；未初始化则回退 MemorySaver
    - 持有 aiosqlite 连接以便退出时关闭
    """

    _instance: Optional['MemoryManager'] = None
    # 检查点存储器实例缓存（AsyncSqliteSaver 或 MemorySaver）
    _checkpointer: Optional[BaseCheckpointSaver] = None
    # aiosqlite 连接（仅持久化模式持有，用于退出时关闭）
    _conn = None
    # 是否已成功启用持久化
    _is_persistent: bool = False

    def __new__(cls):
        """经典单例模式：确保全局只有一个 MemoryManager 实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def init_persistent(self) -> BaseCheckpointSaver:
        """在事件循环内初始化 AsyncSqliteSaver（持久化检查点）。

        幂等：已成功持久化则直接返回。任一步骤失败则回退 MemorySaver，
        并记录告警，保证不阻断服务启动。

        Returns:
            持久化的 AsyncSqliteSaver，或回退的 MemorySaver
        """
        if self._checkpointer is not None and self._is_persistent:
            return self._checkpointer
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            path = _checkpoint_db_path()
            # check_same_thread=False：aiosqlite 在独立线程执行，需放开线程校验
            conn = await aiosqlite.connect(path, check_same_thread=False)
            saver = AsyncSqliteSaver(conn)
            # 预创建检查点表（幂等），避免首次写入时的竞态
            if hasattr(saver, "setup"):
                await saver.setup()

            self._conn = conn
            self._checkpointer = saver
            self._is_persistent = True
            logger.info(f"使用 AsyncSqliteSaver 持久化会话检查点: {path}")
        except Exception as e:
            logger.warning(f"持久化 checkpointer 初始化失败，回退 MemorySaver: {e}")
            if self._checkpointer is None:
                self._checkpointer = MemorySaver()
        return self._checkpointer

    def _create_fallback_checkpointer(self) -> MemorySaver:
        """创建内存检查点存储器（MemorySaver）作为回退。

        速度快、无 IO 开销，但程序重启后数据丢失且内存不淘汰。
        仅在持久化不可用时使用（如 CLI 一次性执行、缺依赖）。
        """
        self._checkpointer = MemorySaver()
        logger.info("使用 MemorySaver 作为 checkpointer（未持久化，重启丢失）")
        return self._checkpointer

    def get_checkpointer(self) -> BaseCheckpointSaver:
        """获取检查点存储器（同步）。

        若 init_persistent() 已初始化则返回持久化 saver；
        否则回退到 MemorySaver。被 build_agent() 同步调用。
        """
        if self._checkpointer is not None:
            return self._checkpointer
        return self._create_fallback_checkpointer()

    async def aclose(self):
        """关闭底层 aiosqlite 连接（持久化模式）。"""
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None


# 模块级全局变量：缓存 MemoryManager 单例实例
_memory_manager: Optional[MemoryManager] = None


def _shutdown_memory_manager():
    """程序退出时的资源清理回调函数（通过 atexit 注册）。

    注：aiosqlite 连接的异步关闭无法在 atexit 同步上下文可靠 await，
    此处仅记录日志；进程退出时操作系统会释放文件句柄。
    """
    logger.info("Memory manager shutdown")


def _ensure_manager() -> MemoryManager:
    """惰性创建 MemoryManager 单例并注册退出钩子。"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
        atexit.register(_shutdown_memory_manager)
    return _memory_manager


def get_memory_saver() -> BaseCheckpointSaver:
    """获取全局唯一的检查点存储器，供 LangGraph Agent 使用（同步入口）。

    被 src/agents/agent.py 的 build_agent() 调用。若 lifespan 已通过
    init_memory_saver() 完成持久化初始化，则返回 AsyncSqliteSaver；
    否则返回回退的 MemorySaver。

    Returns:
        实现 BaseCheckpointSaver 接口的检查点存储器实例
    """
    return _ensure_manager().get_checkpointer()


async def init_memory_saver() -> BaseCheckpointSaver:
    """异步初始化持久化检查点，由 FastAPI lifespan 在启动时 await 调用。

    必须在构建 Agent（build_agent → get_memory_saver）之前调用，
    这样 build_agent 拿到的就是持久化 saver。

    Returns:
        持久化的 AsyncSqliteSaver，或回退的 MemorySaver
    """
    return await _ensure_manager().init_persistent()


async def close_memory_saver():
    """关闭检查点存储器资源，由 FastAPI lifespan 在关闭时调用。"""
    if _memory_manager is not None:
        await _memory_manager.aclose()
