"""本地文件存储 - 替代 S3 对象存储

文件保存到本地 reports/ 目录，返回 file:// 路径。
"""
import os
import re
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOCAL_STORAGE_DIR = os.path.join(os.getcwd(), "local_storage")


def ensure_storage_dir():
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
    """
    ensure_storage_dir()
    file_name = _safe_path(file_name)
    dest_dir = os.path.join(LOCAL_STORAGE_DIR, os.path.dirname(file_name))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(LOCAL_STORAGE_DIR, file_name)
    try:
        shutil.copy2(local_path, dest)
        logger.info(f"文件已保存到本地存储: {dest}")
        # 返回相对 HTTP 路径，匹配 FastAPI StaticFiles 挂载点
        return f"/local_storage/{file_name}"
    except Exception as e:
        logger.error(f"本地存储失败: {e}")
        return f"保存失败: {e}"
