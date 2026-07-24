"""本地文件存储 - 替代 S3 对象存储

本模块实现本地文件存储功能，将生成的报告文件（PDF/Excel/图表）
保存到项目根目录下的 local_storage/ 子目录，并返回 HTTP 可访问的相对路径。

设计思路：
- 替代云端 S3 对象存储，实现完全离线运行
- 通过 FastAPI StaticFiles 挂载 /local_storage 路由提供文件下载
- 文件名自动清洗非法字符，兼容 Windows 路径限制
"""
import os
import re
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 本地存储根目录：项目根目录下的 local_storage/ 文件夹
LOCAL_STORAGE_DIR = os.path.join(os.getcwd(), "local_storage")


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
    # 创建子目录（如 reports/ 或 charts/）
    dest_dir = os.path.join(LOCAL_STORAGE_DIR, os.path.dirname(file_name))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(LOCAL_STORAGE_DIR, file_name)
    try:
        # 复制文件到存储目录（保留元数据）
        shutil.copy2(local_path, dest)
        logger.info(f"文件已保存到本地存储: {dest}")
        # 返回相对 HTTP 路径，匹配 FastAPI StaticFiles 挂载点
        return f"/local_storage/{file_name}"
    except Exception as e:
        logger.error(f"本地存储失败: {e}")
        return f"保存失败: {e}"
