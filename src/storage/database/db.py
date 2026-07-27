"""数据库连接管理与会话工厂

【当前定位】已正式接线的业务库：承载多用户登录与分析历史三张表
（users / session_tokens / analysis_history，见 shared/model.py 与 user_service.py），
服务启动时由 main.py lifespan 幂等建表。会话检查点仍由 memory_saver.py 独立
持久化到 checkpoints.sqlite（两库刻意分离，生命周期不同）；配置 PGDATABASE_URL
可无缝切换 PostgreSQL，缺省使用零配置的本地 SQLite（local_data.db）。

本模块提供 SQLAlchemy 数据库连接的集中管理，支持以下功能：
1. 自动检测数据库 URL（优先从环境变量读取，本地开发默认使用 SQLite）
2. 数据库连接失败时自动重试（最长 20 秒），提高服务启动稳定性
3. 惰性初始化引擎与会话工厂，避免模块加载时立即建立连接
4. 连接池配置（SQLite 使用单连接，PostgreSQL 使用连接池）

使用方式：
    from storage.database.db import get_session
    with get_session() as session:
        session.execute(text("SELECT 1"))
"""
import os
import time
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
import logging
logger = logging.getLogger(__name__)

# 数据库连接最大重试时间（秒），超过此时间仍未连上则抛出异常
MAX_RETRY_TIME = 20

# 尝试从 .env 文件加载环境变量（不强制依赖 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def get_db_url() -> str:
    """获取数据库连接 URL，优先从环境变量读取，本地开发默认使用 SQLite。

    连接 URL 的优先级：
    1. 环境变量 PGDATABASE_URL（适用于 PostgreSQL 等远程数据库）
    2. 环境变量 DATABASE_URL（兼容通用命名）
    3. SQLite 本地文件（当前目录下的 local_data.db，零配置即可运行）

    Returns:
        数据库连接 URL 字符串，
        如 "postgresql://user:pass@host:5432/db" 或 "sqlite:///E:/.../local_data.db"
    """
    # 首先尝试从环境变量获取 PostgreSQL 或其他数据库 URL
    url = os.getenv("PGDATABASE_URL") or ""
    if url:
        return url
    # 无环境变量时，使用 SQLite 本地文件数据库（零配置）
    sqlite_path = os.path.join(os.getcwd(), "local_data.db")
    return f"sqlite:///{sqlite_path}"


# 全局缓存：引擎和会话工厂，惰性初始化，模块加载时不创建连接
_engine = None
_SessionLocal = None


def _create_engine_with_retry():
    """创建数据库引擎并等待连接可用，连接失败时最多重试 MAX_RETRY_TIME 秒。

    处理逻辑：
    1. 根据数据库 URL 判断是否为 SQLite，SQLite 使用单连接，PostgreSQL 使用连接池
    2. 通过 SELECT 1 探测连接是否可用
    3. 连接失败时每隔 1 秒重试一次，直到超出 MAX_RETRY_TIME（20 秒）上限
    4. 重试耗尽后抛出最后一次的异常

    连接池配置（非 SQLite）：
    - pool_size=10：连接池大小
    - max_overflow=100：最大溢出连接数
    - pool_recycle=1800：连接回收时间（30 分钟）
    - pool_timeout=30：获取连接超时

    Returns:
        创建好的 SQLAlchemy Engine 对象

    Raises:
        OperationalError: 重试超时后仍无法连接数据库
        ValueError: 数据库 URL 未配置
    """
    url = get_db_url()
    if not url:
        raise ValueError("数据库 URL 未配置")

    # SQLite 使用单连接模式；PostgreSQL 等远程数据库使用连接池
    is_sqlite = url.startswith("sqlite")
    kwargs = {"pool_pre_ping": True}   # 每次连接前校验可用性
    if not is_sqlite:
        # 远程数据库配置连接池参数，提升并发性能
        kwargs.update(pool_size=10, max_overflow=100, pool_recycle=1800, pool_timeout=30)

    engine = create_engine(url, **kwargs)

    # 连接探测：执行 SELECT 1 验证数据库是否可用
    # 首次启动时数据库可能尚未就绪（如 Docker 容器正在初始化），因此需要重试
    start_time = time.time()
    last_error = None
    while time.time() - start_time < MAX_RETRY_TIME:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            # 连接成功，返回引擎
            return engine
        except OperationalError as e:
            last_error = e
            elapsed = time.time() - start_time
            logger.warning(f"Database connection failed, retrying... (elapsed: {elapsed:.1f}s)")
            # 等待 1 秒后重试（剩余时间不足 1 秒则等待剩余时间）
            time.sleep(min(1, MAX_RETRY_TIME - elapsed))
    # 重试超时，抛出最后一次失败的异常
    logger.error(f"Database connection failed after {MAX_RETRY_TIME}s: {last_error}")
    raise last_error


def get_engine():
    """获取全局唯一的数据库引擎（惰性初始化，首次调用时创建）。

    使用全局变量 _engine 缓存引擎实例，避免重复创建数据库连接。
    线程安全：SQLAlchemy 的 Engine 是线程安全的，可被多个会话共享。

    Returns:
        SQLAlchemy Engine 对象
    """
    global _engine
    if _engine is None:
        _engine = _create_engine_with_retry()
    return _engine


def get_sessionmaker():
    """获取全局唯一的会话工厂（惰性初始化，首次调用时创建）。

    基于已初始化的引擎创建 sessionmaker，用于生成数据库会话实例。
    每次调用 get_sessionmaker()() 或 get_session() 都将获得一个新的独立会话。

    Returns:
        sessionmaker 工厂对象，调用其 () 方法获取 Session 实例
    """
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def get_session():
    """获取一个新的数据库会话实例。

    快捷方法，等价于 get_sessionmaker()()。
    建议在 with 语句中使用以自动管理事务和资源释放：
        with get_session() as session:
            session.add(...)
            session.commit()

    Returns:
        SQLAlchemy Session 对象
    """
    return get_sessionmaker()()


# 导出公共接口，供 other 模块使用
__all__ = [
    "get_db_url",
    "get_engine",
    "get_sessionmaker",
    "get_session",
]
