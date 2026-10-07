"""本地文件存储 - 替代 S3 对象存储

本模块实现本地文件存储功能，将生成的报告文件（PDF/Excel/图表）
保存到项目根目录下的 local_storage/ 子目录，并返回 HTTP 可访问的相对路径。

设计思路：
- 替代云端 S3 对象存储，实现完全离线运行
- 通过 FastAPI StaticFiles 挂载 /local_storage 路由提供文件下载
- 文件名自动清洗非法字符，兼容 Windows 路径限制
- 产物按批次隔离：同一次分析的所有文件（PDF/Excel/图表）统一落在
  local_storage/<YYYYMMDD_HHMMSS>/ 时间戳子目录（精确到秒），旧批次文件
  永不删除、也不与新批次混入同一下载列表，杜绝旧报告冒充新结果。
"""
import os
import re
import shutil
import logging
import contextvars
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 本地存储根目录：项目根目录下的 local_storage/ 文件夹
LOCAL_STORAGE_DIR = os.path.join(os.getcwd(), "local_storage")

# S2 批次目录并发加固：批次时间戳改为 ContextVar（每请求/每任务独立上下文）。
# 旧的模块级全局 _BATCH_STAMP 在并发分析时会被后一次 begin_batch 切走——
# 前一个尚未导完的运行产物被写进后一个运行的批次目录（跨运行/跨用户串库）。
# ContextVar 在 async 请求间天然隔离；同时保留全局镜像兜底：导出兜底在
# ThreadPoolExecutor 工作线程内执行时线程不继承请求上下文，读取回退到镜像，
# 保证单用户本地场景行为与旧版完全一致。agent.py 的导出线程池同时显式
# copy_context() 传播请求上下文，双层保险。
_BATCH_STAMP_VAR: contextvars.ContextVar = contextvars.ContextVar("local_storage_batch_stamp", default=None)
_GLOBAL_BATCH_FALLBACK: str | None = None


def ensure_storage_dir():
    """确保本地存储目录存在，不存在则自动创建。"""
    os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)


def _safe_path(file_name: str) -> str:
    """清洗文件路径中的非法字符，防止 Windows 崩溃"""
    parts = file_name.replace("\\", "/").split("/")
    cleaned = []
    for part in parts:
        safe = re.sub(r'[<>:"|?*\x00-\x1f]', '_', part)
        safe = re.sub(r'_{2,}', '_', safe).strip(' ._')
        cleaned.append(safe or "_")
    return "/".join(cleaned)


def begin_batch(stamp: str | None = None) -> str:
    """开启一个新产物批次：设置批次时间戳并返回（格式 YYYYMMDD_HHMMSS，精确到秒）。

    每次分析运行开始时调用一次；同一次运行内多次上传共享该时间戳子目录，
    下一次 begin_batch 生成全新目录，实现「每次生成一个确切到秒的文件夹」。
    未显式传入 stamp 时按当前时间生成（可注入固定值用于测试）。
    并发语义（S2）：时间戳绑定当前请求上下文，并发运行互不串扰。
    """
    global _GLOBAL_BATCH_FALLBACK
    value = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    _BATCH_STAMP_VAR.set(value)
    _GLOBAL_BATCH_FALLBACK = value
    return value


def current_batch_stamp() -> str:
    """返回当前批次时间戳；尚未初始化时自动按当前时间初始化。

    读取顺序：请求上下文值 → 全局镜像（工作线程兜底） → 自动初始化。
    """
    global _GLOBAL_BATCH_FALLBACK
    value = _BATCH_STAMP_VAR.get()
    if value:
        return value
    if _GLOBAL_BATCH_FALLBACK:
        return _GLOBAL_BATCH_FALLBACK
    value = datetime.now().strftime("%Y%m%d_%H%M%S")
    _BATCH_STAMP_VAR.set(value)
    _GLOBAL_BATCH_FALLBACK = value
    return value


def reset_batch() -> None:
    """清空当前批次时间戳（测试隔离用；下次上传自动重新初始化）。"""
    global _GLOBAL_BATCH_FALLBACK
    _BATCH_STAMP_VAR.set(None)
    _GLOBAL_BATCH_FALLBACK = None


def upload_file_to_storage(local_path: str, file_name: str, content_type: str, expire_seconds: int = 86400) -> str:
    """本地存储替代：将文件复制到 local_storage 目录，返回 HTTP 可访问 URL。

    返回格式: /local_storage/{file_name}
    该 URL 对应 main.py 中 StaticFiles 挂载的 /local_storage 路由，
    浏览器可直接通过 fetch() 下载，不受 file:// 协议安全限制。

    Args:
        local_path: 源文件的本地绝对路径（如临时目录中的 PDF/Excel/图表）
        file_name: 存储中的相对路径（如 "reports/xxx_审计风险报告.pdf"）
        content_type: MIME 类型（本地模式下未使用，保留用于接口兼容）
        expire_seconds: URL 过期时间（本地模式下不生效）
    """
    # 确保存储目录存在
    ensure_storage_dir()
    # 清洗文件名中的非法字符（Windows 不允许 <>:"|?* 等）
    file_name = _safe_path(file_name)
    # 批次子目录：local_storage/<批次时间戳>/<file_name>（file_name 含 reports/ 或
    # charts/ 前缀）。pdf_export / excel_export / visualizer 三处调用零改动，
    # 仅在此统一改写存储相对路径，前端 /local_storage/ 前缀与斜杠子路径天然兼容。
    dest_rel = os.path.join(current_batch_stamp(), file_name)
    dest_dir = os.path.join(LOCAL_STORAGE_DIR, os.path.dirname(dest_rel))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(LOCAL_STORAGE_DIR, dest_rel)
    try:
        # 复制文件到存储目录（保留元数据）
        shutil.copy2(local_path, dest)
        logger.info(f"文件已保存到本地存储: {dest}")
        # 返回相对 HTTP 路径，匹配 FastAPI StaticFiles 挂载点；Windows 下统一转正斜杠
        url_rel = dest_rel.replace(os.sep, "/")
        return f"/local_storage/{url_rel}"
    except Exception as e:
        logger.error(f"本地存储失败: {e}")
        return f"保存失败: {e}"
